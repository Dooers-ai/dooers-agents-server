from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dooers.agents.server.protocol.frames import (
    EventAppendPayload,
    S2C_EventAppend,
    S2C_ThreadUpsert,
    ThreadUpsertPayload,
)
from dooers.agents.server.protocol.models import Actor, ContentPart, Thread, ThreadEvent, User
from dooers.agents.server.protocol.parser import serialize_frame
from dooers.agents.server.thread_access import attach_thread_access, resolve_thread_access
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


def thread_recipients(
    registry: ConnectionRegistry,
    agent_id: str,
    thread: Thread,
    subscriptions: dict[str, set[str]],
) -> list[WebSocketProtocol]:
    """Sockets allowed to receive live events of ``thread``.

    A socket gets them when it subscribed to the thread (subscribe already enforced read
    access and, for supervisors, the access reason) or when its user is a participant
    (``write``) — which covers the sender of the first message of a new thread before the
    subscription exists. Everyone else connected to the same agent receives nothing: an
    agent shared by a whole organization must not stream one person's chat to the others.
    """
    out: list[WebSocketProtocol] = []
    for ws in registry.get_connections(agent_id):
        session = registry.session_for(ws)
        if session is None:
            continue
        if session.ws_id and thread.id in subscriptions.get(session.ws_id, ()):
            out.append(ws)
            continue
        if resolve_thread_access(session.user, thread, connection_workspace_id=session.workspace_id).write:
            out.append(ws)
    return out


class _ThreadCache:
    """Short-lived thread cache so high-frequency ``event.delta`` frames do not hit the database."""

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self._ttl = ttl_seconds
        self._rows: dict[str, tuple[float, Thread]] = {}

    def get(self, thread_id: str) -> Thread | None:
        import time

        hit = self._rows.get(thread_id)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        return None

    def put(self, thread: Thread) -> None:
        import time

        if len(self._rows) > 2048:
            self._rows.clear()
        self._rows[thread.id] = (time.monotonic(), thread)


class PipelineBroadcaster:
    """Turns pipeline payload dicts into S2C frames and delivers them to the right sockets.

    Shared by the WebSocket router and ``AgentServer.dispatch`` (REST, schedules, channels).
    """

    def __init__(
        self,
        *,
        registry: ConnectionRegistry,
        persistence: Persistence,
        subscriptions: dict[str, set[str]],
        hydrate: Any = None,
    ) -> None:
        self._registry = registry
        self._persistence = persistence
        self._subscriptions = subscriptions
        self._hydrate = hydrate
        self._threads = _ThreadCache()

    async def _thread(self, thread_id: str, *, cached: bool) -> Thread | None:
        if cached:
            hit = self._threads.get(thread_id)
            if hit is not None:
                return hit
        thread = await self._persistence.get_thread(thread_id)
        if thread is not None:
            self._threads.put(thread)
        return thread

    async def __call__(self, agent_id: str, payload: dict[str, Any]) -> None:
        from dooers.agents.server.protocol.frames import (
            EventDeltaPayload,
            RunUpsertPayload,
            S2C_EventDelta,
            S2C_RunUpsert,
        )

        payload_type = payload.get("type")
        if payload_type == "thread.upsert":
            thread = payload["thread"]
            self._threads.put(thread)
            await broadcast_thread_upsert(self._registry, agent_id, thread)
            return
        if payload_type == "event.append":
            thread = await self._thread(payload["thread_id"], cached=False)
            if thread is None:
                return
            events = payload["events"]
            if self._hydrate is not None:
                events = await self._hydrate(events, thread)
            frame: Any = S2C_EventAppend(
                id=_generate_id(),
                payload=EventAppendPayload(thread_id=thread.id, events=events),
            )
        elif payload_type == "event.delta":
            thread = await self._thread(payload["thread_id"], cached=True)
            if thread is None:
                return
            frame = S2C_EventDelta(
                id=_generate_id(),
                payload=EventDeltaPayload(**{k: v for k, v in payload.items() if k != "type"}),
            )
        elif payload_type == "run.upsert":
            run = payload["run"]
            thread = await self._thread(run.thread_id, cached=True)
            if thread is None:
                return
            frame = S2C_RunUpsert(id=_generate_id(), payload=RunUpsertPayload(run=run))
        else:
            return
        recipients = thread_recipients(self._registry, agent_id, thread, self._subscriptions)
        await self._registry.send_to(recipients, serialize_frame(frame))


def make_memory_notifier(registry: ConnectionRegistry):
    """``memory.upsert`` / ``memory.deleted`` to the owner's sockets (or the whole workspace)."""
    from dooers.agents.server.protocol.frames import (
        MemoryDeletedPayload,
        MemoryUpsertPayload,
        S2C_MemoryDeleted,
        S2C_MemoryUpsert,
    )

    async def notify(action: str, record: Any, user_id: str | None) -> None:
        if record is None:
            return
        if record.scope == "workspace":
            recipients = [
                ws
                for ws in registry.get_connections(record.agent_id)
                if (session := registry.session_for(ws)) is not None and session.workspace_id == record.workspace_id
            ]
        else:
            recipients = [
                ws
                for ws in registry.connections_for_user(record.agent_id, record.user_id)
                if (session := registry.session_for(ws)) is not None and session.workspace_id == record.workspace_id
            ]
        if not recipients:
            return
        if action == "deleted":
            frame: Any = S2C_MemoryDeleted(id=_generate_id(), payload=MemoryDeletedPayload(memory_id=record.id))
        else:
            frame = S2C_MemoryUpsert(id=_generate_id(), payload=MemoryUpsertPayload(memory=record))
        await registry.send_to(recipients, serialize_frame(frame))

    return notify


def make_schedule_notifier(registry: ConnectionRegistry):
    """``schedule.upsert`` / ``schedule.deleted`` to every socket of the schedule's owner."""
    from dooers.agents.server.protocol.frames import (
        S2C_ScheduleDeleted,
        S2C_ScheduleUpsert,
        ScheduleDeletedPayload,
        ScheduleUpsertPayload,
    )

    async def notify(action: str, record: Any) -> None:
        recipients = registry.connections_for_user(record.agent_id, record.user_id)
        if not recipients:
            return
        if action == "deleted" or record.status == "canceled":
            frame: Any = S2C_ScheduleDeleted(id=_generate_id(), payload=ScheduleDeletedPayload(schedule_id=record.id))
        else:
            frame = S2C_ScheduleUpsert(id=_generate_id(), payload=ScheduleUpsertPayload(schedule=record))
        await registry.send_to(recipients, serialize_frame(frame))

    return notify


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
        recipients = thread_recipients(self._registry, agent_id, thread, self._subscriptions)
        count = await self._registry.send_to(recipients, message)

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
