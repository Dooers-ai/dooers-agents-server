"""Integration: long-term memory and schedule stores against a real PostgreSQL.

Runs only when ``AGENT_TEST_POSTGRES_DSN`` points at a disposable database, e.g.
``postgresql://postgres@127.0.0.1:55432/sdk_test``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import pytest

from dooers.agents.server.features.memory import LongTermMemory, MemoryOwner, PostgresMemoryStore
from dooers.agents.server.features.schedules import AgentSchedules, PostgresScheduleStore
from dooers.agents.server.persistence.postgres import PostgresPersistence

DSN = os.environ.get("AGENT_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="AGENT_TEST_POSTGRES_DSN not set")


@pytest.fixture
async def persistence():
    url = urlparse(DSN)
    p = PostgresPersistence(
        host=url.hostname or "127.0.0.1",
        port=url.port or 5432,
        user=url.username or "postgres",
        database=url.path.lstrip("/"),
        password=url.password or "",
        table_prefix=f"t{uuid.uuid4().hex[:8]}_",
    )
    await p.connect()
    await p.migrate()
    try:
        yield p
    finally:
        async with p._pool.acquire() as conn:
            for table in ("memories", "memory_prefs", "schedules", "threads", "events", "runs", "settings", "analytics_events"):
                await conn.execute(f"DROP TABLE IF EXISTS {p._prefix}{table} CASCADE")
        await p.disconnect()


async def test_memory_store_sql(persistence):
    store = PostgresMemoryStore(persistence)
    await store.migrate()
    await store.migrate()  # idempotent
    alice = LongTermMemory(store, MemoryOwner("a1", "o1", "w1", "alice"))
    bob = LongTermMemory(store, MemoryOwner("a1", "o1", "w1", "bob"))
    first = await alice.remember("Prefere relatórios curtos em tópicos", kind="preference", tags=["estilo"])
    again = await alice.remember("prefere relatórios CURTOS em tópicos!", tags=["formato"])
    assert again.id == first.id and set(again.tags) == {"estilo", "formato"}
    await alice.remember("Trabalha com finanças corporativas na Acme")
    await alice.remember("O fechamento contábil é no dia 25", scope="workspace", pinned=True)

    found = await alice.recall("qual área de finanças?")
    assert found[0].content.startswith("O fechamento") or "finanças" in found[0].content  # pinned first, then relevance
    assert any("finanças" in m.content for m in found)
    bob_view, _ = await bob.list()
    assert [m.content for m in bob_view] == ["O fechamento contábil é no dia 25"]
    page, more = await alice.list(limit=2)
    assert len(page) == 2 and more is True
    filtered, _ = await alice.list(query="acme")
    assert [m.content for m in filtered] == ["Trabalha com finanças corporativas na Acme"]

    updated = await alice.update(first.id, content="Prefere relatórios em tabelas", pinned=True)
    assert updated and updated.pinned and updated.content == "Prefere relatórios em tabelas"
    assert await alice.forget(first.id) is True
    assert await alice.get(first.id) is None
    assert await bob.forget(filtered[0].id) is False

    prefs = await alice.set_preferences(custom_instructions="Responda em tópicos", about_me="CFO")
    assert prefs.custom_instructions == "Responda em tópicos"
    assert (await alice.preferences()).about_me == "CFO"
    assert (await bob.preferences()).custom_instructions == ""


async def test_schedule_store_sql_claims_with_lease(persistence):
    store = PostgresScheduleStore(persistence)
    await store.migrate()
    api = AgentSchedules(
        store, agent_id="a1", organization_id="o1", workspace_id="w1", user_id="u1", thread_id="t1", default_timezone="America/Sao_Paulo"
    )
    record = await api.create(title="Daily", instruction="news", recurrence={"freq": "daily", "at_time": "09:00"})
    assert record.next_run_at and record.timezone == "America/Sao_Paulo"
    listed = await api.list()
    assert [r.id for r in listed] == [record.id] and listed[0].recurrence.at_time == "09:00"

    await store.update(record.id, {"next_run_at": datetime.now(UTC) - timedelta(seconds=5)})
    claimed = await store.claim_due(agent_id="a1", now=datetime.now(UTC), limit=5, lease_seconds=60, owner="r1")
    assert [r.id for r in claimed] == [record.id]
    # Leased: another replica cannot claim it.
    assert await store.claim_due(agent_id=None, now=datetime.now(UTC), limit=5, lease_seconds=60, owner="r2") == []
    released = await store.release(
        record.id, "r1", {"run_count": 1, "last_status": "succeeded", "next_run_at": datetime.now(UTC) + timedelta(days=1)}
    )
    assert released and released.run_count == 1
    assert await store.count_active(agent_id="a1", organization_id="o1", user_id="u1") == 1
    paused = await api.pause(record.id)
    assert paused and paused.status == "paused"
    assert await store.claim_due(agent_id="a1", now=datetime.now(UTC) + timedelta(days=2), limit=5, lease_seconds=60, owner="r1") == []
    await api.cancel(record.id)
    assert await api.list() == [] and len(await api.list(include_closed=True)) == 1
