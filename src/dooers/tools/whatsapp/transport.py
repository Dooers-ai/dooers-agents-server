"""HMAC-signed HTTP transport to dooers-service-whatsapp agent-runtime APIs."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from dooers.agents.server.features.channels.whatsapp.config import tools_base_url
from dooers.agents.server.features.channels.whatsapp.tool_hmac import (
    resolve_dooers_whatsapp_outbound_message_hmac,
)
from dooers.agents.server.persistence.base import Persistence
from dooers.tools.whatsapp.errors import WhatsAppToolsError

logger = logging.getLogger("dooers.tools.whatsapp.transport")


def build_x_message_sig(secret: str, body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


class WhatsAppTransport:
    """Low-level signed POST client (used by ``WhatsAppClient`` and outbound)."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    async def resolve_secret(self, agent_id: str, instance_id: str) -> str:
        secrets = await self._persistence.get_service_secrets(agent_id)
        secret = resolve_dooers_whatsapp_outbound_message_hmac(secrets, instance_id)
        if not secret:
            raise WhatsAppToolsError(
                f"no WhatsApp HMAC secret for agent_id={agent_id} instance_id={instance_id}"
            )
        return secret

    async def signed_post(
        self,
        agent_id: str,
        instance_id: str,
        path: str,
        payload: dict[str, Any],
    ) -> Any:
        secret = await self.resolve_secret(agent_id, instance_id)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Message-Signature": build_x_message_sig(secret, body),
        }
        url = f"{tools_base_url()}/api/v1{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WhatsAppToolsError(f"whatsapp tools request failed: {exc}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            logger.warning(
                "whatsapp tools %s returned %s: %s",
                path,
                response.status_code,
                response.text[:500],
            )
            raise WhatsAppToolsError(
                f"whatsapp tools {path} returned {response.status_code}: {response.text[:500]}"
            )
        if not response.content:
            return None
        return response.json()

    async def send_message_payload(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        instance_id = str(payload.get("instance_id") or "").strip()
        data = await self.signed_post(agent_id, instance_id, "/messages", payload)
        return data if isinstance(data, dict) else {"ok": True, "send_type": payload.get("send_type")}
