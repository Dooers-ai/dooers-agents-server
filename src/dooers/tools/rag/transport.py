"""HTTP transport for the managed Dooers RAG service.

Cloud Run mints an OIDC ID token from the ambient tenant service account. No
shared RAG secret is exposed to creator code.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.whatsapp.runtime import require_agent_id


def _base_url() -> str:
    value = (os.environ.get("DOOERS_RAG_SERVICE_URL") or "").strip().rstrip("/")
    if not value:
        raise RAGToolsError("DOOERS_RAG_SERVICE_URL is not configured")
    return value


def _id_token(audience: str) -> str:
    try:
        return id_token.fetch_id_token(Request(), audience)
    except Exception as exc:  # noqa: BLE001
        raise RAGToolsError("Could not mint workload identity token for RAG service") from exc


async def post(path: str, payload: dict[str, Any]) -> Any:
    base = _base_url()
    agent_id = require_agent_id()
    body_payload = {"agent_id": agent_id, **payload}
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {_id_token(base)}",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base}/api/v1{path}",
                content=json.dumps(body_payload, separators=(",", ":"), ensure_ascii=False).encode(),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise RAGToolsError(f"RAG service request failed: {exc}") from exc
    if not response.is_success:
        raise RAGToolsError(f"RAG service {path} returned {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else None
