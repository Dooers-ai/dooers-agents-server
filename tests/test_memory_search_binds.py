"""Postgres memory search: every bind argument must be referenced by the query (asyncpg rejects extras)."""

import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from dooers.agents.server.features.memory.models import MemoryOwner
from dooers.agents.server.features.memory.store import PostgresMemoryStore


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        used = {int(n) for n in re.findall(r"\$(\d+)", sql)}
        assert used == set(range(1, len(args) + 1)), f"placeholders {sorted(used)} vs {len(args)} args"
        return []


def _store(conn):
    @asynccontextmanager
    async def acquire():
        yield conn

    persistence = SimpleNamespace(_prefix="agent_", _pool=SimpleNamespace(acquire=acquire))
    return PostgresMemoryStore(persistence)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["oi", "ok", "", "prefiro respostas curtas"])
@pytest.mark.parametrize("workspace", ["", "ws-1"])
async def test_search_binds_match_placeholders(query, workspace):
    conn = _Conn()
    owner = MemoryOwner(agent_id="a", organization_id="o", workspace_id=workspace, user_id="u")
    await _store(conn).search(owner, query, scopes=("user", "workspace"))
    assert conn.calls
