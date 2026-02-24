from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..persistence.base import Persistence
    from .broadcaster import SettingsBroadcaster
    from .models import SettingsSchema


class WorkerSettings:
    def __init__(
        self,
        worker_id: str,
        schema: SettingsSchema,
        persistence: Persistence,
        broadcaster: SettingsBroadcaster,
    ) -> None:
        self._worker_id = worker_id
        self._schema = schema
        self._persistence = persistence
        self._broadcaster = broadcaster

    async def get(self, field_id: str) -> Any:
        field = self._schema.get_field(field_id)
        if not field:
            raise KeyError(f"Unknown field: {field_id}")

        values = await self._persistence.get_settings(self._worker_id)
        return values.get(field_id, field.value)

    async def get_all(self, *, exclude: list[str] | None = None) -> dict[str, Any]:
        defaults = self._schema.get_defaults()
        stored = await self._persistence.get_settings(self._worker_id)
        merged = {**defaults, **stored}
        if exclude:
            for key in exclude:
                merged.pop(key, None)
        return merged

    async def set(self, field_id: str, value: Any) -> None:
        self._validate_field(field_id, value)
        await self._persistence.update_setting(self._worker_id, field_id, value)
        await self._broadcaster.broadcast_patch(self._worker_id, field_id, value, schema=self._schema)

    def _validate_field(self, field_id: str, value: Any) -> None:
        field = self._schema.get_field(field_id)
        if not field:
            raise KeyError(f"Unknown field: {field_id}")
        if field.readonly:
            raise ValueError(f"Field '{field_id}' is readonly")
