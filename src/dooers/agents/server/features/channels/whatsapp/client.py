"""Signed HTTP client for Dooers WhatsApp tools agent-runtime endpoints (legacy alias)."""

from __future__ import annotations

from typing import Any

from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.persistence.base import Persistence
from dooers.tools.whatsapp.delivery import deliver_send_event
from dooers.tools.whatsapp.errors import WhatsAppToolsError
from dooers.tools.whatsapp.transport import WhatsAppTransport

__all__ = ["WhatsappToolsClient", "WhatsappToolsError"]

WhatsappToolsError = WhatsAppToolsError


class WhatsappToolsClient:
    """Call WhatsApp tools runtime APIs (HMAC-signed, same secret as outbound messages)."""

    def __init__(self, persistence: Persistence) -> None:
        self._transport = WhatsAppTransport(persistence)

    async def list_templates(
        self,
        agent_id: str,
        instance_id: str,
        *,
        status: str | None = None,
        language: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "instance_id": instance_id,
            "agent_id": agent_id,
        }
        if status:
            payload["status"] = status
        if language:
            payload["language"] = language
        if name:
            payload["name"] = name
        data = await self._transport.signed_post(
            agent_id, instance_id, "/agent/templates/list", payload
        )
        return data if isinstance(data, list) else []

    async def get_template(
        self,
        agent_id: str,
        instance_id: str,
        template_id: str,
    ) -> dict[str, Any]:
        payload = {
            "instance_id": instance_id,
            "agent_id": agent_id,
            "template_id": template_id,
        }
        data = await self._transport.signed_post(
            agent_id, instance_id, "/agent/templates/get", payload
        )
        return data if isinstance(data, dict) else {"raw": data}

    async def create_template(
        self,
        agent_id: str,
        instance_id: str,
        *,
        name: str,
        language: str,
        category: str,
        components: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "instance_id": instance_id,
            "agent_id": agent_id,
            "name": name,
            "language": language,
            "category": category,
            "components": components,
        }
        data = await self._transport.signed_post(
            agent_id, instance_id, "/agent/templates/create", payload
        )
        return data if isinstance(data, dict) else {"raw": data}

    async def send_message(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._transport.send_message_payload(agent_id, payload)

    async def send_event(self, agent_id: str, event: AgentEvent) -> bool:
        return await deliver_send_event(self._transport, agent_id, event)
