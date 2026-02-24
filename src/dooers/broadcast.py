from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dooers.protocol.frames import (
    EventAppendPayload,
    S2C_EventAppend,
    S2C_ThreadUpsert,
    ThreadUpsertPayload,
)
from dooers.protocol.models import Actor, ContentPart, Thread, ThreadEvent, User
from dooers.protocol.parser import serialize_frame

if TYPE_CHECKING:
    from dooers.persistence.base import Persistence
    from dooers.registry import ConnectionRegistry


def _generate_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class BroadcastManager:
    def __init__(
        self,
        registry: ConnectionRegistry,
        persistence: Persistence,
        subscriptions: dict[str, set[str]],
    ) -> None:
        self._registry = registry
        self._persistence = persistence
        self._subscriptions = subscriptions

    async def send_event(
        self,
        worker_id: str,
        thread_id: str,
        content: list[ContentPart],
        actor: Actor = "system",
        user: User | None = None,
    ) -> tuple[ThreadEvent, int]:
        thread = await self._persistence.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found")
        if thread.worker_id != worker_id:
            raise PermissionError(f"Thread {thread_id} belongs to different worker")

        now = _now()

        event = ThreadEvent(
            id=_generate_id(),
            thread_id=thread_id,
            run_id=None,
            type="message",
            actor=actor,
            user=user or User(user_id=""),
            content=content,
            data=None,
            created_at=now,
        )

        await self._persistence.create_event(event)

        thread.last_event_at = now
        thread.updated_at = now
        await self._persistence.update_thread(thread)

        frame = S2C_EventAppend(
            id=_generate_id(),
            payload=EventAppendPayload(thread_id=thread_id, events=[event]),
        )
        message = serialize_frame(frame)
        count = await self._registry.broadcast(worker_id, message)

        return event, count

    async def send_thread_update(
        self,
        worker_id: str,
        thread: Thread,
    ) -> int:
        if thread.worker_id != worker_id:
            raise PermissionError(f"Thread {thread.id} belongs to different worker")

        frame = S2C_ThreadUpsert(
            id=_generate_id(),
            payload=ThreadUpsertPayload(thread=thread),
        )
        message = serialize_frame(frame)
        return await self._registry.broadcast(worker_id, message)

    async def create_thread_and_broadcast(
        self,
        worker_id: str,
        user: User | None = None,
        organization_id: str = "",
        workspace_id: str = "",
        title: str | None = None,
    ) -> tuple[Thread, int]:
        now = _now()
        owner = user or User(user_id="")
        thread = Thread(
            id=_generate_id(),
            worker_id=worker_id,
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

        frame = S2C_ThreadUpsert(
            id=_generate_id(),
            payload=ThreadUpsertPayload(thread=thread),
        )
        message = serialize_frame(frame)
        count = await self._registry.broadcast(worker_id, message)

        return thread, count
