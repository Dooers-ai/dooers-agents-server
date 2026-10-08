"""Parse ``queues`` / ``queue_catalog`` from tools-whatsapp inbound JSON."""

from __future__ import annotations

from typing import Any, Mapping

from dooers.agents.server.protocol.models import ConnectionWorkspace, WorkspaceQueueCatalog


def parse_inbound_queue_catalog(body: Mapping[str, Any] | None) -> list[WorkspaceQueueCatalog]:
    """Handshake-shaped roster on the HMAC payload; empty when omitted or invalid."""
    if not body:
        return []
    try:
        workspace = ConnectionWorkspace(
            id="inbound",
            queues=body.get("queues") or [],
            queue_catalog=body.get("queue_catalog") or [],
        )
    except Exception:
        return []
    if not workspace.queues and not workspace.queue_catalog:
        return []
    return workspace.catalog()
