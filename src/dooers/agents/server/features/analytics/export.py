"""Feedback export for a system that talks to the agent, not to Core.

The caller authenticates with ``ANALYTICS_EXPORT_SECRET``. The agent then
reads feedback from Core with its runtime key. A vote recorded by this SDK
already carries the thread id and the message text. Older rows do not, so
those are filled from the local event store.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

import httpx

from dooers.agents.server.observability.service_token import (
    ServiceTokenClient,
    resolve_runtime_api_key,
)
from dooers.agents.server.persistence.base import Persistence

from .text import message_text

logger = logging.getLogger(__name__)

EXPORT_SECRET_ENV = "ANALYTICS_EXPORT_SECRET"


def _bearer(header: str | None) -> str:
    if not header:
        return ""
    prefix = "bearer "
    if header.lower().startswith(prefix):
        return header[len(prefix) :].strip()
    return ""


def _secret_matches(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


async def export_agent_feedback(
    *,
    persistence: Persistence,
    agent_id: str,
    authorization: str | None,
    export_secret: str | None = None,
    core_base_url: str,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    http_client: httpx.AsyncClient | None = None,
    token_client: ServiceTokenClient | None = None,
) -> tuple[int, dict[str, Any]]:
    secret = (export_secret if export_secret is not None else os.environ.get(EXPORT_SECRET_ENV) or "").strip()
    if not secret:
        return 503, {"error": "feedback export is not configured"}
    if not _secret_matches(_bearer(authorization), secret):
        return 401, {"error": "invalid export credential"}

    agent = (agent_id or "").strip()
    if not agent:
        return 400, {"error": "agent_id is required"}

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=30.0)
    tokens = token_client or ServiceTokenClient(core_base_url=core_base_url, http_client=client)
    try:
        runtime_key = await resolve_runtime_api_key(persistence, agent)
        if not runtime_key:
            return 502, {"error": "agent runtime credential is not available"}
        access = await tokens.get_token(
            agent_id=agent,
            runtime_api_key=runtime_key,
            audience="analytics-service",
            scopes=["analytics:read"],
        )
        if not access:
            return 502, {"error": "could not authorize analytics read"}

        params: dict[str, str] = {}
        if from_timestamp:
            params["from"] = from_timestamp
        if to_timestamp:
            params["to"] = to_timestamp
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = str(limit)
        url = f"{core_base_url.rstrip('/')}/api/v2/analytics/agent/feedback"
        response = await client.get(url, params=params, headers={"Authorization": f"Bearer {access}"})
        if response.status_code != 200:
            logger.warning(
                "feedback export: core returned %s for agent_id=%s", response.status_code, agent
            )
            return 502, {"error": "analytics core rejected the read"}
        try:
            payload = response.json()["data"]
            items = list(payload["items"])
            next_cursor = payload.get("next_cursor")
        except (ValueError, KeyError, TypeError):
            logger.warning("feedback export: malformed core response for agent_id=%s", agent)
            return 502, {"error": "analytics core rejected the read"}

        enriched = []
        for item in items:
            row = dict(item) if isinstance(item, dict) else {}
            target_id = row.get("target_id")
            snapshot_thread = row.get("thread_id") if isinstance(row.get("thread_id"), str) and row["thread_id"].strip() else None
            snapshot_message = row.get("message") if isinstance(row.get("message"), str) and row["message"].strip() else None
            event = None
            if (snapshot_thread is None or snapshot_message is None) and isinstance(target_id, str) and target_id:
                try:
                    event = await persistence.get_event(target_id)
                except Exception:
                    logger.warning(
                        "feedback export: event lookup failed for target_id=%s", target_id, exc_info=True
                    )
            row["thread_id"] = snapshot_thread or (event.thread_id if event is not None else None)
            row["message"] = snapshot_message or message_text(event)
            enriched.append(row)
        return 200, {"items": enriched, "next_cursor": next_cursor}
    finally:
        if owns_client:
            await client.aclose()
            if token_client is None:
                await tokens.aclose()
