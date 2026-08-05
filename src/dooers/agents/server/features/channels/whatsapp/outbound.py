"""Default outbound: POST to Dooers WhatsApp tools ``/api/v1/messages`` (HMAC body)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from dooers.agents.server.handlers.pipeline import HandlerContext
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.persistence.base import Persistence
from dooers.tools.whatsapp.delivery import deliver_send_event
from dooers.tools.whatsapp.errors import WhatsAppToolsError
from dooers.tools.whatsapp.transport import WhatsAppTransport

logger = logging.getLogger("agents.whatsapp.outbound")

WhatsappOutboundCallback: TypeAlias = Callable[[AgentEvent, HandlerContext], Awaitable[None]]


def create_dooers_whatsapp_outbound(persistence: Persistence) -> WhatsappOutboundCallback:
    """Return outbound callback; HMAC secret comes from persisted ``services_secrets`` only (no env)."""
    transport = WhatsAppTransport(persistence)

    async def _post_outbound(event: AgentEvent, context: HandlerContext) -> None:
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
                return
            if "request failed" in msg:
                logger.error("dooers whatsapp tools outbound failed: %s", exc)
                return
            logger.warning("dooers whatsapp tools returned error: %s", exc)
            return
        if not sent:
            logger.info(
                "dooers whatsapp outbound skipped (missing to_e164/instance_id on event) agent_id=%s send_type=%s",
                context.agent_id,
                event.send_type,
            )

    return _post_outbound
