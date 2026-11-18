"""Default outbound: POST to Dooers WhatsApp tools ``/api/v1/messages`` (HMAC body)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from dooers.agents.server.handlers.pipeline import HandlerContext
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.persistence.base import Persistence
from dooers.tools.whatsapp.delivery import deliver_send_event
from dooers.tools.whatsapp.errors import WhatsAppToolsError
from dooers.tools.whatsapp.transport import WhatsAppTransport

logger = logging.getLogger("agents.whatsapp.outbound")

WhatsappOutboundCallback: TypeAlias = Callable[
    [AgentEvent, HandlerContext], Awaitable[dict[str, Any] | None]
]


def _failed(message: str, error_code: Any = None, wa_message_id: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "failed", "error_message": message[:500]}
    if error_code is not None:
        out["error_code"] = error_code
    if isinstance(wa_message_id, str) and wa_message_id.strip():
        out["wa_message_id"] = wa_message_id.strip()
    return out


def _outcome_from_error(exc: WhatsAppToolsError) -> dict[str, Any]:
    text = str(exc)
    start = text.find("{")
    if start >= 0:
        try:
            parsed = json.loads(text[start:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return _failed(
                str(parsed.get("error_message") or parsed.get("error") or "Falha ao enviar pelo WhatsApp."),
                error_code=parsed.get("error_code"),
                wa_message_id=parsed.get("message_id"),
            )
    return _failed("Falha ao enviar pelo WhatsApp.")


def _outcome_from_body(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("ok") is False or body.get("accepted") is False:
        return _failed(
            str(body.get("error_message") or "Falha ao enviar pelo WhatsApp."),
            error_code=body.get("error_code"),
            wa_message_id=body.get("message_id"),
        )
    wa_message_id = body.get("message_id")
    out: dict[str, Any] = {"status": "accepted"}
    if isinstance(wa_message_id, str) and wa_message_id.strip():
        out["wa_message_id"] = wa_message_id.strip()
    return out


def create_dooers_whatsapp_outbound(persistence: Persistence) -> WhatsappOutboundCallback:
    """Return outbound callback; HMAC secret comes from persisted ``services_secrets`` only (no env)."""
    transport = WhatsAppTransport(persistence)

    async def _post_outbound(event: AgentEvent, context: HandlerContext) -> dict[str, Any] | None:
        try:
            sent = await deliver_send_event(transport, context.agent_id, event)
        except WhatsAppToolsError as exc:
            msg = str(exc)
            w = event.data.get("whatsapp") or {}
            instance_id = str(w.get("instance_id") or "").strip()
            if msg.startswith("no WhatsApp HMAC secret"):
                logger.info(
                    "dooers whatsapp outbound skipped (no HMAC in service_secrets for agent_id=%s instance_id=%s); "
                    "ensure settings.merge_service_secrets ran for this worker (tools create / runtime seed).",
                    context.agent_id,
                    instance_id or "-",
                )
                return _failed("WhatsApp não configurado para este agente.")
            if "request failed" in msg:
                logger.error("dooers whatsapp tools outbound failed: %s", exc)
                return _failed("Falha ao contactar o serviço de WhatsApp.")
            logger.warning("dooers whatsapp tools returned error: %s", exc)
            return _outcome_from_error(exc)
        if not sent:
            logger.info(
                "dooers whatsapp outbound skipped (missing to_e164/instance_id on event) agent_id=%s send_type=%s",
                context.agent_id,
                event.send_type,
            )
            return None
        return _outcome_from_body(sent)

    return _post_outbound
