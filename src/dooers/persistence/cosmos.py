import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from dooers.features.analytics.models import AnalyticsEventPayload
from dooers.protocol.models import (
    Run,
    Thread,
    ThreadEvent,
    User,
    deserialize_s2c_part,
)

logger = logging.getLogger(__name__)

try:
    from azure.cosmos import PartitionKey
    from azure.cosmos.aio import CosmosClient
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    COSMOS_AVAILABLE = True
except ImportError:
    COSMOS_AVAILABLE = False
    CosmosClient = None
    PartitionKey = None
    CosmosResourceNotFoundError = Exception


class CosmosPersistence:
    def __init__(
        self,
        *,
        endpoint: str,
        key: str,
        database: str,
        table_prefix: str = "worker_",
    ):
        if not COSMOS_AVAILABLE:
            raise ImportError("Azure Cosmos DB SDK not installed. Install with: pip install workers[cosmos]")

        if not endpoint or not key or not database:
            raise ValueError(
                "Cosmos DB requires endpoint, key, and database configuration. "
                "Set WORKER_DATABASE_HOST (endpoint), WORKER_DATABASE_KEY, and WORKER_DATABASE_NAME "
                "environment variables or pass them to WorkerConfig."
            )

        self._endpoint = endpoint
        self._key = key
        self._database_name = database
        self._prefix = table_prefix
        self._client: CosmosClient | None = None
        self._database = None
        self._containers: dict[str, Any] = {}
        # Cache thread_id -> worker_id to avoid cross-partition queries
        self._thread_worker_cache: dict[str, str] = {}

    async def connect(self) -> None:
        logger.info(
            "[workers] connecting to cosmos db at %s (database=%s)",
            self._endpoint,
            self._database_name,
        )
        self._client = CosmosClient(self._endpoint, credential=self._key)
        self._database = self._client.get_database_client(self._database_name)
        logger.info("[workers] successfully connected to cosmos db")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def migrate(self) -> None:
        """Create containers if they don't exist."""
        if not self._database:
            raise RuntimeError("Not connected")

        container_names = [
            f"{self._prefix}threads",
            f"{self._prefix}events",
            f"{self._prefix}runs",
            f"{self._prefix}settings",
            f"{self._prefix}analytics_events",
        ]

        for container_name in container_names:
            try:
                container = await self._database.create_container_if_not_exists(
                    id=container_name,
                    partition_key=PartitionKey(path="/worker_id"),
                )
                self._containers[container_name] = container
                logger.info("[workers] cosmos container ready: %s", container_name)
            except Exception as e:
                logger.error(
                    "[workers] failed to create cosmos container %s: %s",
                    container_name,
                    e,
                )
                raise

    def _get_container(self, name: str):
        container_name = f"{self._prefix}{name}"
        if container_name not in self._containers:
            if not self._database:
                raise RuntimeError("Not connected")
            self._containers[container_name] = self._database.get_container_client(container_name)
        return self._containers[container_name]

    async def _get_worker_id(self, thread_id: str) -> str | None:
        if thread_id in self._thread_worker_cache:
            return self._thread_worker_cache[thread_id]

        thread = await self.get_thread(thread_id)
        if thread:
            self._thread_worker_cache[thread_id] = thread.worker_id
            return thread.worker_id
        return None

    async def create_thread(self, thread: Thread) -> None:
        container = self._get_container("threads")
        doc = {
            "id": thread.id,
            "worker_id": thread.worker_id,
            "organization_id": thread.organization_id,
            "workspace_id": thread.workspace_id,
            "owner": thread.owner.model_dump(),
            "users": [u.model_dump() for u in thread.users],
            "title": thread.title,
            "created_at": thread.created_at.isoformat(),
            "updated_at": thread.updated_at.isoformat(),
            "last_event_at": thread.last_event_at.isoformat(),
        }
        await container.create_item(doc)
        # Cache the worker_id
        self._thread_worker_cache[thread.id] = thread.worker_id

    async def get_thread(self, thread_id: str) -> Thread | None:
        # Try cache first for point read optimization
        if thread_id in self._thread_worker_cache:
            worker_id = self._thread_worker_cache[thread_id]
            container = self._get_container("threads")
            try:
                row = await container.read_item(thread_id, partition_key=worker_id)
                return self._row_to_thread(row)
            except CosmosResourceNotFoundError:
                del self._thread_worker_cache[thread_id]

        container = self._get_container("threads")
        query = "SELECT * FROM c WHERE c.id = @id"
        params = [{"name": "@id", "value": thread_id}]

        items = [item async for item in container.query_items(query, parameters=params)]

        if not items:
            return None

        row = items[0]
        self._thread_worker_cache[thread_id] = row["worker_id"]
        return self._row_to_thread(row)

    async def update_thread(self, thread: Thread) -> None:
        container = self._get_container("threads")
        doc = {
            "id": thread.id,
            "worker_id": thread.worker_id,
            "organization_id": thread.organization_id,
            "workspace_id": thread.workspace_id,
            "owner": thread.owner.model_dump(),
            "users": [u.model_dump() for u in thread.users],
            "title": thread.title,
            "created_at": thread.created_at.isoformat(),
            "updated_at": thread.updated_at.isoformat(),
            "last_event_at": thread.last_event_at.isoformat(),
        }
        await container.upsert_item(doc)

    async def delete_thread(self, thread_id: str) -> None:
        thread = await self.get_thread(thread_id)
        if not thread:
            return

        worker_id = thread.worker_id

        events_container = self._get_container("events")
        query = "SELECT c.id FROM c WHERE c.thread_id = @thread_id AND c.worker_id = @worker_id"
        params = [
            {"name": "@thread_id", "value": thread_id},
            {"name": "@worker_id", "value": worker_id},
        ]
        async for item in events_container.query_items(query, parameters=params):
            await events_container.delete_item(item["id"], partition_key=worker_id)

        runs_container = self._get_container("runs")
        query = "SELECT c.id FROM c WHERE c.thread_id = @thread_id AND c.worker_id = @worker_id"
        async for item in runs_container.query_items(query, parameters=params):
            await runs_container.delete_item(item["id"], partition_key=worker_id)

        threads_container = self._get_container("threads")
        await threads_container.delete_item(thread_id, partition_key=worker_id)

        self._thread_worker_cache.pop(thread_id, None)

    def _build_scope_conditions(
        self,
        scope: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        user_email: str | None,
        identity_ids: list[str] | None,
        conditions: list[str],
        params: list[dict[str, str]],
    ) -> None:
        """Append scope-based WHERE conditions for thread queries."""
        if scope == "organization":
            conditions.append("c.organization_id = @organization_id")
            params.append({"name": "@organization_id", "value": organization_id})
        elif scope == "workspace":
            conditions.append("c.organization_id = @organization_id")
            params.append({"name": "@organization_id", "value": organization_id})
            conditions.append("c.workspace_id = @workspace_id")
            params.append({"name": "@workspace_id", "value": workspace_id})
        elif scope == "member":
            conditions.append("c.organization_id = @organization_id")
            params.append({"name": "@organization_id", "value": organization_id})
            conditions.append("c.workspace_id = @workspace_id")
            params.append({"name": "@workspace_id", "value": workspace_id})
            if identity_ids:
                conditions.append(
                    "EXISTS(SELECT VALUE u FROM u IN c.users "
                    "WHERE ARRAY_CONTAINS(@all_ids, u.user_id) "
                    "OR (IS_DEFINED(u.identity_ids) AND EXISTS("
                    "SELECT VALUE iid FROM iid IN u.identity_ids "
                    "WHERE ARRAY_CONTAINS(@all_ids, iid))))"
                )
                params.append({"name": "@all_ids", "value": identity_ids})
            elif user_id:
                conditions.append("EXISTS(SELECT VALUE u FROM u IN c.users WHERE u.user_id = @user_id)")
                params.append({"name": "@user_id", "value": user_id})
            elif user_email:
                conditions.append("EXISTS(SELECT VALUE u FROM u IN c.users WHERE u.user_email = @user_email)")
                params.append({"name": "@user_email", "value": user_email})
        # scope == "admin" — no additional filters

    async def count_threads(
        self,
        worker_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        scope: str = "member",
        user_email: str | None = None,
        identity_ids: list[str] | None = None,
    ) -> int:
        container = self._get_container("threads")

        conditions = ["c.worker_id = @worker_id"]
        params = [{"name": "@worker_id", "value": worker_id}]

        self._build_scope_conditions(
            scope, organization_id, workspace_id, user_id, user_email, identity_ids,
            conditions, params,
        )

        where = " AND ".join(conditions)
        query = f"SELECT VALUE COUNT(1) FROM c WHERE {where}"

        items = [
            item
            async for item in container.query_items(
                query,
                parameters=params,
                partition_key=worker_id,
            )
        ]
        return items[0] if items else 0

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
        identity_ids: list[str] | None = None,
    ) -> list[Thread]:
        container = self._get_container("threads")

        conditions = ["c.worker_id = @worker_id"]
        params = [{"name": "@worker_id", "value": worker_id}]

        self._build_scope_conditions(
            scope, organization_id, workspace_id, user_id, user_email, identity_ids,
            conditions, params,
        )

        if cursor:
            if "|" in cursor:
                cursor_ts, cursor_id = cursor.rsplit("|", 1)
                conditions.append("(c.last_event_at < @cursor_ts OR (c.last_event_at = @cursor_ts AND c.id < @cursor_id))")
                params.append({"name": "@cursor_ts", "value": cursor_ts})
                params.append({"name": "@cursor_id", "value": cursor_id})
            else:
                conditions.append("c.last_event_at < @cursor")
                params.append({"name": "@cursor", "value": cursor})

        params.append({"name": "@limit", "value": limit})
        where = " AND ".join(conditions)

        query = f"""
            SELECT * FROM c
            WHERE {where}
            ORDER BY c.last_event_at DESC
            OFFSET 0 LIMIT @limit
        """

        items = [
            item
            async for item in container.query_items(
                query,
                parameters=params,
                partition_key=worker_id,
            )
        ]

        return [self._row_to_thread(row) for row in items]

    async def create_event(self, event: ThreadEvent) -> None:
        worker_id = await self._get_worker_id(event.thread_id)
        if not worker_id:
            raise ValueError(f"Thread {event.thread_id} not found")

        container = self._get_container("events")
        content_json = None
        if event.content:
            content_json = [self._serialize_content_part(p) for p in event.content]

        doc = {
            "id": event.id,
            "worker_id": worker_id,
            "thread_id": event.thread_id,
            "run_id": event.run_id,
            "type": event.type,
            "actor": event.actor,
            "author": event.author,
            "user_id": event.user.user_id or None,
            "user_name": event.user.user_name,
            "user_email": event.user.user_email,
            "content": content_json,
            "data": event.data,
            "created_at": event.created_at.isoformat(),
        }
        await container.create_item(doc)

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
        worker_id = await self._get_worker_id(thread_id)
        if not worker_id:
            return []

        from dooers.persistence.base import FILTERABLE_FIELDS

        container = self._get_container("events")
        conditions = ["c.thread_id = @thread_id"]
        params = [{"name": "@thread_id", "value": thread_id}]

        if after_event_id:
            ref_query = "SELECT c.created_at FROM c WHERE c.id = @id"
            ref_params = [{"name": "@id", "value": after_event_id}]
            ref_items = [item async for item in container.query_items(ref_query, parameters=ref_params)]
            if ref_items:
                op = "<" if order == "desc" else ">"
                conditions.append(f"c.created_at {op} @after_time")
                params.append({"name": "@after_time", "value": ref_items[0]["created_at"]})

        if before_event_id:
            ref_query = "SELECT c.created_at FROM c WHERE c.id = @id"
            ref_params = [{"name": "@id", "value": before_event_id}]
            ref_items = [item async for item in container.query_items(ref_query, parameters=ref_params)]
            if ref_items:
                conditions.append("c.created_at < @before_time")
                params.append({"name": "@before_time", "value": ref_items[0]["created_at"]})

        if filters:
            for key, value in filters.items():
                if key in FILTERABLE_FIELDS:
                    param_name = f"@filter_{key}"
                    conditions.append(f"c.{key} = {param_name}")
                    params.append({"name": param_name, "value": value})

        direction = "DESC" if order == "desc" else "ASC"
        where = " AND ".join(conditions)

        query = f"""
            SELECT * FROM c
            WHERE {where}
            ORDER BY c.created_at {direction}
            OFFSET 0 LIMIT @limit
        """
        params.append({"name": "@limit", "value": limit})

        items = [
            item
            async for item in container.query_items(
                query,
                parameters=params,
                partition_key=worker_id,
            )
        ]

        return [self._row_to_event(row) for row in items]

    def _row_to_event(self, row: dict) -> ThreadEvent:
        content = None
        if row.get("content"):
            content = [self._deserialize_content_part(p) for p in row["content"]]

        return ThreadEvent(
            id=row["id"],
            thread_id=row["thread_id"],
            run_id=row.get("run_id"),
            type=row["type"],
            actor=row["actor"],
            author=row.get("author"),
            user=User(
                user_id=row.get("user_id") or "",
                user_name=row.get("user_name"),
                user_email=row.get("user_email"),
            ),
            content=content,
            data=row.get("data"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_thread(self, row: dict) -> Thread:
        owner_data = row.get("owner") or {"user_id": ""}
        users_data = row.get("users") or []

        return Thread(
            id=row["id"],
            worker_id=row["worker_id"],
            organization_id=row.get("organization_id") or "",
            workspace_id=row.get("workspace_id") or "",
            owner=User(**owner_data),
            users=[User(**u) for u in users_data],
            title=row.get("title"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_event_at=datetime.fromisoformat(row["last_event_at"]),
        )

    async def upsert_thread_participant(self, thread_id: str, user: User) -> None:
        """Add or update a user in the thread's users array (Cosmos DB)."""
        thread = await self.get_thread(thread_id)
        if not thread:
            return

        # Determine match key: prefer user_id, fallback to user_email
        def match(u: User) -> bool:
            if user.user_id:
                return u.user_id == user.user_id
            if user.user_email:
                return u.user_email == user.user_email
            return False

        if not user.user_id and not user.user_email:
            return  # No identifier to match on

        existing = next((u for u in thread.users if match(u)), None)
        if existing:
            thread.users = [user if match(u) else u for u in thread.users]
        else:
            thread.users.append(user)

        thread.updated_at = datetime.now(UTC)
        await self.update_thread(thread)

    async def create_run(self, run: Run) -> None:
        worker_id = await self._get_worker_id(run.thread_id)
        if not worker_id:
            raise ValueError(f"Thread {run.thread_id} not found")

        container = self._get_container("runs")
        doc = {
            "id": run.id,
            "worker_id": worker_id,
            "thread_id": run.thread_id,
            "agent_id": run.agent_id,
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "error": run.error,
        }
        await container.create_item(doc)

    async def update_run(self, run: Run) -> None:
        worker_id = await self._get_worker_id(run.thread_id)
        if not worker_id:
            raise ValueError(f"Thread {run.thread_id} not found")

        container = self._get_container("runs")
        doc = {
            "id": run.id,
            "worker_id": worker_id,
            "thread_id": run.thread_id,
            "agent_id": run.agent_id,
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "error": run.error,
        }
        await container.upsert_item(doc)

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        container = self._get_container("events")
        query = "SELECT * FROM c WHERE c.id = @id"
        params = [{"name": "@id", "value": event_id}]

        items = [item async for item in container.query_items(query, parameters=params)]
        if not items:
            return None
        return self._row_to_event(items[0])

    async def update_event(self, event: ThreadEvent) -> None:
        worker_id = await self._get_worker_id(event.thread_id)
        if not worker_id:
            raise ValueError(f"Thread {event.thread_id} not found")

        container = self._get_container("events")

        # Read existing document to preserve all fields, then update content
        try:
            doc = await container.read_item(event.id, partition_key=worker_id)
        except CosmosResourceNotFoundError:
            return

        content_json = None
        if event.content:
            content_json = [self._serialize_content_part(p) for p in event.content]
        doc["content"] = content_json

        await container.upsert_item(doc)

    async def delete_event(self, event_id: str) -> None:
        container = self._get_container("events")
        query = "SELECT c.id, c.worker_id FROM c WHERE c.id = @id"
        params = [{"name": "@id", "value": event_id}]

        items = [item async for item in container.query_items(query, parameters=params)]
        if items:
            await container.delete_item(items[0]["id"], partition_key=items[0]["worker_id"])

    def _serialize_content_part(self, part) -> dict:
        if hasattr(part, "model_dump"):
            return part.model_dump()
        return dict(part)

    def _deserialize_content_part(self, data: dict):
        return deserialize_s2c_part(data)

    async def get_settings(self, worker_id: str) -> dict[str, Any]:
        container = self._get_container("settings")

        try:
            item = await container.read_item(worker_id, partition_key=worker_id)
            return item.get("values", {})
        except CosmosResourceNotFoundError:
            return {}

    async def update_setting(self, worker_id: str, field_id: str, value: Any) -> datetime:
        container = self._get_container("settings")
        now = datetime.now(UTC)

        current_values = await self.get_settings(worker_id)
        current_values[field_id] = value

        doc = {
            "id": worker_id,
            "worker_id": worker_id,
            "values": current_values,
            "updated_at": now.isoformat(),
        }

        await container.upsert_item(doc)
        return now

    async def set_settings(self, worker_id: str, values: dict[str, Any]) -> datetime:
        container = self._get_container("settings")
        now = datetime.now(UTC)

        doc = {
            "id": worker_id,
            "worker_id": worker_id,
            "values": values,
            "updated_at": now.isoformat(),
        }

        await container.upsert_item(doc)
        return now

    async def insert_analytics_events(self, events: list[AnalyticsEventPayload]) -> None:
        if not events:
            return

        container = self._get_container("analytics_events")
        for ev in events:
            doc = {
                "id": str(uuid.uuid4()),
                "worker_id": ev.worker_id,
                "event": ev.event,
                "timestamp": ev.timestamp.isoformat(),
                "thread_id": ev.thread_id,
                "user_id": ev.user_id,
                "run_id": ev.run_id,
                "event_id": ev.event_id,
                "organization_id": ev.organization_id,
                "workspace_id": ev.workspace_id,
                "data": ev.data,
                "created_at": ev.created_at.isoformat(),
            }
            await container.create_item(doc)
