"""Schedule storage with lease-based claiming (safe across replicas)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from dooers.agents.server.features.schedules.models import Recurrence, ScheduleRecord

if TYPE_CHECKING:
    from dooers.agents.server.persistence.postgres import PostgresPersistence

_UPDATABLE = {
    "title",
    "instruction",
    "recurrence",
    "timezone",
    "next_run_at",
    "status",
    "run_count",
    "last_run_at",
    "last_status",
    "last_error",
    "last_thread_id",
    "thread_id",
    "metadata",
}
_OPEN_STATUSES = ("active", "paused")


def _now() -> datetime:
    return datetime.now(UTC)


def new_schedule_id() -> str:
    return f"sch_{uuid.uuid4().hex}"


class ScheduleStore(Protocol):
    async def migrate(self) -> None: ...
    async def create(self, record: ScheduleRecord) -> ScheduleRecord: ...
    async def get(self, schedule_id: str) -> ScheduleRecord | None: ...
    async def update(self, schedule_id: str, fields: dict[str, Any]) -> ScheduleRecord | None: ...
    async def list(
        self,
        *,
        agent_id: str,
        organization_id: str,
        user_id: str,
        workspace_id: str | None = None,
        thread_id: str | None = None,
        include_closed: bool = False,
        limit: int = 100,
    ) -> list[ScheduleRecord]: ...
    async def claim_due(
        self, *, agent_id: str | None, now: datetime, limit: int, lease_seconds: int, owner: str
    ) -> list[ScheduleRecord]: ...
    async def release(self, schedule_id: str, owner: str, fields: dict[str, Any]) -> ScheduleRecord | None: ...
    async def count_active(self, *, agent_id: str, organization_id: str, user_id: str) -> int: ...


class InMemoryScheduleStore:
    def __init__(self) -> None:
        self._rows: dict[str, ScheduleRecord] = {}
        self._leases: dict[str, tuple[str, datetime]] = {}

    async def migrate(self) -> None:
        return None

    async def create(self, record: ScheduleRecord) -> ScheduleRecord:
        self._rows[record.id] = record.model_copy(deep=True)
        return record

    async def get(self, schedule_id: str) -> ScheduleRecord | None:
        row = self._rows.get(schedule_id)
        return row.model_copy(deep=True) if row else None

    async def update(self, schedule_id: str, fields: dict[str, Any]) -> ScheduleRecord | None:
        row = self._rows.get(schedule_id)
        if row is None:
            return None
        clean = {k: v for k, v in fields.items() if k in _UPDATABLE}
        updated = row.model_copy(update={**clean, "updated_at": _now()}, deep=True)
        self._rows[schedule_id] = updated
        return updated.model_copy(deep=True)

    async def list(self, *, agent_id, organization_id, user_id, workspace_id=None, thread_id=None, include_closed=False, limit=100):
        rows = [
            r
            for r in self._rows.values()
            if r.agent_id == agent_id
            and r.organization_id == organization_id
            and r.user_id == user_id
            and (workspace_id is None or r.workspace_id == workspace_id)
            and (thread_id is None or r.thread_id == thread_id)
            and (include_closed or r.status in _OPEN_STATUSES)
        ]
        rows.sort(key=lambda r: (r.next_run_at is None, r.next_run_at or r.created_at))
        return [r.model_copy(deep=True) for r in rows[:limit]]

    async def claim_due(self, *, agent_id, now, limit, lease_seconds, owner):
        claimed: list[ScheduleRecord] = []
        for row in sorted(self._rows.values(), key=lambda r: r.next_run_at or now):
            if len(claimed) >= limit:
                break
            if agent_id and row.agent_id != agent_id:
                continue
            if row.status != "active" or row.next_run_at is None or row.next_run_at > now:
                continue
            lease = self._leases.get(row.id)
            if lease and lease[1] > now:
                continue
            self._leases[row.id] = (owner, now + timedelta(seconds=lease_seconds))
            claimed.append(row.model_copy(deep=True))
        return claimed

    async def release(self, schedule_id, owner, fields):
        lease = self._leases.get(schedule_id)
        if lease and lease[0] == owner:
            self._leases.pop(schedule_id, None)
        return await self.update(schedule_id, fields)

    async def count_active(self, *, agent_id, organization_id, user_id):
        return sum(
            1
            for r in self._rows.values()
            if r.agent_id == agent_id and r.organization_id == organization_id and r.user_id == user_id and r.status in _OPEN_STATUSES
        )


class PostgresScheduleStore:
    def __init__(self, persistence: PostgresPersistence) -> None:
        self._p = persistence
        self._table = f"{persistence._prefix}schedules"
        self._idx = persistence._prefix

    def _pool(self):
        pool = self._p._pool
        if pool is None:
            raise RuntimeError("AgentServer database is not connected")
        return pool

    _COLUMNS = (
        "id, agent_id, organization_id, workspace_id, user_id, user_name, user_email, thread_id, title, instruction, "
        "kind, recurrence, timezone, next_run_at, status, run_count, last_run_at, last_status, last_error, "
        "last_thread_id, metadata, created_at, updated_at"
    )

    async def migrate(self) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    user_name TEXT,
                    user_email TEXT,
                    thread_id TEXT,
                    title TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'reminder',
                    recurrence JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    next_run_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'active',
                    run_count INTEGER NOT NULL DEFAULT 0,
                    last_run_at TIMESTAMPTZ,
                    last_status TEXT,
                    last_error TEXT,
                    last_thread_id TEXT,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    lease_owner TEXT,
                    lease_until TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._idx}schedules_due
                    ON {self._table}(next_run_at) WHERE status = 'active'
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._idx}schedules_owner
                    ON {self._table}(agent_id, organization_id, user_id, status)
            """)

    @staticmethod
    def _row(row: Any) -> ScheduleRecord:
        data = dict(row)
        for key in ("recurrence", "metadata"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = json.loads(value)
        data["recurrence"] = Recurrence(**(data.get("recurrence") or {}))
        return ScheduleRecord(**data)

    @staticmethod
    def _encode(key: str, value: Any) -> Any:
        if key == "recurrence":
            rule = value if isinstance(value, Recurrence) else Recurrence(**(value or {}))
            return json.dumps(rule.model_dump(mode="json"))
        if key == "metadata":
            return json.dumps(value or {})
        return value

    async def create(self, record: ScheduleRecord) -> ScheduleRecord:
        async with self._pool().acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (
                    id, agent_id, organization_id, workspace_id, user_id, user_name, user_email, thread_id, title,
                    instruction, kind, recurrence, timezone, next_run_at, status, run_count, metadata, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15,$16,$17::jsonb,$18,$19)
                """,
                record.id,
                record.agent_id,
                record.organization_id,
                record.workspace_id,
                record.user_id,
                record.user_name,
                record.user_email,
                record.thread_id,
                record.title,
                record.instruction,
                record.kind,
                self._encode("recurrence", record.recurrence),
                record.timezone,
                record.next_run_at,
                record.status,
                record.run_count,
                self._encode("metadata", record.metadata),
                record.created_at,
                record.updated_at,
            )
        return record

    async def get(self, schedule_id: str) -> ScheduleRecord | None:
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(f"SELECT {self._COLUMNS} FROM {self._table} WHERE id = $1", schedule_id)
        return self._row(row) if row else None

    def _set_clause(self, fields: dict[str, Any], start: int) -> tuple[list[str], list[Any]]:
        sets: list[str] = []
        args: list[Any] = []
        n = start
        for key, value in fields.items():
            if key not in _UPDATABLE:
                continue
            cast = "::jsonb" if key in ("recurrence", "metadata") else ""
            sets.append(f"{key} = ${n}{cast}")
            args.append(self._encode(key, value))
            n += 1
        sets.append("updated_at = NOW()")
        return sets, args

    async def update(self, schedule_id: str, fields: dict[str, Any]) -> ScheduleRecord | None:
        sets, args = self._set_clause(fields, 2)
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {self._table} SET {', '.join(sets)} WHERE id = $1 RETURNING {self._COLUMNS}",
                schedule_id,
                *args,
            )
        return self._row(row) if row else None

    async def list(self, *, agent_id, organization_id, user_id, workspace_id=None, thread_id=None, include_closed=False, limit=100):
        where = ["agent_id = $1", "organization_id = $2", "user_id = $3"]
        args: list[Any] = [agent_id, organization_id, user_id]
        if workspace_id is not None:
            args.append(workspace_id)
            where.append(f"workspace_id = ${len(args)}")
        if thread_id is not None:
            args.append(thread_id)
            where.append(f"thread_id = ${len(args)}")
        if not include_closed:
            where.append("status IN ('active', 'paused')")
        args.append(limit)
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT {self._COLUMNS} FROM {self._table} WHERE {' AND '.join(where)}
                    ORDER BY next_run_at ASC NULLS LAST, created_at DESC LIMIT ${len(args)}""",
                *args,
            )
        return [self._row(r) for r in rows]

    async def claim_due(self, *, agent_id, now, limit, lease_seconds, owner):
        agent_filter = "AND agent_id = $4" if agent_id else ""
        args: list[Any] = [now, limit, lease_seconds]
        if agent_id:
            args.append(agent_id)
        args.append(owner)
        owner_index = len(args)
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE {self._table} AS s SET lease_owner = ${owner_index},
                       lease_until = $1 + make_interval(secs => $3::double precision)
                WHERE s.id IN (
                    SELECT id FROM {self._table}
                    WHERE status = 'active' AND next_run_at <= $1
                      AND (lease_until IS NULL OR lease_until < $1) {agent_filter}
                    ORDER BY next_run_at ASC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING {', '.join('s.' + c.strip() for c in self._COLUMNS.split(','))}
                """,
                *args,
            )
        return [self._row(r) for r in rows]

    async def release(self, schedule_id, owner, fields):
        sets, args = self._set_clause(fields, 3)
        sets.extend(["lease_owner = NULL", "lease_until = NULL"])
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                f"""UPDATE {self._table} SET {', '.join(sets)}
                    WHERE id = $1 AND (lease_owner = $2 OR lease_owner IS NULL) RETURNING {self._COLUMNS}""",
                schedule_id,
                owner,
                *args,
            )
        return self._row(row) if row else None

    async def count_active(self, *, agent_id, organization_id, user_id):
        async with self._pool().acquire() as conn:
            value = await conn.fetchval(
                f"""SELECT COUNT(*) FROM {self._table}
                    WHERE agent_id = $1 AND organization_id = $2 AND user_id = $3 AND status IN ('active', 'paused')""",
                agent_id,
                organization_id,
                user_id,
            )
        return int(value or 0)
