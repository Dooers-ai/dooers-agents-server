"""HTTP transport for the managed Dooers RAG service.

Hosted agents mint an OIDC ID token from the Cloud Run metadata server using the
ambient tenant service account. Agent/workspace/user context comes from the SDK
runtime and is never exposed as an LLM-controlled argument.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx

from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.rag.runtime import current_execution_context
from dooers.tools.whatsapp.runtime import require_agent_id

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)


def _base_url() -> str:
    value = (os.environ.get("DOOERS_RAG_SERVICE_URL") or "").strip().rstrip("/")
    if not value:
        raise RAGToolsError("Managed Dooers RAG is not configured for this runtime")
    return value


async def _id_token(audience: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{_METADATA_IDENTITY_URL}?audience={quote(audience, safe='')}&format=full",
                headers={"Metadata-Flavor": "Google"},
            )
        response.raise_for_status()
        token = response.text.strip()
        if not token:
            raise ValueError("empty identity token")
        return token
    except Exception as exc:  # noqa: BLE001
        raise RAGToolsError("Could not mint workload identity token for RAG service") from exc


async def post(path: str, payload: dict[str, Any]) -> Any:
    base = _base_url()
    runtime = current_execution_context()
    agent_id = runtime.agent_id if runtime and runtime.agent_id else require_agent_id()
    body_payload: dict[str, Any] = {"agent_id": agent_id, **payload}
    if runtime:
        if runtime.workspace_id:
            body_payload["workspace_id"] = runtime.workspace_id
        if runtime.user_id:
            body_payload["user_id"] = runtime.user_id
            body_payload["on_behalf"] = runtime.on_behalf
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {await _id_token(base)}",
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
        raise RAGToolsError(
            f"RAG service {path} returned {response.status_code}: {response.text[:500]}"
        )
    return response.json() if response.content else None
