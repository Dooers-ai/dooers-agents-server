"""Reserved ``llm_models`` settings field and per-turn chat model override."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dooers.agents.server.features.settings.models import SettingsFieldType

if TYPE_CHECKING:
    from dooers.agents.server.features.settings.models import SettingsSchema
    from dooers.agents.server.protocol.models import ChatContext

LLM_MODELS_FIELD_ID = "llm_models"

ReasoningEffort = Literal["none", "low", "medium", "high"]
ALLOWED_REASONING_EFFORT: frozenset[str] = frozenset({"none", "low", "medium", "high"})
DEFAULT_REASONING_EFFORT: ReasoningEffort = "medium"


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


def resolve_chat_reasoning_effort(chat_context: ChatContext | None) -> str | None:
    """Return ``chat_context.reasoning_effort`` when it is a known Responses API value."""

    if chat_context is None:
        return None
    requested = (chat_context.reasoning_effort or "").strip().lower()
    if requested in ALLOWED_REASONING_EFFORT:
        return requested
    return None


def apply_chat_llm_override(
    settings_values: dict[str, Any],
    *,
    chat_context: ChatContext | None,
    schema: SettingsSchema | None,
) -> dict[str, Any]:
    """Copy ``settings_values``, applying allowlisted chat overrides for this turn only."""

    out = settings_values
    override = resolve_chat_llm_override(chat_context, schema=schema)
    if override:
        out = {**out, "llm_model": override}
    effort = resolve_chat_reasoning_effort(chat_context)
    if effort:
        out = {**out, "reasoning_effort": effort}
    return out


# Maestro options are explicit public declarations. Legacy SettingsSchema values,
# defaults, provider keys and legacy reasoning aliases never enter this schema.

MaestroEffort = Literal["minimal", "low", "medium", "high", "max"]


class PublicModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model: str = Field(min_length=1, max_length=128, pattern=r"^[!-~]+$")
    efforts: tuple[MaestroEffort, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def distinct_efforts(self):
        if len(set(self.efforts)) != len(self.efforts):
            raise ValueError("Duplicate effort")
        return self


class PublicOptionsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    models: tuple[PublicModelOption, ...] = Field(min_length=1, max_length=100)
    max_options_per_participant: Literal[1] = 1
    application_supported: Literal[False] = False

    @model_validator(mode="after")
    def distinct_models(self):
        if len({item.model for item in self.models}) != len(self.models):
            raise ValueError("Duplicate model")
        return self
