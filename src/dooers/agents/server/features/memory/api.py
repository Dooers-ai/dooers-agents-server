"""Handler-facing long-term memory bound to one owner (agent + org + workspace + user)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from dooers.agents.server.features.memory.models import (
    MEMORY_KINDS,
    MEMORY_SCOPES,
    PREFERENCES_MAX_CHARS,
    MemoryOwner,
    MemoryPreferences,
    MemoryRecord,
    normalize_content,
    normalize_tags,
)
from dooers.agents.server.features.memory.store import MemoryStore, new_memory_id

#: Called after a write so the owner's open sockets refresh (``memory.upsert`` / ``memory.deleted``).
MemoryNotifier = Callable[[str, MemoryRecord | None, str | None], Awaitable[None]]


class MemoryUnavailableError(RuntimeError):
    """Raised when long-term memory cannot be used for this turn (no store, guest, disabled)."""


class LongTermMemory:
    """``memory.long_term`` inside a handler.

    All identifiers come from the trusted turn context; tools exposed to a model should only
    ever pass ``content`` / ``query`` / ``memory_id`` — never an owner.
    """

    def __init__(
        self,
        store: MemoryStore | None,
        owner: MemoryOwner,
        *,
        notifier: MemoryNotifier | None = None,
        thread_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        self._store = store
        self._owner = owner
        self._notify = notifier
        self._thread_id = thread_id
        self._event_id = event_id

    @property
    def owner(self) -> MemoryOwner:
        return self._owner

    @property
    def available(self) -> bool:
        return self._store is not None and not self._owner.is_anonymous

    def _require(self) -> MemoryStore:
        if self._store is None:
            raise MemoryUnavailableError("long-term memory is not configured for this agent")
        if self._owner.is_anonymous:
            raise MemoryUnavailableError("long-term memory is not available for anonymous users")
        return self._store

    def _scope(self, scope: str | None) -> str:
        value = (scope or "user").strip().lower()
        if value not in MEMORY_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(MEMORY_SCOPES)}")
        if value == "workspace" and not self._owner.workspace_id:
            # Personal chats have no workspace to share with.
            return "user"
        return value

    async def preferences(self) -> MemoryPreferences:
        if self._store is None or self._owner.is_anonymous:
            return MemoryPreferences(
                agent_id=self._owner.agent_id,
                organization_id=self._owner.organization_id,
                user_id=self._owner.user_id or "",
                enabled=False,
            )
        return await self._store.get_preferences(self._owner.agent_id, self._owner.organization_id, self._owner.user_id)

    async def set_preferences(
        self,
        *,
        enabled: bool | None = None,
        custom_instructions: str | None = None,
        about_me: str | None = None,
    ) -> MemoryPreferences:
        store = self._require()
        current = await self.preferences()
        update: dict[str, Any] = {}
        if enabled is not None:
            update["enabled"] = bool(enabled)
        if custom_instructions is not None:
            update["custom_instructions"] = str(custom_instructions)[:PREFERENCES_MAX_CHARS]
        if about_me is not None:
            update["about_me"] = str(about_me)[:PREFERENCES_MAX_CHARS]
        return await store.set_preferences(current.model_copy(update=update))

    async def remember(
        self,
        content: str,
        *,
        scope: str = "user",
        kind: str = "fact",
        tags: list[str] | None = None,
        pinned: bool = False,
        expires_at: datetime | None = None,
        created_by: str = "assistant",
    ) -> MemoryRecord:
        """Save a memory; an identical memory in the same scope is refreshed instead of duplicated."""
        store = self._require()
        text = normalize_content(content)
        scope_value = self._scope(scope)
        kind_value = kind if kind in MEMORY_KINDS else "fact"
        tag_list = normalize_tags(tags)
        existing = await store.find_duplicate(self._owner, scope_value, text)
        if existing is not None:
            merged_tags = normalize_tags([*existing.tags, *tag_list])
            updated = await store.update(
                existing.id,
                {"tags": merged_tags, "pinned": existing.pinned or pinned, "kind": kind_value, "expires_at": expires_at},
            )
            record = updated or existing
            await self._emit("upsert", record)
            return record
        now = datetime.now(UTC)
        agent, org, ws, user = self._owner.scope_key(scope_value)
        record = MemoryRecord(
            id=new_memory_id(),
            agent_id=agent,
            organization_id=org,
            workspace_id=ws,
            user_id=user,
            scope=scope_value,  # type: ignore[arg-type]
            kind=kind_value,  # type: ignore[arg-type]
            content=text,
            tags=tag_list,
            pinned=pinned,
            source_thread_id=self._thread_id,
            source_event_id=self._event_id,
            created_by=created_by,
            author_id="" if self._owner.is_anonymous else self._owner.user_id,
            author_name=self._owner.user_name,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        await store.create(record)
        await self._emit("upsert", record)
        return record

    async def recall(
        self,
        query: str = "",
        *,
        limit: int = 8,
        scopes: tuple[str, ...] = ("user", "workspace"),
        touch: bool = True,
    ) -> list[MemoryRecord]:
        """Most relevant memories for ``query`` (pinned ones always rank first)."""
        if not self.available:
            return []
        store = self._require()
        results = await store.search(self._owner, query or "", scopes=scopes, limit=max(1, min(limit, 50)))
        if touch and results:
            await store.touch([r.id for r in results])
        return results

    async def list(
        self,
        *,
        scope: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MemoryRecord], bool]:
        if not self.available:
            return [], False
        store = self._require()
        scopes = (self._scope(scope),) if scope else ("user", "workspace")
        return await store.list(self._owner, scopes=scopes, query=query, limit=max(1, min(limit, 200)), offset=max(0, offset))

    async def get(self, memory_id: str) -> MemoryRecord | None:
        store = self._require()
        record = await store.get(memory_id)
        if record is None or not self.owns(record):
            return None
        return record

    async def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        tags: list[str] | None = None,
        pinned: bool | None = None,
        kind: str | None = None,
    ) -> MemoryRecord | None:
        store = self._require()
        if await self.get(memory_id) is None:
            return None
        fields: dict[str, Any] = {}
        if content is not None:
            fields["content"] = normalize_content(content)
        if tags is not None:
            fields["tags"] = normalize_tags(tags)
        if pinned is not None:
            fields["pinned"] = bool(pinned)
        if kind is not None and kind in MEMORY_KINDS:
            fields["kind"] = kind
        record = await store.update(memory_id, fields)
        if record is not None:
            await self._emit("upsert", record)
        return record

    async def forget(self, memory_id: str) -> bool:
        store = self._require()
        record = await self.get(memory_id)
        if record is None:
            return False
        deleted = await store.delete(memory_id)
        if deleted:
            await self._emit("deleted", record)
        return deleted

    async def share(self, memory_id: str) -> MemoryRecord | None:
        """Move one of the caller's personal memories into the workspace (shared with its members).

        Sharing is always an explicit human action — a button in the chat or the panel — never
        the model's own call: a personal fact misfiled as workspace memory would be shown to every
        member. Returns ``None`` when the memory is not the caller's personal memory or there is
        no workspace to share with.
        """
        record = await self.get(memory_id)
        if record is None or record.scope != "user" or not self._owner.workspace_id:
            return None
        shared = await self.remember(
            record.content,
            scope="workspace",
            kind=record.kind,
            tags=record.tags,
            pinned=record.pinned,
            expires_at=record.expires_at,
            created_by=record.created_by or "assistant",
        )
        await self.forget(record.id)
        return shared

    def can_modify(self, record: MemoryRecord, *, manager: bool) -> bool:
        """Personal memory: its owner. Workspace memory: managers, and the person who added it."""
        if not self.owns(record):
            return False
        if record.scope != "workspace":
            return True
        return manager or (bool(record.author_id) and record.author_id == self._owner.user_id)

    def owns(self, record: MemoryRecord) -> bool:
        if record.scope == "workspace" and not self._owner.workspace_id:
            return False
        key = self._owner.scope_key(record.scope)
        return (record.agent_id, record.organization_id, record.workspace_id, record.user_id) == key

    async def _emit(self, action: str, record: MemoryRecord) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(action, record, self._owner.user_id)
        except Exception:  # noqa: BLE001 — notification must never fail a write
            import logging

            logging.getLogger(__name__).exception("memory notification failed")


def format_memories_for_prompt(records: list[MemoryRecord], *, max_chars: int = 4000) -> str:
    """Compact bullet list (id + scope + content) for the volatile part of a system prompt."""
    lines: list[str] = []
    used = 0
    for record in records:
        tag = "workspace" if record.scope == "workspace" else "user"
        pin = ", pinned" if record.pinned else ""
        line = f"- [{record.id}] ({tag}{pin}) {record.content}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
