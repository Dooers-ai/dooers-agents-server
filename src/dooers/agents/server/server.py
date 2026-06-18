from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from dooers.agents.server.auth_validation import AuthValidationClient
from dooers.agents.server.broadcast import BroadcastManager
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.dispatch import DispatchStream
from dooers.agents.server.features.analytics.agent_analytics import AgentAnalytics
from dooers.agents.server.features.analytics.collector import AnalyticsCollector
from dooers.agents.server.features.settings.agent_settings import AgentSettings
from dooers.agents.server.features.settings.broadcaster import SettingsBroadcaster
from dooers.agents.server.handlers.content_policy import normalize_allowed_content_types
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.router import Handler, Router, WebSocketProtocol
from dooers.agents.server.persistence.base import Persistence
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.agents.server.protocol.frames import (
    EventAppendPayload,
    RunUpsertPayload,
    S2C_EventAppend,
    S2C_RunUpsert,
    S2C_ThreadUpsert,
    ThreadUpsertPayload,
)
from dooers.agents.server.protocol.models import Thread, ThreadEvent, User, WireC2S_ContentPart
from dooers.agents.server.protocol.parser import parse_frame, serialize_frame
from dooers.agents.server.registry import ConnectionRegistry
from dooers.agents.server.repository import Repository
from dooers.agents.server.settings import (
    ANALYTICS_BATCH_SIZE,
    ANALYTICS_FLUSH_INTERVAL,
    ANALYTICS_WEBHOOK_URL,
)
from dooers.agents.server.storage.chat_upload_file_policy import (
    PERSIST_CHAT_ATTACHMENTS_FIELD,
    enforce_allowed_chat_file_kind,
)
from dooers.agents.server.upload_store import UploadStore

logger = logging.getLogger(__name__)
# Same namespace the Router uses, so handle-loop diagnostics show up in apps that tune `agents`.
_agents = logging.getLogger("agents")


class AgentServer:
    def __init__(self, config: AgentConfig):
        self._config = config
        self._allowed_content_types = normalize_allowed_content_types(config.allowed_content_types)
        self._content_policy_denial_message = (config.content_policy_denial_message or "").strip() or None
        self._persistence: Persistence | None = None
        self._initialized = False

        self._registry = ConnectionRegistry()
        self._subscriptions: dict[str, set[str]] = {}  # ws_id -> set of thread_ids

        self._analytics_subscriptions: dict[str, set[str]] = {}  # agent_id -> set of ws_ids
        self._settings_subscriptions: dict[str, set[str]] = {}  # agent_id -> set of ws_ids
        self._settings_ws_context: dict[str, dict[str, Any]] = {}  # ws_id -> { agent_id, audience, ws }

        self._broadcast: BroadcastManager | None = None

        self._upload_store: UploadStore | None = None
        self._analytics_collector: AnalyticsCollector | None = None
        self._settings_broadcaster: SettingsBroadcaster | None = None
        self._auth_validator: AuthValidationClient | None = None
        self._guest_cleanup_task: asyncio.Task | None = None

    async def _hydrate_thread_events_impl(
        self,
        events: list[ThreadEvent],
        thread: Thread,
    ) -> list[ThreadEvent]:
        from dooers.agents.server.storage.hydrate import hydrate_thread_events

        return await hydrate_thread_events(self._config, events, thread)

    def _resolve_whatsapp_outbound(self, persistence: Persistence) -> Any:
        if not self._config.dooers_whatsapp_service:
            return None
        from dooers.agents.server.features.channels.whatsapp.outbound import create_dooers_whatsapp_outbound

        return create_dooers_whatsapp_outbound(persistence)

    @property
    def registry(self) -> ConnectionRegistry:
        return self._registry

    @property
    def settings_schema(self):
        return self._config.settings_schema

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

    @property
    def allowed_content_types(self) -> frozenset[str] | None:
        """Normalized allowlist from :class:`~dooers.config.AgentConfig` (``None`` = no allowlist)."""

        return self._allowed_content_types

    @property
    def store_chat_uploads(self) -> bool:
        return bool(getattr(self._config, "store_chat_uploads", False))

    @property
    def upload_max_size_bytes(self) -> int:
        return int(getattr(self._config, "upload_max_size_bytes", 25 * 1024 * 1024))

    async def chat_upload(
        self,
        *,
        data: bytes,
        filename: str,
        mime_type: str,
        agent_id: str,
        thread_id: str | None = None,
        source: str = "chat",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Stage bytes for WS ``ref_id``, then optionally persist to chat artifact storage.

        Durable write runs only when creator ``persist_chat_attachments``, ops
        ``store_chat_uploads``, and configured GCS/Azure are all satisfied.
        """
        aid = (agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id is required")
        if not data:
            raise ValueError("empty file")

        cfg = self._config
        creator_persist = False
        try:
            settings_api = await self.settings(aid)
            merged = await settings_api.get_all()
            creator_persist = bool(merged.get(PERSIST_CHAT_ATTACHMENTS_FIELD))
        except Exception:
            logger.exception("persist_chat_attachments read failed for agent_id=%s", aid)

        if creator_persist:
            enforce_allowed_chat_file_kind(
                filename=filename,
                mime_type=mime_type,
                allowed_raw=getattr(cfg, "allowed_content_types", None),
            )

        ref_id = await self.upload(data=data, filename=filename, mime_type=mime_type)
        out: dict[str, Any] = {
            "ref_id": ref_id,
            "mime_type": mime_type,
            "filename": filename,
            "size_bytes": len(data),
            "size": len(data),
            "public_url": None,
        }

        write_durable = creator_persist and bool(getattr(cfg, "store_chat_uploads", False))
        logger.info(
            "[uploads] durable=%s agent=%s thread=%s source=%s run_id=%s",
            write_durable,
            aid,
            thread_id,
            source,
            run_id,
        )
        if not write_durable:
            return out

        from dooers.agents.server.storage.chat_artifacts import chat_storage_service_ready, put_chat_artifact

        if not chat_storage_service_ready(cfg):
            return out
        try:
            uri, object_key = put_chat_artifact(
                cfg,
                data=data,
                content_type=mime_type,
                agent_id=aid,
                thread_id=thread_id,
                ref_id=ref_id,
                filename=filename,
            )
            if uri:
                out["artifact_uri"] = uri
                out["object_key"] = object_key
        except Exception:
            logger.exception("Chat artifact blob upload failed; ref_id still valid in-process")
        return out

    async def upload(self, data: bytes, filename: str, mime_type: str) -> str:
        """Stage a file for a future WebSocket event.create. Returns a reference ID."""
        await self._ensure_initialized()
        assert self._upload_store is not None
        return self._upload_store.store(data, filename, mime_type)

    async def _ensure_initialized(self) -> Persistence:
        if self._persistence and self._initialized:
            return self._persistence

        if self._config.database_type == "cosmos":
            from dooers.agents.server.persistence.cosmos import CosmosPersistence

            self._persistence = CosmosPersistence(
                endpoint=self._config.database_host,
                key=self._config.database_key,
                database=self._config.database_name,
                table_prefix=self._config.database_table_prefix,
                on_settings_updated=self._config.on_settings_updated,
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
                on_settings_updated=self._config.on_settings_updated,
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
            ws_context=self._settings_ws_context,
        )

        self._upload_store = UploadStore(
            max_size=self._config.upload_max_size_bytes,
            ttl=self._config.upload_ttl_seconds,
        )
        await self._upload_store.start()

        self._auth_validator = AuthValidationClient(
            url=self._config.auth_validation_url or "",
            timeout=self._config.auth_validation_timeout,
        )

        if self._config.guest_thread_cleanup_interval_seconds > 0:
            self._guest_cleanup_task = asyncio.create_task(
                self._run_guest_cleanup_loop(),
                name="dooers-guest-thread-cleanup",
            )

        self._initialized = True
        return self._persistence

    async def _run_guest_cleanup_loop(self) -> None:
        interval = self._config.guest_thread_cleanup_interval_seconds
        ttl = self._config.guest_thread_ttl_seconds
        persistence = self._persistence
        if persistence is None:
            return  # should never happen — loop is started after init
        logger.info(
            "[agents] guest thread cleanup task started (interval=%ds, ttl=%ds)",
            interval,
            ttl,
        )
        try:
            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
                try:
                    count = await persistence.delete_idle_guest_threads(ttl)
                    if count > 0:
                        logger.info("[agents] deleted %d idle guest threads", count)
                    else:
                        logger.debug("[agents] deleted %d idle guest threads", count)
                except asyncio.CancelledError:
                    raise
                except NotImplementedError as e:
                    logger.warning(
                        "[agents] guest thread cleanup unsupported by persistence backend: %s",
                        e,
                    )
                    return
                except Exception:
                    logger.exception("[agents] guest thread cleanup iteration failed (will retry)")
        except asyncio.CancelledError:
            logger.debug("[agents] guest thread cleanup task cancelled")
            raise

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
            agent_seed_secret=self._config.agent_seed_secret,
            assistant_name=self._config.assistant_name,
            analytics_subscriptions=self._analytics_subscriptions,
            settings_subscriptions=self._settings_subscriptions,
            settings_ws_context=self._settings_ws_context,
            upload_store=self._upload_store,
            auth_validator=self._auth_validator,
            allowed_content_types=self._allowed_content_types,
            content_policy_denial_message=self._content_policy_denial_message,
            hydrate_thread_events_for_client=self._hydrate_thread_events_impl,
            agent_config=self._config,
            whatsapp_outbound=self._resolve_whatsapp_outbound(persistence),
        )

        try:
            while True:
                data = await websocket.receive_text()
                try:
                    frame = parse_frame(data)
                except Exception as e:
                    _agents.warning(
                        "failed to parse WebSocket C2S frame: len=%d err=%s",
                        len(data),
                        e,
                        exc_info=True,
                    )
                    raise
                await router.route(websocket, frame)
        except Exception as e:
            name = type(e).__name__
            if name in ("WebSocketDisconnect", "ConnectionClosedOK", "ConnectionClosedError"):
                logger.debug("websocket client disconnected (%s)", name)
            else:
                _agents.warning(
                    "WebSocket handle loop error: %s: %s",
                    name,
                    e,
                    exc_info=True,
                )
        finally:
            await router.cleanup()

    async def dispatch(
        self,
        handler: Handler,
        agent_id: str,
        message: str,
        user: User | None = None,
        organization_id: str = "",
        workspace_id: str = "",
        thread_id: str | None = None,
        thread_title: str | None = None,
        content: list[WireC2S_ContentPart | dict[str, Any]] | None = None,
        channel: str = "dooers-platform",
        channel_meta: dict[str, Any] | None = None,
    ) -> DispatchStream:
        persistence = await self._ensure_initialized()

        pipeline = HandlerPipeline(
            persistence=persistence,
            broadcast_callback=self._broadcast_dict_to_agent,
            analytics_collector=self._analytics_collector,
            settings_broadcaster=self._settings_broadcaster,
            settings_schema=self._config.settings_schema,
            assistant_name=self._config.assistant_name,
            upload_store=self._upload_store,
            allowed_content_types=self._allowed_content_types,
            content_policy_denial_message=self._content_policy_denial_message,
            agent_config=self._config,
            whatsapp_outbound=self._resolve_whatsapp_outbound(persistence),
        )

        context = HandlerContext(
            handler=handler,
            agent_id=agent_id,
            message=message,
            organization_id=organization_id,
            workspace_id=workspace_id,
            channel=(channel or "dooers-platform"),
            channel_meta=channel_meta,
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

    async def memory(self, thread_id: str) -> AgentMemory:
        persistence = await self._ensure_initialized()
        return AgentMemory(thread_id=thread_id, persistence=persistence)

    async def settings(self, agent_id: str) -> AgentSettings:
        persistence = await self._ensure_initialized()
        if self._config.settings_schema and self._settings_broadcaster:
            return AgentSettings(
                agent_id=agent_id,
                schema=self._config.settings_schema,
                persistence=persistence,
                broadcaster=self._settings_broadcaster,
            )

        from dooers.agents.server.features.settings.models import SettingsSchema

        class _NoopBroadcaster:
            async def broadcast_snapshot(self, **kwargs) -> None:
                pass

            async def broadcast_patch(self, **kwargs) -> None:
                pass

        class _NoopPersistence:
            async def get_settings(self, agent_id: str) -> dict:
                return {}

            async def update_setting(self, agent_id: str, field_id: str, value) -> None:
                pass

            async def set_settings(self, agent_id: str, values: dict) -> None:
                pass

        return AgentSettings(
            agent_id=agent_id,
            schema=SettingsSchema(fields=[]),
            persistence=_NoopPersistence(),  # type: ignore
            broadcaster=_NoopBroadcaster(),  # type: ignore
        )

    async def analytics(
        self,
        agent_id: str,
        thread_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AgentAnalytics:
        await self._ensure_initialized()
        if self._analytics_collector:
            return AgentAnalytics(
                agent_id=agent_id,
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

        return AgentAnalytics(
            agent_id=agent_id,
            thread_id=thread_id or "",
            user_id=user_id,
            run_id=run_id,
            collector=_NoopCollector(),  # type: ignore
            organization_id=organization_id,
            workspace_id=workspace_id,
        )

    async def _broadcast_dict_to_agent(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Convert dict payload to S2C frame and broadcast via registry."""
        payload_type = payload.get("type")

        if payload_type == "thread.upsert":
            frame = S2C_ThreadUpsert(
                id=str(uuid.uuid4()),
                payload=ThreadUpsertPayload(thread=payload["thread"]),
            )
        elif payload_type == "event.append":
            evs = payload["events"]
            tid = payload["thread_id"]
            thr = await self._persistence.get_thread(tid)
            if thr is not None:
                evs = await self._hydrate_thread_events_impl(evs, thr)
            frame = S2C_EventAppend(
                id=str(uuid.uuid4()),
                payload=EventAppendPayload(
                    thread_id=tid,
                    events=evs,
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
        await self._registry.broadcast(agent_id, message)

    async def close(self) -> None:
        if self._guest_cleanup_task:
            self._guest_cleanup_task.cancel()
            try:
                await self._guest_cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[agents] guest cleanup task errored during shutdown")
            self._guest_cleanup_task = None

        if self._upload_store:
            await self._upload_store.stop()
            self._upload_store = None

        if self._analytics_collector:
            await self._analytics_collector.stop()
            self._analytics_collector = None

        if self._auth_validator:
            await self._auth_validator.close()
            self._auth_validator = None

        if self._persistence:
            await self._persistence.disconnect()
            self._persistence = None

        self._initialized = False
