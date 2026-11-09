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
    resolve_runtime_api_key,
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
    runtime_api_key = await resolve_runtime_api_key(persistence, agent_id)
    if not runtime_api_key:
        raise RAGToolsError(
            "Missing runtime API key — set AGENT_SEED_SECRET or wait for "
            f"{RUNTIME_API_KEY_SECRET_NAME} via settings.seed"
        )
    token = await _service_token_client().get_token(
        agent_id=agent_id,
        workspace_id=workspace_id,
        runtime_api_key=runtime_api_key,
        audience=_RAG_AUDIENCE,
        scopes=_RAG_SCOPES,
    )
    if not token:
        raise RAGToolsError("Could not mint Core service token for RAG")
    return token


async def _delegated_core_jwt(auth_assertion: str) -> str:
    # A runtime credential alone cannot impersonate a human. The assertion is
    # a Core-signed dashboard handshake obtained from an authenticated WS.
    url = f"{_core_base_url()}/api/v2/identity/service-token/exchange"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={
                "audience": _RAG_AUDIENCE,
                "token": auth_assertion,
                "scopes": list(_RAG_SCOPES),
            })
        resp.raise_for_status()
        token = (resp.json().get("data") or {}).get("accessToken")
        if not isinstance(token, str) or not token:
            raise RAGToolsError("Core did not issue a delegated RAG token")
        return token
    except (httpx.HTTPError, ValueError) as exc:
        raise RAGToolsError("Could not validate active user delegation with Core") from exc


async def post(path: str, payload: dict[str, Any]) -> Any:
    base = _base_url()
    runtime = current_execution_context()
    agent_id = runtime.agent_id if runtime and runtime.agent_id else require_agent_id()
    workspace_id = (runtime.workspace_id if runtime else "") or ""
    body_payload: dict[str, Any] = {"agent_id": agent_id, **payload}
    if runtime and runtime.workspace_id:
        body_payload["workspace_id"] = runtime.workspace_id
    if runtime and runtime.auth_assertion and runtime.user_id:
        body_payload["user_id"] = runtime.user_id
        body_payload["on_behalf"] = True
        token = await _delegated_core_jwt(runtime.auth_assertion)
    else:
        # Agent-only access is safe for shared KBs; user-restricted KBs are
        # denied server-side without a verified delegation.
        token = await _core_jwt(agent_id=agent_id, workspace_id=workspace_id)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}",
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
