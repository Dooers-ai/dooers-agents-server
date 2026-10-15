"""Registered per-recipient turn options. No settings persistence or global overrides."""

from __future__ import annotations

import inspect
import math
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from dooers.agents.server.features.settings.chat_llm import PublicOptionsSchema

from .client import MaestroContextError


def recipient_options(trigger: dict[str, Any], envelope: dict[str, Any], schema: PublicOptionsSchema | None) -> dict[str, str]:
    """Read only the authenticated delivery recipient's original trigger block.

    The executor's operator supplies this schema for its exact MaestroConfig
    instance/runtime. A sibling's block is never a fallback. Resume continues to
    use the immutable original message, rather than mutable settings or history.
    """
    if trigger.get("type") != "message.created":
        return {}
    options = trigger["payload"].get("agent_options", [])
    selected = [item for item in options if item["participant_id"] == envelope["recipient_participant_id"]]
    if not selected:
        return {}
    if len(selected) != 1 or schema is None:
        raise MaestroContextError("UNSUPPORTED_CAPABILITY")
    option = selected[0]
    model = next((item for item in schema.models if item.model == option.get("model")), None)
    if model is None or ("effort" in option and option["effort"] not in model.efforts):
        raise MaestroContextError("UNSUPPORTED_CAPABILITY")
    result = {"llm_model": model.model}
    if "effort" in option:
        result["reasoning_effort"] = option["effort"]
    return result


class TurnSettings:
    """A private view for this handler only, preserving the existing settings port.

    Overrides cannot be persisted through this view. Other explicit settings writes
    retain the underlying port's validation and instance scoping.
    """

    def __init__(self, settings: Any, options: dict[str, str]):
        self._settings = settings
        self._options = deepcopy(options)

    @property
    def schema(self):
        return self._settings.schema

    async def get(self, field_id: str):
        if field_id in self._options:
            return self._options[field_id]
        return await self._settings.get(field_id)

    async def get_all(self, *, exclude: list[str] | None = None):
        values = {**await self._settings.get_all(), **self._options}
        return {key: value for key, value in values.items() if key not in (exclude or [])}

    async def set(self, field_id: str, value: Any):
        if field_id in self._options:
            raise ValueError("MAESTRO_TURN_OPTION_READONLY")
        await self._settings.set(field_id, value)


@dataclass(frozen=True)
class MaestroOptionsRegistration:
    """Private current registration, distinct from read-only public discovery.

    generation is a local registration generation, not wire runtime_version.
    The resolver must return the operator's current record or None on removal.
    """

    organization_id: str
    workspace_id: str | None
    instance_agent_id: str
    runtime_id: str
    generation: str
    valid_until: float
    schema: PublicOptionsSchema

    def __post_init__(self):
        for value in (self.organization_id, self.instance_agent_id, self.runtime_id, self.workspace_id):
            if value is not None and (not isinstance(value, str) or str(UUID(value)) != value):
                raise ValueError("MAESTRO_OPTIONS_REGISTRATION_INVALID")
        if (
            not isinstance(self.schema, PublicOptionsSchema)
            or not isinstance(self.generation, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.generation)
            or type(self.valid_until) not in (int, float)
            or not math.isfinite(self.valid_until)
            or self.valid_until <= 0
        ):
            raise ValueError("MAESTRO_OPTIONS_REGISTRATION_INVALID")
        object.__setattr__(self, "schema", self.schema.model_copy(deep=True))

    def assert_binding(self, envelope):
        if (
            any(getattr(self, field) != envelope[field] for field in ("organization_id", "workspace_id", "instance_agent_id", "runtime_id"))
            or time.time() >= self.valid_until
        ):
            raise MaestroContextError("UNSUPPORTED_CAPABILITY")


def prepare_recipient_options(trigger, envelope, resolver: Callable[[], MaestroOptionsRegistration | None] | None):
    """Pin a private current registration around awaited resource resolution.

    No registration lookup is required for an original message without this
    recipient's options; sibling blocks never affect a legacy recipient.
    """
    if not any(item["participant_id"] == envelope["recipient_participant_id"] for item in trigger["payload"].get("agent_options", [])):
        return {}, lambda: None

    def read():
        try:
            current = resolver() if resolver is not None else None
            if inspect.isawaitable(current):
                close = getattr(current, "close", None)
                if close is not None:
                    close()
                raise MaestroContextError("UNSUPPORTED_CAPABILITY")
            if not isinstance(current, MaestroOptionsRegistration):
                raise MaestroContextError("UNSUPPORTED_CAPABILITY")
            current.assert_binding(envelope)
            return current
        except MaestroContextError:
            raise
        except Exception:
            raise MaestroContextError("UNSUPPORTED_CAPABILITY") from None

    registered = read()
    selected = recipient_options(trigger, envelope, registered.schema)

    def assert_current():
        if read() != registered:
            raise MaestroContextError("ACCESS_REVOKED")

    return selected, assert_current
