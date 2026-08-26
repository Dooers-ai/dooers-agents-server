"""HTTP transport for the managed Dooers RAG service.

Auth: Core-scoped JWT (audience ``rag-service``, scopes ``rag:read`` + ``rag:write``)
minted with the agent runtime API key — same contract as OTEL.

Agent/workspace/user context comes from the SDK runtime and is never an LLM-controlled
argument.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from dooers.agents.server.observability.service_token import (
    RUNTIME_API_KEY_SECRET_NAME,
    ServiceTokenClient,
)
from dooers.agents.server.settings import AGENT_CORE_BASE_URL
from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.rag.runtime import current_execution_context
from dooers.tools.whatsapp.runtime import require_agent_id, require_persistence

logger = logging.getLogger(__name__)

_RAG_AUDIENCE = "rag-service"
_RAG_SCOPES = ("rag:read", "rag:write")
_token_client: ServiceTokenClient | None = None


def _base_url() -> str:
    value = (os.environ.get("DOOERS_RAG_SERVICE_URL") or "").strip().rstrip("/")
    if not value:
        raise RAGToolsError("Managed Dooers RAG is not configured for this runtime")
    return value


def _core_base_url() -> str:
    return (
        (os.environ.get("AGENT_CORE_BASE_URL") or "").strip()
        or (os.environ.get("DOOERS_CORE_BASE_URL") or "").strip()
        or AGENT_CORE_BASE_URL
    ).rstrip("/")


def _service_token_client() -> ServiceTokenClient:
    global _token_client
    if _token_client is None:
        _token_client = ServiceTokenClient(core_base_url=_core_base_url())
    return _token_client


async def _core_jwt(*, agent_id: str, workspace_id: str) -> str:
    try:
        persistence = require_persistence()
    except Exception as exc:  # noqa: BLE001
        raise RAGToolsError(
            "RAG requires an active handler turn to mint a Core service token"
        ) from exc
    try:
        secrets = await persistence.get_service_secrets(agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG: failed to read service_secrets for agent_id=%s", agent_id)
        raise RAGToolsError("Could not read agent runtime credentials for RAG") from exc
    runtime_api_key = secrets.get(RUNTIME_API_KEY_SECRET_NAME)
    if not isinstance(runtime_api_key, str) or not runtime_api_key.strip():
        raise RAGToolsError(
            "Missing dooers_runtime_api_key — waiting for settings.seed from Core?"
        )
    token = await _service_token_client().get_token(
        agent_id=agent_id,
        workspace_id=workspace_id,
        runtime_api_key=runtime_api_key.strip(),
        audience=_RAG_AUDIENCE,
        scopes=_RAG_SCOPES,
    )
    if not token:
        raise RAGToolsError("Could not mint Core service token for RAG")
    return token


async def post(path: str, payload: dict[str, Any]) -> Any:
    base = _base_url()
    runtime = current_execution_context()
    agent_id = runtime.agent_id if runtime and runtime.agent_id else require_agent_id()
    workspace_id = (runtime.workspace_id if runtime else "") or ""
    body_payload: dict[str, Any] = {"agent_id": agent_id, **payload}
    if runtime:
        if runtime.workspace_id:
            body_payload["workspace_id"] = runtime.workspace_id
        if runtime.user_id:
            body_payload["user_id"] = runtime.user_id
            body_payload["on_behalf"] = runtime.on_behalf
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {await _core_jwt(agent_id=agent_id, workspace_id=workspace_id)}",
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
