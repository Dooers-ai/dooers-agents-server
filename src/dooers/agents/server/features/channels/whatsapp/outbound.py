"""Default outbound: POST to Dooers WhatsApp tools ``/api/v1/messages`` (HMAC body)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

import httpx

from dooers.agents.server.features.channels.whatsapp.config import tools_base_url
from dooers.agents.server.features.channels.whatsapp.tool_hmac import (
    resolve_dooers_whatsapp_outbound_message_hmac,
)
from dooers.agents.server.handlers.pipeline import HandlerContext
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.persistence.base import Persistence

logger = logging.getLogger("agents.whatsapp.outbound")

WhatsappOutboundCallback: TypeAlias = Callable[[AgentEvent, HandlerContext], Awaitable[None]]


def _build_x_message_sig(secret: str, body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _body_for_event(event: AgentEvent) -> dict[str, Any] | None:
    w = event.data.get("whatsapp") or {}
    to_e164 = w.get("to_e164")
    instance_id = w.get("instance_id")
    if not to_e164 or not instance_id:
        return None
    st = event.send_type
    payload: dict[str, Any] = {
        "instance_id": instance_id,
        "to_e164": to_e164,
        "send_type": st,
    }
    d = event.data
    if st == "text":
        payload["text"] = d.get("text", "")
    elif st == "image":
        payload["url"] = d.get("url")
        payload["mime_type"] = d.get("mime_type")
    elif st == "document":
        payload["url"] = d.get("url")
        payload["filename"] = d.get("filename")
        payload["mime_type"] = d.get("mime_type")
    elif st == "audio":
        payload["url"] = d.get("url")
        payload["mime_type"] = d.get("mime_type")
        payload["duration"] = d.get("duration")
    elif st == "contact":
        payload["display_name"] = d.get("display_name") or ""
        payload["vcard"] = d.get("vcard")
        payload["phones"] = d.get("phones") or []
    else:
        return None
    return payload


def create_dooers_whatsapp_outbound(persistence: Persistence) -> WhatsappOutboundCallback:
    """Return outbound callback; HMAC secret comes from persisted ``services_secrets`` only (no env)."""

    async def _post_outbound(event: AgentEvent, context: HandlerContext) -> None:
        secrets = await persistence.get_service_secrets(context.agent_id)
        w = event.data.get("whatsapp") or {}
        instance_id = str(w.get("instance_id") or "").strip()
        secret = resolve_dooers_whatsapp_outbound_message_hmac(secrets, instance_id)
        if not secret:
            logger.info(
                "dooers whatsapp outbound skipped (no HMAC in service_secrets for agent_id=%s instance_id=%s); "
                "ensure settings.merge_service_secrets ran for this worker (tools create / runtime seed).",
                context.agent_id,
                instance_id or "-",
            )
            return
        payload = _body_for_event(event)
        if not payload:
            logger.info(
                "dooers whatsapp outbound skipped (missing to_e164/instance_id on event) agent_id=%s send_type=%s",
                context.agent_id,
                event.send_type,
            )
            return
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        base = tools_base_url()
        url = f"{base}/api/v1/messages"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Message-Signature": _build_x_message_sig(secret, body),
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url, content=body, headers=headers)
            if r.status_code < 200 or r.status_code >= 300:
                logger.warning("dooers whatsapp tools returned %s: %s", r.status_code, r.text[:500])
        except httpx.HTTPError as e:
            logger.error("dooers whatsapp tools outbound failed: %s", e)

    return _post_outbound
