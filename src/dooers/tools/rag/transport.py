"""Signed HTTP transport for the managed Dooers RAG service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import httpx

from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.whatsapp.runtime import require_agent_id


def _base_url() -> str:
    value = (os.environ.get("DOOERS_RAG_SERVICE_URL") or "").strip().rstrip("/")
    if not value:
        raise RAGToolsError("DOOERS_RAG_SERVICE_URL is not configured")
    return value


def _secret() -> str:
    value = (os.environ.get("DOOERS_RAG_SERVICE_SECRET") or "").strip()
    if not value:
        raise RAGToolsError("DOOERS_RAG_SERVICE_SECRET is not configured")
    return value


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def post(path: str, payload: dict[str, Any]) -> Any:
    agent_id = require_agent_id()
    body_payload = {"agent_id": agent_id, **payload}
    body = json.dumps(body_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Dooers-Signature": _signature(_secret(), body),
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{_base_url()}/api/v1{path}", content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise RAGToolsError(f"RAG service request failed: {exc}") from exc
    if not response.is_success:
        raise RAGToolsError(f"RAG service {path} returned {response.status_code}: {response.text[:500]}")
    if not response.content:
        return None
    return response.json()
