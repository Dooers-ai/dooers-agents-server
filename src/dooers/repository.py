from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dooers.protocol.models import ContentPart, Run, Thread, ThreadEvent, User

if TYPE_CHECKING:
    from dooers.persistence.base import Persistence


def _generate_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Repository:
    """Unified CRUD interface for threads, events, runs, and settings."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    # -- Threads --

    async def list_threads(
        self,
        filter: dict[str, str] | None = None,
        order: dict[str, str] | None = None,
        limit: int = 50,
    ) -> list[Thread]:
        f = filter or {}
        return await self._persistence.list_threads(
            agent_id=f.get("agent_id", ""),
            organization_id=f.get("organization_id", ""),
            workspace_id=f.get("workspace_id", ""),
            user_id=f.get("user_id"),
            cursor=None,
            limit=limit,
            scope=f.get("scope", "member"),
        )

    async def get_thread(self, thread_id: str) -> Thread | None:
        return await self._persistence.get_thread(thread_id)

    async def create_thread(
        self,
        agent_id: str,
        organization_id: str = "",
        workspace_id: str = "",
        user: User | None = None,
        title: str | None = None,
    ) -> Thread:
        now = _now()
        owner = user or User(user_id="")
        thread = Thread(
            id=_generate_id(),
            agent_id=agent_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            owner=owner,
            users=[owner] if owner.user_id else [],
            title=title,
            created_at=now,
            updated_at=now,
            last_event_at=now,
        )
        await self._persistence.create_thread(thread)
        return thread

    async def update_thread(self, thread_id: str, **updates: Any) -> Thread | None:
        thread = await self._persistence.get_thread(thread_id)
        if not thread:
            return None
        if "title" in updates:
            thread.title = updates["title"]
        thread.updated_at = _now()
        await self._persistence.update_thread(thread)
        return thread

    async def remove_thread(self, thread_id: str) -> None:
        await self._persistence.delete_thread(thread_id)

    # -- Events --

    async def list_events(
        self,
        filter: dict[str, str] | None = None,
        order: dict[str, str] | None = None,
        limit: int = 50,
    ) -> list[ThreadEvent]:
        f = filter or {}
        thread_id = f.get("thread_id", "")
        if not thread_id:
            raise ValueError("filter['thread_id'] is required for list_events")

        event_order = "desc"
        if order and order.get("direction") == "asc":
            event_order = "asc"

        event_filters: dict[str, str] = {}
        for key in ("type", "actor", "user_id", "run_id"):
            if key in f:
                event_filters[key] = f[key]

        return await self._persistence.get_events(
            thread_id,
            limit=limit,
            order=event_order,
            filters=event_filters or None,
        )

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        return await self._persistence.get_event(event_id)

    async def create_event(
        self,
        thread_id: str,
        type: str,
        actor: str,
        content: list[ContentPart] | None = None,
        data: dict | None = None,
        user: User | None = None,
        author: str | None = None,
        run_id: str | None = None,
    ) -> ThreadEvent:
        event = ThreadEvent(
            id=_generate_id(),
            thread_id=thread_id,
            run_id=run_id,
            type=type,
            actor=actor,
            author=author,
            user=user or User(user_id=""),
            content=content,
            data=data,
            created_at=_now(),
        )
        await self._persistence.create_event(event)
        return event

    async def remove_event(self, event_id: str) -> None:
        await self._persistence.delete_event(event_id)

    # -- Runs --

    async def list_runs(
        self,
        filter: dict[str, str] | None = None,
        order: dict[str, str] | None = None,
        limit: int = 50,
    ) -> list[Run]:
        f = filter or {}
        return await self._persistence.list_runs(
            thread_id=f.get("thread_id"),
            agent_id=f.get("agent_id"),
            status=f.get("status"),
            limit=limit,
        )

    async def get_run(self, run_id: str) -> Run | None:
        return await self._persistence.get_run(run_id)

    # -- Settings --

    async def get_settings(self, agent_id: str) -> dict:
        return await self._persistence.get_settings(agent_id)

    async def update_settings(self, agent_id: str, values: dict) -> None:
        await self._persistence.set_settings(agent_id, values)
