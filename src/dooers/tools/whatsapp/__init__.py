"""Programmatic WhatsApp tools for Dooers agent handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dooers.tools.whatsapp.errors import WhatsAppToolsError
from dooers.tools.whatsapp.runtime import (
    WhatsAppRuntimeError,
    bind_handler_context,
    reset_handler_context,
)

if TYPE_CHECKING:
    from dooers.tools.whatsapp.client import WhatsAppClient
    from dooers.tools.whatsapp.models import WhatsAppInstance

__all__ = [
    "WhatsAppClient",
    "WhatsAppInstance",
    "WhatsAppRuntimeError",
    "WhatsAppToolsError",
    "bind_handler_context",
    "deliver_send_event",
    "reset_handler_context",
]


def __getattr__(name: str) -> object:
    if name == "WhatsAppClient":
        from dooers.tools.whatsapp.client import WhatsAppClient

        return WhatsAppClient
    if name == "WhatsAppInstance":
        from dooers.tools.whatsapp.models import WhatsAppInstance

        return WhatsAppInstance
    if name == "deliver_send_event":
        from dooers.tools.whatsapp.delivery import deliver_send_event

        return deliver_send_event
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
