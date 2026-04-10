"""Tests for Persistence.delete_idle_guest_threads.

These tests exercise the real Postgres query path against a local
PostgreSQL instance (defaults to localhost:5555, the dev DB shipped with
the monorepo). Tests are skipped if that instance is not reachable so
CI environments without a database still pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from dooers.persistence.postgres import PostgresPersistence
from dooers.protocol.models import Thread, ThreadEvent, User

PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PG_PORT", "5555"))
PG_USER = os.environ.get("TEST_PG_USER", "postgres")
PG_PASSWORD = os.environ.get("TEST_PG_PASSWORD", "123456")
PG_DATABASE = os.environ.get("TEST_PG_DATABASE", "postgres")


async def _postgres_available() -> bool:
    try:
        conn = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=PG_DATABASE,
            timeout=3,
        )
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def persistence():
    if not await _postgres_available():
        pytest.skip(f"postgres unavailable at {PG_HOST}:{PG_PORT}")

    # Unique per-test prefix to fully isolate tables.
    prefix = f"test_cleanup_{uuid.uuid4().hex[:8]}_"
    p = PostgresPersistence(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        database=PG_DATABASE,
        password=PG_PASSWORD,
        ssl=False,
        table_prefix=prefix,
    )
    await p.connect()
    await p.migrate()
    try:
        yield p
    finally:
        # Drop test tables.
        assert p._pool is not None
        async with p._pool.acquire() as conn:
            for table in ("events", "runs", "threads"):
                await conn.execute(f"DROP TABLE IF EXISTS {prefix}{table} CASCADE")
        await p.disconnect()


async def _insert_thread(
    persistence: PostgresPersistence,
    *,
    owner_user_id: str,
    last_event_at: datetime,
    agent_id: str = "agent-test",
) -> str:
    thread_id = str(uuid.uuid4())
    thread = Thread(
        id=thread_id,
        agent_id=agent_id,
        organization_id="org-1",
        workspace_id="ws-1",
        owner=User(user_id=owner_user_id),
        users=[],
        title="test",
        created_at=last_event_at,
        updated_at=last_event_at,
        last_event_at=last_event_at,
    )
    await persistence.create_thread(thread)
    # Insert an event as well so the row count matches reality.
    event = ThreadEvent(
        id=str(uuid.uuid4()),
        thread_id=thread_id,
        type="message",
        actor="user",
        author=None,
        user=User(user_id=owner_user_id),
        content=None,
        data=None,
        created_at=last_event_at,
    )
    await persistence.create_event(event)
    return thread_id


@pytest.mark.asyncio
async def test_deletes_idle_guest_thread_but_keeps_authenticated(persistence):
    old = datetime.now(UTC) - timedelta(hours=48)

    guest_id = await _insert_thread(
        persistence, owner_user_id="guest:abc-123", last_event_at=old
    )
    auth_id = await _insert_thread(
        persistence, owner_user_id="user-real-42", last_event_at=old
    )

    deleted = await persistence.delete_idle_guest_threads(60)  # 1-minute TTL
    assert deleted == 1

    assert await persistence.get_thread(guest_id) is None
    remaining = await persistence.get_thread(auth_id)
    assert remaining is not None
    assert remaining.id == auth_id

    # Events for the guest thread must also be gone.
    assert persistence._pool is not None
    async with persistence._pool.acquire() as conn:
        events_table = f"{persistence._prefix}events"
        row = await conn.fetchval(
            f"SELECT COUNT(*) FROM {events_table} WHERE thread_id = $1", guest_id
        )
        assert row == 0


@pytest.mark.asyncio
async def test_does_not_delete_recent_guest_thread(persistence):
    recent = datetime.now(UTC) - timedelta(seconds=5)

    guest_id = await _insert_thread(
        persistence, owner_user_id="guest:fresh", last_event_at=recent
    )

    deleted = await persistence.delete_idle_guest_threads(3600)
    assert deleted == 0

    still_there = await persistence.get_thread(guest_id)
    assert still_there is not None
    assert still_there.id == guest_id
