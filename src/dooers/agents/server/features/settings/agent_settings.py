from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import is_personal_user, merge_user_values, user_settings_key

if TYPE_CHECKING:
    from ..persistence.base import Persistence
    from .broadcaster import SettingsBroadcaster
    from .models import SettingsSchema


class AgentSettings:
    def __init__(
        self,
        agent_id: str,
        schema: SettingsSchema,
        persistence: Persistence,
        broadcaster: SettingsBroadcaster,
        user_id: str | None = None,
    ) -> None:
        self._agent_id = agent_id
        # The person this turn runs for: their own values for USER fields win over the agent's.
        self._user_id = user_id if is_personal_user(user_id) else None
        self._schema = schema
        self._persistence = persistence
        self._broadcaster = broadcaster

    @property
    def schema(self) -> SettingsSchema:
        return self._schema

    async def _values(self) -> dict[str, Any]:
        agent_values = await self._persistence.get_settings(self._agent_id)
        if not self._user_id:
            return agent_values
        user_values = await self._persistence.get_settings(user_settings_key(self._agent_id, self._user_id))
        return merge_user_values(agent_values, user_values, self._schema)

    async def get(self, field_id: str) -> Any:
        field = self._schema.get_field(field_id)
        if not field:
            raise KeyError(f"Unknown field: {field_id}")

        values = await self._values()
        return values.get(field_id, field.value)

    async def get_all(self, *, exclude: list[str] | None = None) -> dict[str, Any]:
        defaults = self._schema.get_defaults()
        stored = await self._values()
        merged = {**defaults, **stored}
        if exclude:
            for key in exclude:
                merged.pop(key, None)
        return merged

    async def set(self, field_id: str, value: Any) -> None:
        self._validate_field(field_id, value)
        await self._persistence.update_setting(self._agent_id, field_id, value)
        await self._broadcaster.broadcast_patch(self._agent_id, field_id, value, schema=self._schema)

    def _validate_field(self, field_id: str, value: Any) -> None:
        field = self._schema.get_field(field_id)
        if not field:
            raise KeyError(f"Unknown field: {field_id}")
        if field.readonly:
            raise ValueError(f"Field '{field_id}' is readonly")
