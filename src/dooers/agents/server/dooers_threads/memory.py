"""Memória canônica remota; nenhum acesso à persistência legada.

``DooersThreadsMemory`` lê o contexto da entrega (snapshot, artefatos). O handler recebe
``DooersThreadsAgentMemory``: o ``AgentMemory`` de produção sobre esse contexto
(``persistence.py``, carregado só no uso porque importa os handlers de produção).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from .artifacts import DooersThreadsArtifactsClient
from .client import DooersThreadsContextClient, DooersThreadsContextError
from .converters import history_messages, public_actor
from .links import LinkResolver
from .window import DooersThreadsContextWindow


@dataclass(frozen=True)
class DooersThreadsThreadView:
    """View de execução. Não é o Thread legado nem uma projeção completa de UI."""

    thread_id: str
    organization_id: str
    workspace_id: str | None
    context_cutoff_sequence: str
    participants: tuple[dict[str, Any], ...] = field(repr=False)
    mode: Literal["dooers_threads"] = "dooers_threads"


@dataclass(frozen=True)
class DooersThreadsArtifactReference:
    """Referência durável, sem URL, bytes hidratados ou status inventado."""

    artifact_id: str
    event_id: str
    sequence: str
    actor: dict[str, Any]
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


#: Events that start a delivery: a message, or the response to a production form (``send.form``).
TRIGGER_TYPES = frozenset({"message.created", "form.responded"})


class DooersThreadsMemory:
    def __init__(
        self,
        client: DooersThreadsContextClient,
        *,
        max_pages: int = 20,
        max_total_bytes: int = 16777216,
        max_history_pages: int | None = None,
        history_page_size: int = 100,
    ) -> None:
        """``max_pages`` and ``max_total_bytes`` bound ``snapshot()`` (the whole thread, forward).

        ``max_history_pages`` (pages of ``history_page_size`` events; default ``DooersThreadsConfig.max_history_pages``)
        is how far back one history read of the handler's memory may go (``window()``); beyond it the read
        raises ``DooersThreadsHistoryLimitError``.
        """
        if max_history_pages is None:
            max_history_pages = client.config.max_history_pages
        if (
            type(max_history_pages) is not int
            or not 1 <= max_history_pages <= 1000
            or type(history_page_size) is not int
            or not 1 <= history_page_size <= 100
            or type(max_pages) is not int
            or not 1 <= max_pages <= 100
            or type(max_total_bytes) is not int
            or not 1024 <= max_total_bytes <= 33554432
        ):
            raise DooersThreadsContextError("CONTEXT_LIMIT")
        self._client = client
        self._envelope = client.admission.envelope
        self._max_pages, self._max_total_bytes = max_pages, max_total_bytes
        # One link per artifact for the whole execution (incoming parts and history).
        self._links = LinkResolver(DooersThreadsArtifactsClient(client).issue_link)
        self._max_history_pages, self._history_page_size = max_history_pages, history_page_size
        self._window: DooersThreadsContextWindow | None = None

    @property
    def envelope(self) -> Mapping[str, Any]:
        """The admitted dispatch envelope of this execution (read only)."""
        return MappingProxyType(self._envelope)

    @property
    def links(self) -> LinkResolver:
        """The execution's temporary links: one per artifact (incoming parts and history)."""
        return self._links

    def window(self) -> DooersThreadsContextWindow:
        """The newest events of this execution's delivery context, read backward and kept (one per execution).

        Below the cutoff the context does not change, so one window serves the whole execution
        (``on`` and the handler's memory): it starts with one page and grows only when a read
        needs older events (``DooersThreadsContextWindow``). Every use checks the lease, the capability
        and the cancellation again: after the execution ends, it fails. The window is shared:
        read it, never change it.
        """
        if self._window is None:

            async def fetch(cursor: str, limit: int, direction: Literal["forward", "backward"]) -> dict[str, Any]:
                page = await self._client.page(cursor, limit=limit, direction=direction)
                self._validate_page(page, direction)
                return page

            self._window = DooersThreadsContextWindow(
                fetch,
                cursor=self._envelope["context_cursor"],
                check_current=self._client._check_current,
                page_size=self._history_page_size,
                max_pages=self._max_history_pages,
            )
        return self._window

    def _validate_page(self, page: dict[str, Any], direction: Literal["forward", "backward"] = "forward") -> None:
        envelope = self._envelope
        trigger = page["trigger_event"]
        recipients = [p for p in page["participants"] if p["participant_id"] == envelope["recipient_participant_id"]]
        if (
            page["delivery_id"] != envelope["delivery_id"]
            or page["context_cursor"] != envelope["context_cursor"]
            or page["history"]["watermark"] != envelope["context_cutoff_sequence"]
            or trigger["event_id"] != envelope["trigger_event_id"]
            or trigger["sequence"] != envelope["trigger_sequence"]
            or trigger["root_event_id"] != envelope["root_event_id"]
            or trigger["type"] not in TRIGGER_TYPES
            or len(recipients) != 1
            or recipients[0]["kind"] != "agent"
            or recipients[0]["state"] != "active"
            or recipients[0]["instance_agent_id"] != envelope["instance_agent_id"]
            or recipients[0]["principal_id"] != envelope["instance_agent_id"]
            or len({p["participant_id"] for p in page["participants"]}) != len(page["participants"])
        ):
            raise DooersThreadsContextError("CONTEXT_INVALID")
        invocation = page.get("invocation_event")
        if invocation is not None and "form.responded" in (invocation["type"], trigger["type"]):
            # A form response is its own root and goes only to the form's author: no routing decision.
            if invocation != trigger:
                raise DooersThreadsContextError("CONTEXT_INVALID")
        elif invocation is not None:
            decision = invocation["payload"].get("routing_decision", {})
            if (
                invocation["type"] != "message.created"
                or decision.get("version") != 1
                or invocation["root_event_id"] != envelope["root_event_id"]
                or envelope["recipient_participant_id"] not in decision.get("recipients", [])
            ):
                raise DooersThreadsContextError("CONTEXT_INVALID")
        if invocation is not None and any(
            event["event_id"] == invocation["event_id"] and event != invocation for event in page["history"]["items"]
        ):
            raise DooersThreadsContextError("CONTEXT_INVALID")
        previous = (0, "")
        for event in [trigger, *([invocation] if invocation is not None else []), *page["history"]["items"]]:
            if event["thread_id"] != envelope["thread_id"] or (len(event["sequence"]), event["sequence"]) > (
                len(envelope["context_cutoff_sequence"]),
                envelope["context_cutoff_sequence"],
            ):
                raise DooersThreadsContextError("CONTEXT_INVALID")
        for event in page["history"]["items"]:
            sequence = (len(event["sequence"]), event["sequence"])
            # Forward pages grow, backward pages (newest first) shrink.
            if direction == "backward":
                if previous != (0, "") and sequence >= previous:
                    raise DooersThreadsContextError("CONTEXT_INVALID")
            elif sequence <= previous:
                raise DooersThreadsContextError("CONTEXT_INVALID")
            previous = sequence
        history = page["history"]
        if history["has_more"] != (history["next_cursor"] is not None) or history["has_more"] and not history["items"]:
            raise DooersThreadsContextError("CONTEXT_INVALID")

    async def snapshot(self) -> dict[str, Any]:
        """Nova leitura completa e autorizada. Erro de uma página descarta tudo."""
        cursor = self._envelope["context_cursor"]
        seen_cursors, seen_events = set(), set()
        events: list[dict[str, Any]] = []
        first = None
        total_bytes = 0
        for _page_number in range(self._max_pages):
            if cursor in seen_cursors:
                raise DooersThreadsContextError("CONTEXT_INVALID")
            seen_cursors.add(cursor)
            page = await self._client.page(cursor)
            self._validate_page(page)
            # UTF-8 bytes, as the page came over the wire (not ASCII escapes, which are 3 to 6 times larger).
            total_bytes += len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))
            if total_bytes > self._max_total_bytes:
                raise DooersThreadsContextError("CONTEXT_LIMIT")
            if first is None:
                first = page
            elif page["trigger_event"] != first["trigger_event"] or page.get("invocation_event") != first.get("invocation_event"):
                raise DooersThreadsContextError("CONTEXT_INVALID")
            for event in page["history"]["items"]:
                if (
                    event["event_id"] in seen_events
                    or events
                    and (len(event["sequence"]), event["sequence"]) <= (len(events[-1]["sequence"]), events[-1]["sequence"])
                ):
                    raise DooersThreadsContextError("CONTEXT_INVALID")
                seen_events.add(event["event_id"])
                events.append(event)
            if not page["history"]["has_more"]:
                self._client._check_current()
                return {
                    "trigger_event": deepcopy(first["trigger_event"]),
                    **({"invocation_event": deepcopy(first["invocation_event"])} if "invocation_event" in first else {}),
                    "participants": deepcopy(page["participants"]),
                    "events": events,
                }
            cursor = page["history"]["next_cursor"]
        raise DooersThreadsContextError("CONTEXT_LIMIT")

    async def get_thread(self) -> DooersThreadsThreadView:
        page = await self._client.page(self._envelope["context_cursor"], limit=1)
        self._validate_page(page)
        return DooersThreadsThreadView(
            self._envelope["thread_id"],
            self._envelope["organization_id"],
            self._envelope["workspace_id"],
            self._envelope["context_cutoff_sequence"],
            tuple(deepcopy(page["participants"])),
        )

    @staticmethod
    def _selection(limit: int, order: str, filters: dict[str, str] | None) -> None:
        if type(limit) is not int or not 1 <= limit <= 1000 or order not in {"asc", "desc"}:
            raise DooersThreadsContextError("CONTEXT_LIMIT")
        if filters is not None and (
            not isinstance(filters, dict)
            or set(filters) - {"type", "actor_kind", "instance_agent_id"}
            or any(not isinstance(value, str) for value in filters.values())
        ):
            raise DooersThreadsContextError("CONTEXT_INVALID")

    async def get_history_raw(
        self, limit: int = 50, order: Literal["asc", "desc"] = "asc", filters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        self._selection(limit, order, filters)
        events = (await self.snapshot())["events"]
        for key, value in (filters or {}).items():
            events = [
                event
                for event in events
                if (event["type"] if key == "type" else event["actor"].get("kind" if key == "actor_kind" else "instance_agent_id")) == value
            ]
        return (events if order == "asc" else list(reversed(events)))[:limit]

    async def get_history(
        self, limit: int = 50, format: str = "openai", order: Literal["asc", "desc"] = "desc", filters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Projeção textual do Dooers Threads; o handler usa ``DooersThreadsAgentMemory.get_history`` (produção)."""
        self._selection(limit, order, filters)
        # Seleção de mensagens é feita após filtrar tipos, sem multiplicador heurístico.
        selected_filters = {**(filters or {}), "type": "message.created"}
        events = await self.get_history_raw(limit, order, selected_filters)
        if order == "desc":
            events.reverse()
        return history_messages(events, self._envelope["instance_agent_id"], format)

    async def hydrate_artifact(self, reference: DooersThreadsArtifactReference):
        """Fresh canonical context first; no arbitrary ref/URL, no consume-once cache."""
        from .artifacts import DooersThreadsArtifactsClient

        if not isinstance(reference, DooersThreadsArtifactReference):
            raise DooersThreadsContextError("CONTEXT_INVALID")
        matches = []
        for event in (await self.snapshot())["events"]:
            for part in event["payload"].get("content", []):
                if (
                    part["type"] == "attachment"
                    and part["artifact_id"] == reference.artifact_id
                    and event["event_id"] == reference.event_id
                    and event["sequence"] == reference.sequence
                ):
                    matches.append(part)
        if not matches:
            raise DooersThreadsContextError("CONTEXT_INVALID")
        # Repeated references may omit optional metadata independently. Merge only
        # compatible declarations; the current proxy still validates type/size.
        part = {"type": "attachment", "artifact_id": reference.artifact_id}
        for metadata_field in ("filename", "mime_type", "size_bytes"):
            values = {item[metadata_field] for item in matches if metadata_field in item}
            if len(values) > 1:
                raise DooersThreadsContextError("CONTEXT_INVALID")
            if values:
                part[metadata_field] = values.pop()
        current = DooersThreadsArtifactReference(
            reference.artifact_id,
            reference.event_id,
            reference.sequence,
            reference.actor,
            part.get("filename"),
            part.get("mime_type"),
            part.get("size_bytes"),
        )
        return await DooersThreadsArtifactsClient(self._client).hydrate(current)

    async def list_artifacts(
        self, *, cursor: str | None = None, limit: int = 30, direction: Literal["in", "out", "all"] = "all"
    ) -> tuple[list[DooersThreadsArtifactReference], str | None, bool]:
        if type(limit) is not int or not 1 <= limit <= 100 or direction not in {"in", "out", "all"}:
            raise DooersThreadsContextError("CONTEXT_LIMIT")
        # Cursor local é apenas offset; cada chamada refaz autorização + snapshot.
        if cursor is not None and (not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 5):
            raise DooersThreadsContextError("CONTEXT_INVALID")
        offset = int(cursor or "0")
        refs = []
        for event in reversed((await self.snapshot())["events"]):
            own = event["actor"].get("instance_agent_id") == self._envelope["instance_agent_id"]
            if direction != "all" and own != (direction == "out"):
                continue
            for part in event["payload"].get("content", []):
                if part["type"] == "attachment":
                    refs.append(
                        DooersThreadsArtifactReference(
                            part["artifact_id"],
                            event["event_id"],
                            event["sequence"],
                            # Without the person's e-mail and identity IDs, as production's ThreadArtifact.
                            public_actor(deepcopy(event["actor"])),
                            part.get("filename"),
                            part.get("mime_type"),
                            part.get("size_bytes"),
                        )
                    )
        has_more = offset + limit < len(refs)
        return refs[offset : offset + limit], str(offset + limit) if has_more else None, has_more


def __getattr__(name: str) -> Any:
    # ``DooersThreadsAgentMemory`` subclasses the production ``AgentMemory``; importing the handlers
    # here would be an import cycle at startup (``AgentConfig`` imports ``dooers_threads``).
    if name == "DooersThreadsAgentMemory":
        from .persistence import DooersThreadsAgentMemory

        return DooersThreadsAgentMemory
    raise AttributeError(name)
