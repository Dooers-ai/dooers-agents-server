from __future__ import annotations

import logging
import uuid

from dooers.broadcast import BroadcastManager
from dooers.config import WorkerConfig
from dooers.dispatch import DispatchStream
from dooers.features.analytics.collector import AnalyticsCollector
from dooers.features.analytics.worker_analytics import WorkerAnalytics
from dooers.features.settings.broadcaster import SettingsBroadcaster
from dooers.features.settings.worker_settings import WorkerSettings
from dooers.handlers.memory import WorkerMemory
from dooers.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.handlers.router import Handler, Router, WebSocketProtocol
from dooers.persistence.base import Persistence
from dooers.persistence.postgres import PostgresPersistence
from dooers.protocol.frames import (
    EventAppendPayload,
    RunUpsertPayload,
    S2C_EventAppend,
    S2C_RunUpsert,
    S2C_ThreadUpsert,
    ThreadUpsertPayload,
)
from dooers.protocol.models import User
from dooers.protocol.parser import parse_frame, serialize_frame
from dooers.registry import ConnectionRegistry
from dooers.repository import Repository
from dooers.settings import (
    ANALYTICS_BATCH_SIZE,
    ANALYTICS_FLUSH_INTERVAL,
    ANALYTICS_WEBHOOK_URL,
)
from dooers.upload_store import UploadStore

logger = logging.getLogger(__name__)


class WorkerServer:
    def __init__(self, config: WorkerConfig):
        self._config = config
        self._persistence: Persistence | None = None
        self._initialized = False

        self._registry = ConnectionRegistry()
        self._subscriptions: dict[str, set[str]] = {}  # ws_id -> set of thread_ids

        self._analytics_subscriptions: dict[str, set[str]] = {}  # worker_id -> set of ws_ids
        self._settings_subscriptions: dict[str, set[str]] = {}  # worker_id -> set of ws_ids

        self._broadcast: BroadcastManager | None = None

        self._upload_store: UploadStore | None = None
        self._analytics_collector: AnalyticsCollector | None = None
        self._settings_broadcaster: SettingsBroadcaster | None = None

    @property
    def registry(self) -> ConnectionRegistry:
        return self._registry

    @property
    def persistence(self) -> Persistence:
        if not self._persistence:
            raise RuntimeError("Server not initialized. Call handle() first or ensure_initialized().")
        return self._persistence

    @property
    def broadcast(self) -> BroadcastManager:
        if not self._broadcast:
            raise RuntimeError("Server not initialized. Call handle() first or ensure_initialized().")
        return self._broadcast

    @property
    def upload_store(self) -> UploadStore | None:
        return self._upload_store

    async def upload(self, data: bytes, filename: str, mime_type: str) -> str:
        """Stage a file for a future WebSocket event.create. Returns a reference ID."""
        await self._ensure_initialized()
        assert self._upload_store is not None
        return self._upload_store.store(data, filename, mime_type)

    async def _ensure_initialized(self) -> Persistence:
        if self._persistence and self._initialized:
            return self._persistence

        if self._config.database_type == "cosmos":
            from dooers.persistence.cosmos import CosmosPersistence

            self._persistence = CosmosPersistence(
                endpoint=self._config.database_host,
                key=self._config.database_key,
                database=self._config.database_name,
                table_prefix=self._config.database_table_prefix,
            )
        else:
            self._persistence = PostgresPersistence(
                host=self._config.database_host,
                port=self._config.database_port,
                user=self._config.database_user,
                database=self._config.database_name,
                password=self._config.database_password,
                ssl=self._config.database_ssl,
                table_prefix=self._config.database_table_prefix,
            )

        await self._persistence.connect()

        if self._config.database_auto_migrate:
            await self._persistence.migrate()

        self._broadcast = BroadcastManager(
            registry=self._registry,
            persistence=self._persistence,
            subscriptions=self._subscriptions,
        )

        if self._config.analytics_enabled:
            batch_size = self._config.analytics_batch_size or ANALYTICS_BATCH_SIZE
            flush_interval = self._config.analytics_flush_interval or ANALYTICS_FLUSH_INTERVAL

            webhook_url = self._config.analytics_webhook_url or ANALYTICS_WEBHOOK_URL
            self._analytics_collector = AnalyticsCollector(
                webhook_url=webhook_url,
                registry=self._registry,
                subscriptions=self._analytics_subscriptions,
                batch_size=batch_size,
                flush_interval=flush_interval,
                persistence=self._persistence,
            )
            await self._analytics_collector.start()

        self._settings_broadcaster = SettingsBroadcaster(
            registry=self._registry,
            subscriptions=self._settings_subscriptions,
        )

        self._upload_store = UploadStore(
            max_size=self._config.upload_max_size_bytes,
            ttl=self._config.upload_ttl_seconds,
        )
        await self._upload_store.start()

        self._initialized = True
        return self._persistence

    async def ensure_initialized(self) -> None:
        await self._ensure_initialized()

    async def migrate(self) -> None:
        persistence = await self._ensure_initialized()
        await persistence.migrate()

    async def handle(self, websocket: WebSocketProtocol, handler: Handler) -> None:
        persistence = await self._ensure_initialized()
        router = Router(
            persistence=persistence,
            handler=handler,
            registry=self._registry,
            subscriptions=self._subscriptions,
            analytics_collector=self._analytics_collector,
            settings_broadcaster=self._settings_broadcaster,
            settings_schema=self._config.settings_schema,
            assistant_name=self._config.assistant_name,
            analytics_subscriptions=self._analytics_subscriptions,
            settings_subscriptions=self._settings_subscriptions,
            upload_store=self._upload_store,
        )

        try:
            while True:
                data = await websocket.receive_text()
                frame = parse_frame(data)
                await router.route(websocket, frame)
        except Exception as e:
            error_name = type(e).__name__
            if error_name not in ("WebSocketDisconnect", "ConnectionClosedOK", "ConnectionClosedError"):
                logger.debug("[workers] websocket connection error: %s: %s", error_name, e)
        finally:
            await router.cleanup()

    async def dispatch(
        self,
        handler: Handler,
        worker_id: str,
        message: str,
        user: User | None = None,
        organization_id: str = "",
        workspace_id: str = "",
        thread_id: str | None = None,
        thread_title: str | None = None,
        content: list | None = None,
    ) -> DispatchStream:
        persistence = await self._ensure_initialized()

        pipeline = HandlerPipeline(
            persistence=persistence,
            broadcast_callback=self._broadcast_dict_to_worker,
            analytics_collector=self._analytics_collector,
            settings_broadcaster=self._settings_broadcaster,
            settings_schema=self._config.settings_schema,
            assistant_name=self._config.assistant_name,
            upload_store=self._upload_store,
        )

        context = HandlerContext(
            handler=handler,
            worker_id=worker_id,
            message=message,
            organization_id=organization_id,
            workspace_id=workspace_id,
            user=user or User(user_id=""),
            thread_id=thread_id,
            thread_title=thread_title,
            content=content,
        )

        result = await pipeline.setup(context)

        # Upsert participant for existing threads (new threads already include the user)
        resolved_user = user or User(user_id="")
        if not result.is_new_thread and (resolved_user.user_id or resolved_user.user_email):
            await persistence.upsert_thread_participant(result.thread.id, resolved_user)

        return DispatchStream(pipeline=pipeline, context=context, result=result)

    async def repository(self) -> Repository:
        persistence = await self._ensure_initialized()
        return Repository(persistence)

    async def memory(self, thread_id: str) -> WorkerMemory:
        persistence = await self._ensure_initialized()
        return WorkerMemory(thread_id=thread_id, persistence=persistence)

    async def settings(self, worker_id: str) -> WorkerSettings:
        persistence = await self._ensure_initialized()
        if self._config.settings_schema and self._settings_broadcaster:
            return WorkerSettings(
                worker_id=worker_id,
                schema=self._config.settings_schema,
                persistence=persistence,
                broadcaster=self._settings_broadcaster,
            )

        from dooers.features.settings.models import SettingsSchema

        class _NoopBroadcaster:
            async def broadcast_snapshot(self, **kwargs) -> None:
                pass

            async def broadcast_patch(self, **kwargs) -> None:
                pass

        class _NoopPersistence:
            async def get_settings(self, worker_id: str) -> dict:
                return {}

            async def update_setting(self, worker_id: str, field_id: str, value) -> None:
                pass

            async def set_settings(self, worker_id: str, values: dict) -> None:
                pass

        return WorkerSettings(
            worker_id=worker_id,
            schema=SettingsSchema(fields=[]),
            persistence=_NoopPersistence(),  # type: ignore
            broadcaster=_NoopBroadcaster(),  # type: ignore
        )

    async def analytics(
        self,
        worker_id: str,
        thread_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> WorkerAnalytics:
        await self._ensure_initialized()
        if self._analytics_collector:
            return WorkerAnalytics(
                worker_id=worker_id,
                thread_id=thread_id or "",
                user_id=user_id,
                run_id=run_id,
                collector=self._analytics_collector,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )

        class _NoopCollector:
            async def track(self, **kwargs) -> None:
                pass

            async def feedback(self, **kwargs) -> None:
                pass

        return WorkerAnalytics(
            worker_id=worker_id,
            thread_id=thread_id or "",
            user_id=user_id,
            run_id=run_id,
            collector=_NoopCollector(),  # type: ignore
            organization_id=organization_id,
            workspace_id=workspace_id,
        )

    async def _broadcast_dict_to_worker(self, worker_id: str, payload: dict) -> None:
        """Convert dict payload to S2C frame and broadcast via registry."""
        payload_type = payload.get("type")

        if payload_type == "thread.upsert":
            frame = S2C_ThreadUpsert(
                id=str(uuid.uuid4()),
                payload=ThreadUpsertPayload(thread=payload["thread"]),
            )
        elif payload_type == "event.append":
            frame = S2C_EventAppend(
                id=str(uuid.uuid4()),
                payload=EventAppendPayload(
                    thread_id=payload["thread_id"],
                    events=payload["events"],
                ),
            )
        elif payload_type == "run.upsert":
            frame = S2C_RunUpsert(
                id=str(uuid.uuid4()),
                payload=RunUpsertPayload(run=payload["run"]),
            )
        else:
            return

        message = serialize_frame(frame)
        await self._registry.broadcast(worker_id, message)

    async def close(self) -> None:
        if self._upload_store:
            await self._upload_store.stop()
            self._upload_store = None

        if self._analytics_collector:
            await self._analytics_collector.stop()
            self._analytics_collector = None

        if self._persistence:
            await self._persistence.disconnect()
            self._persistence = None

        self._initialized = False
