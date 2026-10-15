"""Memória canônica remota; nenhum acesso à persistência legada."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from .client import MaestroContextClient, MaestroContextError
from .converters import history_messages


@dataclass(frozen=True)
class MaestroThreadView:
    """View de execução. Não é o Thread legado nem uma projeção completa de UI."""

    thread_id: str
    organization_id: str
    workspace_id: str | None
    context_cutoff_sequence: str
    participants: tuple[dict[str, Any], ...] = field(repr=False)
    mode: Literal["maestro"] = "maestro"


@dataclass(frozen=True)
class MaestroArtifactReference:
    """Referência durável, sem URL, bytes hidratados ou status inventado."""

    artifact_id: str
    event_id: str
    sequence: str
    actor: dict[str, Any]
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


class MaestroMemory:
    def __init__(self, client: MaestroContextClient, *, max_pages: int = 20, max_total_bytes: int = 8388608) -> None:
        if (
            type(max_pages) is not int
            or not 1 <= max_pages <= 100
            or type(max_total_bytes) is not int
            or not 1024 <= max_total_bytes <= 33554432
        ):
            raise MaestroContextError("CONTEXT_LIMIT")
        self._client = client
        self._envelope = client.admission.envelope
        self._max_pages, self._max_total_bytes = max_pages, max_total_bytes

    def _validate_page(self, page: dict[str, Any]) -> None:
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
            or trigger["type"] != "message.created"
            or len(recipients) != 1
            or recipients[0]["kind"] != "agent"
            or recipients[0]["state"] != "active"
            or recipients[0]["instance_agent_id"] != envelope["instance_agent_id"]
            or recipients[0]["principal_id"] != envelope["instance_agent_id"]
            or len({p["participant_id"] for p in page["participants"]}) != len(page["participants"])
        ):
            raise MaestroContextError("CONTEXT_INVALID")
        previous = (0, "")
        for event in [trigger, *page["history"]["items"]]:
            if event["thread_id"] != envelope["thread_id"] or (len(event["sequence"]), event["sequence"]) > (
                len(envelope["context_cutoff_sequence"]),
                envelope["context_cutoff_sequence"],
            ):
                raise MaestroContextError("CONTEXT_INVALID")
        for event in page["history"]["items"]:
            sequence = (len(event["sequence"]), event["sequence"])
            if sequence <= previous:
                raise MaestroContextError("CONTEXT_INVALID")
            previous = sequence
        history = page["history"]
        if history["has_more"] != (history["next_cursor"] is not None) or history["has_more"] and not history["items"]:
            raise MaestroContextError("CONTEXT_INVALID")

    async def snapshot(self) -> dict[str, Any]:
        """Nova leitura completa e autorizada. Erro de uma página descarta tudo."""
        cursor = self._envelope["context_cursor"]
        seen_cursors, seen_events = set(), set()
        events: list[dict[str, Any]] = []
        first = None
        total_bytes = 0
        for _page_number in range(self._max_pages):
            if cursor in seen_cursors:
                raise MaestroContextError("CONTEXT_INVALID")
            seen_cursors.add(cursor)
            page = await self._client.page(cursor)
            self._validate_page(page)
            total_bytes += len(json.dumps(page, ensure_ascii=True, separators=(",", ":")).encode())
            if total_bytes > self._max_total_bytes:
                raise MaestroContextError("CONTEXT_LIMIT")
            if first is None:
                first = page
            elif page["trigger_event"] != first["trigger_event"]:
                raise MaestroContextError("CONTEXT_INVALID")
            for event in page["history"]["items"]:
                if (
                    event["event_id"] in seen_events
                    or events
                    and (len(event["sequence"]), event["sequence"]) <= (len(events[-1]["sequence"]), events[-1]["sequence"])
                ):
                    raise MaestroContextError("CONTEXT_INVALID")
                seen_events.add(event["event_id"])
                events.append(event)
            if not page["history"]["has_more"]:
                self._client._check_current()
                return {"trigger_event": deepcopy(first["trigger_event"]), "participants": deepcopy(page["participants"]), "events": events}
            cursor = page["history"]["next_cursor"]
        raise MaestroContextError("CONTEXT_LIMIT")

    async def get_thread(self) -> MaestroThreadView:
        page = await self._client.page(self._envelope["context_cursor"], limit=1)
        self._validate_page(page)
        return MaestroThreadView(
            self._envelope["thread_id"],
            self._envelope["organization_id"],
            self._envelope["workspace_id"],
            self._envelope["context_cutoff_sequence"],
            tuple(deepcopy(page["participants"])),
        )

    @staticmethod
    def _selection(limit: int, order: str, filters: dict[str, str] | None) -> None:
        if type(limit) is not int or not 1 <= limit <= 1000 or order not in {"asc", "desc"}:
            raise MaestroContextError("CONTEXT_LIMIT")
        if filters is not None and (
            not isinstance(filters, dict)
            or set(filters) - {"type", "actor_kind", "instance_agent_id"}
            or any(not isinstance(value, str) for value in filters.values())
        ):
            raise MaestroContextError("CONTEXT_INVALID")

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
        self._selection(limit, order, filters)
        # Seleção de mensagens é feita após filtrar tipos, sem multiplicador heurístico.
        selected_filters = {**(filters or {}), "type": "message.created"}
        events = await self.get_history_raw(limit, order, selected_filters)
        if order == "desc":
            events.reverse()
        return history_messages(events, self._envelope["instance_agent_id"], format)

    async def hydrate_artifact(self, reference: MaestroArtifactReference):
        """Fresh canonical context first; no arbitrary ref/URL, no consume-once cache."""
        from .artifacts import MaestroArtifactsClient

        if not isinstance(reference, MaestroArtifactReference):
            raise MaestroContextError("CONTEXT_INVALID")
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
            raise MaestroContextError("CONTEXT_INVALID")
        # Repeated references may omit optional metadata independently. Merge only
        # compatible declarations; the current proxy still validates type/size.
        part = {"type": "attachment", "artifact_id": reference.artifact_id}
        for metadata_field in ("filename", "mime_type", "size_bytes"):
            values = {item[metadata_field] for item in matches if metadata_field in item}
            if len(values) > 1:
                raise MaestroContextError("CONTEXT_INVALID")
            if values:
                part[metadata_field] = values.pop()
        current = MaestroArtifactReference(
            reference.artifact_id,
            reference.event_id,
            reference.sequence,
            reference.actor,
            part.get("filename"),
            part.get("mime_type"),
            part.get("size_bytes"),
        )
        return await MaestroArtifactsClient(self._client).hydrate(current)

    async def list_artifacts(
        self, *, cursor: str | None = None, limit: int = 30, direction: Literal["in", "out", "all"] = "all"
    ) -> tuple[list[MaestroArtifactReference], str | None, bool]:
        if type(limit) is not int or not 1 <= limit <= 100 or direction not in {"in", "out", "all"}:
            raise MaestroContextError("CONTEXT_LIMIT")
        # Cursor local é apenas offset; cada chamada refaz autorização + snapshot.
        if cursor is not None and (not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 5):
            raise MaestroContextError("CONTEXT_INVALID")
        offset = int(cursor or "0")
        refs = []
        for event in reversed((await self.snapshot())["events"]):
            own = event["actor"].get("instance_agent_id") == self._envelope["instance_agent_id"]
            if direction != "all" and own != (direction == "out"):
                continue
            for part in event["payload"].get("content", []):
                if part["type"] == "attachment":
                    refs.append(
                        MaestroArtifactReference(
                            part["artifact_id"],
                            event["event_id"],
                            event["sequence"],
                            deepcopy(event["actor"]),
                            part.get("filename"),
                            part.get("mime_type"),
                            part.get("size_bytes"),
                        )
                    )
        has_more = offset + limit < len(refs)
        return refs[offset : offset + limit], str(offset + limit) if has_more else None, has_more
