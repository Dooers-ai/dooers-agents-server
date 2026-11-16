"""Connector tool execution: interactions (card, wait, re-execute) and the chat's tool events."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from dooers.tools.connectors.executor import execute

TIMEOUTS = {"approval": 120, "connect": 180}
CONNECTOR = {"id": "c-1", "name": "Agenda Recepção", "icon": "google-calendar"}


@pytest.fixture(autouse=True)
def _no_real_time(fake_clock):
    """Nothing here waits for real; tests that check deadlines move ``fake_clock`` themselves."""


def interaction(interaction_id: str, kind: str) -> dict:
    return {
        "status": "interaction_required",
        "content": None,
        "truncated": False,
        "message": "Aguardando a pessoa.",
        "interaction": {
            "id": interaction_id,
            "kind": kind,
            "title": "Criar evento",
            "summary": "Criar 'Consulta Ana' amanhã 10:00 em Recepção",
            "connector": CONNECTOR,
            "expires_at": "2026-11-16T10:02:00Z",
        },
        "connect_url": None,
    }


def ok(content: dict) -> dict:
    return {"status": "ok", "content": content, "truncated": False, "message": "Feito.", "interaction": None, "connect_url": None}


def run(name="agenda__create_event", arguments=None, *, call_id="c1", bound=None, on_behalf_of=None, timeouts=None, events=True):
    return execute(
        name,
        {"summary": "x"} if arguments is None else arguments,
        call_id=call_id,
        bound=bound or {},
        on_behalf_of=on_behalf_of,
        interaction_timeout=timeouts or TIMEOUTS,
        emit_tool_events=events,
    )


def cards(turn) -> list[dict]:
    return [e.data for e in turn.emitted if e.send_type == "connector_interaction"]


def tool_results(turn) -> list[dict]:
    return [e.data for e in turn.emitted if e.send_type == "tool_result"]


async def test_interaction_emits_card_waits_and_reexecutes(svc, turn):
    svc.execute.side_effect = [interaction("i1", "approval"), ok({"id": "e1"})]
    svc.wait.side_effect = [{"status": "pending"}, {"status": "approved"}]

    out = await run()

    assert out["status"] == "ok" and out["content"] == {"id": "e1"}
    first, again = (c.json() for c in svc.execute.calls)
    assert (first["name"], first["arguments"], first["call_id"], first["interaction_id"]) == (
        "agenda__create_event",
        {"summary": "x"},
        "c1",
        None,
    )
    assert again["interaction_id"] == "i1"
    assert [c.interaction_id for c in svc.wait.calls] == ["i1", "i1"]
    assert all(c.json()["timeout_seconds"] == 20 for c in svc.wait.calls)
    assert [e.send_type for e in turn.emitted] == ["tool_call", "connector_interaction", "tool_result"]
    # the service sends interaction.id; the chat event (and the app-web card) reads interaction_id
    assert cards(turn) == [
        {
            "interaction_id": "i1",
            "kind": "approval",
            "title": "Criar evento",
            "summary": "Criar 'Consulta Ana' amanhã 10:00 em Recepção",
            "connector": CONNECTOR,
            "expires_at": "2026-11-16T10:02:00Z",
        }
    ]
    call, _, result = (e.data for e in turn.emitted)
    assert (call["id"], call["name"], call["args"]) == ("c1", "agenda__create_event", {"summary": "x"})
    assert (result["id"], result["result"]) == ("c1", {"status": "ok", "content": {"id": "e1"}, "truncated": False})


@pytest.mark.parametrize(
    ("kind", "timeouts"),
    [("approval", TIMEOUTS), ("connect", TIMEOUTS), ("approval", {"approval": 40, "connect": 180})],
    ids=["approval_default", "connect_default", "approval_configured"],
)
async def test_interaction_timeout_returns_message(svc, turn, fake_clock, kind, timeouts):
    """The person closed the tab: every wait slice comes back pending after its full 20 s."""
    svc.execute.return_value = interaction("i1", kind)

    def held_for_the_whole_slice(call):
        fake_clock.advance(call.json()["timeout_seconds"])
        return {"status": "pending"}

    svc.wait.side_effect = held_for_the_whole_slice
    start = fake_clock.now

    out = await run(timeouts=timeouts)

    assert out == {"status": "interaction_timeout", "message": "A pessoa não respondeu a tempo."}
    limit = timeouts[kind]
    assert fake_clock.now - start <= limit + 20
    assert len(svc.wait.calls) == limit // 20
    assert len(svc.execute.calls) == 1
    assert tool_results(turn)[-1]["result"] == {"status": "interaction_timeout", "content": None, "truncated": False}


async def test_wait_answered_at_once_does_not_spin_on_the_service(svc, turn, fake_clock):
    svc.execute.return_value = interaction("i1", "approval")
    svc.wait.return_value = {"status": "pending"}  # answers immediately; the clock only moves when we pause

    out = await run(timeouts={"approval": 10, "connect": 180})

    assert out["status"] == "interaction_timeout"
    assert 1 <= len(svc.wait.calls) <= 10
    assert fake_clock.slept and sum(fake_clock.slept) >= 9


async def test_declined(svc, turn):
    svc.execute.side_effect = [interaction("i1", "approval")]
    svc.wait.side_effect = [{"status": "pending"}, {"status": "declined"}]

    out = await run()

    assert out == {"status": "declined", "message": "A pessoa recusou a ação."}
    assert len(svc.execute.calls) == 1
    assert tool_results(turn)[-1]["result"] == {"status": "declined", "content": None, "truncated": False}


@pytest.mark.parametrize(
    ("wait_status", "expected", "executions"),
    [("expired", "interaction_timeout", 1), ("connected", "ok", 2), ("consumed", "ok", 2)],
)
async def test_wait_status_decides_the_next_step(svc, turn, wait_status, expected, executions):
    svc.execute.side_effect = [interaction("i1", "connect"), ok({"id": "e1"})]
    svc.wait.side_effect = [{"status": wait_status}]

    out = await run()

    assert out["status"] == expected
    assert len(svc.execute.calls) == executions
    if executions == 2:
        assert svc.execute.calls[1].json()["interaction_id"] == "i1"
    else:
        assert out["message"] == "A pessoa não respondeu a tempo."


async def test_connect_then_approval_in_one_call(svc, turn):
    svc.execute.side_effect = [interaction("i1", "connect"), interaction("i2", "approval"), ok({"id": "e1"})]
    svc.wait.side_effect = [{"status": "connected"}, {"status": "approved"}]

    out = await run()

    assert out["status"] == "ok"
    assert [c.json()["interaction_id"] for c in svc.execute.calls] == [None, "i1", "i2"]
    assert [c.interaction_id for c in svc.wait.calls] == ["i1", "i2"]
    assert [(c["interaction_id"], c["kind"]) for c in cards(turn)] == [("i1", "connect"), ("i2", "approval")]
    assert [e.send_type for e in turn.emitted] == ["tool_call", "connector_interaction", "connector_interaction", "tool_result"]


async def test_interaction_rounds_are_capped(svc, turn):
    svc.execute.side_effect = [interaction(f"i{n}", "approval") for n in range(1, 5)]
    svc.wait.return_value = {"status": "approved"}

    out = await run()

    assert out["status"] == "provider_error" and out["message"]
    assert len(svc.execute.calls) == 4  # the call plus three re-executions
    assert [c["interaction_id"] for c in cards(turn)] == ["i1", "i2", "i3"]


async def test_repeated_interaction_id_is_provider_error_without_a_new_card(svc, turn):
    svc.execute.side_effect = [interaction("i1", "approval"), interaction("i1", "approval")]
    svc.wait.return_value = {"status": "approved"}

    out = await run()

    assert out["status"] == "provider_error" and out["message"]
    assert (len(svc.execute.calls), len(svc.wait.calls)) == (2, 1)
    assert [c["interaction_id"] for c in cards(turn)] == ["i1"]


async def test_deadline_is_checked_before_the_card(svc, turn, fake_clock):
    """A second approval after the shared deadline is gone: no card the person could no longer answer."""
    svc.execute.side_effect = [interaction("i1", "approval"), interaction("i2", "approval")]

    def approved_at_the_deadline(call):
        fake_clock.advance(TIMEOUTS["approval"])
        return {"status": "approved"}

    svc.wait.side_effect = approved_at_the_deadline

    out = await run()

    assert out == {"status": "interaction_timeout", "message": "A pessoa não respondeu a tempo."}
    assert [c["interaction_id"] for c in cards(turn)] == ["i1"]
    assert len(svc.wait.calls) == 1


async def _until(predicate) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_two_parallel_approvals_are_independent(svc, turn, fake_clock):
    """A framework runs two approval tools at once: two cards, two waits keyed by interaction id, no mixed results."""
    i2_reexecuted = asyncio.Event()

    def on_execute(call):
        body = call.json()
        n = body["call_id"].removeprefix("c")
        if body["interaction_id"] is None:
            return interaction(f"i{n}", "approval")
        if body["interaction_id"] == "i2":
            i2_reexecuted.set()
        return ok({"event": f"e{n}", "approved_by": body["interaction_id"], "arguments": body["arguments"]})

    async def on_wait(call):
        await _until(lambda: len(cards(turn)) == 2)  # both cards are in the chat before anyone answers
        if call.interaction_id == "i2":
            return {"status": "approved"}
        if [c.interaction_id for c in svc.wait.calls].count("i1") == 1:  # i1's first slice ends pending, after i2 ran
            await i2_reexecuted.wait()
            return {"status": "pending"}
        return {"status": "approved"}

    svc.execute.side_effect = on_execute
    svc.wait.side_effect = on_wait

    out1, out2 = await asyncio.gather(run(arguments={"n": 1}, call_id="c1"), run(arguments={"n": 2}, call_id="c2"))

    assert out1["content"] == {"event": "e1", "approved_by": "i1", "arguments": {"n": 1}}
    assert out2["content"] == {"event": "e2", "approved_by": "i2", "arguments": {"n": 2}}
    reexecuted = {c.json()["call_id"]: c.json()["interaction_id"] for c in svc.execute.calls if c.json()["interaction_id"]}
    assert reexecuted == {"c1": "i1", "c2": "i2"}
    assert sorted(c["interaction_id"] for c in cards(turn)) == ["i1", "i2"]
    assert sorted(c.interaction_id for c in svc.wait.calls) == ["i1", "i1", "i2"]
    assert {r["id"]: r["result"]["content"]["event"] for r in tool_results(turn)} == {"c1": "e1", "c2": "e2"}
    assert svc.session.call_count == 1  # one presence exchange for the turn, shared by both calls


async def test_bound_args_override_llm_args(svc, turn):
    svc.execute.return_value = ok({"id": "e1"})

    await run(arguments={"summary": "x", "calendar_id": "llm-choice"}, bound={"calendar_id": "rec@x"}, on_behalf_of="user-9")

    body = svc.execute.calls[0].json()
    assert body["arguments"] == {"summary": "x", "calendar_id": "rec@x"}
    assert body["on_behalf_of"] == "user-9"
    assert turn.emitted[0].data["args"] == {"summary": "x", "calendar_id": "rec@x"}


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (httpx.Response(503, text="down"), "provider_error"),
        (httpx.Response(403, text="no"), "denied"),
        (httpx.ConnectError("down"), "provider_error"),
    ],
    ids=["5xx", "403", "network"],
)
@pytest.mark.parametrize("stage", ["execute", "wait"])
async def test_transport_error_becomes_status_not_exception(svc, turn, failure, status, stage):
    if stage == "execute":
        svc.execute.side_effect = [failure]
    else:
        svc.execute.side_effect = [interaction("i1", "approval")]
        svc.wait.side_effect = [failure]

    out = await run()

    assert out["status"] == status and out["message"]
    assert set(out) == {"status", "message"}
    assert tool_results(turn)[-1]["result"] == {"status": status, "content": None, "truncated": False}


@pytest.mark.parametrize(
    ("execute_answers", "wait_answer"),
    [
        ([["not", "an", "object"]], None),
        ([{"status": "interaction_required", "interaction": {"kind": "approval"}}], None),
        ([interaction("i1", "approval")], {"status": "weird"}),
        ([{"content": {"id": "e1"}}], None),
        ([{"status": 7, "content": None}], None),
    ],
    ids=["body_not_object", "interaction_without_id", "unknown_wait_status", "body_without_status", "status_not_string"],
)
async def test_malformed_service_answers_become_provider_error(svc, turn, execute_answers, wait_answer):
    svc.execute.side_effect = execute_answers
    svc.wait.return_value = wait_answer

    out = await run()

    assert out["status"] == "provider_error" and out["message"]
    assert len(svc.execute.calls) == 1


async def test_without_active_turn_returns_error_status(svc):
    out = await run()

    assert out == {"status": "error", "message": "connectors precisa de um turno ativo do handler"}


async def test_without_tool_events_only_the_card_is_emitted(svc, turn):
    svc.execute.side_effect = [interaction("i1", "approval"), ok({"id": "e1"})]
    svc.wait.return_value = {"status": "approved"}

    out = await run(events=False)

    assert out["status"] == "ok"
    assert [e.send_type for e in turn.emitted] == ["connector_interaction"]
