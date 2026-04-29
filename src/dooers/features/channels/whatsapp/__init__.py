"""WhatsApp channel: thread ids and built-in Dooers tools outbound."""

from dooers.features.channels.whatsapp.config import tools_base_url
from dooers.features.channels.whatsapp.outbound import (
    WhatsappOutboundCallback,
    create_dooers_whatsapp_outbound,
)
from dooers.features.channels.whatsapp.thread_id import normalize_e164, whatsapp_thread_id
from dooers.features.channels.whatsapp.tool_hmac import (
    dooers_whatsapp_hmac_key_fingerprint,
    parse_dooers_whatsapp_instance_hmac_map,
    verify_dooers_whatsapp_tool_inbound_signature,
    verify_dooers_whatsapp_tool_inbound_with_persistence,
)

__all__ = [
    "WhatsappOutboundCallback",
    "create_dooers_whatsapp_outbound",
    "dooers_whatsapp_hmac_key_fingerprint",
    "normalize_e164",
    "parse_dooers_whatsapp_instance_hmac_map",
    "tools_base_url",
    "verify_dooers_whatsapp_tool_inbound_signature",
    "verify_dooers_whatsapp_tool_inbound_with_persistence",
    "whatsapp_thread_id",
]
