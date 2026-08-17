from unittest.mock import AsyncMock, MagicMock

import pytest

from dooers.agents.server import AgentConfig, AgentServer, SqlDatabase
from dooers.agents.server.persistence.postgres import PostgresPersistence


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_sql_database_reuses_persistence_pool():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value='OK')
    conn.fetchrow = AsyncMock(return_value={'value': 7})
    conn.fetch = AsyncMock(return_value=[{'value': 1}, {'value': 2}])
    pool = MagicMock()
    pool.acquire.return_value = _Acquire(conn)
    persistence = PostgresPersistence(host='h', port=5432, user='u', database='d', password='p')
    persistence._pool = pool
    db = SqlDatabase(persistence)
    assert await db.execute('SELECT 1') == 'OK'
    assert await db.fetchrow('SELECT 7') == {'value': 7}
    assert await db.fetch('SELECT value') == [{'value': 1}, {'value': 2}]
    assert pool.acquire.call_count == 3


@pytest.mark.asyncio
async def test_agent_server_database_returns_facade_for_postgres_persistence():
    server = AgentServer(AgentConfig(database_type='postgres'))
    persistence = PostgresPersistence(host='h', port=5432, user='u', database='d', password='p')
    server._ensure_initialized = AsyncMock(return_value=persistence)
    db = await server.database()
    assert isinstance(db, SqlDatabase)
    assert db._persistence is persistence


@pytest.mark.asyncio
async def test_agent_server_database_rejects_non_sql_backend():
    server = AgentServer(AgentConfig(database_type='cosmos'))
    persistence = MagicMock()
    server._ensure_initialized = AsyncMock(return_value=persistence)
    with pytest.raises(RuntimeError, match='SQL database access is unavailable'):
        await server.database()
