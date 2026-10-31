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
    # Adding to the workspace from the panel is an explicit choice any member may make.
    assert acks["m2"]["ok"] is True
    upserts = ws.frames("memory.upsert")
    assert upserts and upserts[0]["payload"]["memory"]["content"] == "Gosta de chá"

    await router.route(ws, _frame({"id": "l1", "type": "memory.list", "payload": {}}))
    result = ws.frames("memory.list.result")[-1]["payload"]
    assert result["available"] is True and result["can_manage_workspace"] is False
    memory_id = next(m["id"] for m in result["memories"] if m["scope"] == "user")

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


async def _member_router(store, user):
    router, ws, _, _ = await _router(_noop, user=user, memory_store=store)
    return router, ws


async def test_workspace_memory_author_and_managers_moderate():
    store = InMemoryMemoryStore()
    ana = User(user_id="ana", user_name="Ana", workspace_role="member")
    router_ana, ws_ana = await _member_router(store, ana)
    await router_ana.route(
        ws_ana, _frame({"id": "c1", "type": "memory.create", "payload": {"content": "Clientes chegam pelo WhatsApp", "scope": "workspace"}})
    )
    record = ws_ana.frames("memory.upsert")[-1]["payload"]["memory"]
    assert record["scope"] == "workspace" and record["author_id"] == "ana" and record["author_name"] == "Ana"

    bruno = User(user_id="bruno", workspace_role="member")
    router_bruno, ws_bruno = await _member_router(store, bruno)
    await router_bruno.route(ws_bruno, _frame({"id": "d1", "type": "memory.delete", "payload": {"memory_id": record["id"]}}))
    assert ws_bruno.frames("ack")[-1]["payload"]["error"]["code"] == "FORBIDDEN"

    manager = User(user_id="carla", workspace_role="manager")
    router_carla, ws_carla = await _member_router(store, manager)
    await router_carla.route(
        ws_carla, _frame({"id": "u1", "type": "memory.update", "payload": {"memory_id": record["id"], "pinned": True}})
    )
    assert ws_carla.frames("ack")[-1]["payload"]["ok"] is True

    await router_ana.route(ws_ana, _frame({"id": "d2", "type": "memory.delete", "payload": {"memory_id": record["id"]}}))
    assert ws_ana.frames("ack")[-1]["payload"]["ok"] is True


async def test_sharing_a_personal_memory_moves_it_to_the_workspace():
    store = InMemoryMemoryStore()
    ana = User(user_id="ana", user_name="Ana")
    router, ws = await _member_router(store, ana)
    await router.route(ws, _frame({"id": "c1", "type": "memory.create", "payload": {"content": "O time usa Notion"}}))
    personal = ws.frames("memory.upsert")[-1]["payload"]["memory"]
    assert personal["scope"] == "user"

    await router.route(
        ws, _frame({"id": "s1", "type": "memory.update", "payload": {"memory_id": personal["id"], "scope": "workspace"}})
    )
    assert ws.frames("ack")[-1]["payload"]["ok"] is True
    assert ws.frames("memory.deleted")[-1]["payload"]["memory_id"] == personal["id"]
    shared = ws.frames("memory.upsert")[-1]["payload"]["memory"]
    assert shared["scope"] == "workspace" and shared["content"] == "O time usa Notion" and shared["author_id"] == "ana"


async def test_no_workspace_memory_in_personal_chats():
    store = InMemoryMemoryStore()
    router, ws = await _member_router(store, User(user_id="ana"))
    router._workspace_id = ""
    await router.route(ws, _frame({"id": "c1", "type": "memory.create", "payload": {"content": "x", "scope": "workspace"}}))
    assert ws.frames("ack")[-1]["payload"]["error"]["code"] == "FORBIDDEN"


def test_context_knows_when_a_thread_is_shared():
    from dooers.agents.server.handlers.context import AgentContext

    alone = AgentContext(thread_id="t", agent_id="a", event_id="e", user=User(user_id="ana"), participants=[User(user_id="ana")])
    together = AgentContext(
        thread_id="t", agent_id="a", event_id="e", user=User(user_id="ana"), participants=[User(user_id="ana"), User(user_id="bruno")]
    )
    assert alone.is_shared is False and together.is_shared is True

