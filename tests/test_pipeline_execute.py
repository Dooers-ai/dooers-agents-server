"""Characterization of HandlerPipeline.execute: what one turn persists, broadcasts and tracks.

Pins the behavior before the loop body moved to ``_apply_event``; it must keep passing as is.
Ids and clock are deterministic (``id-N``, ``tN`` = N seconds after ``_BASE``).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dooers.agents.server.exceptions import HandlerError
from dooers.agents.server.handlers import pipeline as pipeline_module
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.protocol.models import Thread, User

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    ids = itertools.count(1)
    ticks = itertools.count(0)
    monkeypatch.setattr(pipeline_module, "_generate_id", lambda: f"id-{next(ids)}")
    monkeypatch.setattr(pipeline_module, "_now", lambda: _BASE + timedelta(seconds=next(ticks)))


def _t(ts: datetime | None) -> str | None:
    return None if ts is None else f"t{int((ts - _BASE).total_seconds())}"


def _writes(harness) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for op, obj in harness.persistence.writes:
        if op in ("create_event", "update_event"):
            out.append((op, obj.id, obj.type, obj.actor, obj.run_id))
        elif op in ("create_run", "update_run"):
            out.append((op, obj.id, obj.status, obj.error))
        elif op == "upsert_thread_participant":
            out.append((op, *obj))
        else:
            out.append((op, obj.id, obj.title, _t(obj.last_event_at), _t(obj.updated_at)))
    return out


def _broadcasts(harness) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for b in harness.broadcasts:
        if b["type"] == "event.append":
            out.append(("event.append", [(e.id, e.type, e.run_id) for e in b["events"]]))
        elif b["type"] == "run.upsert":
            out.append(("run.upsert", b["run"].id, b["run"].status))
        else:
            out.append((b["type"], b["thread"].title))
    return out


def _tracked(harness) -> list[tuple[Any, ...]]:
    return [(t["event"], t["run_id"], t["event_id"]) for t in harness.collector.tracked]


async def test_turn_with_mixed_events(pipeline_harness):
    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start(agent_id="agent-1")
        yield send.reasoning("thinking")
        yield send.tool_call(name="search", args={"q": "x"}, id="call-1")
        yield send.tool_result(name="search", result={"hits": 1}, args={"q": "x"}, id="call-1")
        yield send.text("answer")
        yield send.update_thread(title="Renamed")
        yield AgentEvent(send_type="not_a_real_type", data={})
        yield send.run_end()
        yield send.text("after the run")

    yielded = await pipeline_harness.run(handler)

    assert [e.send_type for e in yielded] == [
        "run_start", "reasoning", "tool_call", "tool_result", "text",
        "thread_update", "not_a_real_type", "run_end", "text",
    ]  # fmt: skip
    assert _writes(pipeline_harness) == [
        # setup: thread id-1 at t0, user event id-2
        ("create_thread", "id-1", None, "t0", "t0"),
        ("create_event", "id-2", "message", "user", None),
        # run_start (t1): run id-3, user event joins the run, then last-event
        ("create_run", "id-3", "running", None),
        ("update_event", "id-2", "message", "user", "id-3"),
        ("update_thread", "id-1", None, "t1", "t1"),
        ("create_event", "id-4", "reasoning", "assistant", "id-3"),
        ("update_thread", "id-1", None, "t2", "t2"),
        ("create_event", "id-5", "tool.call", "assistant", "id-3"),
        ("update_thread", "id-1", None, "t3", "t3"),
        ("create_event", "id-6", "tool.result", "tool", "id-3"),
        ("update_thread", "id-1", None, "t4", "t4"),
        ("create_event", "id-7", "message", "assistant", "id-3"),
        ("update_thread", "id-1", None, "t5", "t5"),
        # thread_update (t6): title + updated_at only, no last-event update
        ("update_thread", "id-1", "Renamed", "t5", "t6"),
        # unknown send_type (t7): nothing persisted, last-event still moves
        ("update_thread", "id-1", "Renamed", "t7", "t7"),
        ("update_run", "id-3", "succeeded", None),
        ("update_thread", "id-1", "Renamed", "t8", "t8"),
        # after run_end the run id is cleared
        ("create_event", "id-8", "message", "assistant", None),
        ("update_thread", "id-1", "Renamed", "t9", "t9"),
    ]
    assert _broadcasts(pipeline_harness) == [
        ("thread.upsert", None),
        ("event.append", [("id-2", "message", None)]),
        ("run.upsert", "id-3", "running"),
        ("event.append", [("id-2", "message", "id-3")]),
        ("event.append", [("id-4", "reasoning", "id-3")]),
        ("event.append", [("id-5", "tool.call", "id-3")]),
        ("event.append", [("id-6", "tool.result", "id-3")]),
        ("event.append", [("id-7", "message", "id-3")]),
        ("thread.upsert", "Renamed"),
        ("run.upsert", "id-3", "succeeded"),
        ("event.append", [("id-8", "message", None)]),
    ]
    assert _tracked(pipeline_harness) == [
        ("thread.created", None, None),
        ("message.c2s", "id-3", "id-2"),
        ("tool.called", "id-3", "id-5"),
        ("message.s2c", "id-3", "id-7"),
        ("message.s2c", None, "id-8"),
    ]
    tool_call = next(e for e in pipeline_harness.persisted if e.type == "tool.call")
    assert tool_call.data == {"id": "call-1", "name": "search", "display_name": None, "args": {"q": "x"}}


async def test_handler_error_fails_the_open_run(pipeline_harness):
    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        yield send.tool_call(name="search", args={}, id="call-1")
        raise RuntimeError("boom")

    with pytest.raises(HandlerError, match="boom"):
        await pipeline_harness.run(handler)

    assert _writes(pipeline_harness) == [
        ("create_thread", "id-1", None, "t0", "t0"),
        ("create_event", "id-2", "message", "user", None),
        ("create_run", "id-3", "running", None),
        ("update_event", "id-2", "message", "user", "id-3"),
        ("update_thread", "id-1", None, "t1", "t1"),
        ("create_event", "id-4", "tool.call", "assistant", "id-3"),
        ("update_thread", "id-1", None, "t2", "t2"),
        # except path (error_now = t3): run failed, system error event in the run
        ("update_run", "id-3", "failed", "boom"),
        ("create_event", "id-5", "message", "system", "id-3"),
        ("update_thread", "id-1", None, "t3", "t3"),
    ]
    assert _broadcasts(pipeline_harness)[-2:] == [
        ("run.upsert", "id-3", "failed"),
        ("event.append", [("id-5", "message", "id-3")]),
    ]
    assert _tracked(pipeline_harness)[-1] == ("error.occurred", "id-3", None)


async def test_participant_add_backfills_agent_then_adds_user(pipeline_harness):
    pipeline_harness.persistence.threads["th-1"] = Thread(
        id="th-1",
        agent_id="agent-1",
        organization_id="org-1",
        workspace_id="ws-1",
        owner=User(user_id="u1"),
        users=[User(user_id="u1")],
        created_at=_BASE,
        updated_at=_BASE,
        last_event_at=_BASE,
    )

    async def handler(incoming, send, memory, analytics, settings):
        yield send.add_participant("u2", user_name="Bia")

    yielded = await pipeline_harness.run(handler, thread_id="th-1")

    assert [e.send_type for e in yielded] == ["participant_add"]
    assert _writes(pipeline_harness) == [
        ("create_event", "id-1", "message", "user", None),
        # the agent is not a participant of this old thread: lazy backfill, then the new user
        ("upsert_thread_participant", "th-1", "agent-1"),
        ("upsert_thread_participant", "th-1", "u2"),
        # participant_add never moves last-event
    ]
    assert _broadcasts(pipeline_harness)[0] == ("event.append", [("id-1", "message", None)])
    (upsert,) = pipeline_harness.broadcasts[1:]
    assert upsert["type"] == "thread.upsert"
    assert [(u.user_id, u.user_name) for u in upsert["thread"].users] == [("u1", None), ("agent-1", "Assistant"), ("u2", "Bia")]
    assert _tracked(pipeline_harness) == [("message.c2s", None, "id-1")]
