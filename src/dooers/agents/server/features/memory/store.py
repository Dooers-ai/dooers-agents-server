"""Long-term memory storage: PostgreSQL (production) and in-process (tests / local dev)."""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from dooers.agents.server.features.memory.models import (
    MemoryOwner,
    MemoryPreferences,
    MemoryRecord,
    dedupe_key,
)

if TYPE_CHECKING:
    from dooers.agents.server.persistence.postgres import PostgresPersistence

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[\w-]{2,}", re.UNICODE)
_UPDATABLE = {"content", "kind", "tags", "pinned", "expires_at", "scope"}


def _now() -> datetime:
    return datetime.now(UTC)


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex}"


def _terms(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


def rank_score(record: MemoryRecord, lexical: float, *, now: datetime | None = None) -> float:
    """Blend lexical relevance with pinning, usage and recency (all bounded, lexical dominates)."""
    now = now or _now()
    age_days = max(0.0, (now - record.updated_at).total_seconds() / 86400.0)
    recency = 1.0 / (1.0 + age_days / 30.0)
    usage = math.log1p(max(0, record.use_count)) / 5.0
    return lexical * 3.0 + (2.0 if record.pinned else 0.0) + recency * 0.5 + min(usage, 0.5)


class MemoryStore(Protocol):
    async def migrate(self) -> None: ...
    async def create(self, record: MemoryRecord) -> MemoryRecord: ...
    async def get(self, memory_id: str) -> MemoryRecord | None: ...
    async def update(self, memory_id: str, fields: dict[str, Any]) -> MemoryRecord | None: ...
    async def delete(self, memory_id: str) -> bool: ...
    async def find_duplicate(self, owner: MemoryOwner, scope: str, content: str) -> MemoryRecord | None: ...
    async def list(
        self,
        owner: MemoryOwner,
        *,
        scopes: tuple[str, ...] = ("user", "workspace"),
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MemoryRecord], bool]: ...
    async def search(
        self,
        owner: MemoryOwner,
        query: str,
        *,
        scopes: tuple[str, ...] = ("user", "workspace"),
        limit: int = 8,
    ) -> list[MemoryRecord]: ...
    async def touch(self, memory_ids: list[str]) -> None: ...
    async def get_preferences(self, agent_id: str, organization_id: str, user_id: str) -> MemoryPreferences: ...
    async def set_preferences(self, prefs: MemoryPreferences) -> MemoryPreferences: ...


def _visible(record: MemoryRecord, owner: MemoryOwner, scopes: tuple[str, ...], now: datetime) -> bool:
    if record.scope not in scopes:
        return False
    if record.expires_at is not None and record.expires_at <= now:
        return False
    if record.scope == "workspace" and not owner.workspace_id:
        return False
    key = owner.scope_key(record.scope)
    return (record.agent_id, record.organization_id, record.workspace_id, record.user_id) == key


class InMemoryMemoryStore:
    """Process-local store. Loses data on restart — use for tests and offline development only."""

    def __init__(self) -> None:
        self._rows: dict[str, MemoryRecord] = {}
        self._deleted: set[str] = set()
        self._prefs: dict[tuple[str, str, str], MemoryPreferences] = {}

    async def migrate(self) -> None:
        return None

    async def create(self, record: MemoryRecord) -> MemoryRecord:
        self._rows[record.id] = record.model_copy()
        return record

    async def get(self, memory_id: str) -> MemoryRecord | None:
        if memory_id in self._deleted:
            return None
        row = self._rows.get(memory_id)
        return row.model_copy() if row else None

    async def update(self, memory_id: str, fields: dict[str, Any]) -> MemoryRecord | None:
        row = await self.get(memory_id)
        if row is None:
            return None
        clean = {k: v for k, v in fields.items() if k in _UPDATABLE}
        updated = row.model_copy(update={**clean, "updated_at": _now()})
        self._rows[memory_id] = updated
        return updated

    async def delete(self, memory_id: str) -> bool:
        if memory_id not in self._rows or memory_id in self._deleted:
            return False
        self._deleted.add(memory_id)
        return True

    def _live(self) -> list[MemoryRecord]:
        return [r for k, r in self._rows.items() if k not in self._deleted]

    async def find_duplicate(self, owner: MemoryOwner, scope: str, content: str) -> MemoryRecord | None:
        key = dedupe_key(content)
        now = _now()
        for row in self._live():
            if _visible(row, owner, (scope,), now) and dedupe_key(row.content) == key:
                return row.model_copy()
        return None

    async def list(self, owner, *, scopes=("user", "workspace"), query=None, limit=50, offset=0):
        now = _now()
        rows = [r for r in self._live() if _visible(r, owner, scopes, now)]
        if query and query.strip():
            needle = query.strip().lower()
            rows = [r for r in rows if needle in r.content.lower() or any(needle in t for t in r.tags)]
        rows.sort(key=lambda r: (not r.pinned, -r.updated_at.timestamp()))
        page = rows[offset : offset + limit]
        return [r.model_copy() for r in page], len(rows) > offset + limit

    async def search(self, owner, query, *, scopes=("user", "workspace"), limit=8):
        now = _now()
        q_terms = set(_terms(query))
        scored: list[MemoryRecord] = []
        for row in self._live():
            if not _visible(row, owner, scopes, now):
                continue
            words = set(_terms(row.content)) | set(_terms(" ".join(row.tags)))
            overlap = len(q_terms & words) / max(1, len(q_terms)) if q_terms else 0.0
            if q_terms and overlap == 0 and not row.pinned:
                continue
            scored.append(row.model_copy(update={"score": rank_score(row, overlap, now=now)}))
        scored.sort(key=lambda r: r.score or 0.0, reverse=True)
        return scored[:limit]

    async def touch(self, memory_ids: list[str]) -> None:
        now = _now()
        for mid in memory_ids:
            row = self._rows.get(mid)
            if row is not None:
                self._rows[mid] = row.model_copy(update={"use_count": row.use_count + 1, "last_used_at": now})

    async def get_preferences(self, agent_id: str, organization_id: str, user_id: str) -> MemoryPreferences:
        found = self._prefs.get((agent_id, organization_id, user_id))
        return found.model_copy() if found else MemoryPreferences(agent_id=agent_id, organization_id=organization_id, user_id=user_id)

    async def set_preferences(self, prefs: MemoryPreferences) -> MemoryPreferences:
        stored = prefs.model_copy(update={"updated_at": _now()})
        self._prefs[(prefs.agent_id, prefs.organization_id, prefs.user_id)] = stored
        return stored


class PostgresMemoryStore:
    """Rows in ``{prefix}memories`` / ``{prefix}memory_prefs`` on the SDK persistence pool."""

    def __init__(self, persistence: PostgresPersistence) -> None:
        self._p = persistence
        prefix = persistence._prefix
        self._table = f"{prefix}memories"
        self._prefs_table = f"{prefix}memory_prefs"
        self._idx = prefix

    def _pool(self):
        pool = self._p._pool
        if pool is None:
            raise RuntimeError("AgentServer database is not connected")
        return pool

    async def migrate(self) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'user',
                    kind TEXT NOT NULL DEFAULT 'fact',
                    content TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    tags TEXT[] NOT NULL DEFAULT '{{}}',
                    pinned BOOLEAN NOT NULL DEFAULT FALSE,
                    source_thread_id TEXT,
                    source_event_id TEXT,
                    created_by TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_used_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ,
                    deleted_at TIMESTAMPTZ
                )
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._idx}memories_owner
                    ON {self._table}(agent_id, organization_id, workspace_id, user_id, scope)
                    WHERE deleted_at IS NULL
            """)
            # Expression indexes need IMMUTABLE functions: to_tsvector with an explicit config is,
            # array_to_string is not — tags are matched with ILIKE instead.
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._idx}memories_fts
                    ON {self._table} USING GIN (to_tsvector('simple', content))
            """)
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._prefs_table} (
                    agent_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    custom_instructions TEXT NOT NULL DEFAULT '',
                    about_me TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (agent_id, organization_id, user_id)
                )
            """)

    @staticmethod
    def _row(row: Any) -> MemoryRecord:
        data = dict(row)
        data.pop("dedupe_key", None)
        data.pop("deleted_at", None)
        score = data.pop("rank", None)
        data["tags"] = list(data.get("tags") or [])
        record = MemoryRecord(**data)
        if score is not None:
            record.score = float(score)
        return record

    _COLUMNS = (
        "id, agent_id, organization_id, workspace_id, user_id, scope, kind, content, tags, pinned, "
        "source_thread_id, source_event_id, created_by, use_count, created_at, updated_at, last_used_at, expires_at"
    )

    def _owner_clause(self, owner: MemoryOwner, scopes: tuple[str, ...], start: int) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        n = start
        for scope in scopes:
            if scope == "workspace" and not owner.workspace_id:
                continue
            agent, org, ws, user = owner.scope_key(scope)
            parts.append(
                f"(scope = ${n} AND agent_id = ${n + 1} AND organization_id = ${n + 2} "
                f"AND workspace_id = ${n + 3} AND user_id = ${n + 4})"
            )
            args.extend([scope, agent, org, ws, user])
            n += 5
        if not parts:
            return "FALSE", []
        clause = "(" + " OR ".join(parts) + ") AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())"
        return clause, args

    async def create(self, record: MemoryRecord) -> MemoryRecord:
        async with self._pool().acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (
                    id, agent_id, organization_id, workspace_id, user_id, scope, kind, content, dedupe_key,
                    tags, pinned, source_thread_id, source_event_id, created_by, use_count,
                    created_at, updated_at, last_used_at, expires_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                """,
                record.id,
                record.agent_id,
                record.organization_id,
                record.workspace_id,
                record.user_id,
                record.scope,
                record.kind,
                record.content,
                dedupe_key(record.content),
                record.tags,
                record.pinned,
                record.source_thread_id,
                record.source_event_id,
                record.created_by,
                record.use_count,
                record.created_at,
                record.updated_at,
                record.last_used_at,
                record.expires_at,
            )
        return record

    async def get(self, memory_id: str) -> MemoryRecord | None:
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLUMNS} FROM {self._table} WHERE id = $1 AND deleted_at IS NULL",
                memory_id,
            )
        return self._row(row) if row else None

    async def update(self, memory_id: str, fields: dict[str, Any]) -> MemoryRecord | None:
        clean = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if "content" in clean:
            clean["dedupe_key"] = dedupe_key(str(clean["content"]))
        sets = []
        args: list[Any] = []
        for i, (key, value) in enumerate(clean.items(), start=2):
            sets.append(f"{key} = ${i}")
            args.append(value)
        sets.append("updated_at = NOW()")
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {self._table} SET {', '.join(sets)} WHERE id = $1 AND deleted_at IS NULL RETURNING {self._COLUMNS}",
                memory_id,
                *args,
            )
        return self._row(row) if row else None

    async def delete(self, memory_id: str) -> bool:
        async with self._pool().acquire() as conn:
            status = await conn.execute(
                f"UPDATE {self._table} SET deleted_at = NOW() WHERE id = $1 AND deleted_at IS NULL",
                memory_id,
            )
        return status.endswith(" 1")

    async def find_duplicate(self, owner: MemoryOwner, scope: str, content: str) -> MemoryRecord | None:
        clause, args = self._owner_clause(owner, (scope,), 2)
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {self._COLUMNS} FROM {self._table} WHERE dedupe_key = $1 AND {clause} LIMIT 1",
                dedupe_key(content),
                *args,
            )
        return self._row(row) if row else None

    async def list(self, owner, *, scopes=("user", "workspace"), query=None, limit=50, offset=0):
        clause, args = self._owner_clause(owner, scopes, 1)
        filter_sql = ""
        if query and query.strip():
            args.append(f"%{query.strip()}%")
            filter_sql = f" AND (content ILIKE ${len(args)} OR array_to_string(tags, ' ') ILIKE ${len(args)})"
        args.extend([limit + 1, offset])
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {self._COLUMNS} FROM {self._table}
                WHERE {clause}{filter_sql}
                ORDER BY pinned DESC, updated_at DESC
                LIMIT ${len(args) - 1} OFFSET ${len(args)}
                """,
                *args,
            )
        records = [self._row(r) for r in rows]
        return records[:limit], len(records) > limit

    async def search(self, owner, query, *, scopes=("user", "workspace"), limit=8):
        clause, args = self._owner_clause(owner, scopes, 1)
        terms = [t for t in _terms(query) if len(t) >= 3][:12]
        args.append(" OR ".join(terms) if terms else "")
        q_index = len(args)
        args.append([f"%{t}%" for t in terms] or ["%%"])
        like_index = len(args)
        doc = "to_tsvector('simple', content)"
        match = (
            f"(pinned OR ({doc} @@ websearch_to_tsquery('simple', ${q_index})) OR content ILIKE ANY(${like_index})"
            f" OR array_to_string(tags, ' ') ILIKE ANY(${like_index}))"
            if terms
            else "TRUE"
        )
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {self._COLUMNS},
                       ts_rank_cd({doc}, websearch_to_tsquery('simple', ${q_index})) AS rank
                FROM {self._table}
                WHERE {clause} AND {match}
                ORDER BY pinned DESC, rank DESC, updated_at DESC
                LIMIT {max(limit * 4, 20)}
                """,
                *args,
            )
        now = _now()
        q_terms = set(terms)
        records = []
        for row in rows:
            record = self._row(row)
            words = set(_terms(record.content)) | set(_terms(" ".join(record.tags)))
            overlap = len(q_terms & words) / max(1, len(q_terms)) if q_terms else 0.0
            lexical = max(overlap, min(1.0, (record.score or 0.0) * 10))
            records.append(record.model_copy(update={"score": rank_score(record, lexical, now=now)}))
        records.sort(key=lambda r: r.score or 0.0, reverse=True)
        return records[:limit]

    async def touch(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        async with self._pool().acquire() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET use_count = use_count + 1, last_used_at = NOW() WHERE id = ANY($1)",
                memory_ids,
            )

    async def get_preferences(self, agent_id: str, organization_id: str, user_id: str) -> MemoryPreferences:
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT agent_id, organization_id, user_id, enabled, custom_instructions, about_me, updated_at
                    FROM {self._prefs_table} WHERE agent_id = $1 AND organization_id = $2 AND user_id = $3""",
                agent_id,
                organization_id,
                user_id,
            )
        if row is None:
            return MemoryPreferences(agent_id=agent_id, organization_id=organization_id, user_id=user_id)
        return MemoryPreferences(**dict(row))

    async def set_preferences(self, prefs: MemoryPreferences) -> MemoryPreferences:
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._prefs_table} (agent_id, organization_id, user_id, enabled, custom_instructions, about_me, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (agent_id, organization_id, user_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    custom_instructions = EXCLUDED.custom_instructions,
                    about_me = EXCLUDED.about_me,
                    updated_at = NOW()
                RETURNING agent_id, organization_id, user_id, enabled, custom_instructions, about_me, updated_at
                """,
                prefs.agent_id,
                prefs.organization_id,
                prefs.user_id,
                prefs.enabled,
                prefs.custom_instructions,
                prefs.about_me,
            )
        return MemoryPreferences(**dict(row))


def records_to_json(records: list[MemoryRecord]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in records], ensure_ascii=False)
