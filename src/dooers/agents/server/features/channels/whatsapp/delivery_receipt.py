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


def _dooers_threads_agent_api(server: Any) -> Any:
    """``server._dooers_threads_api()`` when ``server`` (found by :func:`bind_agent_server`) runs Dooers Threads mode.

    ``None`` in legacy mode, or when no server was bound for this ``persistence`` yet (a webhook that
    arrives before the server finishes setting up): the caller then falls back to the local path, as
    before this branch existed — the local path returns ``False`` too (no ``persistence`` method), so
    the receipt is only lost if it truly arrives before the server is up, same as today.
    """
    if server is None:
        return None
    config = getattr(server, "_config", None)
    if config is None or getattr(config, "thread_management", None) != "dooers_threads":
        return None
    get_api = getattr(server, "_dooers_threads_api", None)
    return get_api() if get_api is not None else None


async def _apply_dooers_threads_delivery_receipt(agent_api: Any, agent_id: str, wa_message_id: str, data: dict[str, Any]) -> bool:
    """Dooers Threads-mode body of :func:`apply_whatsapp_delivery_receipt` (plan 5, Task 7).

    ``find_channel_delivery``/``record_channel_delivery`` (``DooersThreadsAgentApi``, plan 5 Task 5b) replace
    the local ``persistence`` the legacy path reads/writes; ``_receipt_moves_forward`` (F15) is the same
    status-ordering rule, applied here against the status ``find_channel_delivery`` returns before
    writing, so a stale receipt never reaches the agent API. An unknown ``wa_message_id``
    (``DooersThreadsAgentNotFound``, the agent API's 404) is production's own "pending" case: ``False``, so
    the caller retries, same as the legacy path's unknown wamid. Once found, this always returns
    ``True``: a status that does not move forward is kept locally (nothing to retry), and a
    ``record_channel_delivery`` the agent API itself did not apply (a race already moved the status
    further) needs no retry either — both are "nothing left to do", not failures.
    """
    from dooers.agents.server.dooers_threads.agent_api import DooersThreadsAgentNotFound

    try:
        current = await agent_api.find_channel_delivery(wa_message_id)
    except DooersThreadsAgentNotFound:
        logger.info("delivery receipt pending (dooers_threads) wa_message_id=%s agent_id=%s", wa_message_id, agent_id)
        return False
    status = _canonical_status(str(data.get("status") or "failed"))
    if not _receipt_moves_forward(str(current.get("status") or ""), status):
        logger.info(
            "delivery receipt kept (dooers_threads) wa_message_id=%s status=%s incoming=%s thread=%s",
            wa_message_id,
            current.get("status"),
            status,
            current.get("thread_id"),
        )
        return True
    error_code = data.get("error_code")
    message = str(data.get("error_message") or "").strip()
    result = await agent_api.record_channel_delivery(
        thread_id=current["thread_id"],
        event_id=current["event_id"],
        status=status,
        provider_message_id=wa_message_id,
        error=message[:500] if message else None,
        error_code=error_code if error_code is not None else None,
    )
    logger.info(
        "delivery receipt applied (dooers_threads)=%s wa_message_id=%s status=%s error_code=%s thread=%s",
        bool(isinstance(result, dict) and result.get("applied")),
        wa_message_id,
        status,
        error_code,
        current.get("thread_id"),
    )
    return True


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

    In Dooers Threads mode (plan 5, Task 7) — the server :func:`bind_agent_server` remembered for this
    ``persistence`` has ``AgentConfig.thread_management == "dooers_threads"`` — the same rule runs over
    ``DooersThreadsAgentApi.find_channel_delivery``/``record_channel_delivery`` instead of ``persistence``;
    legacy mode is byte-for-byte unchanged below.
    """
    data = _as_dict(body)
    if not data or not data.get("delivery_receipt"):
        return False
    wa_message_id = str(data.get("wa_message_id") or "").strip()
    if not wa_message_id:
        return False
    dooers_threads_api = _dooers_threads_agent_api(_servers.get(persistence))
    if dooers_threads_api is not None:
        return await _apply_dooers_threads_delivery_receipt(dooers_threads_api, agent_id, wa_message_id, data)
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
