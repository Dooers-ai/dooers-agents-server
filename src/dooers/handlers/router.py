from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import bcrypt

from dooers.auth_validation import AuthValidationClient
from dooers.exceptions import HandlerError
from dooers.features.settings.models import SettingsFieldVisibility
from dooers.handlers.pipeline import Handler, HandlerContext, HandlerPipeline, UploadReferenceError
from dooers.persistence.base import Persistence
from dooers.protocol.frames import (
    AckPayload,
    C2S_AnalyticsSubscribe,
    C2S_AnalyticsUnsubscribe,
    C2S_Connect,
    C2S_EventCreate,
    C2S_EventList,
    C2S_Feedback,
    C2S_SettingsPatch,
    C2S_SettingsPublicSchema,
    C2S_SettingsSeed,
    C2S_SettingsSubscribe,
    C2S_SettingsUnsubscribe,
    C2S_ThreadDelete,
    C2S_ThreadList,
    C2S_ThreadSubscribe,
    C2S_ThreadUnsubscribe,
    ClientToServer,
    EventAppendPayload,
    EventListResultPayload,
    FeedbackAckPayload,
    RunUpsertPayload,
    S2C_Ack,
    S2C_EventAppend,
    S2C_EventListResult,
    S2C_FeedbackAck,
    S2C_RunUpsert,
    S2C_SettingsPublicSchemaResult,
    S2C_ThreadDeleted,
    S2C_ThreadListResult,
    S2C_ThreadSnapshot,
    S2C_ThreadUpsert,
    ServerToClient,
    SettingsPublicSchemaResultPayload,
    ThreadDeletedPayload,
    ThreadListResultPayload,
    ThreadSnapshotPayload,
    ThreadUpsertPayload,
)
from dooers.protocol.models import User
from dooers.protocol.parser import serialize_frame
from dooers.registry import ConnectionRegistry

if TYPE_CHECKING:
    from dooers.features.analytics.collector import AnalyticsCollector
    from dooers.features.settings.broadcaster import SettingsBroadcaster
    from dooers.features.settings.models import SettingsSchema
    from dooers.upload_store import UploadStore

logger = logging.getLogger("agents")


class WebSocketProtocol(Protocol):
    async def receive_text(self) -> str: ...
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


def resolve_scope(user: User) -> str:
    if user.system_role == "admin":
        return "admin"
    if user.organization_role in ("owner", "manager"):
        return "organization"
    if user.workspace_role == "manager":
        return "workspace"
    return "member"


def _generate_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _can_access_creator_settings(user: User, *, agent_owner_user_id: str | None) -> bool:
    """Creator-level settings: org owner/manager, or the platform agent owner user."""
    if user.organization_role in ("owner", "manager"):
        return True
    if agent_owner_user_id and user.user_id == agent_owner_user_id:
        return True
    return False


class Router:
    def __init__(
        self,
        persistence: Persistence,
        handler: Handler,
        registry: ConnectionRegistry,
        subscriptions: dict[str, set[str]],
        analytics_collector: AnalyticsCollector | None = None,
        settings_broadcaster: SettingsBroadcaster | None = None,
        settings_schema: SettingsSchema | None = None,
        agent_seed_secret: str = "",
        assistant_name: str = "Assistant",
        analytics_subscriptions: dict[str, set[str]] | None = None,
        settings_subscriptions: dict[str, set[str]] | None = None,
        settings_ws_context: dict[str, dict[str, Any]] | None = None,
        upload_store: UploadStore | None = None,
        auth_validator: AuthValidationClient | None = None,
    ):
        self._persistence = persistence
        self._handler = handler
        self._registry = registry
        self._subscriptions = subscriptions

        self._analytics_collector = analytics_collector
        self._settings_broadcaster = settings_broadcaster
        self._settings_schema = settings_schema
        self._agent_seed_secret = (agent_seed_secret or "").strip()
        self._assistant_name = assistant_name
        self._analytics_subscriptions = analytics_subscriptions if analytics_subscriptions is not None else {}
        self._settings_subscriptions = settings_subscriptions if settings_subscriptions is not None else {}
        self._settings_ws_context = settings_ws_context if settings_ws_context is not None else {}

        self._auth_validator: AuthValidationClient | None = auth_validator

        self._ws: WebSocketProtocol | None = None
        self._ws_id: str = _generate_id()
        self._agent_id: str | None = None
        self._user: User | None = None
        self._organization_id: str = ""
        self._workspace_id: str = ""
        self._agent_owner_user_id: str | None = None
        self._subscribed_threads: set[str] = set()
        self._rate_limits: dict[str, Any] = {}
        self._event_timestamps: deque[float] = deque()

        self._pipeline = HandlerPipeline(
            persistence=persistence,
            broadcast_callback=self._broadcast_to_agent_dict,
            analytics_collector=analytics_collector,
            settings_broadcaster=settings_broadcaster,
            settings_schema=settings_schema,
            assistant_name=assistant_name,
            upload_store=upload_store,
        )

    async def _send(self, ws: WebSocketProtocol, frame: ServerToClient) -> None:
        await ws.send_text(serialize_frame(frame))

    async def _send_ack(
        self,
        ws: WebSocketProtocol,
        ack_id: str,
        ok: bool = True,
        error: dict[str, str] | None = None,
    ) -> None:
        frame = S2C_Ack(
            id=_generate_id(),
            payload=AckPayload(ack_id=ack_id, ok=ok, error=error),
        )
        await self._send(ws, frame)

    async def _broadcast_to_agent(self, frame: ServerToClient) -> None:
        """Broadcast a frame to all connections for the current agent."""
        if not self._agent_id:
            return
        message = serialize_frame(frame)
        await self._registry.broadcast(self._agent_id, message)

    async def _broadcast_to_agent_except_self(self, ws: WebSocketProtocol, frame: ServerToClient) -> None:
        """Broadcast a frame to all connections for the current agent except this one."""
        if not self._agent_id:
            return
        message = serialize_frame(frame)
        await self._registry.broadcast_except(self._agent_id, ws, message)

    async def _broadcast_to_agent_dict(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Convert a dict payload from the pipeline into S2C frames and broadcast."""
        payload_type = payload.get("type")

        if payload_type == "thread.upsert":
            frame = S2C_ThreadUpsert(
                id=_generate_id(),
                payload=ThreadUpsertPayload(thread=payload["thread"]),
            )
        elif payload_type == "event.append":
            frame = S2C_EventAppend(
                id=_generate_id(),
                payload=EventAppendPayload(
                    thread_id=payload["thread_id"],
                    events=payload["events"],
                ),
            )
        elif payload_type == "run.upsert":
            frame = S2C_RunUpsert(
                id=_generate_id(),
                payload=RunUpsertPayload(run=payload["run"]),
            )
        else:
            return

        message = serialize_frame(frame)
        await self._registry.broadcast(agent_id, message)

    async def route(self, ws: WebSocketProtocol, frame: ClientToServer) -> None:
        self._ws = ws
        match frame:
            case C2S_Connect():
                await self._handle_connect(ws, frame)
            case C2S_ThreadList():
                await self._handle_thread_list(ws, frame)
            case C2S_ThreadSubscribe():
                await self._handle_thread_subscribe(ws, frame)
            case C2S_ThreadUnsubscribe():
                await self._handle_thread_unsubscribe(ws, frame)
            case C2S_ThreadDelete():
                await self._handle_thread_delete(ws, frame)
            case C2S_EventCreate():
                await self._handle_event_create(ws, frame)
            case C2S_EventList():
                await self._handle_event_list(ws, frame)

            case C2S_AnalyticsSubscribe():
                await self._handle_analytics_subscribe(ws, frame)
            case C2S_AnalyticsUnsubscribe():
                await self._handle_analytics_unsubscribe(ws, frame)
            case C2S_Feedback():
                await self._handle_feedback(ws, frame)

            case C2S_SettingsSubscribe():
                await self._handle_settings_subscribe(ws, frame)
            case C2S_SettingsUnsubscribe():
                await self._handle_settings_unsubscribe(ws, frame)
            case C2S_SettingsPatch():
                await self._handle_settings_patch(ws, frame)
            case C2S_SettingsPublicSchema():
                await self._handle_settings_public_schema(ws, frame)
            case C2S_SettingsSeed():
                await self._handle_settings_seed(ws, frame)

    async def cleanup(self) -> None:
        logger.info(
            "[agents] router cleanup: ws=%s, agent=%s, had_analytics_sub=%s",
            self._ws_id,
            self._agent_id,
            self._ws_id in self._analytics_subscriptions.get(self._agent_id or "", set()),
        )
        if self._agent_id:
            await self._registry.unregister(self._agent_id, self._ws)

            if self._agent_id in self._analytics_subscriptions:
                self._analytics_subscriptions[self._agent_id].discard(self._ws_id)
                if not self._analytics_subscriptions[self._agent_id]:
                    logger.info("[agents] analytics subscriptions emptied for agent %s — deleting key", self._agent_id)
                    del self._analytics_subscriptions[self._agent_id]

            if self._agent_id in self._settings_subscriptions:
                self._settings_subscriptions[self._agent_id].discard(self._ws_id)
                if not self._settings_subscriptions[self._agent_id]:
                    del self._settings_subscriptions[self._agent_id]

        self._settings_ws_context.pop(self._ws_id, None)
        self._event_timestamps.clear()

        if self._ws_id in self._subscriptions:
            del self._subscriptions[self._ws_id]

    async def _handle_connect(self, ws: WebSocketProtocol, frame: C2S_Connect) -> None:
        # Reset per-connection rate-limit state so a second C2S_Connect on the
        # same session starts clean.
        self._rate_limits = {}
        self._event_timestamps.clear()

        self._agent_id = frame.payload.agent_id
        self._organization_id = frame.payload.organization_id
        self._workspace_id = frame.payload.workspace_id
        incoming_user = frame.payload.user

        is_anonymous = not (incoming_user and incoming_user.user_id)

        if is_anonymous:
            if self._auth_validator is None:
                await self._send_ack(
                    ws,
                    frame.id,
                    ok=False,
                    error={
                        "code": "ANONYMOUS_NOT_ALLOWED",
                        "message": "Anonymous connections are not accepted on this server",
                    },
                )
                return

            result = await self._auth_validator.validate(
                auth_token=frame.payload.auth_token,
                agent_id=self._agent_id,
                guest_user_id=(incoming_user.user_id if incoming_user else ""),
                organization_id=self._organization_id,
                workspace_id=self._workspace_id,
            )
            if not result.valid or result.user is None:
                await self._send_ack(
                    ws,
                    frame.id,
                    ok=False,
                    error={
                        "code": "CONNECTION_REJECTED",
                        "message": result.reason or "rejected",
                    },
                )
                return

            self._user = result.user
            # For guest visitors, the webhook has no display info to return —
            # the visitor's name/email/phone exist only on the pre-chat form,
            # which the frontend carries in the connect frame. Merge those
            # display fields in. Identity (user_id), roles, and org/workspace
            # still come from the webhook; this only fills in visible labels.
            if self._user.connection_type == "guest" and incoming_user is not None:
                patches: dict[str, Any] = {}
                if not self._user.user_name and incoming_user.user_name:
                    patches["user_name"] = incoming_user.user_name
                if not self._user.user_email and incoming_user.user_email:
                    patches["user_email"] = incoming_user.user_email
                if patches:
                    self._user = self._user.model_copy(update=patches)
            self._rate_limits = result.rate_limits
            self._agent_owner_user_id = result.agent_owner_user_id
            self._organization_id = result.organization_id or frame.payload.organization_id
            self._workspace_id = result.workspace_id or frame.payload.workspace_id
        else:
            # Authenticated path: when an auth validator is configured, the
            # validator auto-detects JWT vs opaque token. For JWTs the
            # validation URL is extracted from the token itself; for opaque
            # tokens the configured auth_validation_url is used.
            # The webhook response is the sole source of truth — no merging
            # with frame data.
            if self._auth_validator is not None and incoming_user is not None and incoming_user.user_id:
                try:
                    result = await self._auth_validator.validate(
                        auth_token=frame.payload.auth_token,
                        agent_id=self._agent_id,
                        guest_user_id="",
                        organization_id=self._organization_id,
                        workspace_id=self._workspace_id,
                        user_id=incoming_user.user_id,
                    )
                except Exception:
                    logger.exception("[agents] dashboard auth validation raised")
                    await self._send_ack(
                        ws,
                        frame.id,
                        ok=False,
                        error={
                            "code": "CONNECTION_REJECTED",
                            "message": "auth validation error",
                        },
                    )
                    return

                if not result.valid or result.user is None:
                    await self._send_ack(
                        ws,
                        frame.id,
                        ok=False,
                        error={
                            "code": "CONNECTION_REJECTED",
                            "message": result.reason or "rejected",
                        },
                    )
                    return

                self._user = result.user
                self._organization_id = result.organization_id or self._organization_id
                self._workspace_id = result.workspace_id or self._workspace_id
                self._agent_owner_user_id = result.agent_owner_user_id
                self._rate_limits = result.rate_limits or {}
            else:
                # No validator configured — trust the frame identity (legacy).
                self._user = incoming_user
                self._rate_limits = {}

        self._ws = ws
        await self._registry.register(self._agent_id, ws)
        self._subscriptions[self._ws_id] = set()
        await self._send_ack(ws, frame.id)

    async def _handle_thread_list(self, ws: WebSocketProtocol, frame: C2S_ThreadList) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        user = self._user or User(user_id="")
        scope = resolve_scope(user)
        # Build complete identity set for thread filtering
        all_identity_ids = [uid for uid in [user.user_id, *(user.identity_ids or [])] if uid]
        limit = frame.payload.limit or 30
        threads = await self._persistence.list_threads(
            agent_id=self._agent_id,
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            user_id=user.user_id,
            cursor=frame.payload.cursor,
            limit=limit + 1,
            scope=scope,
            user_email=user.user_email,
            identity_ids=all_identity_ids or None,
        )
        has_more = len(threads) > limit
        if has_more:
            threads = threads[:limit]

        if frame.payload.cursor:
            total_count = 0
        else:
            total_count = await self._persistence.count_threads(
                agent_id=self._agent_id,
                organization_id=self._organization_id,
                workspace_id=self._workspace_id,
                user_id=user.user_id,
                scope=scope,
                user_email=user.user_email,
                identity_ids=all_identity_ids or None,
            )

        last = threads[-1] if has_more else None
        cursor = f"{last.last_event_at.isoformat()}|{last.id}" if last else None
        result = S2C_ThreadListResult(
            id=_generate_id(),
            payload=ThreadListResultPayload(
                threads=threads,
                cursor=cursor,
                total_count=total_count,
            ),
        )
        await self._send(ws, result)

    async def _handle_thread_subscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_ThreadSubscribe,
    ) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        thread_id = frame.payload.thread_id
        thread = await self._persistence.get_thread(thread_id)

        if not thread:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_FOUND", "message": "Thread not found"},
            )
            return

        if thread.agent_id != self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Thread belongs to different agent"},
            )
            return

        events = await self._persistence.get_events(
            thread_id,
            after_event_id=frame.payload.after_event_id,
            limit=100,
        )

        self._subscribed_threads.add(thread_id)
        self._subscriptions[self._ws_id].add(thread_id)

        snapshot = S2C_ThreadSnapshot(
            id=_generate_id(),
            payload=ThreadSnapshotPayload(thread=thread, events=events),
        )
        await self._send(ws, snapshot)

    async def _handle_thread_unsubscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_ThreadUnsubscribe,
    ) -> None:
        thread_id = frame.payload.thread_id
        self._subscribed_threads.discard(thread_id)
        if self._ws_id in self._subscriptions:
            self._subscriptions[self._ws_id].discard(thread_id)
        await self._send_ack(ws, frame.id)

    async def _handle_thread_delete(self, ws: WebSocketProtocol, frame: C2S_ThreadDelete) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        thread_id = frame.payload.thread_id
        thread = await self._persistence.get_thread(thread_id)

        if not thread:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_FOUND", "message": "Thread not found"},
            )
            return

        if thread.agent_id != self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Thread belongs to different agent"},
            )
            return

        await self._persistence.delete_thread(thread_id)

        self._subscribed_threads.discard(thread_id)
        if self._ws_id in self._subscriptions:
            self._subscriptions[self._ws_id].discard(thread_id)

        deleted_frame = S2C_ThreadDeleted(
            id=_generate_id(),
            payload=ThreadDeletedPayload(thread_id=thread_id),
        )
        await self._broadcast_to_agent(deleted_frame)

        await self._send_ack(ws, frame.id)

    async def _handle_event_create(self, ws: WebSocketProtocol, frame: C2S_EventCreate) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        msgs_per_min = int(self._rate_limits.get("messagesPerMinute") or 0)
        if msgs_per_min > 0:
            now = time.monotonic()
            cutoff = now - 60.0
            while self._event_timestamps and self._event_timestamps[0] < cutoff:
                self._event_timestamps.popleft()
            if len(self._event_timestamps) >= msgs_per_min:
                await self._send_ack(
                    ws,
                    frame.id,
                    ok=False,
                    error={"code": "RATE_LIMITED", "message": f"Limit {msgs_per_min}/min exceeded"},
                )
                return
            self._event_timestamps.append(now)

        content_parts = list(frame.payload.event.content)

        user = self._user or User(user_id="")

        context = HandlerContext(
            handler=self._handler,
            agent_id=self._agent_id,
            message="",
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            user=user,
            thread_id=frame.payload.thread_id,
            content=content_parts,
            data=frame.payload.event.data,
            client_event_id=frame.payload.client_event_id,
            event_type=frame.payload.event.type,
            metadata=frame.payload.metadata if not frame.payload.thread_id else None,
        )

        try:
            result = await self._pipeline.setup(context)
        except UploadReferenceError as e:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "UPLOAD_NOT_FOUND", "message": str(e)},
            )
            return
        except ValueError:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_FOUND", "message": "Thread not found"},
            )
            return
        except PermissionError:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Thread belongs to different agent"},
            )
            return

        if result.is_new_thread:
            self._subscribed_threads.add(result.thread.id)
            self._subscriptions[self._ws_id].add(result.thread.id)

        # Upsert participant into thread's users array
        if user.user_id or user.user_email:
            await self._persistence.upsert_thread_participant(result.thread.id, user)

        await self._send_ack(ws, frame.id)

        try:
            async for _event in self._pipeline.execute(context, result):
                pass
        except HandlerError:
            pass  # Pipeline already handled cleanup (error event, run failure, broadcast)

    async def _handle_event_list(self, ws: WebSocketProtocol, frame: C2S_EventList) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        payload = frame.payload
        limit = payload.limit
        events = await self._persistence.get_events(
            payload.thread_id,
            before_event_id=payload.before_event_id,
            limit=limit + 1,
            order="desc",
        )

        has_more = len(events) > limit
        if has_more:
            events = events[:limit]

        # Reverse to chronological order
        events.reverse()

        cursor = events[0].id if events and has_more else None
        result = S2C_EventListResult(
            id=_generate_id(),
            payload=EventListResultPayload(
                thread_id=payload.thread_id,
                events=events,
                cursor=cursor,
                has_more=has_more,
            ),
        )
        await self._send(ws, result)

    async def _handle_analytics_subscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_AnalyticsSubscribe,
    ) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        agent_id = frame.payload.agent_id
        if agent_id != self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Cannot subscribe to other agent's analytics"},
            )
            return

        if agent_id not in self._analytics_subscriptions:
            self._analytics_subscriptions[agent_id] = set()
        self._analytics_subscriptions[agent_id].add(self._ws_id)

        logger.info(
            "[agents] analytics subscribed: ws=%s, agent=%s, total_subscribers=%d",
            self._ws_id,
            agent_id,
            len(self._analytics_subscriptions[agent_id]),
        )

        await self._send_ack(ws, frame.id)

    async def _handle_analytics_unsubscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_AnalyticsUnsubscribe,
    ) -> None:
        agent_id = frame.payload.agent_id
        logger.info(
            "[agents] analytics unsubscribed: ws=%s, agent=%s",
            self._ws_id,
            agent_id,
        )
        if agent_id in self._analytics_subscriptions:
            self._analytics_subscriptions[agent_id].discard(self._ws_id)
            if not self._analytics_subscriptions[agent_id]:
                del self._analytics_subscriptions[agent_id]

        await self._send_ack(ws, frame.id)

    async def _handle_feedback(
        self,
        ws: WebSocketProtocol,
        frame: C2S_Feedback,
    ) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        if self._analytics_collector:
            user = self._user or User(user_id="")
            # Derive thread_id only when the feedback directly targets a thread.
            thread_id = frame.payload.target_id if frame.payload.target_type == "thread" else None
            await self._analytics_collector.feedback(
                feedback_type=frame.payload.feedback,
                target_type=frame.payload.target_type,
                target_id=frame.payload.target_id,
                agent_id=self._agent_id,
                user_id=user.user_id,
                reason=frame.payload.reason,
                classification=frame.payload.classification,
                thread_id=thread_id,
                organization_id=self._organization_id,
                workspace_id=self._workspace_id,
            )

        ack = S2C_FeedbackAck(
            id=_generate_id(),
            payload=FeedbackAckPayload(
                target_type=frame.payload.target_type,
                target_id=frame.payload.target_id,
                feedback=frame.payload.feedback,
                ok=True,
            ),
        )
        await self._send(ws, ack)

    async def _handle_settings_subscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsSubscribe,
    ) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        agent_id = frame.payload.agent_id
        if agent_id != self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Cannot subscribe to other agent's settings"},
            )
            return

        if not self._settings_schema:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONFIGURED", "message": "No settings schema configured"},
            )
            return

        audience = frame.payload.audience
        if audience == "creator":
            if not self._user or not _can_access_creator_settings(
                self._user,
                agent_owner_user_id=self._agent_owner_user_id,
            ):
                await self._send_ack(
                    ws,
                    frame.id,
                    ok=False,
                    error={
                        "code": "FORBIDDEN",
                        "message": "Creator-level settings require org admin or agent owner",
                    },
                )
                return

        if agent_id not in self._settings_subscriptions:
            self._settings_subscriptions[agent_id] = set()
        self._settings_subscriptions[agent_id].add(self._ws_id)

        self._settings_ws_context[self._ws_id] = {
            "agent_id": agent_id,
            "audience": audience,
            "ws": ws,
        }

        if self._settings_broadcaster:
            values = await self._persistence.get_settings(agent_id)
            await self._settings_broadcaster.broadcast_snapshot_to_ws(
                agent_id=agent_id,
                ws=ws,
                schema=self._settings_schema,
                values=values,
                audience=audience,
            )

        await self._send_ack(ws, frame.id)

    async def _handle_settings_unsubscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsUnsubscribe,
    ) -> None:
        agent_id = frame.payload.agent_id
        if agent_id in self._settings_subscriptions:
            self._settings_subscriptions[agent_id].discard(self._ws_id)
            if not self._settings_subscriptions[agent_id]:
                del self._settings_subscriptions[agent_id]

        self._settings_ws_context.pop(self._ws_id, None)

        await self._send_ack(ws, frame.id)

    async def _handle_settings_patch(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsPatch,
    ) -> None:
        if not self._agent_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        if not self._settings_schema:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONFIGURED", "message": "No settings schema configured"},
            )
            return

        field_id = frame.payload.field_id
        value = frame.payload.value

        field = self._settings_schema.get_field(field_id)
        if not field:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_FOUND", "message": f"Unknown field: {field_id}"},
            )
            return

        if field.readonly:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "READONLY", "message": f"Field '{field_id}' is readonly"},
            )
            return

        if field.visibility == SettingsFieldVisibility.INTERNAL:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "INTERNAL", "message": f"Field '{field_id}' is internal"},
            )
            return

        ctx = self._settings_ws_context.get(self._ws_id)
        effective_audience = ctx.get("audience", "user") if ctx else "user"
        if field.visibility == SettingsFieldVisibility.CREATOR and effective_audience != "creator":
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={
                    "code": "FORBIDDEN",
                    "message": "Subscribe with audience=creator before patching creator settings",
                },
            )
            return
        if field.visibility == SettingsFieldVisibility.USER and effective_audience not in (
            "user",
            "creator",
        ):
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={
                    "code": "FORBIDDEN",
                    "message": "Subscribe with audience=user or audience=creator before patching user settings",
                },
            )
            return

        if field.visibility == SettingsFieldVisibility.USER and effective_audience == "user" and not field.user_editable:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={
                    "code": "FORBIDDEN",
                    "message": "This field is fixed at template level; only the agent owner or organization admin can change it",
                },
            )
            return

        await self._persistence.update_setting(self._agent_id, field_id, value)

        if self._settings_broadcaster:
            await self._settings_broadcaster.broadcast_patch(
                agent_id=self._agent_id,
                field_id=field_id,
                value=value,
            )

        await self._send_ack(ws, frame.id)

    async def _handle_settings_public_schema(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsPublicSchema,
    ) -> None:
        if not self._settings_schema:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONFIGURED", "message": "No settings schema configured"},
            )
            return
        public = self._settings_schema.to_public_http_dict()
        result = S2C_SettingsPublicSchemaResult(
            id=frame.id,
            payload=SettingsPublicSchemaResultPayload(schema=public),
        )
        await self._send(ws, result)

    async def _handle_settings_seed(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsSeed,
    ) -> None:
        """
        Authorize seed using a bcrypt hash stored per worker_id in the runtime DB (not in core).
        Core sends the creator's API key in `seed_secret`; on first success we persist a hash so
        later seeds must match. Optional `next_seed_secret` rotates the stored hash after apply.
        If AGENT_SEED_SECRET is set, it can still authorize (bootstrap / legacy).
        """
        worker_id = frame.payload.worker_id
        incoming = frame.payload.seed_secret.encode("utf-8")

        stored = await self._persistence.get_worker_seed_hash_bytes(worker_id)
        bootstrap = (self._agent_seed_secret or "").strip()

        authorized = False
        if stored is not None:
            if bcrypt.checkpw(incoming, stored):
                authorized = True
            elif bootstrap and frame.payload.seed_secret == bootstrap:
                authorized = True
        elif bootstrap:
            authorized = frame.payload.seed_secret == bootstrap
        else:
            authorized = len(frame.payload.seed_secret) > 0

        if not authorized:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Invalid seed_secret"},
            )
            return

        await self._persistence.set_settings(worker_id, frame.payload.values)

        to_store = frame.payload.next_seed_secret or frame.payload.seed_secret
        new_hash = bcrypt.hashpw(to_store.encode("utf-8"), bcrypt.gensalt())
        await self._persistence.set_worker_seed_hash_bytes(worker_id, new_hash)

        await self._send_ack(ws, frame.id)
