"""``ToolSet`` and the public connector API: ``tools()``, ``call()`` and ``list_connectors()`` (exported as ``list``).

Agent, workspace, thread, channel and presence come from the active handler turn. The person (``on_behalf_of``) is
only ever the one the dev passes. ``ToolSet.run`` and the framework adapters never raise for connector, network or
permission errors: the LLM gets ``status`` and ``message``. ``call()`` raises the typed errors instead.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Coroutine
from typing import Any

from dooers.tools.connectors.errors import (
    ApprovalDeclined,
    ConnectionRequired,
    ConnectorError,
    InteractionTimeout,
    InvalidArguments,
    PermissionDenied,
)
from dooers.tools.connectors.executor import execute
from dooers.tools.connectors.formats import (
    ParsedCall,
    ToolSpec,
    parse_call,
    result_message,
    to_anthropic,
    to_gemini,
    to_openai,
    to_openai_responses,
)
from dooers.tools.connectors.transport import ConnectorTransport

_LIST_PATH = "/v1/tools/list"

_ERRORS: dict[str, type[ConnectorError]] = {
    "declined": ApprovalDeclined,
    "interaction_timeout": InteractionTimeout,
    "denied": PermissionDenied,
    "invalid_arguments": InvalidArguments,
}


def _timeouts(interaction_timeout: float | None) -> dict[str, float]:
    """One number overrides both deadlines (connect and approval); ``None`` keeps the executor's defaults."""
    if interaction_timeout is None:
        return {}
    return {"connect": float(interaction_timeout), "approval": float(interaction_timeout)}


def _for_llm(out: dict[str, Any]) -> dict[str, Any]:
    """The service body as the LLM sees it: no ``interaction`` (already handled here) and no null fields."""
    return {key: value for key, value in out.items() if key != "interaction" and value is not None}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


class ToolSet:
    """The connector tools of this turn, in each provider's format, executed by Dooers.

    ``specs`` are the ``/v1/tools/list`` items (bound arguments already removed from ``input_schema``).
    """

    def __init__(
        self,
        specs: list[ToolSpec],
        *,
        on_behalf_of: str | None = None,
        interaction_timeout: dict[str, float] | None = None,
        bound: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.specs = specs
        self._on_behalf_of = on_behalf_of
        self._interaction_timeout = dict(interaction_timeout or {})
        self._bound = {name: dict(values) for name, values in (bound or {}).items()}

    @property
    def names(self) -> list[str]:
        return [spec["name"] for spec in self.specs]

    def bind(self, name: str, **values: Any) -> ToolSet:
        """A new ``ToolSet`` where ``name`` always gets ``values`` and the LLM no longer sees those arguments.

        A ``name`` not in this set (for example, unavailable in this channel) changes nothing.
        """
        specs = copy.deepcopy(self.specs)
        for spec in specs:
            schema = spec.get("input_schema")
            if spec["name"] != name or not isinstance(schema, dict):
                continue
            properties = schema.get("properties")
            if isinstance(properties, dict):
                for key in values:
                    properties.pop(key, None)
            required = schema.get("required")
            if isinstance(required, list):
                schema["required"] = [key for key in required if key not in values]
        bound = {**self._bound, name: {**self._bound.get(name, {}), **values}}
        return ToolSet(specs, on_behalf_of=self._on_behalf_of, interaction_timeout=self._interaction_timeout, bound=bound)

    def openai(self) -> list[dict[str, Any]]:
        return [to_openai(spec) for spec in self.specs]

    def openai_responses(self) -> list[dict[str, Any]]:
        return [to_openai_responses(spec) for spec in self.specs]

    def anthropic(self) -> list[dict[str, Any]]:
        return [to_anthropic(spec) for spec in self.specs]

    def gemini(self) -> list[dict[str, Any]]:
        return [to_gemini(spec) for spec in self.specs]

    def langchain(self) -> list[Any]:
        """``StructuredTool``s that run themselves; each returns the JSON the LLM reads."""
        try:
            from langchain_core.tools import StructuredTool
        except ImportError as exc:
            raise ImportError("ToolSet.langchain() precisa do LangChain: pip install langchain-core") from exc
        return [
            StructuredTool(
                name=spec["name"],
                description=spec.get("description", ""),
                args_schema=spec["input_schema"],
                coroutine=self._langchain_coroutine(spec["name"]),
            )
            for spec in self.specs
        ]

    def openai_agents(self) -> list[Any]:
        """``FunctionTool``s for the OpenAI Agents SDK that run themselves; each returns the JSON the LLM reads."""
        try:
            from agents import FunctionTool
        except ImportError as exc:
            raise ImportError("ToolSet.openai_agents() precisa do OpenAI Agents SDK: pip install openai-agents") from exc
        return [
            FunctionTool(
                name=spec["name"],
                description=spec.get("description", ""),
                params_json_schema=spec["input_schema"],
                on_invoke_tool=self._agents_invoker(spec["name"]),
                strict_json_schema=False,
            )
            for spec in self.specs
        ]

    async def run(self, call: Any) -> dict[str, Any] | Any:
        """Execute a provider tool call and return the result message in that provider's format.

        Raises ``ValueError`` only when ``call`` has no known provider shape (a bug in the caller's code).
        """
        parsed = parse_call(call)
        return result_message(parsed, await self._run_parsed(parsed))

    async def _run_parsed(self, parsed: ParsedCall) -> dict[str, Any]:
        if parsed.arguments is None:
            return {"status": "invalid_arguments", "message": parsed.raw_arguments_error or "Argumentos inválidos."}
        if parsed.name not in self.names:  # never send the service a tool this set does not offer (only / read_only)
            return {"status": "denied", "message": f"A tool {parsed.name!r} não está disponível. Use só as tools oferecidas."}
        out = await execute(
            parsed.name,
            parsed.arguments,
            call_id=parsed.call_id,
            bound=self._bound.get(parsed.name, {}),
            on_behalf_of=self._on_behalf_of,
            interaction_timeout=self._interaction_timeout,
            emit_tool_events=True,
        )
        return _for_llm(out)

    def _langchain_coroutine(self, name: str) -> Callable[..., Coroutine[Any, Any, str]]:
        async def invoke(**arguments: Any) -> str:
            return _dumps(await self._run_parsed(ParsedCall("generic", "", name, arguments)))

        return invoke

    def _agents_invoker(self, name: str) -> Callable[[Any, str], Coroutine[Any, Any, str]]:
        async def invoke(ctx: Any, arguments: str) -> str:
            call = {"id": getattr(ctx, "tool_call_id", "") or "", "type": "function", "function": {"name": name, "arguments": arguments}}
            return _dumps(await self._run_parsed(parse_call(call)))

        return invoke


def _tool_items(body: Any) -> list[dict[str, Any]]:
    items = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ConnectorError("Resposta inválida do serviço de conectores", status="provider_error")
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)]


async def tools(
    *,
    only: list[str] | None = None,
    read_only: bool = False,
    on_behalf_of: str | None = None,
    interaction_timeout: float | None = None,
) -> ToolSet:
    """The connector tools this agent may use in this turn. Raises ``ConnectorError`` if the list cannot be fetched."""
    body = await ConnectorTransport().post(
        _LIST_PATH,
        {"only": only, "read_only": read_only, "include_unavailable": False},
        on_behalf_of=on_behalf_of,
    )
    specs = [item for item in _tool_items(body) if item.get("status") != "unavailable"]
    return ToolSet(specs, on_behalf_of=on_behalf_of, interaction_timeout=_timeouts(interaction_timeout))


async def call(
    connector: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    on_behalf_of: str | None = None,
    interaction_timeout: float | None = None,
) -> dict[str, Any]:
    """Run one connector tool without an LLM. Returns the ``/v1/tools/execute`` body; any status but ``ok`` raises."""
    out = await execute(
        f"{connector}__{tool}",
        dict(arguments),
        call_id="",
        bound={},
        on_behalf_of=on_behalf_of,
        interaction_timeout=_timeouts(interaction_timeout),
        emit_tool_events=False,
    )
    status = out.get("status")
    if status == "ok":
        return out
    message = out.get("message") or f"O conector respondeu {status}."
    if status == "connection_required":
        raise ConnectionRequired(message, connect_url=out.get("connect_url"))
    error = _ERRORS.get(status)
    raise error(message) if error else ConnectorError(message, status=status or "error")


async def list_connectors(*, on_behalf_of: str | None = None) -> list[dict[str, Any]]:
    """The agent's connectors in this turn, each with the tools it may use and why the others are missing.

    Shape: ``[{"slug", "name", "type", "tools": [tool], "unavailable": [{"tool", "reason"}]}]``.
    """
    body = await ConnectorTransport().post(
        _LIST_PATH,
        {"only": None, "read_only": False, "include_unavailable": True},
        on_behalf_of=on_behalf_of,
    )
    groups: dict[str, dict[str, Any]] = {}
    for item in _tool_items(body):
        prefix, _, suffix = item["name"].partition("__")  # the LLM name is "{slug}__{tool}"
        connector = item.get("connector") if isinstance(item.get("connector"), dict) else {}
        slug = connector.get("slug") or prefix
        tool = item.get("tool") or suffix
        entry = groups.setdefault(slug, {"slug": slug, "name": None, "type": None, "tools": [], "unavailable": []})
        entry["name"] = entry["name"] or connector.get("name")
        entry["type"] = entry["type"] or connector.get("type")
        if item.get("status") == "unavailable":
            entry["unavailable"].append({"tool": tool, "reason": item.get("reason")})
        else:
            entry["tools"].append(tool)
    for entry in groups.values():
        entry["name"] = entry["name"] or entry["slug"]
    return [*groups.values()]
