"""Exchange of the turn's ``presence_token`` for a short-lived ``connector_session`` (core).

Never logs the presence token, the runtime key or the session. A failed exchange returns ``None``
(the service then treats the call as having no presence) and never raises into the turn. A token the core
refused (401/403) is remembered for 60 s, so the turn's other tool calls do not ask again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime

import httpx

from dooers.agents.server.observability.service_token import core_base_url, resolve_runtime_api_key
from dooers.tools.connectors.runtime import TurnContext

logger = logging.getLogger(__name__)

# The JWT lives 600 s; stop reusing it this long before ``expiresAt``.
_EXPIRY_MARGIN_SECONDS = 30.0
# A token the core refused (401/403) is not sent again for this long: every tool call of the turn would ask.
_REFUSAL_TTL_SECONDS = 60.0

_now = time.time  # wall clock (``expiresAt`` is an absolute timestamp); replaced in tests
# (agent_id, sha256(presence_token)) -> (connector_session, usable_until)
_sessions: dict[tuple[str, str], tuple[str, float]] = {}
# Exchanges in flight by the same key: tools running in parallel in one turn share a single call to the core.
_inflight: dict[tuple[str, str], asyncio.Future[str | None]] = {}
# Same key -> refused_until, for tokens the core answered 401/403.
_refused: dict[tuple[str, str], float] = {}


def reset_presence_cache() -> None:
    _sessions.clear()
    _inflight.clear()
    _refused.clear()


def _parse_expires_at(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def connector_session(turn: TurnContext) -> str | None:
    if not turn.presence_token:
        return None
    key = (turn.agent_id, hashlib.sha256(turn.presence_token.encode()).hexdigest())
    cached = _sessions.get(key)
    if cached and cached[1] > _now():
        return cached[0]
    _sessions.pop(key, None)
    if _refused.get(key, 0.0) > _now():
        return None

    pending = _inflight.get(key)
    if pending is None:
        pending = asyncio.ensure_future(_exchange(turn, key))
        _inflight[key] = pending

        def _forget(done: asyncio.Future[str | None]) -> None:
            if _inflight.get(key) is done:
                del _inflight[key]

        pending.add_done_callback(_forget)
    # Shielded: one caller being cancelled must not cancel the exchange the others are waiting on.
    return await asyncio.shield(pending)


async def _exchange(turn: TurnContext, key: tuple[str, str]) -> str | None:
    runtime_api_key = await resolve_runtime_api_key(turn.persistence, turn.agent_id)
    if not runtime_api_key:
        logger.warning("connectors: no runtime API key for agent_id=%s, skipping presence exchange", turn.agent_id)
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{core_base_url()}/api/v2/identity/connector-session",
                json={
                    "presenceToken": turn.presence_token,
                    "agentId": turn.agent_id,
                    "runtimeApiKey": runtime_api_key,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("connectors: presence exchange failed for agent_id=%s: %s", turn.agent_id, type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.warning("connectors: presence exchange refused (%d) for agent_id=%s", response.status_code, turn.agent_id)
        if response.status_code in (401, 403):
            now = _now()
            for stale in [k for k, until in _refused.items() if until <= now]:
                del _refused[stale]
            _refused[key] = now + _REFUSAL_TTL_SECONDS
        return None
    try:
        data = response.json()["data"]
        session = data["token"]
    except (ValueError, KeyError, TypeError):
        logger.warning("connectors: malformed presence exchange response for agent_id=%s", turn.agent_id)
        return None
    if not isinstance(session, str) or not session:
        return None
    expires_at = _parse_expires_at(data.get("expiresAt"))
    if expires_at is not None:
        now = _now()
        for stale in [k for k, (_, usable_until) in _sessions.items() if usable_until <= now]:
            del _sessions[stale]
        _sessions[key] = (session, expires_at - _EXPIRY_MARGIN_SECONDS)
    return session
