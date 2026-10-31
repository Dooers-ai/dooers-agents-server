"""Router: background turns, run.cancel, memory.* and schedule.* frames."""

from __future__ import annotations

import asyncio

from dooers.agents.server.features.memory import InMemoryMemoryStore
from dooers.agents.server.features.schedules import AgentSchedules, InMemoryScheduleStore
from dooers.agents.server.handlers.router import Router
from dooers.agents.server.protocol.models import User
from dooers.agents.server.protocol.parser import parse_frame
from dooers.agents.server.registry import ConnectionRegistry
from dooers.agents.server.runs import RunRegistry
from tests.fakes import FakeSocket, MemoryPersistence


async def _router(handler, *, user: User, memory_store=None, schedule_store=None):
    persistence = MemoryPersistence()
    registry = ConnectionRegistry()
    subscriptions: dict[str, set[str]] = {}
    runs = RunRegistry()
    router = Router(
        persistence=persistence,
        handler=handler,
        registry=registry,
        subscriptions=subscriptions,
        run_registry=runs,
        memory_store=memory_store,
        schedule_store=schedule_store,
    )
    ws = FakeSocket()
    router._agent_id = "a1"
    router._organization_id = "o1"
    router._workspace_id = "w1"
    router._user = user
    router._ws = ws
    await registry.register("a1", ws, user=user, workspace_id="w1", ws_id=router._ws_id)
    subscriptions[router._ws_id] = set()
    return router, ws, persistence, runs


def _create(text: str, frame_id: str) -> dict:
    return {
        "id": frame_id,
        "type": "event.create",
        "payload": {"event": {"type": "message", "actor": "user", "content": [{"type": "text", "text": text}]}},
    }


def _frame(raw: dict):
    import json

    return parse_frame(json.dumps(raw))


async def test_turn_runs_in_background_and_can_be_stopped():
    started = asyncio.Event()

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        sid = send.new_stream_id()
        yield send.text_delta("trabalhando…", stream_id=sid)
        started.set()
        await asyncio.sleep(30)
        yield send.run_end()

    user = User(user_id="u1")
    router, ws, persistence, runs = await _router(handler, user=user)
    await router.route(
        ws,
        _frame(
            {
                "id": "f1",
                "type": "event.create",
                "payload": {"event": {"type": "message", "actor": "user", "content": [{"type": "text", "text": "oi"}]}},
            }
        ),
    )
    # route() returned while the turn is still running: the socket keeps being served.
    await asyncio.wait_for(started.wait(), 2)
    thread_id = next(iter(persistence.threads))
    await router.route(ws, _frame({"id": "ping", "type": "ping", "payload": {}}))
    assert ws.frames("pong")
    await router.route(ws, _frame({"id": "c1", "type": "run.cancel", "payload": {"thread_id": thread_id}}))
    acks = {f["payload"]["ack_id"]: f["payload"] for f in ws.frames("ack")}
    assert acks["c1"]["ok"] is True
    await asyncio.wait_for(router.wait_idle(), 2)
    run = next(iter(persistence.runs.values()))
    assert run.status == "canceled"
    assert ws.frames("event.delta") == [] or all(f["payload"]["thread_id"] == thread_id for f in ws.frames("event.delta"))
    assert persistence.messages(thread_id)[0].data["interrupted"] is True
    # Nothing left to cancel.
    await router.route(ws, _frame({"id": "c2", "type": "run.cancel", "payload": {"thread_id": thread_id}}))
    acks = {f["payload"]["ack_id"]: f["payload"] for f in ws.frames("ack")}
    assert acks["c2"]["error"]["code"] == "RUN_NOT_FOUND"


async def test_turns_of_one_connection_keep_their_order():
    order: list[str] = []

    async def handler(incoming, send, memory, analytics, settings):
        order.append(f"start:{incoming.message}")
        await asyncio.sleep(0.01)
        order.append(f"end:{incoming.message}")
        yield send.text("ok")

    router, ws, _, _ = await _router(handler, user=User(user_id="u1"))
    for text in ("a", "b"):
        await router.route(
            ws,
            _frame(
                {
                    "id": text,
                    "type": "event.create",
                    "payload": {"event": {"type": "message", "actor": "user", "content": [{"type": "text", "text": text}]}},
                }
            ),
        )
    await asyncio.wait_for(router.wait_idle(), 2)
    assert order == ["start:a", "end:a", "start:b", "end:b"]


async def _noop(incoming, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


async def test_memory_frames_crud_and_workspace_permissions():
    store = InMemoryMemoryStore()
    member = User(user_id="u1", workspace_role="member")
    router, ws, _, _ = await _router(_noop, user=member, memory_store=store)

    await router.route(ws, _frame({"id": "m1", "type": "memory.create", "payload": {"content": "Gosta de chá", "pinned": True}}))
    await router.route(ws, _frame({"id": "m2", "type": "memory.create", "payload": {"content": "Regra do time", "scope": "workspace"}}))
    acks = {f["payload"]["ack_id"]: f["payload"] for f in ws.frames("ack")}
    assert acks["m1"]["ok"] is True
    assert acks["m2"]["error"]["code"] == "FORBIDDEN"
    upserts = ws.frames("memory.upsert")
    assert upserts and upserts[0]["payload"]["memory"]["content"] == "Gosta de chá"

    await router.route(ws, _frame({"id": "l1", "type": "memory.list", "payload": {}}))
    result = ws.frames("memory.list.result")[-1]["payload"]
    assert result["available"] is True and result["can_manage_workspace"] is False
    memory_id = result["memories"][0]["id"]

    await router.route(ws, _frame({"id": "u1", "type": "memory.update", "payload": {"memory_id": memory_id, "content": "Chá verde"}}))
    await router.route(ws, _frame({"id": "d1", "type": "memory.delete", "payload": {"memory_id": memory_id}}))
    assert ws.frames("memory.deleted")[0]["payload"]["memory_id"] == memory_id

    await router.route(ws, _frame({"id": "p1", "type": "memory.prefs.update", "payload": {"custom_instructions": "Seja breve"}}))
    prefs = ws.frames("memory.prefs")[-1]["payload"]
    assert prefs["prefs"]["custom_instructions"] == "Seja breve"


async def test_memory_unavailable_without_store():
    router, ws, _, _ = await _router(_noop, user=User(user_id="u1"))
    await router.route(ws, _frame({"id": "l1", "type": "memory.list", "payload": {}}))
    assert ws.frames("memory.list.result")[0]["payload"]["available"] is False


async def test_schedule_frames():
    store = InMemoryScheduleStore()
    user = User(user_id="u1")
    router, ws, _, _ = await _router(_noop, user=user, schedule_store=store)
    api = AgentSchedules(store, agent_id="a1", organization_id="o1", workspace_id="w1", user_id="u1")
    record = await api.create(title="Daily", instruction="news", recurrence={"freq": "daily", "at_time": "08:00"})

    await router.route(ws, _frame({"id": "s1", "type": "schedule.list", "payload": {}}))
    listed = ws.frames("schedule.list.result")[0]["payload"]["schedules"]
    assert [s["id"] for s in listed] == [record.id]

    await router.route(ws, _frame({"id": "s2", "type": "schedule.update", "payload": {"schedule_id": record.id, "status": "paused"}}))
    assert ws.frames("schedule.upsert")[-1]["payload"]["schedule"]["status"] == "paused"
    await router.route(ws, _frame({"id": "s3", "type": "schedule.delete", "payload": {"schedule_id": record.id}}))
    assert ws.frames("schedule.deleted")[-1]["payload"]["schedule_id"] == record.id

    # Another user's schedule is invisible.
    other = AgentSchedules(store, agent_id="a1", organization_id="o1", workspace_id="w1", user_id="u2")
    foreign = await other.create(title="X", instruction="y", recurrence={"freq": "daily"})
    await router.route(ws, _frame({"id": "s4", "type": "schedule.delete", "payload": {"schedule_id": foreign.id}}))
    acks = {f["payload"]["ack_id"]: f["payload"] for f in ws.frames("ack")}
    assert acks["s4"]["error"]["code"] == "NOT_FOUND"
