"""The handler of ``AgentServer.dispatch`` in the Dooers Threads executor (plan 5, task 6b; rulings F5 and F14).

``dispatch`` registers its handler in the process under the SDK event id (the ``client_event_id`` of the trigger event). The executor
that receives the delivery:

- runs that handler instead of the mounted one when the delivery lands in the process that called ``dispatch``, and records the events
  the handler yields (the stream returns them);
- fails the delivery visibly when the trigger event carries the marker of a handler this process does not have and does not mount;
- runs the mounted handler when there is no entry and no marker, or the marker names the mounted handler.

These tests deliver to the real adapter and executor through ``FakeDooersThreads``; ``tests/test_dooers_threads_dispatch.py`` follows
the stream against the agent API fake, and the last tests here join both (``Bridge``) to compare ``collect()`` with production.
"""

from __future__ import annotations

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.dooers_threads import create_text_delivery_adapter
from dooers.agents.server.dooers_threads.dispatch_registry import DISPATCHES, MARKER_KEY, handler_fingerprint
from tests.parity.fake_dooers_threads import FakeDooersThreads, private_resources
from tests.test_dooers_threads_agent_persistence import ANA

ASSISTANT = "Assistente"
TEXT = [{"type": "text", "text": "Oi"}]


@pytest.fixture(autouse=True)
def clean_registry():
    DISPATCHES.clear()
    yield
    DISPATCHES.clear()


@pytest.fixture
def dooers_threads():
    fake = FakeDooersThreads(assistant_name=ASSISTANT, core_people=[ANA])
    with fake.serving():
        yield fake


def agent_config(fake: FakeDooersThreads) -> AgentConfig:
    return AgentConfig(
        database_type="postgres",
        thread_management="dooers_threads",
        dooers_threads=fake.config,
        assistant_name=ASSISTANT,
        analytics_enabled=False,
        agent_seed_secret="",
    )


class Handlers:
    """A mounted handler and a custom one; ``calls`` says which ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

        async def mounted(on, send, memory, analytics, settings):
            self.calls.append("mounted")
            yield send.text("montado")

        async def custom(on, send, memory, analytics, settings):
            self.calls.append("custom")
            yield send.run_start()
            yield send.text("personalizado")
            yield send.run_end()

        self.mounted, self.custom = mounted, custom


async def deliver(fake: FakeDooersThreads, handlers: Handlers, *, client_event_id: str = "evt-1", data: dict | None = None):
    adapter = create_text_delivery_adapter(agent_config(fake), handlers.mounted, private_resources)
    thread = fake.create_thread(ANA, title="Atendimento")
    trigger = fake.agent_publish(thread, ANA, TEXT, client_event_id=client_event_id, data=data)
    delivery = fake.deliveries_for(trigger)[0]
    response = await fake.deliver(adapter, delivery)
    texts = [e["payload"]["content"][0]["text"] for e in fake.events[thread] if e["type"] == "message.created"][1:]
    return delivery, response, texts


async def test_the_registered_dispatch_handler_runs_instead_of_the_mounted_one(dooers_threads):
    handlers = Handlers()
    entry = DISPATCHES.register("evt-1", handlers.custom)
    delivery, response, texts = await deliver(dooers_threads, handlers)
    assert response.status_code == 200 and delivery.state == "responded"
    assert handlers.calls == ["custom"] and texts == ["personalizado"]
    # The executor recorded what the handler yielded, in order, as the very objects the handler made.
    recorded = entry.runs[delivery.execution_id]
    assert [(e.send_type, e.data.get("text")) for e in recorded] == [("run_start", None), ("text", "personalizado"), ("run_end", None)]
    assert entry.failures == {}


async def test_the_mounted_handler_runs_without_an_entry_or_a_marker(dooers_threads):
    handlers = Handlers()
    delivery, response, texts = await deliver(dooers_threads, handlers)
    assert response.status_code == 200 and handlers.calls == ["mounted"] and texts == ["montado"]


async def test_the_entry_belongs_to_one_trigger_event(dooers_threads):
    handlers = Handlers()
    DISPATCHES.register("another-event", handlers.custom)
    _, _, texts = await deliver(dooers_threads, handlers)
    assert handlers.calls == ["mounted"] and texts == ["montado"]


async def test_a_marker_of_another_handler_fails_the_delivery_visibly(dooers_threads, caplog):
    handlers = Handlers()
    delivery, response, texts = await deliver(dooers_threads, handlers, data={MARKER_KEY: "my_app.handlers:billing_handler"})
    assert handlers.calls == []  # a different handler never runs silently
    assert delivery.state == "failed" and delivery.reason_code == "handler_error"
    # Visible: the error text is a message of the agent in the thread and it names the handler and the single-process rule.
    assert len(texts) == 1 and "my_app.handlers:billing_handler" in texts[0] and "process that called dispatch" in texts[0]
    assert "handler error" in caplog.text


async def mounted_by_name(on, send, memory, analytics, settings):
    yield send.text("montado pelo nome")


async def test_a_marker_of_the_mounted_handler_runs_it(dooers_threads):
    handlers = Handlers()
    handlers.mounted = mounted_by_name  # a named handler: another process that mounts it has the same fingerprint
    delivery, response, texts = await deliver(dooers_threads, handlers, data={MARKER_KEY: handler_fingerprint(handlers.mounted)})
    assert texts == ["montado pelo nome"] and delivery.state == "responded"


async def test_a_marker_with_an_entry_in_this_process_runs_the_entry(dooers_threads):
    handlers = Handlers()
    DISPATCHES.register("evt-1", handlers.custom)
    _, _, texts = await deliver(dooers_threads, handlers, data={MARKER_KEY: "my_app.handlers:billing_handler"})
    assert handlers.calls == ["custom"] and texts == ["personalizado"]


async def test_the_failure_of_the_dispatch_handler_is_recorded_with_the_exception(dooers_threads):
    boom = RuntimeError("boom")

    async def failing(on, send, memory, analytics, settings):
        yield send.text("antes")
        raise boom

    handlers = Handlers()
    entry = DISPATCHES.register("evt-1", failing)
    delivery, response, texts = await deliver(dooers_threads, handlers)
    assert delivery.state == "failed" and delivery.reason_code == "handler_error"
    assert entry.failures[delivery.execution_id] is boom
    assert [e.data["text"] for e in entry.runs[delivery.execution_id]] == ["antes"]


# -- the marker a dispatch call writes --


async def named_handler(on, send, memory, analytics, settings):
    yield send.text("nomeado")


def test_marker_is_written_only_when_a_delivery_could_run_a_different_handler():
    handlers = Handlers()
    handlers.custom = named_handler
    # Nothing mounted here: the executor is in another process, it compares the fingerprint.
    assert DISPATCHES.marker_for("agent-1", handlers.custom) == handler_fingerprint(handlers.custom)
    DISPATCHES.mount("agent-1", handlers.mounted)
    assert DISPATCHES.marker_for("agent-1", handlers.mounted) is None  # the mounted handler itself
    assert DISPATCHES.marker_for("agent-1", handlers.custom) == handler_fingerprint(handlers.custom)
    assert DISPATCHES.marker_for("agent-2", named_handler) == handler_fingerprint(named_handler)


def test_fingerprint_of_a_named_function_is_stable_and_of_a_local_one_is_unique():
    first, second = Handlers(), Handlers()
    assert handler_fingerprint(first.mounted) != handler_fingerprint(first.mounted)  # a closure has no name another process can match
    assert handler_fingerprint(Handlers) == f"{__name__}:Handlers"
    assert handler_fingerprint(second.custom).startswith("object:")


def test_the_registry_is_bounded():
    from dooers.agents.server.dooers_threads import dispatch_registry as module

    handlers = Handlers()
    for number in range(module.MAX_ENTRIES + 5):
        DISPATCHES.register(f"e{number}", handlers.custom)
    assert DISPATCHES.claim("e0", "x") is None and DISPATCHES.claim(f"e{module.MAX_ENTRIES + 4}", "x") is not None


# -- the stream against the real executor: collect() against production --

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import respx  # noqa: E402

import dooers.agents.server.dooers_threads.dispatch as dispatch_module  # noqa: E402
from dooers.agents.server.exceptions import HandlerError  # noqa: E402
from dooers.agents.server.protocol.models import User  # noqa: E402
from tests.parity.fake_agent_api import FakeAgentApi  # noqa: E402
from tests.parity.fake_dooers_threads import INSTANCE_AGENT_ID, ORGANIZATION_ID  # noqa: E402
from tests.parity.model import WORKSPACE_ID  # noqa: E402
from tests.test_dooers_threads_agent_persistence import core_person  # noqa: E402
from tests.test_dooers_threads_ingest import World, make_world  # noqa: E402


class Bridge:
    """The agent API fake (the thread the dispatch publishes to and follows) joined to FakeDooersThreads (the real executor).

    A publication with ``trigger`` becomes a delivery of ``FakeDooersThreads`` to the real adapter. What the execution wrote (handler
    outputs) is copied to the thread of the agent API with the delivery and execution ids, followed by what Dooers Threads writes at the
    end (``execution.*`` and ``delivery.updated``). ``foreign=True`` is a delivery that lands in another process: the registry of this
    process knows nothing of the call.
    """

    def __init__(
        self, api: FakeAgentApi, dooers_threads: FakeDooersThreads, adapter: Any, people: dict[str, User], *, foreign: bool = False
    ) -> None:
        self.api, self.dooers_threads, self.adapter, self.people, self.foreign = api, dooers_threads, adapter, people, foreign
        self.threads: dict[str, str] = {}
        self.tasks: list[asyncio.Task[None]] = []
        api.on_trigger = self._on_trigger

    def _on_trigger(self, thread_id: str, event: dict[str, Any], delivery_id: str) -> None:
        self.tasks.append(asyncio.get_running_loop().create_task(self._run(thread_id, event, delivery_id)))

    async def _run(self, thread_id: str, event: dict[str, Any], delivery_id: str) -> None:
        api_thread = self.api.threads[thread_id]
        human = self.people[event["actor"]["principal_id"]]
        if thread_id not in self.threads:
            self.threads[thread_id] = self.dooers_threads.create_thread(human, title=api_thread.title)
        mirror = self.threads[thread_id]
        payload = event["payload"]
        trigger = self.dooers_threads.agent_publish(
            mirror, human, payload["content"], client_event_id=payload["client_event_id"], data=payload.get("data")
        )
        delivery = self.dooers_threads.deliveries_for(trigger)[0]
        if self.foreign:
            DISPATCHES.forget(payload["client_event_id"])
        agent = next(p for p in api_thread.participants if p["kind"] == "agent")

        def write(type_: str, body: dict[str, Any], *, execution: bool = True) -> None:
            links = {"delivery_id": delivery_id, **({"execution_id": delivery.execution_id} if execution else {})}
            self.api.inject_event(thread_id, type_, body, actor=event["actor"], **links)

        def execution(state: str, outcome: str | None, reason: str | None) -> dict[str, Any]:
            return {
                "execution_id": delivery.execution_id,
                "delivery_id": delivery_id,
                "participant_id": agent["participant_id"],
                "phase_id": "initial",
                "state": state,
                "outcome": outcome,
                "reason_code": reason,
                "deadline_at": "2026-11-13T12:10:00Z",
                "completed_at": None if state == "running" else "2026-11-13T12:00:45Z",
            }

        write("execution.started", execution("running", None, None))
        await self.dooers_threads.deliver(self.adapter, delivery)
        for stored in self.dooers_threads.events[mirror]:
            if int(stored["sequence"]) > int(trigger["sequence"]):
                view = self.dooers_threads._agent_event(stored)
                self.api.inject_event(thread_id, view["type"], view["payload"], delivery_id=delivery_id, execution_id=delivery.execution_id)
        if delivery.state == "failed":
            write("execution.failed", execution("failed", "failed", delivery.reason_code))
        else:
            write("execution.completed", execution("completed", delivery.state, None))
        body = {
            "attempt_count": 1,
            "completed_at": None,
            "created_at": "2026-11-13T12:00:00Z",
            "delivery_id": delivery_id,
            "execution_id": delivery.execution_id,
            "expires_at": "2026-11-13T13:00:00Z",
            "participant_id": agent["participant_id"],
            "phase_id": "initial",
            "reason_code": delivery.reason_code,
            "state": delivery.state,
            "trigger_event_id": event["event_id"],
            "updated_at": "2026-11-13T12:00:00Z",
        }
        write("delivery.updated", {"delivery": body})


async def tour(on, send, memory, analytics, settings) -> AsyncIterator[Any]:
    yield send.run_start()
    yield send.reasoning("pensando")
    yield send.text("Olá, " + on.context.user.user_name)
    yield send.tool_call("busca", {"q": "x"}, id="call-1")
    yield send.tool_result("busca", {"ok": True}, args={"q": "x"}, id="call-1")
    yield send.update_thread(title="Atendimento da Ana")
    yield send.text("Pronto", author="Bot")
    yield send.run_end()


async def failing(on, send, memory, analytics, settings) -> AsyncIterator[Any]:
    yield send.run_start()
    yield send.text("antes do erro")
    raise ValueError("falhou de propósito")


@pytest.fixture
async def agent_api():
    api = await FakeAgentApi(workspace_id=WORKSPACE_ID).start()
    api.agent_id, api.organization_id = INSTANCE_AGENT_ID, ORGANIZATION_ID
    api.core_people[ANA.user_id] = core_person(ANA)
    try:
        yield api
        assert api.failures == []
    finally:
        await api.stop()


@pytest.fixture
def fast_polls(monkeypatch):
    async def sleep(_seconds: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(dispatch_module, "_sleep", sleep)


@pytest.fixture
async def arena(monkeypatch, agent_api, fast_polls):
    """``arena(handler, foreign=...)``: a production server and a Dooers Threads one wired to the bridge, and the ``dispatch`` of both."""
    worlds: list[World] = []
    dooers_threadss: list[FakeDooersThreads] = []
    stack = respx.mock(assert_all_called=False, assert_all_mocked=False)

    async def make(handler: Any, *, foreign: bool = False, mounted: Any = None):
        dooers_threads = FakeDooersThreads(assistant_name=ASSISTANT, core_people=[ANA])
        dooers_threadss.append(dooers_threads)
        world = await make_world(monkeypatch, agent_api)
        worlds.append(world)
        adapter = create_text_delivery_adapter(agent_config(dooers_threads), mounted or handler, private_resources)
        Bridge(agent_api, dooers_threads, adapter, {ANA.user_id: ANA}, foreign=foreign)
        # The agent API fake is a real local server: its requests pass through; the executor callbacks go to FakeDooersThreads.
        stack.route(host="127.0.0.1", port=agent_api.port).pass_through()
        stack.start()
        dooers_threads.serving(stack).__enter__()
        kwargs = {"organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID}

        async def prod(thread_id: str = "t-1", message: str = "Oi", handler_: Any = None, user: User = ANA):
            return await world.prod.dispatch(handler_ or handler, INSTANCE_AGENT_ID, message, user=user, thread_id=thread_id, **kwargs)

        async def via_dooers_threads(thread_id: str = "t-1", message: str = "Oi", handler_: Any = None, user: User = ANA):
            return await world.dooers_threads.dispatch(
                handler_ or handler, INSTANCE_AGENT_ID, message, user=user, thread_id=thread_id, **kwargs
            )

        return prod, via_dooers_threads

    yield make
    for world in worlds:
        await world.prod.close()
        await world.dooers_threads.close()
    stack.stop()


def shape(events: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(event.send_type, event.data) for event in events]


async def test_dispatch_streams_handler_events_like_production(arena):
    prod, via_dooers_threads = await arena(tour)
    expected = await (await prod()).collect()
    stream = await via_dooers_threads()
    got = await stream.collect()
    assert shape(expected)[:3] == [
        ("run_start", {"agent_id": None}),
        ("reasoning", {"text": "pensando", "author": None}),
        ("text", {"text": "Olá, Ana Souza", "author": None}),
    ]
    # In the process that called dispatch the stream returns the handler's own events: equal to production's, nothing converted.
    assert shape(got) == shape(expected)
    assert (stream.thread_id, stream.is_new_thread) == ("t-1", True)
    assert stream._entry.claimed(next(iter(stream._entry.runs)))  # it ran here: these are the recorded originals, not conversions


async def test_events_from_another_process_equal_production_for_this_scenario(arena):
    prod, via_dooers_threads = await arena(tour, foreign=True)
    expected = await (await prod()).collect()
    stream = await via_dooers_threads()
    got = await stream.collect()
    assert stream._entry.runs == {}  # the delivery landed elsewhere: nothing was recorded, everything is converted from the thread
    assert shape(got) == shape(expected)


async def test_handler_failure_raises_handler_error_like_production(arena):
    prod, via_dooers_threads = await arena(failing)
    seen: list[Any] = []
    with pytest.raises(HandlerError) as expected:
        async for event in await prod():
            seen.append(event)
    stream = await via_dooers_threads()
    got: list[Any] = []
    with pytest.raises(HandlerError) as caught:
        async for event in stream:
            got.append(event)
    assert shape(got) == shape(seen) == [("run_start", {"agent_id": None}), ("text", {"text": "antes do erro", "author": None})]
    assert str(caught.value) == str(expected.value) == "falhou de propósito"
    assert type(caught.value.original) is type(expected.value.original) is ValueError


async def test_handler_failure_from_another_process_raises_handler_error_with_the_error_text(arena):
    prod, via_dooers_threads = await arena(failing, foreign=True)
    stream = await via_dooers_threads()
    got: list[Any] = []
    with pytest.raises(HandlerError) as caught:
        async for event in stream:
            got.append(event)
    assert str(caught.value) == "falhou de propósito" and caught.value.original is None
    # Declared difference: the executor's own failure report (the error text as a message and the run ending failed) is in the thread.
    assert shape(got) == [
        ("run_start", {"agent_id": None}),
        ("text", {"text": "antes do erro", "author": None}),
        ("text", {"text": "falhou de propósito", "author": None}),
        ("run_end", {"status": "failed", "error": "falhou de propósito"}),
    ]


async def other_handler(on, send, memory, analytics, settings):
    yield send.run_start()
    yield send.text("outro handler")
    yield send.run_end()


async def test_two_dispatch_handlers_run_each_its_own_in_this_process(arena, agent_api):
    handlers = Handlers()
    _, via_dooers_threads = await arena(tour, mounted=handlers.mounted)
    first = await via_dooers_threads("t-a", handler_=tour)
    second = await via_dooers_threads("t-b", handler_=other_handler)
    got_first, got_second = await first.collect(), await second.collect()
    # Each call ran its own handler, neither the mounted one (``handlers.mounted``) nor the other call's.
    assert [e.data["text"] for e in got_first if e.send_type == "text"] == ["Olá, Ana Souza", "Pronto"]
    assert [e.data["text"] for e in got_second if e.send_type == "text"] == ["outro handler"]
    assert handlers.calls == []
    # Both are different from the mounted handler, so both triggers carry the marker (other processes could not tell).
    for thread in agent_api.threads.values():
        trigger = next(e for e in thread.events if e["type"] == "message.created" and "client_event_id" in e["payload"])
        assert trigger["payload"]["data"][MARKER_KEY].startswith(f"{__name__}:")


async def test_the_mounted_handler_as_dispatch_handler_writes_no_marker(arena, agent_api):
    _, via_dooers_threads = await arena(tour)
    stream = await via_dooers_threads()
    await stream.collect()
    trigger = next(e for e in next(iter(agent_api.threads.values())).events if e["payload"].get("client_event_id") == stream.event_id)
    assert MARKER_KEY not in (trigger["payload"].get("data") or {})


async def test_a_dispatch_handler_delivered_to_another_process_fails_visibly(arena):
    handlers = Handlers()
    _, via_dooers_threads = await arena(tour, foreign=True, mounted=handlers.mounted)
    stream = await via_dooers_threads(handler_=other_handler)
    with pytest.raises(HandlerError) as caught:
        await stream.collect()
    # The mounted handler of that process did not answer in place of the handler of the call; the error says why.
    assert handlers.calls == []
    assert "process that called dispatch" in str(caught.value) and f"{__name__}:other_handler" in str(caught.value)


async def who(on, send, memory, analytics, settings) -> AsyncIterator[Any]:
    user = on.context.user
    yield send.text(f"{user.user_id}|{user.user_name}|{user.user_email}|{user.system_role}|{user.organization_role}|{user.workspace_role}")


async def test_the_roles_of_the_person_come_from_the_core_not_from_the_caller(arena):
    """core_people (carry of task 5a): production gives the handler the ``User`` of the caller; Dooers Threads, what the core knows."""
    prod, via_dooers_threads = await arena(who)
    same = await (await prod()).collect()
    assert [e.data["text"] for e in await (await via_dooers_threads()).collect()] == [e.data["text"] for e in same]
    # A caller whose ``User`` says other roles than the core (viewer, no e-mail): the declared difference.
    caller = ANA.model_copy(update={"workspace_role": "viewer", "organization_role": "member", "user_email": None})
    production = [e.data["text"] for e in await (await prod("t-2", user=caller)).collect()]
    dooers_threads = [e.data["text"] for e in await (await via_dooers_threads("t-2", user=caller)).collect()]
    assert production == [f"{ANA.user_id}|Ana Souza|None|user|member|viewer"]
    assert dooers_threads == [f"{ANA.user_id}|Ana Souza|ana@example.com|user|owner|manager"]
