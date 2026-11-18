"""Apply a WhatsApp delivery receipt onto the thread event that was sent."""

from __future__ import annotations

import json
import logging
import weakref
from typing import Any

from dooers.agents.server.persistence.base import Persistence

logger = logging.getLogger("dooers.whatsapp.delivery_receipt")

_servers: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()

# A late webhook must not walk the bubble backwards. ``failed`` is terminal.
_STATUS_RANK = {"accepted": 1, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


def _canonical_status(status: str) -> str:
    status = status.strip().lower()
    return "failed" if status == "error" else status


def _receipt_moves_forward(current: str, incoming: str) -> bool:
    incoming = _canonical_status(incoming)
    if not incoming:
        return False
    current = _canonical_status(current)
    if current == "failed":
        return False
    if incoming == "failed":
        return True
    incoming_rank = _STATUS_RANK.get(incoming)
    if incoming_rank is None:
        return False
    return incoming_rank >= _STATUS_RANK.get(current, 0)


def bind_agent_server(server: Any) -> None:
    """Remember the live server so a receipt can push ``event.append`` to open consoles."""
    persistence = getattr(server, "_persistence", None)
    if persistence is not None:
        _servers[persistence] = server


def _as_dict(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def apply_whatsapp_delivery_receipt(
    persistence: Persistence,
    agent_id: str,
    body: bytes,
) -> bool:
    """Write the receipt status onto the outbound event.

    ``failed`` sticks. ``read`` does not go back to ``delivered``. Unknown wamids
    return False so the caller can try again: the status webhook sometimes beats
    the insert of the id. A stale status returns True, because retrying will not
    change it. Agents that only check ``connectivity_check`` never dispatch this
    payload.
    """
    data = _as_dict(body)
    if not data or not data.get("delivery_receipt"):
        return False
    wa_message_id = str(data.get("wa_message_id") or "").strip()
    if not wa_message_id:
        return False
    finder = getattr(persistence, "find_event_by_whatsapp_message_id", None)
    if finder is None:
        logger.warning("delivery receipt ignored: persistence cannot look up wa_message_id")
        return False
    event = await finder(wa_message_id)
    if event is None:
        logger.info("delivery receipt pending wa_message_id=%s agent_id=%s", wa_message_id, agent_id)
        return False
    thread = await persistence.get_thread(event.thread_id)
    if thread is None or thread.agent_id != agent_id:
        logger.warning(
            "delivery receipt ignored wa_message_id=%s agent_id=%s thread=%s",
            wa_message_id,
            agent_id,
            event.thread_id,
        )
        return False

    status = _canonical_status(str(data.get("status") or "failed"))
    current = dict(event.data or {})
    delivery = dict(current.get("delivery") or {})
    if not _receipt_moves_forward(str(delivery.get("status") or ""), status):
        logger.info(
            "delivery receipt kept wa_message_id=%s status=%s incoming=%s thread=%s",
            wa_message_id,
            delivery.get("status"),
            status,
            event.thread_id,
        )
        return True
    delivery["status"] = status
    delivery["wa_message_id"] = wa_message_id
    if data.get("error_code") is not None:
        delivery["error_code"] = data.get("error_code")
    message = str(data.get("error_message") or "").strip()
    if message:
        delivery["error_message"] = message[:500]
    current["delivery"] = delivery
    event.data = current
    await persistence.update_event(event)

    server = _servers.get(persistence)
    broadcast = getattr(server, "_broadcast_dict_to_agent", None) if server is not None else None
    if broadcast is not None:
        await broadcast(
            agent_id,
            {"type": "event.append", "thread_id": event.thread_id, "events": [event]},
        )
    logger.info(
        "delivery receipt applied wa_message_id=%s status=%s error_code=%s thread=%s",
        wa_message_id,
        status,
        delivery.get("error_code"),
        event.thread_id,
    )
    return True
