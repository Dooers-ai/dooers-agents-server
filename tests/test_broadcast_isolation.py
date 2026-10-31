"""Thread events reach subscribers and participants only — never every socket of the agent."""

from __future__ import annotations

from datetime import UTC, datetime

from dooers.agents.server.broadcast import PipelineBroadcaster
from dooers.agents.server.protocol.models import Run, Thread, ThreadEvent, User, WireS2C_TextPart
from dooers.agents.server.registry import ConnectionRegistry
from tests.fakes import FakeSocket, MemoryPersistence


def _thread(owner: User) -> Thread:
    now = datetime.now(UTC)
    return Thread(
        id="t1",
        agent_id="a1",
        organization_id="o1",
        workspace_id="w1",
        owner=owner,
        users=[owner, User(user_id="a1")],
        created_at=now,
        updated_at=now,
        last_event_at=now,
    )


async def _setup():
    persistence = MemoryPersistence()
    registry = ConnectionRegistry()
    subscriptions: dict[str, set[str]] = {}
    alice, bob, carol = FakeSocket(), FakeSocket(), FakeSocket()
    await registry.register("a1", alice, user=User(user_id="alice"), workspace_id="w1", ws_id="ws-alice")
    await registry.register("a1", bob, user=User(user_id="bob"), workspace_id="w1", ws_id="ws-bob")
    await registry.register("a1", carol, user=User(user_id="carol", workspace_role="manager"), workspace_id="w1", ws_id="ws-carol")
    subscriptions.update({"ws-alice": set(), "ws-bob": set(), "ws-carol": set()})
    thread = _thread(User(user_id="alice"))
    await persistence.create_thread(thread)
    broadcaster = PipelineBroadcaster(registry=registry, persistence=persistence, subscriptions=subscriptions)
    return broadcaster, subscriptions, thread, (alice, bob, carol)


def _event() -> ThreadEvent:
    return ThreadEvent(
        id="e1",
        thread_id="t1",
        type="message",
        actor="assistant",
        content=[WireS2C_TextPart(text="segredo da Alice")],
        created_at=datetime.now(UTC),
    )


async def test_event_append_goes_to_participant_only():
    broadcaster, _, _, (alice, bob, carol) = await _setup()
    await broadcaster("a1", {"type": "event.append", "thread_id": "t1", "events": [_event()]})
    assert len(alice.frames("event.append")) == 1
    assert bob.frames() == []
    # A workspace manager may supervise, but only after subscribing (with a reason) — not by default.
    assert carol.frames() == []


async def test_subscriber_receives_deltas_and_runs():
    broadcaster, subscriptions, _, (alice, bob, carol) = await _setup()
    subscriptions["ws-carol"].add("t1")
    await broadcaster(
        "a1",
        {"type": "event.delta", "thread_id": "t1", "event_id": "e1", "delta": "abc", "kind": "text", "seq": 1},
    )
    now = datetime.now(UTC)
    await broadcaster("a1", {"type": "run.upsert", "run": Run(id="r1", thread_id="t1", status="running", started_at=now)})
    assert [f["type"] for f in carol.frames()] == ["event.delta", "run.upsert"]
    assert [f["type"] for f in alice.frames()] == ["event.delta", "run.upsert"]
    assert bob.frames() == []
    assert carol.frames("event.delta")[0]["payload"]["delta"] == "abc"


async def test_unknown_thread_is_not_broadcast():
    broadcaster, _, _, sockets = await _setup()
    await broadcaster("a1", {"type": "event.append", "thread_id": "missing", "events": [_event()]})
    assert all(s.frames() == [] for s in sockets)
