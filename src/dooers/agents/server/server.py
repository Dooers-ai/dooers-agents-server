from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from dooers.agents.server.auth_validation import AuthValidationClient
from dooers.agents.server.broadcast import (
    BroadcastManager,
    PipelineBroadcaster,
    make_memory_notifier,
    make_schedule_notifier,
)
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.database import SqlDatabase
from dooers.agents.server.dispatch import DispatchStream
from dooers.agents.server.features.analytics.agent_analytics import AgentAnalytics
from dooers.agents.server.features.analytics.collector import AnalyticsCollector
from dooers.agents.server.features.memory.api import LongTermMemory
from dooers.agents.server.features.memory.models import MemoryOwner, MemoryRecord
from dooers.agents.server.features.memory.store import MemoryStore, PostgresMemoryStore
from dooers.agents.server.features.schedules.api import AgentSchedules
from dooers.agents.server.features.schedules.models import ScheduleRecord, ScheduleRunResult
from dooers.agents.server.features.schedules.recurrence import next_run_after
from dooers.agents.server.features.schedules.store import PostgresScheduleStore, ScheduleStore
from dooers.agents.server.features.settings.agent_settings import AgentSettings
from dooers.agents.server.features.settings.broadcaster import SettingsBroadcaster
from dooers.agents.server.handlers.content_policy import normalize_allowed_content_types
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.router import Handler, Router, WebSocketProtocol
from dooers.agents.server.persistence.base import Persistence
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.agents.server.protocol.models import (
    ChatContext,
    Thread,
    ThreadEvent,
    User,
    WireC2S_ContentPart,
    WorkspaceQueueCatalog,
)
from dooers.agents.server.protocol.parser import parse_frame
from dooers.agents.server.registry import ConnectionRegistry
from dooers.agents.server.repository import Repository
from dooers.agents.server.runs import RunRegistry
from dooers.agents.server.settings import (
    ANALYTICS_BATCH_SIZE,
    ANALYTICS_FLUSH_INTERVAL,
    ANALYTICS_WEBHOOK_URL,
)
from dooers.agents.server.storage.chat_upload_file_policy import (
    PERSIST_CHAT_ATTACHMENTS_FIELD,
    enforce_allowed_chat_file_kind,
)
from dooers.agents.server.thread_access import ThreadViewGrantStore
from dooers.agents.server.upload_store import UploadStore

logger = logging.getLogger(__name__)
# Same namespace the Router uses, so handle-loop diagnostics show up in apps that tune `agents`.
_agents = logging.getLogger("agents")


def _catalog_slugs(queues: list[WorkspaceQueueCatalog] | None) -> list[str] | None:
    slugs = [queue.slug for queue in (queues or []) if queue.slug]
    return slugs or None


def _build_persistence(config: AgentConfig) -> Persistence:
    """Select the persistence backend from ``config.database_type``.

    ``dooers`` connects to the platform-managed AlloyDB via IAM (no password);
    ``cosmos`` uses Azure Cosmos; anything else uses the self-provided Postgres.
    """
    if config.database_type == "cosmos":
        from dooers.agents.server.persistence.cosmos import CosmosPersistence

        return CosmosPersistence(
            endpoint=config.database_host,
            key=config.database_key,
            database=config.database_name,
            table_prefix=config.database_table_prefix,
            on_settings_updated=config.on_settings_updated,
        )
    if config.database_type == "dooers":
        from dooers.agents.server.persistence.dooers import DooersPersistence

        return DooersPersistence(
            instance_uri=config.database_instance_uri,
            user=config.database_user,
            database=config.database_name,
            table_prefix=config.database_table_prefix,
            on_settings_updated=config.on_settings_updated,
        )
    return PostgresPersistence(
        host=config.database_host,
        port=config.database_port,
        user=config.database_user,
        database=config.database_name,
        password=config.database_password,
        ssl=config.database_ssl,
        table_prefix=config.database_table_prefix,
        on_settings_updated=config.on_settings_updated,
    )


class AgentServer:
    def __init__(self, config: AgentConfig):
        self._config = config
        self._allowed_content_types = normalize_allowed_content_types(config.allowed_content_types)
        self._content_policy_denial_message = (config.content_policy_denial_message or "").strip() or None
        self._persistence: Persistence | None = None
        self._initialized = False

        # Template-as-agent: surface AgentConfig.agent_seed_secret to RAG/OTEL credential resolve
        # (Settings may load AGENT_SEED_SECRET from .env without exporting it into os.environ).
        from dooers.agents.server.observability.service_token import configure_process_seed_secret

        configure_process_seed_secret(config.agent_seed_secret)

        self._registry = ConnectionRegistry()
        self._subscriptions: dict[str, set[str]] = {}  # ws_id -> set of thread_ids
        self._view_grants = ThreadViewGrantStore()

        self._analytics_subscriptions: dict[str, set[str]] = {}  # agent_id -> set of ws_ids
        self._settings_subscriptions: dict[str, set[str]] = {}  # agent_id -> set of ws_ids
        self._settings_ws_context: dict[str, dict[str, Any]] = {}  # ws_id -> { agent_id, audience, ws }

        self._broadcast: BroadcastManager | None = None

        self._upload_store: UploadStore | None = None
        self._analytics_collector: AnalyticsCollector | None = None
        self._settings_broadcaster: SettingsBroadcaster | None = None
        self._auth_validator: AuthValidationClient | None = None
        self._guest_cleanup_task: asyncio.Task | None = None

        #: Executions in this process (``run.cancel`` from any socket finds them here).
        self._run_registry = RunRegistry()
        self._memory_store: MemoryStore | None = None
        self._schedule_store: ScheduleStore | None = None
        self._schedule_handler: Handler | None = None
        self._schedule_task: asyncio.Task | None = None
        self._schedule_lock = asyncio.Lock()
        self._instance_id = f"agent-{uuid.uuid4().hex[:12]}"
        self._pipeline_broadcaster: PipelineBroadcaster | None = None
        self._memory_notify_impl = make_memory_notifier(self._registry)
        self._schedule_notify_impl = make_schedule_notifier(self._registry)

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

        self._persistence = _build_persistence(self._config)

        await self._persistence.connect()

        if self._config.database_auto_migrate:
            await self._persistence.migrate()

        self._broadcast = BroadcastManager(
            registry=self._registry,
            persistence=self._persistence,
            subscriptions=self._subscriptions,
        )
        self._pipeline_broadcaster = PipelineBroadcaster(
            registry=self._registry,
            persistence=self._persistence,
            subscriptions=self._subscriptions,
            hydrate=self._hydrate_thread_events_impl,
        )
        await self._init_long_term_features(self._persistence)

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
            # None → platform default plus known Dooers cores (see auth_validation).
            core_base_url=self._config.agent_core_base_url or None,
        )

        if self._config.guest_thread_cleanup_interval_seconds > 0:
            self._guest_cleanup_task = asyncio.create_task(
                self._run_guest_cleanup_loop(),
                name="dooers-guest-thread-cleanup",
            )

        from dooers.agents.server.observability.otel import init_otel
        from dooers.agents.server.settings import (
            AGENT_CORE_BASE_URL,
            AGENT_OTEL_SERVICE_URL,
            OTEL_SERVICE_NAME,
        )

        # Optional overrides via AgentConfig / env; otherwise platform defaults (like analytics).
        init_otel(
            otel_service_url=self._config.otel_service_url or AGENT_OTEL_SERVICE_URL,
            core_base_url=self._config.agent_core_base_url or AGENT_CORE_BASE_URL,
            persistence=self._persistence,
            service_name=self._config.otel_service_name or OTEL_SERVICE_NAME,
        )

        self._initialized = True
        self._maybe_start_schedule_loop()
        from dooers.agents.server.features.channels.whatsapp.delivery_receipt import bind_agent_server

        bind_agent_server(self)
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
            view_grants=self._view_grants,
            run_registry=self._run_registry,
            memory_store=self._memory_store,
            schedule_store=self._schedule_store,
            memory_notifier=self._notify_memory,
            schedule_notifier=self._notify_schedule,
            pipeline_broadcaster=self._pipeline_broadcaster,
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
                if router.closed:
                    break
        except Exception as e:
            name = type(e).__name__
            if name in (
                "WebSocketDisconnect",
                "WebSocketDisconnected",
                "ConnectionClosedOK",
                "ConnectionClosedError",
            ):
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
        queues: list[WorkspaceQueueCatalog] | None = None,
        *,
        chat_context: ChatContext | None = None,
        trigger: dict[str, Any] | None = None,
        actor: str = "user",
        data: dict[str, Any] | None = None,
    ) -> DispatchStream:
        """Run ``handler`` outside a WebSocket (REST, webhooks, schedules).

        ``actor="system"`` with a ``trigger`` records the inbound event as platform-originated
        (e.g. a reminder firing) instead of as something the user typed.
        """
        persistence = await self._ensure_initialized()

        pipeline = self._new_pipeline(persistence, whatsapp_outbound=self._resolve_whatsapp_outbound(persistence))

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
            queues=list(queues or []),
            allowed_queues=_catalog_slugs(queues),
            chat_context=chat_context,
            trigger=trigger,
            persist_actor=actor,
            persist_author=None if actor == "user" else (self._config.assistant_name if actor == "assistant" else "System"),
            data=data,
        )

        result = await pipeline.setup(context)

        # Upsert participant for existing threads (new threads already include the user)
        resolved_user = user or User(user_id="")
        if not result.is_new_thread and (resolved_user.user_id or resolved_user.user_email):
            await persistence.upsert_thread_participant(result.thread.id, resolved_user)

        from dooers.agents.server.observability.otel import start_tracker
        tracker = start_tracker(
            thread_id=result.thread.id,
            event_id=result.user_event.id,
            agent_id=agent_id,
            thread_title=result.thread.title,
            organization_id=organization_id,
            workspace_id=workspace_id,
            channel=channel or "dooers-platform",
            user_id=resolved_user.user_id or None,
            user_name=resolved_user.user_name or None,
            user_email=resolved_user.user_email or None,
        )

        return DispatchStream(pipeline=pipeline, context=context, result=result, tracker=tracker)

    async def ingest(
        self,
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
        queues: list[WorkspaceQueueCatalog] | None = None,
        *,
        actor: str = "assistant",
        author: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> str:
        """Persist a channel message on the thread without running the agent handler.

        Used for WhatsApp human/peer outbound (``whatsapp_peer_message="register"``).
        Returns ``thread_id``.
        """

        async def _noop_handler(*_args: Any, **_kwargs: Any):
            if False:  # pragma: no cover
                yield None

        persistence = await self._ensure_initialized()
        pipeline = self._new_pipeline(persistence, whatsapp_outbound=None)
        context = HandlerContext(
            handler=_noop_handler,
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
            data=data,
            persist_actor=actor,
            persist_author=author,
            queues=list(queues or []),
            allowed_queues=_catalog_slugs(queues),
        )
        result = await pipeline.setup(context)
        resolved_user = user or User(user_id="")
        if not result.is_new_thread and (resolved_user.user_id or resolved_user.user_email):
            await persistence.upsert_thread_participant(result.thread.id, resolved_user)
        return result.thread.id

    async def database(self) -> SqlDatabase:
        """Return app SQL access backed by the SDK-owned persistence pool.

        For ``database_type="dooers"`` this is the same managed AlloyDB/IAM
        pool used internally by AgentServer. Application code never owns DB
        connection credentials, connector construction, or pool lifecycle.
        """
        persistence = await self._ensure_initialized()
        if not isinstance(persistence, PostgresPersistence):
            raise RuntimeError(
                f"SQL database access is unavailable for database_type={self._config.database_type!r}"
            )
        return SqlDatabase(persistence)

    async def repository(self) -> Repository:
        persistence = await self._ensure_initialized()
        return Repository(persistence)

    async def memory(self, thread_id: str) -> AgentMemory:
        persistence = await self._ensure_initialized()
        return AgentMemory(thread_id=thread_id, persistence=persistence)

    async def long_term_memory(
        self,
        *,
        agent_id: str,
        user_id: str,
        organization_id: str = "",
        workspace_id: str = "",
    ) -> LongTermMemory:
        """Long-term memory of one user outside a handler (admin jobs, REST endpoints)."""
        await self._ensure_initialized()
        owner = MemoryOwner(agent_id=agent_id, organization_id=organization_id, workspace_id=workspace_id, user_id=user_id)
        return LongTermMemory(self._memory_store, owner, notifier=self._notify_memory)

    async def schedules(
        self,
        *,
        agent_id: str,
        user_id: str,
        organization_id: str = "",
        workspace_id: str = "",
        thread_id: str | None = None,
        timezone: str | None = None,
    ) -> AgentSchedules:
        await self._ensure_initialized()
        return AgentSchedules(
            self._schedule_store,
            agent_id=agent_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user_id,
            thread_id=thread_id,
            default_timezone=timezone,
            notifier=self._notify_schedule,
        )

    @property
    def run_registry(self) -> RunRegistry:
        return self._run_registry

    def cancel_run(self, thread_id: str, *, run_id: str | None = None, reason: str = "canceled") -> int:
        """Stop the run(s) executing on ``thread_id`` in this process. Returns how many were signalled."""
        return self._run_registry.cancel(thread_id, run_id=run_id, reason=reason)

    async def store_artifact(
        self,
        *,
        data: bytes,
        filename: str,
        mime_type: str,
        agent_id: str,
        thread_id: str | None,
    ) -> dict[str, Any] | None:
        """Persist an agent-generated file to chat storage.

        Returns ``{"ref_id", "url", "object_key", "size_bytes"}`` — pass ``ref_id`` to
        ``send.document/send.image`` so the URL is re-signed whenever the thread is replayed —
        or ``None`` when no durable storage is configured (``CHAT_STORAGE_SERVICE=none``).
        """
        from dooers.agents.server.storage.chat_artifacts import (
            chat_storage_service_ready,
            put_chat_artifact,
            sign_chat_artifact_read_url,
        )

        await self._ensure_initialized()
        if not data:
            raise ValueError("empty artifact")
        if not chat_storage_service_ready(self._config):
            return None
        ref_id = f"art_{uuid.uuid4().hex}"
        uri, object_key = await asyncio.to_thread(
            put_chat_artifact,
            self._config,
            data=data,
            content_type=mime_type,
            agent_id=agent_id,
            thread_id=thread_id,
            ref_id=ref_id,
            filename=filename,
        )
        if not uri:
            return None
        url = await asyncio.to_thread(sign_chat_artifact_read_url, self._config, object_key)
        return {"ref_id": ref_id, "url": url or uri, "object_key": object_key, "size_bytes": len(data)}

    # --- schedules ------------------------------------------------------------------------

    def enable_schedules(self, handler: Handler) -> None:
        """Run due schedules with ``handler``.

        Starts an in-process poller (``AgentConfig.schedules_poll_interval_seconds``) once the
        server is initialized. On hosts that scale to zero, also expose an endpoint that calls
        :meth:`run_due_schedules` so a platform scheduler can wake the agent.
        """
        self._schedule_handler = handler
        if self._initialized:
            self._maybe_start_schedule_loop()

    def _maybe_start_schedule_loop(self) -> None:
        interval = int(self._config.schedules_poll_interval_seconds or 0)
        if (
            interval <= 0
            or self._schedule_handler is None
            or self._schedule_store is None
            or (self._schedule_task is not None and not self._schedule_task.done())
        ):
            return
        try:
            self._schedule_task = asyncio.get_running_loop().create_task(
                self._run_schedule_loop(interval), name="dooers-schedules"
            )
        except RuntimeError:
            self._schedule_task = None

    async def _run_schedule_loop(self, interval: int) -> None:
        while True:
            try:
                await self.run_due_schedules()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[agents] schedule tick failed")
            await asyncio.sleep(interval)

    async def run_due_schedules(
        self,
        handler: Handler | None = None,
        *,
        agent_id: str | None = None,
        limit: int = 10,
    ) -> list[ScheduleRunResult]:
        """Claim and execute schedules that are due. Safe to call concurrently / from many replicas."""
        await self._ensure_initialized()
        run_handler = handler or self._schedule_handler
        if run_handler is None or self._schedule_store is None:
            return []
        from datetime import UTC, datetime

        async with self._schedule_lock:
            due = await self._schedule_store.claim_due(
                agent_id=agent_id,
                now=datetime.now(UTC),
                limit=max(1, min(limit, 50)),
                lease_seconds=int(self._config.schedule_lease_seconds),
                owner=self._instance_id,
            )
        results: list[ScheduleRunResult] = []
        for record in due:
            results.append(await self._execute_schedule(record, run_handler))
        return results

    async def _execute_schedule(self, record: ScheduleRecord, handler: Handler) -> ScheduleRunResult:
        from datetime import UTC, datetime

        assert self._schedule_store is not None
        started = datetime.now(UTC)
        trigger = {
            "type": "schedule",
            "schedule_id": record.id,
            "kind": record.kind,
            "title": record.title,
            "instruction": record.instruction,
            "run_number": record.run_count + 1,
            "scheduled_for": record.next_run_at.isoformat() if record.next_run_at else None,
            "timezone": record.timezone,
        }
        message = f"[{record.kind}] {record.title}\n{record.instruction}"
        status = "succeeded"
        error: str | None = None
        thread_id: str | None = None
        try:
            stream = await self.dispatch(
                handler=handler,
                agent_id=record.agent_id,
                organization_id=record.organization_id,
                workspace_id=record.workspace_id,
                message=message,
                user=User(user_id=record.user_id, user_name=record.user_name, user_email=record.user_email),
                thread_id=record.thread_id,
                thread_title=record.title,
                chat_context=ChatContext(timezone=record.timezone),
                trigger=trigger,
                actor="system",
                data={"trigger": trigger},
            )
            thread_id = stream.thread_id
            async for _event in stream:
                pass
        except Exception as exc:  # noqa: BLE001 — record and continue with the next schedule
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"[:500]
            logger.exception("[agents] schedule %s failed", record.id)
        run_count = record.run_count + 1
        anchor_raw = (record.metadata or {}).get("anchor")
        try:
            anchor = datetime.fromisoformat(anchor_raw) if anchor_raw else (record.next_run_at or started)
        except ValueError:
            anchor = record.next_run_at or started
        next_run = next_run_after(
            record.recurrence,
            tz_name=record.timezone,
            previous=record.next_run_at or started,
            anchor=anchor,
            run_count=run_count,
        )
        fields: dict[str, Any] = {
            "run_count": run_count,
            "last_run_at": started,
            "last_status": status,
            "last_error": error,
            "last_thread_id": thread_id,
            "next_run_at": next_run,
        }
        if next_run is None:
            fields["status"] = "completed" if status == "succeeded" else "failed"
        if record.thread_id is None and thread_id and record.recurrence.freq == "once":
            fields["thread_id"] = thread_id
        updated = await self._schedule_store.release(record.id, self._instance_id, fields)
        if updated is not None:
            await self._notify_schedule("upsert", updated)
        if self._analytics_collector:
            await self._analytics_collector.track(
                event="schedule.fired",
                agent_id=record.agent_id,
                thread_id=thread_id,
                user_id=record.user_id,
                data={"schedule_id": record.id, "status": status, "kind": record.kind},
                organization_id=record.organization_id,
                workspace_id=record.workspace_id,
            )
        return ScheduleRunResult(
            schedule_id=record.id, thread_id=thread_id, status=status, error=error, next_run_at=next_run  # type: ignore[arg-type]
        )

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
        """Convert a pipeline payload to S2C frames for the sockets allowed to see the thread."""
        if self._pipeline_broadcaster is None:
            return
        await self._pipeline_broadcaster(agent_id, payload)

    def _new_pipeline(self, persistence: Persistence, *, whatsapp_outbound: Any) -> HandlerPipeline:
        return HandlerPipeline(
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
            whatsapp_outbound=whatsapp_outbound,
            run_registry=self._run_registry,
            memory_store=self._memory_store,
            schedule_store=self._schedule_store,
            memory_notifier=self._notify_memory,
            schedule_notifier=self._notify_schedule,
        )

    async def _init_long_term_features(self, persistence: Persistence) -> None:
        """Long-term memory and schedules need SQL; other backends run without them."""
        if not isinstance(persistence, PostgresPersistence):
            logger.info("[agents] long-term memory/schedules disabled for database_type=%s", self._config.database_type)
            return
        if self._config.memory_enabled:
            store = PostgresMemoryStore(persistence)
            try:
                if self._config.database_auto_migrate:
                    await store.migrate()
                self._memory_store = store
            except Exception:  # noqa: BLE001 — degrade, never block boot
                logger.exception("[agents] long-term memory migration failed; feature disabled")
        if self._config.schedules_enabled:
            schedule_store = PostgresScheduleStore(persistence)
            try:
                if self._config.database_auto_migrate:
                    await schedule_store.migrate()
                self._schedule_store = schedule_store
            except Exception:  # noqa: BLE001
                logger.exception("[agents] schedules migration failed; feature disabled")

    def use_stores(self, *, memory_store: MemoryStore | None = None, schedule_store: ScheduleStore | None = None) -> None:
        """Override the long-term stores (tests, custom backends). Call before serving traffic."""
        if memory_store is not None:
            self._memory_store = memory_store
        if schedule_store is not None:
            self._schedule_store = schedule_store

    async def _notify_memory(self, action: str, record: MemoryRecord | None, user_id: str | None) -> None:
        await self._memory_notify_impl(action, record, user_id)

    async def _notify_schedule(self, action: str, record: ScheduleRecord) -> None:
        await self._schedule_notify_impl(action, record)

    async def close(self) -> None:
        if self._schedule_task:
            self._schedule_task.cancel()
            try:
                await self._schedule_task
            except (asyncio.CancelledError, Exception):
                pass
            self._schedule_task = None

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
