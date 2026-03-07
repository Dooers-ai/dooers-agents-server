import json
import logging
import ssl as ssl_module
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

from dooers.features.analytics.models import AnalyticsEventPayload
from dooers.protocol.models import (
    Run,
    Thread,
    ThreadEvent,
    User,
    deserialize_s2c_part,
)

logger = logging.getLogger(__name__)


def _build_ssl_context(ssl_config: bool | str) -> ssl_module.SSLContext | str | None:

    if isinstance(ssl_config, str):
        lower = ssl_config.lower().strip()
        if lower in ("false", "0", "no", "off", "", "disable"):
            return None
        if lower in ("true", "1", "yes", "on", "require"):
            ctx = ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_NONE
            return ctx
        return ssl_config

    if ssl_config is True:
        ctx = ssl_module.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_NONE
        return ctx

    return None


class PostgresPersistence:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        database: str,
        password: str,
        ssl: bool | str = False,
        table_prefix: str = "worker_",
    ):
        self._host = host
        self._port = port
        self._user = user
        self._database = database
        self._password = password
        self._ssl = ssl
        self._prefix = table_prefix
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        ssl_param = _build_ssl_context(self._ssl)
        ssl_display = self._ssl if isinstance(self._ssl, str) else ("require" if self._ssl else "disabled")
        logger.info(
            "[workers] connecting to postgresql at %s:%s/%s (user=%s, ssl=%s)",
            self._host,
            self._port,
            self._database,
            self._user,
            ssl_display,
        )
        try:
            self._pool = await asyncpg.create_pool(
                host=self._host,
                port=self._port,
                user=self._user,
                database=self._database,
                password=self._password,
                ssl=ssl_param,
                min_size=1,
                max_size=10,
                timeout=30,
                command_timeout=30,
            )
            logger.info("[workers] successfully connected to postgresql")
        except TimeoutError:
            logger.error(
                "[workers] connection to postgresql timed out. host=%s, port=%s, db=%s, ssl=%s. ",
                self._host,
                self._port,
                self._database,
                ssl_display,
            )
            raise
        except OSError as e:
            logger.error(
                "[workers] cannot reach postgresql at %s:%s - %s. ",
                self._host,
                self._port,
                e,
            )
            raise
        except asyncpg.InvalidPasswordError:
            logger.error(
                "[workers] authentication failed for user '%s' on database '%s'. ",
                self._user,
                self._database,
            )
            raise
        except Exception as e:
            logger.error(
                "[workers] failed to connect to postgresql at %s:%s/%s - %s: %s",
                self._host,
                self._port,
                self._database,
                type(e).__name__,
                e,
            )
            raise

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()

    async def migrate(self) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        threads_table = f"{self._prefix}threads"
        events_table = f"{self._prefix}events"
        runs_table = f"{self._prefix}runs"

        async with self._pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {threads_table} (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL DEFAULT '',
                    workspace_id TEXT NOT NULL DEFAULT '',
                    owner JSONB NOT NULL DEFAULT '{{}}',
                    users JSONB NOT NULL DEFAULT '[]',
                    title TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    last_event_at TIMESTAMPTZ NOT NULL
                )
            """)

            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {events_table} (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES {threads_table}(id),
                    run_id TEXT,
                    type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    author TEXT,
                    user_id TEXT,
                    user_name TEXT,
                    user_email TEXT,
                    content JSONB,
                    data JSONB,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)

            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {runs_table} (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES {threads_table}(id),
                    agent_id TEXT,
                    status TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    ended_at TIMESTAMPTZ,
                    error TEXT
                )
            """)

            await conn.execute(f"""
                ALTER TABLE {events_table} ADD COLUMN IF NOT EXISTS run_id TEXT
            """)
            await conn.execute(f"""
                ALTER TABLE {events_table} ADD COLUMN IF NOT EXISTS user_name TEXT
            """)
            await conn.execute(f"""
                ALTER TABLE {events_table} ADD COLUMN IF NOT EXISTS user_email TEXT
            """)
            await conn.execute(f"""
                ALTER TABLE {events_table} ADD COLUMN IF NOT EXISTS author TEXT
            """)

            await conn.execute(f"""
                ALTER TABLE {threads_table} ADD COLUMN IF NOT EXISTS organization_id TEXT
            """)
            await conn.execute(f"""
                ALTER TABLE {threads_table} ADD COLUMN IF NOT EXISTS workspace_id TEXT
            """)

            await conn.execute(f"""
                ALTER TABLE {threads_table} ADD COLUMN IF NOT EXISTS owner JSONB NOT NULL DEFAULT '{{}}'
            """)
            await conn.execute(f"""
                ALTER TABLE {threads_table} ADD COLUMN IF NOT EXISTS users JSONB NOT NULL DEFAULT '[]'
            """)

            # Migrate legacy user_id column for existing databases
            await conn.execute(f"""
                ALTER TABLE {threads_table} DROP COLUMN IF EXISTS user_id
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}threads_users
                    ON {threads_table} USING GIN (users jsonb_path_ops)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}threads_worker_id
                    ON {threads_table}(worker_id)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}threads_organization_id
                    ON {threads_table}(organization_id)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}threads_workspace_id
                    ON {threads_table}(workspace_id)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}events_thread_id
                    ON {events_table}(thread_id)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}events_user_id
                    ON {events_table}(user_id)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}threads_worker_last_event
                    ON {threads_table}(worker_id, last_event_at DESC)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}events_thread_created
                    ON {events_table}(thread_id, created_at)
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}runs_thread_id
                    ON {runs_table}(thread_id)
            """)

            settings_table = f"{self._prefix}settings"
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {settings_table} (
                    worker_id TEXT PRIMARY KEY,
                    values JSONB NOT NULL DEFAULT '{{}}',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}settings_worker
                    ON {settings_table}(worker_id)
            """)

            analytics_table = f"{self._prefix}analytics_events"
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {analytics_table} (
                    id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL,
                    worker_id TEXT NOT NULL,
                    thread_id TEXT,
                    user_id TEXT,
                    run_id TEXT,
                    event_id TEXT,
                    organization_id TEXT,
                    workspace_id TEXT,
                    data JSONB,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}analytics_events_worker_id
                    ON {analytics_table}(worker_id)
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._prefix}analytics_events_timestamp
                    ON {analytics_table}(timestamp)
            """)

    async def create_thread(self, thread: Thread) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}threads"
        owner_json = json.dumps(thread.owner.model_dump())
        users_json = json.dumps([u.model_dump() for u in thread.users])

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table}
                    (id, worker_id, organization_id, workspace_id, owner, users, title, created_at, updated_at, last_event_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                thread.id,
                thread.worker_id,
                thread.organization_id,
                thread.workspace_id,
                owner_json,
                users_json,
                thread.title,
                thread.created_at,
                thread.updated_at,
                thread.last_event_at,
            )

    async def get_thread(self, thread_id: str) -> Thread | None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}threads"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE id = $1",
                thread_id,
            )

        if not row:
            return None

        return self._row_to_thread(row)

    def _row_to_thread(self, row: asyncpg.Record) -> Thread:
        owner_data = row["owner"] if row["owner"] else {"user_id": ""}
        if isinstance(owner_data, str):
            owner_data = json.loads(owner_data)

        users_data = row["users"] if row["users"] else []
        if isinstance(users_data, str):
            users_data = json.loads(users_data)

        return Thread(
            id=row["id"],
            worker_id=row["worker_id"],
            organization_id=row["organization_id"] or "",
            workspace_id=row["workspace_id"] or "",
            owner=User(**owner_data),
            users=[User(**u) for u in users_data],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_at=row["last_event_at"],
        )

    async def update_thread(self, thread: Thread) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}threads"
        owner_json = json.dumps(thread.owner.model_dump())
        users_json = json.dumps([u.model_dump() for u in thread.users])

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {table}
                SET organization_id = $1, workspace_id = $2, owner = $3, users = $4,
                    title = $5, updated_at = $6, last_event_at = $7
                WHERE id = $8
                """,
                thread.organization_id,
                thread.workspace_id,
                owner_json,
                users_json,
                thread.title,
                thread.updated_at,
                thread.last_event_at,
                thread.id,
            )

    async def delete_thread(self, thread_id: str) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        events_table = f"{self._prefix}events"
        runs_table = f"{self._prefix}runs"
        threads_table = f"{self._prefix}threads"

        async with self._pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {events_table} WHERE thread_id = $1", thread_id)
            await conn.execute(f"DELETE FROM {runs_table} WHERE thread_id = $1", thread_id)
            await conn.execute(f"DELETE FROM {threads_table} WHERE id = $1", thread_id)

    async def count_threads(
        self,
        worker_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        scope: str = "member",
        user_email: str | None = None,
    ) -> int:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}threads"
        conditions: list[str] = ["worker_id = $1"]
        params: list[Any] = [worker_id]
        idx = 2

        if scope == "organization":
            conditions.append(f"organization_id = ${idx}")
            params.append(organization_id)
            idx += 1
        elif scope == "workspace":
            conditions.append(f"organization_id = ${idx}")
            params.append(organization_id)
            idx += 1
            conditions.append(f"workspace_id = ${idx}")
            params.append(workspace_id)
            idx += 1
        elif scope == "member":
            conditions.append(f"organization_id = ${idx}")
            params.append(organization_id)
            idx += 1
            conditions.append(f"workspace_id = ${idx}")
            params.append(workspace_id)
            idx += 1
            if user_id:
                conditions.append(f"users @> ${idx}::jsonb")
                params.append(json.dumps([{"user_id": user_id}]))
                idx += 1
            elif user_email:
                conditions.append(f"users @> ${idx}::jsonb")
                params.append(json.dumps([{"user_email": user_email}]))
                idx += 1
        # scope == "admin" — no additional filters

        where = " AND ".join(conditions)
        query = f"SELECT COUNT(*) FROM {table} WHERE {where}"

        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *params)

    async def list_threads(
        self,
        worker_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        cursor: str | None,
        limit: int,
        scope: str = "member",
        user_email: str | None = None,
    ) -> list[Thread]:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}threads"
        conditions: list[str] = ["worker_id = $1"]
        params: list[Any] = [worker_id]
        idx = 2

        if scope == "organization":
            conditions.append(f"organization_id = ${idx}")
            params.append(organization_id)
            idx += 1
        elif scope == "workspace":
            conditions.append(f"organization_id = ${idx}")
            params.append(organization_id)
            idx += 1
            conditions.append(f"workspace_id = ${idx}")
            params.append(workspace_id)
            idx += 1
        elif scope == "member":
            conditions.append(f"organization_id = ${idx}")
            params.append(organization_id)
            idx += 1
            conditions.append(f"workspace_id = ${idx}")
            params.append(workspace_id)
            idx += 1
            if user_id:
                conditions.append(f"users @> ${idx}::jsonb")
                params.append(json.dumps([{"user_id": user_id}]))
                idx += 1
            elif user_email:
                conditions.append(f"users @> ${idx}::jsonb")
                params.append(json.dumps([{"user_email": user_email}]))
                idx += 1
        # scope == "admin" — no additional filters

        if cursor:
            if "|" in cursor:
                cursor_ts, cursor_id = cursor.rsplit("|", 1)
                conditions.append(f"(last_event_at < ${idx} OR (last_event_at = ${idx} AND id < ${idx + 1}))")
                params.append(datetime.fromisoformat(cursor_ts))
                params.append(cursor_id)
                idx += 2
            else:
                conditions.append(f"last_event_at < ${idx}")
                params.append(datetime.fromisoformat(cursor))
                idx += 1

        params.append(limit)
        where = " AND ".join(conditions)

        query = f"""
            SELECT * FROM {table}
            WHERE {where}
            ORDER BY last_event_at DESC, id DESC
            LIMIT ${idx}
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_thread(row) for row in rows]

    async def create_event(self, event: ThreadEvent) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}events"
        content_json = None
        if event.content:
            content_json = json.dumps([self._serialize_content_part(p) for p in event.content])

        data_json = json.dumps(event.data) if event.data else None

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table} (id, thread_id, run_id, type, actor, author, user_id, user_name, user_email, content, data, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                event.id,
                event.thread_id,
                event.run_id,
                event.type,
                event.actor,
                event.author,
                event.user.user_id or None,
                event.user.user_name,
                event.user.user_email,
                content_json,
                data_json,
                event.created_at,
            )

    async def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: str = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        if not self._pool:
            raise RuntimeError("Not connected")

        from dooers.persistence.base import FILTERABLE_FIELDS

        table = f"{self._prefix}events"
        conditions = ["thread_id = $1"]
        params: list[Any] = [thread_id]
        idx = 2

        if after_event_id:
            async with self._pool.acquire() as conn:
                ref_row = await conn.fetchrow(f"SELECT created_at FROM {table} WHERE id = $1", after_event_id)
            if ref_row:
                op = "<" if order == "desc" else ">"
                conditions.append(f"created_at {op} ${idx}")
                params.append(ref_row["created_at"])
                idx += 1

        if before_event_id:
            async with self._pool.acquire() as conn:
                ref_row = await conn.fetchrow(f"SELECT created_at FROM {table} WHERE id = $1", before_event_id)
            if ref_row:
                conditions.append(f"created_at < ${idx}")
                params.append(ref_row["created_at"])
                idx += 1

        if filters:
            for key, value in filters.items():
                if key in FILTERABLE_FIELDS:
                    conditions.append(f"{key} = ${idx}")
                    params.append(value)
                    idx += 1

        direction = "DESC" if order == "desc" else "ASC"
        where = " AND ".join(conditions)
        params.append(limit)

        query = f"SELECT * FROM {table} WHERE {where} ORDER BY created_at {direction} LIMIT ${idx}"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_event(row) for row in rows]

    def _row_to_event(self, row: asyncpg.Record) -> ThreadEvent:
        content = None
        if row["content"]:
            content_data = json.loads(row["content"])
            content = [self._deserialize_content_part(p) for p in content_data]

        data = json.loads(row["data"]) if row["data"] else None

        return ThreadEvent(
            id=row["id"],
            thread_id=row["thread_id"],
            run_id=row["run_id"],
            type=row["type"],
            actor=row["actor"],
            author=row.get("author"),
            user=User(
                user_id=row["user_id"] or "",
                user_name=row["user_name"],
                user_email=row["user_email"],
            ),
            content=content,
            data=data,
            created_at=row["created_at"],
        )

    async def upsert_thread_participant(self, thread_id: str, user: User) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        # Determine match key: prefer user_id, fallback to user_email
        if user.user_id:
            match_field = "user_id"
            match_value = user.user_id
        elif user.user_email:
            match_field = "user_email"
            match_value = user.user_email
        else:
            return  # No identifier to match on

        table = f"{self._prefix}threads"
        user_json = json.dumps(user.model_dump())
        containment_check = json.dumps([{match_field: match_value}])

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {table}
                SET users = (
                    CASE
                        WHEN NOT users @> $2::jsonb
                        THEN users || $3::jsonb
                        ELSE (
                            SELECT jsonb_agg(
                                CASE
                                    WHEN elem->>$5 = $4 THEN $3::jsonb
                                    ELSE elem
                                END
                            )
                            FROM jsonb_array_elements(users) elem
                        )
                    END
                ),
                updated_at = NOW()
                WHERE id = $1
                """,
                thread_id,
                containment_check,
                user_json,
                match_value,
                match_field,
            )

    async def create_run(self, run: Run) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}runs"
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table} (id, thread_id, agent_id, status, started_at, ended_at, error)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                run.id,
                run.thread_id,
                run.agent_id,
                run.status,
                run.started_at,
                run.ended_at,
                run.error,
            )

    async def update_run(self, run: Run) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}runs"
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {table}
                SET agent_id = $1, status = $2, ended_at = $3, error = $4
                WHERE id = $5
                """,
                run.agent_id,
                run.status,
                run.ended_at,
                run.error,
                run.id,
            )

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}events"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE id = $1",
                event_id,
            )
        if not row:
            return None
        return self._row_to_event(row)

    async def update_event(self, event: ThreadEvent) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}events"
        content_json = None
        if event.content:
            content_json = json.dumps([self._serialize_content_part(p) for p in event.content])

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {table} SET content = $1 WHERE id = $2",
                content_json,
                event.id,
            )

    async def delete_event(self, event_id: str) -> None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}events"
        async with self._pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {table} WHERE id = $1", event_id)

    async def get_run(self, run_id: str) -> Run | None:
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}runs"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE id = $1",
                run_id,
            )
        if not row:
            return None
        return Run(
            id=row["id"],
            thread_id=row["thread_id"],
            agent_id=row["agent_id"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            error=row["error"],
        )

    async def list_runs(
        self,
        thread_id: str | None = None,
        worker_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Run]:
        if not self._pool:
            raise RuntimeError("Not connected")

        runs_table = f"{self._prefix}runs"
        threads_table = f"{self._prefix}threads"
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if thread_id:
            conditions.append(f"r.thread_id = ${idx}")
            params.append(thread_id)
            idx += 1
        if worker_id:
            conditions.append(f"t.worker_id = ${idx}")
            params.append(worker_id)
            idx += 1
        if status:
            conditions.append(f"r.status = ${idx}")
            params.append(status)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        join = f"JOIN {threads_table} t ON r.thread_id = t.id" if worker_id else ""
        query = f"""
            SELECT r.* FROM {runs_table} r
            {join}
            {where}
            ORDER BY r.started_at DESC
            LIMIT ${idx}
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [
            Run(
                id=row["id"],
                thread_id=row["thread_id"],
                agent_id=row["agent_id"],
                status=row["status"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                error=row["error"],
            )
            for row in rows
        ]

    def _serialize_content_part(self, part) -> dict:
        if hasattr(part, "model_dump"):
            return part.model_dump()
        return dict(part)

    def _deserialize_content_part(self, data: dict):
        return deserialize_s2c_part(data)

    async def get_settings(self, worker_id: str) -> dict[str, Any]:
        """Get all stored values for a worker. Returns empty dict if none."""
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}settings"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT values FROM {table} WHERE worker_id = $1",
                worker_id,
            )

        if not row:
            return {}

        values = row["values"]
        if isinstance(values, str):
            return json.loads(values)
        return values

    async def update_setting(self, worker_id: str, field_id: str, value: Any) -> datetime:
        """Update a single field value. Returns updated_at timestamp."""
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}settings"
        now = datetime.now(UTC)

        current_values = await self.get_settings(worker_id)
        current_values[field_id] = value
        values_json = json.dumps(current_values)

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table} (worker_id, values, created_at, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT(worker_id) DO UPDATE SET
                    values = EXCLUDED.values,
                    updated_at = EXCLUDED.updated_at
                """,
                worker_id,
                values_json,
                now,
                now,
            )
        return now

    async def set_settings(self, worker_id: str, values: dict[str, Any]) -> datetime:
        """Replace all settings values. Returns updated_at timestamp."""
        if not self._pool:
            raise RuntimeError("Not connected")

        table = f"{self._prefix}settings"
        now = datetime.now(UTC)
        values_json = json.dumps(values)

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {table} (worker_id, values, created_at, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT(worker_id) DO UPDATE SET
                    values = EXCLUDED.values,
                    updated_at = EXCLUDED.updated_at
                """,
                worker_id,
                values_json,
                now,
                now,
            )
        return now

    async def insert_analytics_events(self, events: list[AnalyticsEventPayload]) -> None:
        if not self._pool or not events:
            return

        table = f"{self._prefix}analytics_events"
        data_json: str | None
        async with self._pool.acquire() as conn:
            for ev in events:
                data_json = json.dumps(ev.data) if ev.data else None
                await conn.execute(
                    f"""
                    INSERT INTO {table}
                        (id, event, timestamp, worker_id, thread_id, user_id, run_id, event_id,
                         organization_id, workspace_id, data, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """,
                    str(uuid.uuid4()),
                    ev.event,
                    ev.timestamp,
                    ev.worker_id,
                    ev.thread_id,
                    ev.user_id,
                    ev.run_id,
                    ev.event_id,
                    ev.organization_id,
                    ev.workspace_id,
                    data_json,
                    ev.created_at,
                )
