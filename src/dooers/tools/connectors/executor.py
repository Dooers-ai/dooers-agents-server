"""One connector tool call: ``/v1/tools/execute``, the chat card when the person must act, and the wait for them.

When the service answers ``interaction_required`` (connect an account, approve the call), the card goes into the
chat through the turn's emitter, outside the handler's ``yield``; the call waits on the interaction in 20 s slices
and runs again with its ``interaction_id``. Concurrent calls each wait on their own interaction id.

Never raises for connector, network, permission or unexpected errors: they come back as ``{"status", "message"}``.
Only a cancellation goes through (after the chat got the ``tool.result``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any
from urllib.parse import quote

from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.tools.connectors.errors import ConnectorError
from dooers.tools.connectors.runtime import current_turn
from dooers.tools.connectors.transport import ConnectorTransport

DEFAULT_INTERACTION_TIMEOUT: dict[str, float] = {"connect": 180.0, "approval": 120.0}
WAIT_SLICE_SECONDS = 20
# The service holds a wait for up to WAIT_SLICE_SECONDS (its own call to the core up to 25 s); leave room on top.
_WAIT_HTTP_TIMEOUT = WAIT_SLICE_SECONDS + 10.0
# connect → approval takes two rounds; one more covers a retried approval. Beyond that something is looping.
_MAX_INTERACTION_ROUNDS = 3
# A "pending" answered faster than this did not hold its slice; pause before asking again instead of spinning.
_MIN_WAIT_SECONDS = 1.0
# A 5xx or a network error during the wait is asked again after 1, 2, 4, then every 8 s, while the deadline allows.
_MAX_RETRY_SECONDS = 8.0

_clock = time.monotonic  # replaced in tests
_sleep = asyncio.sleep  # replaced in tests

_send = AgentSend()
logger = logging.getLogger(__name__)


def _declined() -> dict[str, Any]:
    return {"status": "declined", "message": "A pessoa recusou a ação."}


def _timed_out() -> dict[str, Any]:
    return {"status": "interaction_timeout", "message": "A pessoa não respondeu a tempo."}


def _interrupted() -> dict[str, Any]:
    return {"status": "provider_error", "message": "A chamada foi interrompida antes de terminar."}


async def execute(
    name: str,
    arguments: dict,
    *,
    call_id: str,
    bound: dict,
    on_behalf_of: str | None,
    interaction_timeout: dict[str, float],
    emit_tool_events: bool,
    display_name: str | None = None,
) -> dict:
    """Run ``name`` and return the final ``/v1/tools/execute`` body (or ``{"status", "message"}`` on failure).

    ``bound`` (set in the dev's code) wins over ``arguments`` (from the LLM). With ``emit_tool_events`` the chat
    gets ``tool.call`` and ``tool.result`` (titled ``display_name``); the interaction card is emitted either way.
    A ``tool.call`` in the chat always gets its ``tool.result``, also when the call fails unexpectedly or is
    cancelled (the cancellation is re-raised).
    """
    args = {**arguments, **bound}
    call_id = call_id or f"call_{uuid.uuid4().hex}"
    out = _interrupted()  # what the tool.result says if a cancellation goes through
    call_shown = False
    try:
        if emit_tool_events:
            await _emit(_send.tool_call(name=name, args=args, display_name=display_name, id=call_id))
            call_shown = True
        out = await _execute_with_interactions(name, args, call_id, on_behalf_of, interaction_timeout)
    except ConnectorError as exc:
        out = {"status": exc.status, "message": exc.message}
    except Exception:
        logger.warning("connectors: unexpected error running %s", name, exc_info=True)
        out = {"status": "provider_error", "message": "Erro inesperado ao executar a tool."}
    finally:
        if call_shown:
            await _show_result(name, args, display_name, call_id, out)
    return out


async def _show_result(name: str, args: dict, display_name: str | None, call_id: str, out: dict[str, Any]) -> None:
    result = {"status": out.get("status"), "content": out.get("content"), "truncated": bool(out.get("truncated", False))}
    try:
        await _emit(_send.tool_result(name=name, result=result, args=args, display_name=display_name, id=call_id))
    except Exception:  # the LLM still gets its answer; never replace it (or a cancellation) with an emitter error
        logger.warning("connectors: could not emit tool.result for %s", name, exc_info=True)


async def _execute_with_interactions(
    name: str,
    args: dict,
    call_id: str,
    on_behalf_of: str | None,
    interaction_timeout: dict[str, float],
) -> dict[str, Any]:
    transport = ConnectorTransport()
    timeouts = {**DEFAULT_INTERACTION_TIMEOUT, **interaction_timeout}
    deadlines: dict[str, float] = {}  # per kind, shared by every round of this call
    seen: set[str] = set()
    interaction_id: str | None = None
    while True:
        out = await transport.post(
            "/v1/tools/execute",
            {"name": name, "arguments": args, "call_id": call_id, "interaction_id": interaction_id},
            on_behalf_of=on_behalf_of,
        )
        if not isinstance(out, dict) or not isinstance(out.get("status"), str):
            raise ConnectorError("Resposta inválida do serviço de conectores", status="provider_error")
        if out["status"] != "interaction_required":
            return out
        if len(seen) == _MAX_INTERACTION_ROUNDS:
            raise ConnectorError("A ação pediu interações demais com a pessoa e foi interrompida.", status="provider_error")

        interaction = out.get("interaction")
        if not isinstance(interaction, dict) or not interaction.get("id"):
            raise ConnectorError("Resposta inválida do serviço de conectores", status="provider_error")
        interaction_id = str(interaction["id"])
        if interaction_id in seen:  # the service asked again for an interaction this call already resolved
            raise ConnectorError("O serviço de conectores repetiu uma interação já resolvida.", status="provider_error")
        seen.add(interaction_id)
        kind = interaction.get("kind")
        limit = timeouts.get(kind, DEFAULT_INTERACTION_TIMEOUT["approval"])
        deadline = deadlines.setdefault(kind, _clock() + limit)
        if _clock() >= deadline:  # no card the person could no longer answer
            return _timed_out()
        await _emit(AgentEvent(send_type="connector_interaction", data=_card(interaction)))

        status = await _wait(transport, interaction_id, deadline, on_behalf_of)
        if status == "declined":
            return _declined()
        if status == "expired":
            return _timed_out()
        if status not in ("approved", "connected", "consumed"):
            raise ConnectorError(f"Status de interação inesperado: {status!r}", status="provider_error")
        # approved / connected: run again with the interaction; consumed: run again and let the core decide


async def _wait(transport: ConnectorTransport, interaction_id: str, deadline: float, on_behalf_of: str | None) -> Any:
    """Wait in slices until the interaction leaves ``pending``; past ``deadline`` it counts as ``expired``.

    A ``provider_error`` (5xx, network) in one slice is asked again while the deadline allows: the card is still in
    the chat and the person may still answer. If the last slice before the deadline failed, that error is raised.
    A refusal (``denied``) ends the wait at once.
    """
    path = f"/v1/interactions/{quote(interaction_id, safe='')}/wait"
    retry_in = _MIN_WAIT_SECONDS
    last_error: ConnectorError | None = None
    while _clock() < deadline:
        started = _clock()
        try:
            answer = await transport.post(
                path,
                {"timeout_seconds": WAIT_SLICE_SECONDS},
                timeout=_WAIT_HTTP_TIMEOUT,
                on_behalf_of=on_behalf_of,
            )
        except ConnectorError as exc:
            if exc.status != "provider_error":
                raise
            last_error = exc
            await _sleep(min(retry_in, max(0.0, deadline - _clock())))
            retry_in = min(retry_in * 2, _MAX_RETRY_SECONDS)
            continue
        last_error = None
        retry_in = _MIN_WAIT_SECONDS
        status = answer.get("status") if isinstance(answer, dict) else None
        if status != "pending":
            return status
        held = _clock() - started
        if held < _MIN_WAIT_SECONDS:
            await _sleep(_MIN_WAIT_SECONDS - held)
    if last_error is not None:
        raise last_error
    return "expired"


def _card(interaction: dict[str, Any]) -> dict[str, Any]:
    """The ``connector.interaction`` event data. The service names the id ``id``; the chat contract, ``interaction_id``."""
    return {
        "interaction_id": str(interaction["id"]),
        "kind": interaction.get("kind"),
        "title": interaction.get("title"),
        "summary": interaction.get("summary"),
        "connector": interaction.get("connector"),
        "expires_at": interaction.get("expires_at"),
    }


async def _emit(event: AgentEvent) -> None:
    turn = current_turn()
    if turn is not None and turn.emit is not None:
        await turn.emit(event)
