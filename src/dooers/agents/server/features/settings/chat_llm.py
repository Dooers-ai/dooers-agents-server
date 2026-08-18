"""Reserved ``llm_models`` settings field and per-turn chat model override."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dooers.agents.server.features.settings.models import SettingsFieldType

if TYPE_CHECKING:
    from dooers.agents.server.features.settings.models import SettingsSchema
    from dooers.agents.server.protocol.models import ChatContext

LLM_MODELS_FIELD_ID = "llm_models"


def resolve_chat_llm_override(
    chat_context: ChatContext | None,
    *,
    schema: SettingsSchema | None,
) -> str | None:
    """Return ``chat_context.llm_model`` when it is in the schema ``llm_models`` options."""

    if chat_context is None or schema is None:
        return None
    requested = (chat_context.llm_model or "").strip()
    if not requested:
        return None
    field = schema.get_field(LLM_MODELS_FIELD_ID)
    if field is None or field.type != SettingsFieldType.SELECT:
        return None
    allowed = {opt.value for opt in (field.options or []) if opt.value}
    if requested in allowed:
        return requested
    return None


def apply_chat_llm_override(
    settings_values: dict[str, Any],
    *,
    chat_context: ChatContext | None,
    schema: SettingsSchema | None,
) -> dict[str, Any]:
    """Copy ``settings_values``, replacing ``llm_model`` when the chat override is allowlisted."""

    override = resolve_chat_llm_override(chat_context, schema=schema)
    if not override:
        return settings_values
    return {**settings_values, "llm_model": override}
