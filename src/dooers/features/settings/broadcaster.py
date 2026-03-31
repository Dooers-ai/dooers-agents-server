from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from dooers.registry import ConnectionRegistry, WebSocketProtocol

    from .models import SettingsSchema

logger = logging.getLogger(__name__)


class SettingsBroadcaster:
    def __init__(
        self,
        registry: ConnectionRegistry,
        subscriptions: dict[str, set[str]],  # worker_id -> set of ws_ids
        ws_context: dict[str, dict[str, Any]],
    ) -> None:
        self._registry = registry
        self._subscriptions = subscriptions
        self._ws_context = ws_context

    async def broadcast_snapshot_to_ws(
        self,
        worker_id: str,
        ws: WebSocketProtocol,
        schema: SettingsSchema,
        values: dict[str, Any],
        audience: Literal["creator", "user"] = "user",
    ) -> None:
        from dooers.protocol.frames import S2C_SettingsSnapshot, SettingsSnapshotPayload

        from .models import SettingsField, SettingsFieldGroup

        public_items = schema.get_fields_for_audience(audience)

        def _inject_values(field: SettingsField) -> SettingsField:
            if field.id in values:
                return field.model_copy(update={"value": values[field.id]})
            return field

        items_with_values: list[SettingsField | SettingsFieldGroup] = []
        for item in public_items:
            if isinstance(item, SettingsFieldGroup):
                updated_fields = [_inject_values(f) for f in item.fields]
                items_with_values.append(item.model_copy(update={"fields": updated_fields}))
            else:
                items_with_values.append(_inject_values(item))

        message = S2C_SettingsSnapshot(
            id=str(uuid4()),
            payload=SettingsSnapshotPayload(
                worker_id=worker_id,
                fields=items_with_values,
                updated_at=datetime.now(UTC),
            ),
        )

        try:
            await ws.send_text(message.model_dump_json())
        except Exception:
            logger.warning("[workers] failed to send settings snapshot")

    async def broadcast_patch(
        self,
        worker_id: str,
        field_id: str,
        value: Any,
        exclude_ws: WebSocketProtocol | None = None,
        schema: SettingsSchema | None = None,
    ) -> None:
        from dooers.protocol.frames import S2C_SettingsPatch, SettingsPatchBroadcastPayload

        from .models import SettingsFieldVisibility

        if schema:
            field = schema.get_field(field_id)
            if field and field.visibility == SettingsFieldVisibility.INTERNAL:
                return

        message = S2C_SettingsPatch(
            id=str(uuid4()),
            payload=SettingsPatchBroadcastPayload(
                worker_id=worker_id,
                field_id=field_id,
                value=value,
                updated_at=datetime.now(UTC),
            ),
        )
        message_json = message.model_dump_json()

        field_vis = None
        if schema:
            f = schema.get_field(field_id)
            if f:
                field_vis = f.visibility

        sent = False
        has_ctx_for_worker = any(
            ctx.get("worker_id") == worker_id for ctx in self._ws_context.values()
        )
        for _ws_id, ctx in list(self._ws_context.items()):
            if ctx.get("worker_id") != worker_id:
                continue
            if field_vis is not None:
                aud = ctx.get("audience", "user")
                if field_vis == SettingsFieldVisibility.CREATOR and aud != "creator":
                    continue
                if field_vis == SettingsFieldVisibility.USER and aud not in ("user", "creator"):
                    continue
            ws = ctx.get("ws")
            if ws is None or ws is exclude_ws:
                continue
            try:
                await ws.send_text(message_json)
                sent = True
            except Exception:
                logger.warning("Failed to send settings patch to subscriber")

        if sent or has_ctx_for_worker:
            return

        connections = self._registry.get_connections(worker_id)
        for ws in connections:
            if ws is not exclude_ws:
                try:
                    await ws.send_text(message_json)
                except Exception:
                    logger.warning("Failed to send settings patch to subscriber")
