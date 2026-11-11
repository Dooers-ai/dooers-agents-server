"""MemoryPersistence follows PostgresPersistence row semantics (the production backend)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dooers.agents.server.protocol.models import (
    Run,
    Thread,
    ThreadEvent,
    User,
    WireS2C_ImagePart,
    WireS2C_TextPart,
)
from tests.parity.memory_persistence import MemoryPersistence

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _thread(thread_id: str = "t1", **kwargs) -> Thread:
    values = {
        "id": thread_id,
        "agent_id": "agent-1",
        "organization_id": "org-1",
        "workspace_id": "ws-1",
        "owner": User(user_id="user-1", user_name="Ana", identity_ids=["idp-1"]),
        "users": [User(user_id="user-1", user_name="Ana", identity_ids=["idp-1"])],
        "created_at": T0,
        "updated_at": T0,
        "last_event_at": T0,
    }
    values.update(kwargs)
    return Thread(**values)


def _event(event_id: str, *, seconds: int = 0, **kwargs) -> ThreadEvent:
    values = {
        "id": event_id,
        "thread_id": "t1",
        "type": "message",
        "actor": "user",
        "user": User(user_id="user-1", user_name="Ana", user_email="ana@example.com", identity_ids=["idp-1"]),
        "content": [WireS2C_TextPart(text=event_id)],
        "created_at": T0 + timedelta(seconds=seconds),
    }
    values.update(kwargs)
    return ThreadEvent(**values)


async def test_event_round_trip_keeps_only_postgres_columns():
    db = MemoryPersistence()
    await db.create_thread(_thread())
    await db.create_event(_event("e1", data={}, client_event_id="client-1", streaming=True))

    stored = await db.get_event("e1")

    assert stored is not None
    assert stored.data is None  # empty dict is stored as NULL
    assert stored.client_event_id is None
    assert stored.streaming is None
    assert stored.user == User(user_id="user-1", user_name="Ana", user_email="ana@example.com")


async def test_stored_rows_do_not_follow_later_mutation():
    db = MemoryPersistence()
    thread = _thread()
    await db.create_thread(thread)
    event = _event("e1")
    await db.create_event(event)

    thread.title = "changed"
    event.run_id = "r1"

    assert (await db.get_thread("t1")).title is None
    assert (await db.get_event("e1")).run_id is None


async def test_update_event_keeps_data_when_none_and_clears_empty_content():
    db = MemoryPersistence()
    await db.create_thread(_thread())
    await db.create_event(_event("e1", data={"k": "v"}))
    event = await db.get_event("e1")
    event.data = None
    event.content = []
    event.run_id = "r1"

    await db.update_event(event)
    stored = await db.get_event("e1")

    assert stored.data == {"k": "v"}
    assert stored.content is None
    assert stored.run_id == "r1"


async def test_get_events_orders_filters_and_pages_like_postgres():
    db = MemoryPersistence()
    await db.create_thread(_thread())
    await db.create_event(_event("e1", seconds=1))
    await db.create_event(_event("e2", seconds=2, actor="assistant", user=User(user_id="")))
    await db.create_event(_event("e3", seconds=3, content=[WireS2C_ImagePart(url="https://x.test/a.png")]))

    asc = await db.get_events("t1")
    desc = await db.get_events("t1", order="desc", limit=2)
    users = await db.get_events("t1", filters={"actor": "user"})
    before = await db.get_events("t1", before_event_id="e3", order="desc")

    assert [e.id for e in asc] == ["e1", "e2", "e3"]
    assert [e.id for e in desc] == ["e3", "e2"]
    assert [e.id for e in users] == ["e1", "e3"]
    assert [e.id for e in before] == ["e2", "e1"]
    assert asc[2].content[0].url == "https://x.test/a.png"


async def test_upsert_participant_replaces_match_or_appends():
    db = MemoryPersistence()
    await db.create_thread(_thread())

    await db.upsert_thread_participant("t1", User(user_id="user-1", user_name="Ana Souza"))
    await db.upsert_thread_participant("t1", User(user_id="", user_email="bia@example.com"))
    await db.upsert_thread_participant("t1", User(user_id=""))
    thread = await db.get_thread("t1")

    assert [(u.user_id, u.user_name, u.user_email) for u in thread.users] == [
        ("user-1", "Ana Souza", None),
        ("", None, "bia@example.com"),
    ]
    assert thread.updated_at > T0


async def test_update_thread_does_not_touch_metadata():
    db = MemoryPersistence()
    await db.create_thread(_thread(metadata={"guest_name": "Carlos"}))
    thread = await db.get_thread("t1")
    thread.metadata = {"guest_name": "Outro"}
    thread.title = "Novo"

    await db.update_thread(thread)
    stored = await db.get_thread("t1")

    assert stored.metadata == {"guest_name": "Carlos"}
    assert stored.title == "Novo"


async def test_runs_settings_secrets_and_whatsapp_lookup():
    db = MemoryPersistence()
    await db.create_thread(_thread())
    await db.create_run(Run(id="r1", thread_id="t1", agent_id=None, status="running", started_at=T0))
    await db.update_run(
        Run(id="r1", thread_id="t1", agent_id="agent-2", status="failed", started_at=T0 + timedelta(hours=1), ended_at=T0, error="boom")
    )
    await db.create_event(_event("e1", data={"delivery": {"wa_message_id": "wamid.1"}}))
    await db.set_settings("agent-1", {"tone": "formal"})
    await db.update_setting("agent-1", "lang", "pt")
    await db.merge_service_secrets("agent-1", {"a": "1"})
    await db.merge_service_secrets("agent-1", {"b": "2"})

    run = await db.get_run("r1")
    assert (run.agent_id, run.status, run.error, run.started_at) == ("agent-2", "failed", "boom", T0)
    assert [r.id for r in await db.list_runs(thread_id="t1")] == ["r1"]
    assert (await db.find_event_by_whatsapp_message_id("wamid.1")).id == "e1"
    assert await db.find_event_by_whatsapp_message_id("wamid.2") is None
    assert await db.get_settings("agent-1") == {"tone": "formal", "lang": "pt"}
    assert await db.get_service_secrets("agent-1") == {"a": "1", "b": "2"}


async def test_delete_thread_removes_events_and_runs():
    db = MemoryPersistence()
    await db.create_thread(_thread())
    await db.create_event(_event("e1"))
    await db.create_run(Run(id="r1", thread_id="t1", status="running", started_at=T0))

    await db.delete_thread("t1")

    assert await db.get_thread("t1") is None
    assert await db.get_event("e1") is None
    assert await db.get_run("r1") is None


async def test_list_threads_member_scope():
    db = MemoryPersistence()
    await db.create_thread(_thread("t1"))
    await db.create_thread(_thread("t2", users=[User(user_id="user-2")], owner=User(user_id="user-2")))

    mine = await db.list_threads("agent-1", "org-1", "ws-1", "user-1", None, 50)

    assert [t.id for t in mine] == ["t1"]


async def test_methods_outside_the_scenarios_are_not_implemented():
    db = MemoryPersistence()
    with pytest.raises(NotImplementedError):
        await db.count_threads("agent-1", "org-1", "ws-1", None)
    with pytest.raises(NotImplementedError):
        await db.delete_idle_guest_threads(60)
