"""Apply a WhatsApp delivery receipt onto the thread event that was sent."""

from __future__ import annotations

import json
import logging
import weakref
from typing import Any

from dooers.agents.server.persistence.base import Persistence

logger = logging.getLogger("dooers.whatsapp.delivery_receipt")

_servers: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


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
    """Mark the outbound event failed when tools-whatsapp posts a delivery receipt.

    Returns True when an event was updated. Unknown wamids return False so the
    caller can try again: the status webhook sometimes beats the insert of the id.
    Agents that only check ``connectivity_check`` never dispatch this payload.
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

    status = str(data.get("status") or "failed").strip().lower() or "failed"
    current = dict(event.data or {})
    delivery = dict(current.get("delivery") or {})
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
