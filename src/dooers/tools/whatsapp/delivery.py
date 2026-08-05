"""Outbound delivery helpers (handler send events → tools ``/messages``)."""

from __future__ import annotations

from dooers.agents.server.handlers.send import AgentEvent
from dooers.tools.whatsapp.events import body_for_event
from dooers.tools.whatsapp.transport import WhatsAppTransport


async def deliver_send_event(
    transport: WhatsAppTransport,
    agent_id: str,
    event: AgentEvent,
) -> bool:
    """Deliver a handler ``send.whatsapp.*`` event via ``/messages`` (outbound path)."""
    payload = body_for_event(event)
    if not payload:
        return False
    await transport.send_message_payload(agent_id, payload)
    return True
