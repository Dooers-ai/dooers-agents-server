from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from dooers.exceptions import HandlerError
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
    S2C_ThreadDeleted,
    S2C_ThreadListResult,
    S2C_ThreadSnapshot,
    S2C_ThreadUpsert,
    ServerToClient,
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

logger = logging.getLogger("workers")


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
        assistant_name: str = "Assistant",
        analytics_subscriptions: dict[str, set[str]] | None = None,
        settings_subscriptions: dict[str, set[str]] | None = None,
        upload_store: UploadStore | None = None,
    ):
        self._persistence = persistence
        self._handler = handler
        self._registry = registry
        self._subscriptions = subscriptions

        self._analytics_collector = analytics_collector
        self._settings_broadcaster = settings_broadcaster
        self._settings_schema = settings_schema
        self._assistant_name = assistant_name
        self._analytics_subscriptions = analytics_subscriptions if analytics_subscriptions is not None else {}
        self._settings_subscriptions = settings_subscriptions if settings_subscriptions is not None else {}

        self._ws: WebSocketProtocol | None = None
        self._ws_id: str = _generate_id()
        self._worker_id: str | None = None
        self._user: User | None = None
        self._organization_id: str = ""
        self._workspace_id: str = ""
        self._subscribed_threads: set[str] = set()

        self._pipeline = HandlerPipeline(
            persistence=persistence,
            broadcast_callback=self._broadcast_to_worker_dict,
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
        error: dict | None = None,
    ) -> None:
        frame = S2C_Ack(
            id=_generate_id(),
            payload=AckPayload(ack_id=ack_id, ok=ok, error=error),
        )
        await self._send(ws, frame)

    async def _broadcast_to_worker(self, frame: ServerToClient) -> None:
        """Broadcast a frame to all connections for the current worker."""
        if not self._worker_id:
            return
        message = serialize_frame(frame)
        await self._registry.broadcast(self._worker_id, message)

    async def _broadcast_to_worker_except_self(self, ws: WebSocketProtocol, frame: ServerToClient) -> None:
        """Broadcast a frame to all connections for the current worker except this one."""
        if not self._worker_id:
            return
        message = serialize_frame(frame)
        await self._registry.broadcast_except(self._worker_id, ws, message)

    async def _broadcast_to_worker_dict(self, worker_id: str, payload: dict) -> None:
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
        await self._registry.broadcast(worker_id, message)

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

    async def cleanup(self) -> None:
        logger.info(
            "[workers] router cleanup: ws=%s, worker=%s, had_analytics_sub=%s",
            self._ws_id,
            self._worker_id,
            self._ws_id in self._analytics_subscriptions.get(self._worker_id or "", set()),
        )
        if self._worker_id:
            await self._registry.unregister(self._worker_id, self._ws)

            if self._worker_id in self._analytics_subscriptions:
                self._analytics_subscriptions[self._worker_id].discard(self._ws_id)
                if not self._analytics_subscriptions[self._worker_id]:
                    logger.info("[workers] analytics subscriptions emptied for worker %s — deleting key", self._worker_id)
                    del self._analytics_subscriptions[self._worker_id]

            if self._worker_id in self._settings_subscriptions:
                self._settings_subscriptions[self._worker_id].discard(self._ws_id)
                if not self._settings_subscriptions[self._worker_id]:
                    del self._settings_subscriptions[self._worker_id]

        if self._ws_id in self._subscriptions:
            del self._subscriptions[self._ws_id]

    async def _handle_connect(self, ws: WebSocketProtocol, frame: C2S_Connect) -> None:
        self._worker_id = frame.payload.worker_id
        self._organization_id = frame.payload.organization_id
        self._workspace_id = frame.payload.workspace_id
        # NOTE: Roles are currently trusted from the client. A future iteration should
        # validate them server-side using the auth_token (frame.payload.auth_token).
        self._user = frame.payload.user
        self._ws = ws

        await self._registry.register(self._worker_id, ws)

        self._subscriptions[self._ws_id] = set()

        await self._send_ack(ws, frame.id)

    async def _handle_thread_list(self, ws: WebSocketProtocol, frame: C2S_ThreadList) -> None:
        if not self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        user = self._user or User(user_id="")
        scope = resolve_scope(user)
        limit = frame.payload.limit or 30
        threads = await self._persistence.list_threads(
            worker_id=self._worker_id,
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            user_id=user.user_id,
            cursor=frame.payload.cursor,
            limit=limit + 1,
            scope=scope,
            user_email=user.user_email,
        )
        has_more = len(threads) > limit
        if has_more:
            threads = threads[:limit]

        if frame.payload.cursor:
            total_count = 0
        else:
            total_count = await self._persistence.count_threads(
                worker_id=self._worker_id,
                organization_id=self._organization_id,
                workspace_id=self._workspace_id,
                user_id=user.user_id,
                scope=scope,
                user_email=user.user_email,
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
        if not self._worker_id:
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

        if thread.worker_id != self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Thread belongs to different worker"},
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
        if not self._worker_id:
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

        if thread.worker_id != self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Thread belongs to different worker"},
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
        await self._broadcast_to_worker(deleted_frame)

        await self._send_ack(ws, frame.id)

    async def _handle_event_create(self, ws: WebSocketProtocol, frame: C2S_EventCreate) -> None:
        if not self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        content_parts = list(frame.payload.event.content)

        user = self._user or User(user_id="")

        context = HandlerContext(
            handler=self._handler,
            worker_id=self._worker_id,
            message="",
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            user=user,
            thread_id=frame.payload.thread_id,
            content=content_parts,
            data=frame.payload.event.data,
            client_event_id=frame.payload.client_event_id,
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
                error={"code": "FORBIDDEN", "message": "Thread belongs to different worker"},
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
        if not self._worker_id:
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
        if not self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        worker_id = frame.payload.worker_id
        if worker_id != self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Cannot subscribe to other worker's analytics"},
            )
            return

        if worker_id not in self._analytics_subscriptions:
            self._analytics_subscriptions[worker_id] = set()
        self._analytics_subscriptions[worker_id].add(self._ws_id)

        logger.info(
            "[workers] analytics subscribed: ws=%s, worker=%s, total_subscribers=%d",
            self._ws_id,
            worker_id,
            len(self._analytics_subscriptions[worker_id]),
        )

        await self._send_ack(ws, frame.id)

    async def _handle_analytics_unsubscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_AnalyticsUnsubscribe,
    ) -> None:
        worker_id = frame.payload.worker_id
        logger.info(
            "[workers] analytics unsubscribed: ws=%s, worker=%s",
            self._ws_id,
            worker_id,
        )
        if worker_id in self._analytics_subscriptions:
            self._analytics_subscriptions[worker_id].discard(self._ws_id)
            if not self._analytics_subscriptions[worker_id]:
                del self._analytics_subscriptions[worker_id]

        await self._send_ack(ws, frame.id)

    async def _handle_feedback(
        self,
        ws: WebSocketProtocol,
        frame: C2S_Feedback,
    ) -> None:
        if not self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        if self._analytics_collector:
            user = self._user or User(user_id="")
            await self._analytics_collector.feedback(
                feedback_type=frame.payload.feedback,
                target_type=frame.payload.target_type,
                target_id=frame.payload.target_id,
                worker_id=self._worker_id,
                user_id=user.user_id,
                reason=frame.payload.reason,
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
        if not self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "NOT_CONNECTED", "message": "Must connect first"},
            )
            return

        worker_id = frame.payload.worker_id
        if worker_id != self._worker_id:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "FORBIDDEN", "message": "Cannot subscribe to other worker's settings"},
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

        if worker_id not in self._settings_subscriptions:
            self._settings_subscriptions[worker_id] = set()
        self._settings_subscriptions[worker_id].add(self._ws_id)

        if self._settings_broadcaster:
            values = await self._persistence.get_settings(worker_id)
            await self._settings_broadcaster.broadcast_snapshot_to_ws(
                worker_id=worker_id,
                ws=ws,
                schema=self._settings_schema,
                values=values,
            )

        await self._send_ack(ws, frame.id)

    async def _handle_settings_unsubscribe(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsUnsubscribe,
    ) -> None:
        worker_id = frame.payload.worker_id
        if worker_id in self._settings_subscriptions:
            self._settings_subscriptions[worker_id].discard(self._ws_id)
            if not self._settings_subscriptions[worker_id]:
                del self._settings_subscriptions[worker_id]

        await self._send_ack(ws, frame.id)

    async def _handle_settings_patch(
        self,
        ws: WebSocketProtocol,
        frame: C2S_SettingsPatch,
    ) -> None:
        if not self._worker_id:
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

        if field.is_internal:
            await self._send_ack(
                ws,
                frame.id,
                ok=False,
                error={"code": "INTERNAL", "message": f"Field '{field_id}' is internal"},
            )
            return

        await self._persistence.update_setting(self._worker_id, field_id, value)

        if self._settings_broadcaster:
            await self._settings_broadcaster.broadcast_patch(
                worker_id=self._worker_id,
                field_id=field_id,
                value=value,
            )

        await self._send_ack(ws, frame.id)

    async def _track_event(
        self,
        event: str,
        thread_id: str | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        """Track an analytics event if collector is enabled."""
        if self._analytics_collector and self._worker_id:
            user = self._user or User(user_id="")
            await self._analytics_collector.track(
                event=event,
                worker_id=self._worker_id,
                thread_id=thread_id,
                user_id=user.user_id,
                run_id=run_id,
                event_id=event_id,
                data=data,
            )
