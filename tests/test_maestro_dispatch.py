"""``AgentServer.dispatch`` and ``MaestroDispatchStream`` in Maestro mode (plan 5, task 6b).

``dispatch`` publishes the trigger message over the agent API and follows the thread until the execution of the delivery ends. The
handler runs in the Maestro executor, not in the stream. What is compared and what differs from production is declared here, in
``tests/test_maestro_dispatch_run.py`` (the stream against the real executor) and in "dispatch pela API do agente" of the Maestro
README:

- the handler runs when Maestro delivers the message (also when nobody reads the stream); production runs it only while the stream is read;
- in the process that called ``dispatch`` the stream returns the handler's own ``AgentEvent`` (F5, F14); in another process it
  returns the events converted from the Maestro thread (``agent_event_from`` and the corrections of C32), with the temporary link of a
  file as the URL, and the failure of the handler as ``HandlerError`` without ``original``;
- the roles of the person come from the core, not from the ``User`` of the caller (core_people).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

import dooers.agents.server.maestro.dispatch as dispatch_module
from dooers.agents.server.exceptions import HandlerError
from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.maestro.agent_api import MaestroAgentApiError, MaestroAgentScopeError
from dooers.agents.server.maestro.agent_persistence import MaestroAgentUnsupported
from dooers.agents.server.maestro.dispatch import MaestroDispatchStream
from tests.parity.fake_agent_api import AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, FakeAgentApi
from tests.test_maestro_agent_persistence import ANA, BIA, core_person
from tests.test_maestro_ingest import CONTACT, KEY, META, PNG, CanonicalMimeApi, World, make_world, media_served

COMMON = {"organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID}
send = AgentSend()


async def never_runs(*_args: Any):  # the handler of a dispatch: it runs in the executor, not in these tests
    if False:  # pragma: no cover
        yield None


@pytest.fixture
async def fake():
    server = await CanonicalMimeApi(workspace_id=WORKSPACE_ID).start()
    for person in (ANA, BIA):
        server.core_people[person.user_id] = core_person(person)
    try:
        yield server
        assert server.failures == []  # a response of the fake that is not contract-valid is a bug of the fake
    finally:
        await server.stop()


@pytest.fixture
async def world(monkeypatch, fake):
    built = await make_world(monkeypatch, fake)
    yield built
    await built.prod.close()
    await built.maestro.close()


class Clock:
    """The sleep of the stream, virtual: every sleep moves the clock and may run what the script scheduled for that time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.script: list[tuple[float, Callable[[], None]]] = []

    def at(self, seconds: float, action: Callable[[], None]) -> None:
        self.script.append((seconds, action))

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        due = [item for item in self.script if item[0] <= self.now]
        for item in due:
            self.script.remove(item)
            item[1]()


@pytest.fixture
def clock(monkeypatch):
    virtual = Clock()
    monkeypatch.setattr(dispatch_module, "_sleep", virtual.sleep)
    return virtual


class Maestro:
    """What Maestro writes for one delivery in the fake thread: the execution, the handler outputs and the delivery state."""

    def __init__(self, fake: FakeAgentApi, stream: MaestroDispatchStream) -> None:
        self.fake, self.stream = fake, stream
        self.thread_id = stream._published.maestro_thread_id
        self.delivery_id = stream._delivery_id
        self.execution_id = str(uuid4())
        self.trigger = fake.threads[self.thread_id].events[-1]
        agent = next(p for p in fake.threads[self.thread_id].participants if p["kind"] == "agent")
        self.agent_participant = agent["participant_id"]

    def _event(self, type_: str, payload: dict[str, Any], *, execution: bool = True) -> dict[str, Any]:
        links = {"delivery_id": self.delivery_id, **({"execution_id": self.execution_id} if execution else {})}
        return self.fake.inject_event(self.thread_id, type_, payload, actor=deepcopy(self.trigger["actor"]), **links)

    def _execution(self, state: str, outcome: str | None, reason: str | None) -> dict[str, Any]:
        done = state != "running"
        return {
            "execution_id": self.execution_id,
            "delivery_id": self.delivery_id,
            "participant_id": self.agent_participant,
            "phase_id": "initial",
            "state": state,
            "outcome": outcome,
            "reason_code": reason,
            "deadline_at": "2026-11-13T12:10:00Z",
            "completed_at": "2026-11-13T12:00:45Z" if done else None,
        }

    def delivery(self, state: str, *, execution: bool = True, reason: str | None = None) -> None:
        body = {
            "attempt_count": 1,
            "completed_at": None,
            "created_at": "2026-11-13T12:00:00Z",
            "delivery_id": self.delivery_id,
            "execution_id": self.execution_id if execution else None,
            "expires_at": "2026-11-13T13:00:00Z",
            "participant_id": self.agent_participant,
            "phase_id": "initial",
            "reason_code": reason,
            "state": state,
            "trigger_event_id": self.trigger["event_id"],
            "updated_at": "2026-11-13T12:00:00Z",
        }
        self._event("delivery.updated", {"delivery": body}, execution=execution)

    def started(self) -> None:
        self._event("execution.started", self._execution("running", None, None))

    def run(self, status: str = "running", *, run_id: str = "11111111-1111-4111-8111-111111111111", error: str | None = None) -> None:
        payload = {"run_id": run_id, "status": status, "agent_id": AGENT_ID, "trigger_event_id": self.trigger["event_id"]}
        self._event("run.updated", {**payload, **({"error": error} if error else {})})

    def text(self, text: str, **extra: Any) -> None:
        self._event("message.created", {"content": [{"type": "text", "text": text}], "mentions": [], **extra})

    def completed(self, outcome: str = "responded") -> None:
        self._event("execution.completed", self._execution("completed", outcome, None))
        self.delivery(outcome)

    def failed(self, reason: str = "handler_error") -> None:
        self._event("execution.failed", self._execution("failed", "failed", reason))
        self.delivery("failed", reason=reason)


async def dispatched(world: World, **overrides: Any) -> tuple[MaestroDispatchStream, Maestro]:
    kwargs: dict[str, Any] = {
        "handler": never_runs,
        "agent_id": AGENT_ID,
        "message": "Oi",
        "user": ANA,
        "thread_id": "atendimento-1",
        **COMMON,
    }
    kwargs.update(overrides)
    stream = await world.maestro.dispatch(**kwargs)
    return stream, Maestro(world.fake, stream)


# -- the publication --


async def test_dispatch_publishes_the_trigger_and_returns_a_stream(world):
    stream = await world.maestro.dispatch(
        never_runs,
        AGENT_ID,
        "Olá",
        user=ANA,
        thread_id=KEY,
        thread_title="Atendimento",
        queues=None,
        **COMMON,
    )
    assert isinstance(stream, MaestroDispatchStream)
    assert (stream.thread_id, stream.is_new_thread) == (KEY, True)
    thread = world.maestro_thread(KEY)
    assert thread.title == "Atendimento" and thread.external_key == KEY
    trigger = thread.events[-1]
    # The SDK event id is the Maestro client_event_id; the actor is the declared human of the user; the message triggers the agent.
    assert stream.event_id == trigger["payload"]["client_event_id"]
    assert trigger["actor"]["principal_id"] == ANA.user_id and trigger["payload"]["content"] == [{"type": "text", "text": "Olá"}]
    posts = [r for r in world.fake.requests if r.method == "POST" and r.path.endswith("/events")]
    assert posts[-1].body["trigger"] is True and posts[-1].body["actor"]["kind"] == "human"
    assert stream._delivery_id in world.fake.agent_deliveries
    again = await world.maestro.dispatch(never_runs, AGENT_ID, "De novo", user=ANA, thread_id=KEY, **COMMON)
    assert (again.thread_id, again.is_new_thread) == (KEY, False) and again.event_id != stream.event_id


async def test_dispatch_without_thread_id_uses_a_generated_one(world):
    first = await world.maestro.dispatch(never_runs, AGENT_ID, "Oi", user=ANA, **COMMON)
    second = await world.maestro.dispatch(never_runs, AGENT_ID, "Oi", user=ANA, thread_id=first.thread_id, **COMMON)
    assert world.maestro_thread(first.thread_id).external_key == first.thread_id
    assert second.thread_id == first.thread_id and second.is_new_thread is False


async def test_dispatch_whatsapp_creates_external_participant(world):
    stream = await world.maestro.dispatch(
        never_runs, AGENT_ID, "Oi do WhatsApp", user=CONTACT, thread_id=KEY, channel="whatsapp", channel_meta=META, **COMMON
    )
    thread = world.maestro_thread(KEY)
    assert (thread.channel, thread.channel_meta) == ("whatsapp", META)
    external = [p for p in thread.participants if p["kind"] == "external"]
    assert [(p["channel"], p["external_id"]) for p in external] == [("whatsapp", CONTACT.user_id)]
    trigger = thread.events[-1]
    assert trigger["actor"]["kind"] == "external" and trigger["actor"]["external_id"] == CONTACT.user_id
    assert stream.is_new_thread and stream.thread_id == KEY
    post = [r for r in world.fake.requests if r.method == "POST" and r.path.endswith("/events")][-1]
    assert post.body["trigger"] is True and post.body["actor"]["kind"] == "external"


async def test_dispatch_agent_id_is_the_instance_agent_or_empty(world):
    for agent_id in ("", AGENT_ID, AGENT_ID.upper()):
        stream = await world.maestro.dispatch(never_runs, agent_id, "Oi", user=ANA, thread_id="t1", **COMMON)
        assert stream.thread_id == "t1"
    before = len(world.fake.requests)
    with pytest.raises(MaestroAgentScopeError):
        await world.maestro.dispatch(never_runs, "outro-agente", "Oi", user=ANA, thread_id="t1", **COMMON)
    assert len(world.fake.requests) == before


async def test_dispatch_user_without_id_is_refused_before_any_write(world):
    with pytest.raises(MaestroAgentUnsupported):
        await world.maestro.dispatch(never_runs, AGENT_ID, "Oi", thread_id="t1", **COMMON)
    assert world.fake.requests == [] and world.fake.threads == {}


async def test_dispatch_ref_id_becomes_an_attachment_and_a_failure_restores_the_file(world):
    await world.maestro.dispatch(never_runs, AGENT_ID, "Primeira", user=ANA, thread_id="t-media", **COMMON)
    uploaded = await world.maestro.chat_upload(data=PNG, filename="foto.png", mime_type="image/png", agent_id=AGENT_ID)
    content = [{"type": "text", "text": "Veja"}, {"type": "image", "ref_id": uploaded["ref_id"], "mime_type": "image/png"}]
    thread = world.maestro_thread("t-media")
    # A failure of the publication gives the file back: the same ref_id works again (the ingest rule, task 6a).
    world.fake.fail_next[f"/api/v1/agent/threads/{thread.id}/events"] = "DEPENDENCY_UNAVAILABLE"
    with pytest.raises(MaestroAgentApiError):
        await world.maestro.dispatch(never_runs, AGENT_ID, "", user=ANA, thread_id="t-media", content=content, **COMMON)
    stream = await world.maestro.dispatch(never_runs, AGENT_ID, "", user=ANA, thread_id="t-media", content=content, **COMMON)
    parts = thread.events[-1]["payload"]["content"]
    assert [p["type"] for p in parts] == ["text", "attachment"]
    # The part carries the MIME type the upload returned (image/png is stored as image/webp by this fake).
    assert parts[1]["mime_type"] == "image/webp" and stream.thread_id == "t-media"
    store = await world.maestro._ensure_upload_store()
    assert store.consume(uploaded["ref_id"]) is None  # single use


async def test_dispatch_media_url_is_fetched_and_uploaded(world):
    url = "https://cdn.example.com/foto.png"
    with media_served({url: (PNG, "image/png")}):
        await world.maestro.dispatch(
            never_runs, AGENT_ID, "", user=CONTACT, thread_id=KEY, channel="whatsapp", channel_meta=META,
            content=[{"type": "image", "url": url}], **COMMON,
        )  # fmt: skip
    part = world.maestro_thread(KEY).events[-1]["payload"]["content"][0]
    assert part["type"] == "attachment" and part["mime_type"] == "image/webp"


# -- the stream follows the delivery --


def kinds(events: list[AgentEvent]) -> list[str]:
    return [event.send_type for event in events]


async def test_stream_yields_the_events_of_the_execution_until_it_completes(world, clock):
    stream, maestro = await dispatched(world)
    clock.at(0.25, maestro.started)
    clock.at(0.5, maestro.run)
    clock.at(0.75, lambda: maestro.text("Olá, Ana", author="Assistente"))
    clock.at(1.0, lambda: (maestro.run("succeeded"), maestro.completed()))
    events = await stream.collect()
    assert kinds(events) == ["run_start", "text", "run_end"]
    assert events[1].data == {"text": "Olá, Ana", "author": "Assistente"}
    assert events[2].data == {"status": "succeeded", "error": None}
    assert set(clock.sleeps) == {0.25}  # 250 ms between reads
    # It reads only what is new: the first read of each poll is the newest page.
    assert stream.thread_id == "atendimento-1"


async def test_dispatch_waits_for_slow_handler(world, clock):
    stream, maestro = await dispatched(world)
    clock.at(0.25, maestro.started)
    clock.at(0.5, maestro.run)
    clock.at(45.0, lambda: (maestro.text("Resposta lenta"), maestro.run("succeeded"), maestro.completed()))
    events = await stream.collect()
    assert kinds(events) == ["run_start", "text", "run_end"] and events[1].data["text"] == "Resposta lenta"
    # No deadline of its own: it kept reading for the 45 s of the handler, every 250 ms, without a real wait.
    assert clock.now >= 45.0 and len(clock.sleeps) >= 180 and set(clock.sleeps) == {0.25}


async def test_stream_ignores_events_of_other_deliveries_and_people(world, clock):
    stream, maestro = await dispatched(world)
    other = Maestro(world.fake, stream)
    other.delivery_id = str(uuid4())
    clock.at(0.25, maestro.started)
    clock.at(0.5, lambda: other.text("de outra entrega"))
    clock.at(
        0.75,
        lambda: world.fake.inject_event(maestro.thread_id, "message.created", {"content": [{"type": "text", "text": "x"}], "mentions": []}),
    )
    clock.at(1.0, lambda: (maestro.text("minha"), maestro.completed()))
    assert [e.data["text"] for e in await stream.collect()] == ["minha"]


async def test_stream_is_replayable(world, clock):
    stream, maestro = await dispatched(world)
    clock.at(0.25, lambda: (maestro.started(), maestro.text("uma vez"), maestro.completed()))
    first = await stream.collect()
    second = await stream.collect()
    assert [e.data["text"] for e in first] == [e.data["text"] for e in second] == ["uma vez"]


# -- the end of the stream --


async def test_stream_ends_on_a_terminal_delivery_without_execution(world, clock):
    for state in ("silent", "responded", "canceled"):
        stream, maestro = await dispatched(world, thread_id=f"t-{state}")
        clock.at(clock.now + 0.25, lambda m=maestro, s=state: m.delivery(s, execution=False))
        assert await stream.collect() == []


@pytest.mark.parametrize("state", ["failed", "expired"])
async def test_stream_raises_handler_error_when_the_delivery_ends_without_running(world, clock, state):
    stream, maestro = await dispatched(world)
    clock.at(0.25, lambda: maestro.delivery(state, execution=False, reason="delivery_expired"))
    with pytest.raises(HandlerError, match="delivery_expired"):
        await stream.collect()


async def test_stream_ends_on_a_canceled_execution(world, clock):
    stream, maestro = await dispatched(world)

    def canceled() -> None:
        maestro.started()
        maestro.text("parcial")
        maestro._event("execution.canceled", maestro._execution("canceled", "canceled", None))
        maestro.delivery("canceled")

    clock.at(0.25, canceled)
    assert [e.data["text"] for e in await stream.collect()] == ["parcial"]


async def test_stream_raises_handler_error_when_the_execution_fails(world, clock):
    stream, maestro = await dispatched(world)

    def failing() -> None:
        maestro.started()
        maestro.run()
        maestro.text("antes do erro")
        maestro.text("boom")  # what the executor reports: the error text as a message and the run ends failed
        maestro.run("failed", error="boom")
        maestro.failed("handler_error")

    clock.at(0.25, failing)
    seen: list[AgentEvent] = []
    with pytest.raises(HandlerError) as caught:
        async for event in stream:
            seen.append(event)
    assert str(caught.value) == "boom" and caught.value.original is None
    assert kinds(seen)[:2] == ["run_start", "text"] and seen[1].data["text"] == "antes do erro"


async def test_stream_failure_without_error_text_names_the_reason(world, clock):
    stream, maestro = await dispatched(world)
    clock.at(0.25, lambda: (maestro.started(), maestro.failed("handler_timeout")))
    with pytest.raises(HandlerError, match="handler_timeout"):
        await stream.collect()


async def test_stream_keeps_waiting_while_the_delivery_waits_for_input(world, clock):
    stream, maestro = await dispatched(world)
    clock.at(0.25, lambda: (maestro.started(), maestro.delivery("waiting_input")))
    clock.at(100.0, lambda: (maestro.text("depois da resposta humana"), maestro.completed()))
    assert [e.data["text"] for e in await stream.collect()] == ["depois da resposta humana"]


async def test_stream_retries_a_transient_read_failure(world, clock):
    stream, maestro = await dispatched(world)
    path = f"/api/v1/agent/threads/{maestro.thread_id}/events"
    world.fake.fail_next[path] = "DEPENDENCY_UNAVAILABLE"
    clock.at(0.5, lambda: (maestro.started(), maestro.text("ok"), maestro.completed()))
    assert [e.data["text"] for e in await stream.collect()] == ["ok"]


async def test_stream_a_deleted_thread_raises_the_api_error(world, clock):
    from dooers.agents.server.maestro.agent_api import MaestroAgentMissing

    stream, maestro = await dispatched(world)
    clock.at(0.25, lambda: world.fake.delete_thread_state(maestro.thread_id))
    with pytest.raises(MaestroAgentMissing):
        await stream.collect()


# -- events out of the process that called dispatch (agent_event_from and the C32 corrections) --


async def test_events_from_maestro_have_the_production_shape(world, clock):
    stream, maestro = await dispatched(world, thread_title="Antes")

    def outputs() -> None:
        maestro.started()
        maestro.run()
        maestro._event("reasoning.created", {"text": "pensando"})
        maestro._event("tool_call.created", {"id": "c1", "name": "busca", "args": {"q": "x"}})
        maestro._event("tool_result.created", {"id": "c1", "name": "busca", "args": {"q": "x"}, "result": {"ok": True}})
        maestro._event(
            "thread.updated", {"thread_id": maestro.thread_id, "title": "Antes", "state": "active", "version": 2, "queue": "vendas"}
        )
        maestro._event(
            "thread.updated", {"thread_id": maestro.thread_id, "title": "Novo", "state": "active", "version": 3, "queue": "vendas"}
        )
        maestro._event(
            "participant.added",
            {
                "history_policy": "full",
                "participant": {
                    "kind": "human",
                    "principal_id": BIA.user_id,
                    "display_name": "Bia Lima",
                    "participant_id": str(uuid4()),
                    "joined_sequence": "9",
                    "left_sequence": None,
                    "role": "member",
                    "state": "active",
                },
            },
        )
        maestro.text("fim")
        maestro.run("succeeded")
        maestro.completed()

    clock.at(0.25, outputs)
    events = await stream.collect()
    assert [(e.send_type, e.data) for e in events] == [
        ("run_start", {"agent_id": None}),
        ("reasoning", {"text": "pensando", "author": None}),
        ("tool_call", {"id": "c1", "name": "busca", "display_name": None, "args": {"q": "x"}}),
        ("tool_result", {"id": "c1", "name": "busca", "display_name": None, "args": {"q": "x"}, "result": {"ok": True}}),
        ("thread_update", {"queue": "vendas"}),
        ("thread_update", {"title": "Novo"}),
        (
            "participant_add",
            {"user": {"user_id": BIA.user_id, "user_name": "Bia Lima", "user_email": None, "identity_ids": []}},
        ),
        ("text", {"text": "fim", "author": None}),
        ("run_end", {"status": "succeeded", "error": None}),
    ]
    assert events[0].data == send.run_start().data and events[-2].data == send.text("fim").data


async def test_media_events_carry_a_temporary_link_as_the_url(world, clock):
    stream, maestro = await dispatched(world)
    artifact = world.fake.artifacts  # filled by the upload below
    uploaded = await world.maestro._maestro_api().upload_artifact(maestro.thread_id, PNG, "foto.png", "image/png")
    assert uploaded["artifact_id"] in artifact

    def outputs() -> None:
        maestro.started()
        maestro._event(
            "message.created",
            {
                "content": [
                    {
                        "type": "attachment",
                        "artifact_id": uploaded["artifact_id"],
                        "mime_type": "image/webp",
                        "filename": "foto.png",
                        "size_bytes": len(PNG),
                    }
                ],
                "mentions": [],
            },
        )
        maestro._event(
            "message.created",
            {
                "content": [
                    {
                        "type": "media_link",
                        "kind": "document",
                        "url": "https://cdn.example.com/a.pdf",
                        "mime_type": "application/pdf",
                        "filename": "a.pdf",
                    }
                ],
                "mentions": [],
            },
        )
        maestro.completed()

    clock.at(0.25, outputs)
    events = await stream.collect()
    assert kinds(events) == ["image", "document"]
    assert events[0].data["url"].startswith("http") and events[0].data["mime_type"] == "image/webp" and events[0].data["alt"] is None
    assert events[1].data == {"url": "https://cdn.example.com/a.pdf", "filename": "a.pdf", "mime_type": "application/pdf", "author": None}
