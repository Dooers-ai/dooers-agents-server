"""Managed RAG tools for Dooers agent handlers.

Usage::

    from dooers.tools import rag

    document = await rag.upload(name="catalog.pdf", content=pdf_bytes, content_type="application/pdf")
    results = await rag.search("warranty policy", limit=5)
"""

from __future__ import annotations

import base64
from typing import Any

from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.rag.transport import post

__all__ = ["RAGToolsError", "delete", "get", "list_documents", "search", "upload"]


async def upload(
    *,
    name: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upload and index a document in the current agent's RAG namespace."""
    data = await post(
        "/documents",
        {
            "name": name,
            "content_type": content_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "metadata": metadata or {},
        },
    )
    return data if isinstance(data, dict) else {"ok": True}


async def search(
    query: str,
    *,
    limit: int = 5,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Semantic search scoped by the service to the current agent/organization."""
    data = await post("/search", {"query": query, "limit": limit, "filters": filters or {}})
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    return data if isinstance(data, list) else []


async def list_documents() -> list[dict[str, Any]]:
    data = await post("/documents/list", {})
    if isinstance(data, dict) and isinstance(data.get("documents"), list):
        return data["documents"]
    return data if isinstance(data, list) else []


async def get(document_id: str) -> dict[str, Any]:
    data = await post("/documents/get", {"document_id": document_id})
    return data if isinstance(data, dict) else {"document_id": document_id}


async def delete(document_id: str) -> dict[str, Any]:
    data = await post("/documents/delete", {"document_id": document_id})
    return data if isinstance(data, dict) else {"ok": True, "document_id": document_id}
