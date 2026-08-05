"""Read provisioned WhatsApp instance ids from agent ``service_secrets``."""

from __future__ import annotations

from dooers.agents.server.features.channels.whatsapp.tool_hmac import (
    parse_dooers_whatsapp_instance_hmac_map,
)
from dooers.agents.server.persistence.base import Persistence


async def list_provisioned_instance_ids(persistence: Persistence, agent_id: str) -> list[str]:
    secrets = await persistence.get_service_secrets(agent_id)
    raw = secrets.get("dooers_whatsapp_instance_hmac_json")
    parsed = parse_dooers_whatsapp_instance_hmac_map(raw)
    if not parsed:
        return []
    return list(parsed.keys())
