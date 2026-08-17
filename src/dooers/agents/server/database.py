"""SDK-owned application SQL access over the persistence connection.

Applications use this facade instead of creating database clients. For
``database_type='dooers'`` it reuses the same SDK-managed AlloyDB/IAM
pool that stores threads, events and settings. For self-hosted Postgres
it reuses that Postgres pool. Cosmos does not expose SQL access.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from dooers.agents.server.persistence.postgres import PostgresPersistence


class SqlDatabase:
    """Application SQL facade backed by the AgentServer persistence pool."""

    def __init__(self, persistence: PostgresPersistence):
        self._persistence = persistence

    def _pool(self) -> asyncpg.Pool:
        pool = self._persistence._pool
        if pool is None:
            raise RuntimeError("AgentServer database is not connected")
        return pool

    async def execute(self, query: str, *args: Any) -> str:
        async with self._pool().acquire() as conn:
            return await conn.execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(query, *args)
        return dict(row) if row is not None else None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(row) for row in rows]

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool().acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool().acquire() as conn:
            async with conn.transaction():
                yield conn
