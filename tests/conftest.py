"""Shared fixtures. Connector tasks extend this file (pipeline_harness, svc, turn...)."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.router import Router
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.protocol.frames import C2S_EventCreate, EventCreatePayload
from dooers.agents.server.protocol.models import Run, Thread, ThreadEvent, User
from dooers.agents.server.registry import ConnectionRegistry
from dooers.tools.connectors.runtime import TurnContext, bind_turn, reset_turn


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


@pytest.fixture(autouse=True)
def _reset_connector_state():
    """Module-level connector caches (service token client, connector_session cache) must not leak between tests."""
    from dooers.tools.connectors.transport import reset_transport_state

    reset_transport_state()
    yield
    reset_transport_state()


# --- connector service (svc), connector turn (turn), fake clock (fake_clock) ---

SERVICE_URL = "https://connectors.test"
CORE_URL = "https://core.test"


class ServiceCall:
    """One request a fake service route received; ``params`` are the named groups of the route's URL."""

    def __init__(self, request: httpx.Request, params: dict[str, str]) -> None:
        self.request = request
        self.params = params

    def json(self) -> Any:
        return json.loads(self.request.content)

    @property
    def interaction_id(self) -> str | None:
        return self.params.get("interaction_id")


class ServiceEndpoint:
    """A fake service route driven like ``unittest.mock``: ``return_value``, or ``side_effect`` as a list answered
    in order or a callable (sync or async) taking the ``ServiceCall``. A dict answer becomes a ``200`` JSON reply,
    an ``httpx.Response`` goes as is and an exception is raised as the HTTP client would. ``calls`` keeps requests."""

    def __init__(self) -> None:
        self.calls: list[ServiceCall] = []
        self.return_value: Any = None
        self._side_effect: Any = None

    @property
    def side_effect(self) -> Any:
        return self._side_effect

    @side_effect.setter
    def side_effect(self, value: Any) -> None:
        self._side_effect = iter(value) if isinstance(value, list | tuple) else value

    async def respond(self, request: httpx.Request, **params: str) -> httpx.Response:
        call = ServiceCall(request, params)
        self.calls.append(call)
        answer = self._side_effect if self._side_effect is not None else self.return_value
        if isinstance(answer, Iterator):
            answer = next(answer, None)
            assert answer is not None, f"no answer left for request #{len(self.calls)} to {request.url.path}"
        if callable(answer) and not isinstance(answer, type):
            answer = answer(call)
            if inspect.isawaitable(answer):
                answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        assert answer is not None, f"no answer configured for {request.url.path}"
        return httpx.Response(200, json=answer)


class FakeConnectorService:
    """``/v1/tools/list``, ``/v1/tools/execute`` and ``/v1/interactions/{id}/wait`` of the connector service, plus
    the core routes the transport calls (service token, presence exchange) answering successfully."""

    def __init__(self) -> None:
        self.list = ServiceEndpoint()
        self.execute = ServiceEndpoint()
        self.wait = ServiceEndpoint()
        self.token: respx.Route | None = None
        self.session: respx.Route | None = None


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("DOOERS_CONNECTOR_SERVICE_URL", SERVICE_URL)
    monkeypatch.setenv("AGENT_CORE_BASE_URL", CORE_URL)
    monkeypatch.delenv("AGENT_SEED_SECRET", raising=False)
    fake = FakeConnectorService()
    with respx.mock(assert_all_called=False) as mock:
        fake.token = mock.post(f"{CORE_URL}/api/v2/identity/service-token/agent").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"accessToken": "svc-jwt", "expiresIn": 300}})
        )
        fake.session = mock.post(f"{CORE_URL}/api/v2/identity/connector-session").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"token": "sess-1", "expiresAt": "2030-01-01T00:10:00Z"}})
        )
        mock.post(f"{SERVICE_URL}/v1/tools/list").mock(side_effect=fake.list.respond)
        mock.post(f"{SERVICE_URL}/v1/tools/execute").mock(side_effect=fake.execute.respond)
        mock.post(url__regex=rf"^{re.escape(SERVICE_URL)}/v1/interactions/(?P<interaction_id>[^/]+)/wait$").mock(
            side_effect=fake.wait.respond
        )
        yield fake


class _SecretsPersistence:
    async def get_service_secrets(self, agent_id: str) -> dict[str, Any]:
        return {"dooers_runtime_api_key": "runtime-key"}


@dataclass
class TurnHarness:
    ctx: TurnContext
    emitted: list[AgentEvent]


@pytest.fixture
def turn():
    """An active connector turn (web chat, with presence) whose emitter records the events it gets."""
    emitted: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        emitted.append(event)

    ctx = TurnContext(
        agent_id="agent-1",
        organization_id="org-1",
        workspace_id="ws-1",
        thread_id="th-1",
        user_id="u1",
        channel="dooers-platform",
        presence_token="presence-1",
        persistence=_SecretsPersistence(),  # type: ignore[arg-type]
        emit=emit,
    )
    token = bind_turn(ctx)
    yield TurnHarness(ctx=ctx, emitted=emitted)
    reset_turn(token)


class FakeClock:
    """Replaces the executor's monotonic clock and sleep: time only moves when a test or ``sleep`` moves it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def fake_clock(monkeypatch) -> FakeClock:
    from dooers.tools.connectors import executor

    clock = FakeClock()
    monkeypatch.setattr(executor, "_clock", clock)
    monkeypatch.setattr(executor, "_sleep", clock.sleep)
    return clock
