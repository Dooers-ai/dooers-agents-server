"""Pure conversions between connector tool specs and provider tool formats.

Nothing here does I/O. ``to_*`` build the tool definitions each provider expects, ``parse_call`` reads a
provider's tool-call shape back into ``(name, arguments)`` and ``result_message`` builds the reply to feed back.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

Provider = Literal["openai", "openai_responses", "anthropic", "gemini", "generic"]


class ToolSpec(TypedDict, total=False):
    """One item of ``tools`` in the ``/v1/tools/list`` response."""

    name: str
    connector: dict[str, Any]
    tool: str
    title: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    state: str
    status: str
    reason: str


def to_openai(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": spec["name"], "description": spec.get("description", ""), "parameters": spec["input_schema"]},
    }


def to_openai_responses(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": spec["name"],
        "description": spec.get("description", ""),
        "parameters": spec["input_schema"],
    }


def to_anthropic(spec: ToolSpec) -> dict[str, Any]:
    return {"name": spec["name"], "description": spec.get("description", ""), "input_schema": spec["input_schema"]}


# JSON-Schema keywords Gemini rejects. Only ever removed from schema nodes, never from the names under "properties".
_GEMINI_UNSUPPORTED = ("additionalProperties", "$schema", "title", "default")
_SCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_MAP_KEYS = ("properties", "$defs", "definitions", "patternProperties")


def _clean_for_gemini(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _GEMINI_UNSUPPORTED:
            continue
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {name: _clean_for_gemini(sub) for name, sub in value.items()}
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            out[key] = [_clean_for_gemini(sub) for sub in value]
        elif key in ("items", "not"):
            out[key] = _clean_for_gemini(value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def to_gemini(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "parameters": _clean_for_gemini(spec["input_schema"]),
    }


@dataclass
class ParsedCall:
    provider: Provider
    call_id: str
    name: str
    arguments: dict | None
    raw_arguments_error: str | None = None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _has(obj: Any, key: str) -> bool:
    return key in obj if isinstance(obj, dict) else hasattr(obj, key)


def _parse_json_arguments(raw: Any) -> tuple[dict | None, str | None]:
    """Arguments that arrive as a JSON string (OpenAI). Malformed input becomes an error message, never an exception."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return dict(raw), None
    if not isinstance(raw, str | bytes | bytearray):
        return None, f"Tool arguments must be a JSON object, got {type(raw).__name__}."
    if not raw.strip():
        return {}, None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return None, f"Tool arguments are not valid JSON ({exc}). Send a single JSON object."
    if not isinstance(value, dict):
        return None, f"Tool arguments must be a JSON object, got {type(value).__name__}."
    return value, None


def _object_arguments(raw: Any) -> tuple[dict | None, str | None]:
    if raw is None:
        return {}, None
    try:
        return dict(raw), None
    except (TypeError, ValueError):
        return None, f"Tool arguments must be a JSON object, got {type(raw).__name__}."


def parse_call(call: Any) -> ParsedCall:
    """Read a provider tool call into a ``ParsedCall``. Raises ``ValueError`` only for shapes no provider uses."""
    if isinstance(call, tuple | list) and len(call) == 2 and isinstance(call[0], str):
        args, err = _object_arguments(call[1])
        return ParsedCall("generic", "", call[0], args, err)

    kind = _get(call, "type")
    function = _get(call, "function")
    if function is not None:  # OpenAI Chat Completions
        args, err = _parse_json_arguments(_get(function, "arguments"))
        return ParsedCall("openai", str(_get(call, "id") or ""), _get(function, "name") or "", args, err)
    if kind == "function_call":  # OpenAI Responses
        args, err = _parse_json_arguments(_get(call, "arguments"))
        call_id = _get(call, "call_id") or _get(call, "id") or ""
        return ParsedCall("openai_responses", str(call_id), _get(call, "name") or "", args, err)
    if kind == "tool_use":  # Anthropic
        args, err = _object_arguments(_get(call, "input"))
        return ParsedCall("anthropic", str(_get(call, "id") or ""), _get(call, "name") or "", args, err)

    inner = _get(call, "function_call") if _has(call, "function_call") else None
    gemini = inner if inner is not None else call
    if _has(gemini, "name") and (_has(gemini, "args") or inner is not None or isinstance(call, dict)):
        name = _get(gemini, "name")
        if isinstance(name, str) and name:
            args, err = _object_arguments(_get(gemini, "args"))
            return ParsedCall("gemini", str(_get(gemini, "id") or name), name, args, err)
    raise ValueError(f"Unrecognized tool call shape: {type(call).__name__}")


def result_message(parsed: ParsedCall, payload: dict) -> Any:
    """The reply to hand back to the provider for ``parsed``; ``generic`` returns the payload unchanged."""
    if parsed.provider == "generic":
        return payload
    if parsed.provider == "gemini":
        return {"function_response": {"name": parsed.name, "response": payload}}
    body = json.dumps(payload, ensure_ascii=False, default=str)
    if parsed.provider == "openai":
        return {"role": "tool", "tool_call_id": parsed.call_id, "content": body}
    if parsed.provider == "openai_responses":
        return {"type": "function_call_output", "call_id": parsed.call_id, "output": body}
    return {"type": "tool_result", "tool_use_id": parsed.call_id, "content": body}
