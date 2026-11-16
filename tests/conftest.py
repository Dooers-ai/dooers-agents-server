"""Shared fixtures. Connector tasks extend this file (pipeline_harness, svc, turn...)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.router import Router
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.protocol.frames import C2S_EventCreate, EventCreatePayload
from dooers.agents.server.protocol.models import Run, Thread, ThreadEvent, User
from dooers.agents.server.registry import ConnectionRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:  # pragma: no cover
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        pass


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


@dataclass
class RouterHarness:
    router: Router
    ws: _FakeWS
    persistence: AsyncMock
    contexts: list[Any] = field(default_factory=list)

    async def send_event_create(self, payload: dict[str, Any]) -> None:
        frame = C2S_EventCreate(
            id="frame-1",
            type="event.create",
            payload=EventCreatePayload.model_validate(payload),
        )
        await self.router._handle_event_create(self.ws, frame)


@pytest.fixture
def router_harness() -> RouterHarness:
    """A connected Router with a mock persistence; the real pipeline.setup runs,
    pipeline.execute is replaced by a recorder of the HandlerContext it receives."""
    persistence = AsyncMock()
    router = Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
    )
    router._agent_id = "agent-1"
    router._organization_id = "org-1"
    router._workspace_id = "ws-1"
    router._user = User(user_id="u1")
    router._ws_id = "ws-id-1"
    router._subscriptions["ws-id-1"] = set()
    harness = RouterHarness(router=router, ws=_FakeWS(), persistence=persistence)

    async def _execute(context, result):
        harness.contexts.append(context)
        return
        yield  # pragma: no cover

    router._pipeline.execute = _execute  # type: ignore[method-assign]
    return harness


class MemoryPersistence:
    """In-memory slice of Persistence that pipeline.setup/execute touch.

    Stores copies (like a database would) and logs every write, in order, in ``writes``.
    """

    def __init__(self) -> None:
        self.threads: dict[str, Thread] = {}
        self.events: dict[str, ThreadEvent] = {}
        self.runs: dict[str, Run] = {}
        self.persisted: list[ThreadEvent] = []
        self.writes: list[tuple[str, Any]] = []

    async def create_thread(self, thread: Thread) -> None:
        self.threads[thread.id] = thread.model_copy(deep=True)
        self.writes.append(("create_thread", thread.model_copy(deep=True)))

    async def get_thread(self, thread_id: str) -> Thread | None:
        thread = self.threads.get(thread_id)
        return thread.model_copy(deep=True) if thread else None

    async def update_thread(self, thread: Thread) -> None:
        self.threads[thread.id] = thread.model_copy(deep=True)
        self.writes.append(("update_thread", thread.model_copy(deep=True)))

    async def create_event(self, event: ThreadEvent) -> None:
        self.events[event.id] = event.model_copy(deep=True)
        self.persisted.append(event.model_copy(deep=True))
        self.writes.append(("create_event", event.model_copy(deep=True)))

    async def update_event(self, event: ThreadEvent) -> None:
        self.events[event.id] = event.model_copy(deep=True)
        self.writes.append(("update_event", event.model_copy(deep=True)))

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        event = self.events.get(event_id)
        return event.model_copy(deep=True) if event else None

    async def create_run(self, run: Run) -> None:
        self.runs[run.id] = run.model_copy(deep=True)
        self.writes.append(("create_run", run.model_copy(deep=True)))

    async def update_run(self, run: Run) -> None:
        self.runs[run.id] = run.model_copy(deep=True)
        self.writes.append(("update_run", run.model_copy(deep=True)))

    async def upsert_thread_participant(self, thread_id: str, user: User) -> None:
        thread = self.threads.get(thread_id)
        if thread is None:
            return
        user = user.model_copy(deep=True)
        if any(u.user_id == user.user_id for u in thread.users):
            thread.users = [user if u.user_id == user.user_id else u for u in thread.users]
        else:
            thread.users.append(user)
        self.writes.append(("upsert_thread_participant", (thread_id, user.user_id)))


class _RecordingCollector:
    def __init__(self) -> None:
        self.tracked: list[dict[str, Any]] = []

    async def track(self, **kwargs: Any) -> None:
        self.tracked.append(kwargs)

    async def feedback(self, **kwargs: Any) -> None:  # pragma: no cover
        pass


@dataclass
class PipelineHarness:
    pipeline: HandlerPipeline
    persistence: MemoryPersistence
    collector: _RecordingCollector
    broadcasts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def persisted(self) -> list[ThreadEvent]:
        return self.persistence.persisted

    async def run(self, handler: Any, *, presence_token: str | None = None, **overrides: Any) -> list[AgentEvent]:
        """Run the real setup + execute for one turn; return the events execute yielded."""
        fields: dict[str, Any] = {
            "handler": handler,
            "agent_id": "agent-1",
            "message": "hello",
            "organization_id": "org-1",
            "workspace_id": "ws-1",
            "user": User(user_id="u1", user_name="Ana"),
            "presence_token": presence_token,
        }
        fields.update(overrides)
        context = HandlerContext(**fields)
        result = await self.pipeline.setup(context)
        return [event async for event in self.pipeline.execute(context, result)]


@pytest.fixture
def pipeline_harness() -> PipelineHarness:
    """The real HandlerPipeline over an in-memory persistence; broadcasts and analytics are
    recorded (broadcast payloads are deep-copied at send time, as the wire would freeze them)."""
    persistence = MemoryPersistence()
    collector = _RecordingCollector()
    broadcasts: list[dict[str, Any]] = []

    async def _broadcast(agent_id: str, payload: dict[str, Any]) -> None:
        broadcasts.append(copy.deepcopy(payload))

    pipeline = HandlerPipeline(
        persistence=persistence,  # type: ignore[arg-type]
        broadcast_callback=_broadcast,
        analytics_collector=collector,  # type: ignore[arg-type]
    )
    return PipelineHarness(pipeline=pipeline, persistence=persistence, collector=collector, broadcasts=broadcasts)
