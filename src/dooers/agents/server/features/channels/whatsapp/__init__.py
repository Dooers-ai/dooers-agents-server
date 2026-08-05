"""WhatsApp channel: thread ids and built-in Dooers tools outbound."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dooers.agents.server.features.channels.whatsapp.config import tools_base_url
from dooers.agents.server.features.channels.whatsapp.thread_id import normalize_e164, whatsapp_thread_id
from dooers.agents.server.features.channels.whatsapp.tool_hmac import (
    dooers_whatsapp_hmac_key_fingerprint,
    parse_dooers_whatsapp_instance_hmac_map,
    verify_dooers_whatsapp_tool_inbound_signature,
    verify_dooers_whatsapp_tool_inbound_with_persistence,
)

if TYPE_CHECKING:
    from dooers.agents.server.features.channels.whatsapp.client import WhatsappToolsClient, WhatsappToolsError
    from dooers.agents.server.features.channels.whatsapp.outbound import (
        WhatsappOutboundCallback,
        create_dooers_whatsapp_outbound,
    )

__all__ = [
    "WhatsappOutboundCallback",
    "WhatsappToolsClient",
    "WhatsappToolsError",
    "create_dooers_whatsapp_outbound",
    "dooers_whatsapp_hmac_key_fingerprint",
    "normalize_e164",
    "parse_dooers_whatsapp_instance_hmac_map",
    "tools_base_url",
    "verify_dooers_whatsapp_tool_inbound_signature",
    "verify_dooers_whatsapp_tool_inbound_with_persistence",
    "whatsapp_thread_id",
]


def __getattr__(name: str) -> object:
    if name in ("WhatsappToolsClient", "WhatsappToolsError"):
        from dooers.agents.server.features.channels.whatsapp.client import WhatsappToolsClient, WhatsappToolsError

        return WhatsappToolsClient if name == "WhatsappToolsClient" else WhatsappToolsError
    if name in ("WhatsappOutboundCallback", "create_dooers_whatsapp_outbound"):
        from dooers.agents.server.features.channels.whatsapp.outbound import (
            WhatsappOutboundCallback,
            create_dooers_whatsapp_outbound,
        )

        return WhatsappOutboundCallback if name == "WhatsappOutboundCallback" else create_dooers_whatsapp_outbound
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
