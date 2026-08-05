"""Public ``WhatsAppClient`` for programmatic WhatsApp tools access."""

from __future__ import annotations

from typing import Any

from dooers.tools.whatsapp.errors import WhatsAppToolsError
from dooers.tools.whatsapp.models import WhatsAppInstance
from dooers.tools.whatsapp.runtime import require_agent_id, require_persistence
from dooers.tools.whatsapp.secrets import list_provisioned_instance_ids
from dooers.tools.whatsapp.transport import WhatsAppTransport

__all__ = ["WhatsAppClient", "WhatsAppToolsError"]


def _resolve_instance_id(default: str, override: str | None) -> str:
    iid = (override if override is not None else default).strip()
    if not iid:
        raise WhatsAppToolsError("instance_id is required")
    return iid


class _InstancesAPI:
    def __init__(self, transport: WhatsAppTransport, agent_id: str, persistence) -> None:
        self._transport = transport
        self._agent_id = agent_id
        self._persistence = persistence

    async def list(self, *, with_details: bool = True) -> list[WhatsAppInstance]:
        ids = await list_provisioned_instance_ids(self._persistence, self._agent_id)
        if not with_details:
            return [WhatsAppInstance(instance_id=iid) for iid in ids]
        out: list[WhatsAppInstance] = []
        for iid in ids:
            try:
                out.append(await self.get(iid))
            except WhatsAppToolsError:
                out.append(WhatsAppInstance(instance_id=iid))
        return out

    async def get(self, instance_id: str) -> WhatsAppInstance:
        iid = instance_id.strip()
        payload = {"instance_id": iid, "agent_id": self._agent_id}
        data = await self._transport.signed_post(
            self._agent_id,
            iid,
            "/agent/instances/get",
            payload,
        )
        if not isinstance(data, dict):
            return WhatsAppInstance(instance_id=iid)
        return WhatsAppInstance(
            instance_id=str(data.get("instance_id") or iid),
            phone_number=data.get("phone_number") if data.get("phone_number") else None,
            status=str(data.get("status") or "") or None,
        )


class _TemplatesAPI:
    def __init__(self, transport: WhatsAppTransport, agent_id: str, default_instance_id: str) -> None:
        self._transport = transport
        self._agent_id = agent_id
        self._default_instance_id = default_instance_id

    async def list(
        self,
        instance_id: str | None = None,
        *,
        status: str | None = None,
        language: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        iid = _resolve_instance_id(self._default_instance_id, instance_id)
        payload: dict[str, Any] = {
            "instance_id": iid,
            "agent_id": self._agent_id,
        }
        if status:
            payload["status"] = status
        if language:
            payload["language"] = language
        if name:
            payload["name"] = name
        data = await self._transport.signed_post(
            self._agent_id, iid, "/agent/templates/list", payload
        )
        return data if isinstance(data, list) else []

    async def get(self, template_id: str, instance_id: str | None = None) -> dict[str, Any]:
        iid = _resolve_instance_id(self._default_instance_id, instance_id)
        payload = {
            "instance_id": iid,
            "agent_id": self._agent_id,
            "template_id": template_id,
        }
        data = await self._transport.signed_post(
            self._agent_id, iid, "/agent/templates/get", payload
        )
        return data if isinstance(data, dict) else {"raw": data}

    async def create(
        self,
        instance_id: str | None = None,
        *,
        name: str,
        language: str,
        category: str,
        components: list[dict[str, Any]],
    ) -> dict[str, Any]:
        iid = _resolve_instance_id(self._default_instance_id, instance_id)
        payload = {
            "instance_id": iid,
            "agent_id": self._agent_id,
            "name": name,
            "language": language,
            "category": category,
            "components": components,
        }
        data = await self._transport.signed_post(
            self._agent_id, iid, "/agent/templates/create", payload
        )
        return data if isinstance(data, dict) else {"raw": data}

    async def send(
        self,
        instance_id: str | None = None,
        *,
        to_e164: str,
        name: str,
        language: str,
        template_components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        iid = _resolve_instance_id(self._default_instance_id, instance_id)
        payload: dict[str, Any] = {
            "instance_id": iid,
            "to_e164": to_e164,
            "send_type": "template",
            "template_name": name,
            "template_language": language,
        }
        if template_components:
            payload["template_components"] = template_components
        return await self._transport.send_message_payload(self._agent_id, payload)


class _TextAPI:
    def __init__(self, transport: WhatsAppTransport, agent_id: str, default_instance_id: str) -> None:
        self._transport = transport
        self._agent_id = agent_id
        self._default_instance_id = default_instance_id

    async def send(
        self,
        instance_id: str | None = None,
        *,
        to_e164: str,
        text: str,
    ) -> dict[str, Any]:
        iid = _resolve_instance_id(self._default_instance_id, instance_id)
        payload = {
            "instance_id": iid,
            "to_e164": to_e164,
            "send_type": "text",
            "text": text,
        }
        return await self._transport.send_message_payload(self._agent_id, payload)


class WhatsAppClient:
    """Programmatic WhatsApp tools client (handler/dispatch turn required).

    Args:
        instance_id: WhatsApp tools instance id (from Studio Public Chats / agent settings).
    """

    def __init__(self, instance_id: str) -> None:
        self._instance_id = (instance_id or "").strip()
        if not self._instance_id:
            raise WhatsAppToolsError("instance_id is required")
        persistence = require_persistence()
        agent_id = require_agent_id()
        transport = WhatsAppTransport(persistence)
        self.instances = _InstancesAPI(transport, agent_id, persistence)
        self.templates = _TemplatesAPI(transport, agent_id, self._instance_id)
        self.text = _TextAPI(transport, agent_id, self._instance_id)

    @property
    def instance_id(self) -> str:
        return self._instance_id