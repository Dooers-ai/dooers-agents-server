from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dooers.agents.server.protocol.frames import (
    EventAppendPayload,
    S2C_EventAppend,
    S2C_ThreadUpsert,
    ThreadUpsertPayload,
)
from dooers.agents.server.protocol.models import Actor, ContentPart, Thread, ThreadEvent, User
from dooers.agents.server.protocol.parser import serialize_frame
from dooers.agents.server.thread_access import attach_thread_access
from dooers.agents.server.thread_queue import queue_for_create

if TYPE_CHECKING:
    from dooers.agents.server.persistence.base import Persistence
    from dooers.agents.server.registry import ConnectionRegistry, WebSocketProtocol


def _generate_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


async def broadcast_thread_upsert(
    registry: ConnectionRegistry,
    agent_id: str,
    thread: Thread,
) -> int:
    """Send ``thread.upsert`` with each socket's own ``access``.

    The thread row is shared. ``access`` is not: a participant may write, a
    supervisor may only manage. One serialized frame would leak the actor's
    flags (or omit them) for every other connection.
    """
    bare = thread.model_copy(update={"access": None})
    connections = registry.get_connections(agent_id)
    if not connections:
        return 0

    async def send_one(ws: WebSocketProtocol) -> bool:
        session = registry.session_for(ws)
        outgoing = (
            attach_thread_access(bare, session.user, connection_workspace_id=session.workspace_id)
            if session is not None
            else bare
        )
        frame = S2C_ThreadUpsert(
            id=_generate_id(),
            payload=ThreadUpsertPayload(thread=outgoing),
        )
        try:
            await ws.send_text(serialize_frame(frame))
            return True
        except Exception:
            return False

    results = await asyncio.gather(*(send_one(ws) for ws in connections), return_exceptions=True)
    return sum(1 for result in results if result is True)


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
        agent_id: str,
        thread_id: str,
        content: list[ContentPart],
        actor: Actor = "system",
        user: User | None = None,
    ) -> tuple[ThreadEvent, int]:
        thread = await self._persistence.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found")
        if thread.agent_id != agent_id:
            raise PermissionError(f"Thread {thread_id} belongs to different agent")

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
        count = await self._registry.broadcast(agent_id, message)

        return event, count

    async def send_thread_update(
        self,
        agent_id: str,
        thread: Thread,
    ) -> int:
        if thread.agent_id != agent_id:
            raise PermissionError(f"Thread {thread.id} belongs to different agent")

        return await broadcast_thread_upsert(self._registry, agent_id, thread)

    async def create_thread_and_broadcast(
        self,
        agent_id: str,
        user: User | None = None,
        organization_id: str = "",
        workspace_id: str = "",
        title: str | None = None,
        queue: str | None = None,
    ) -> tuple[Thread, int]:
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
            queue=queue_for_create(queue, workspace_id=workspace_id),
            created_at=now,
            updated_at=now,
            last_event_at=now,
        )

        await self._persistence.create_thread(thread)
        count = await broadcast_thread_upsert(self._registry, agent_id, thread)
        return thread, count
