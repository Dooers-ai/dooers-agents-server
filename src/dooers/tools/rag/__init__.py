"""Managed RAG tools for Dooers agent handlers.

    from dooers.tools import rag
    await rag.upload(name="catalog.csv", content=data, knowledge_base="products")
    results = await rag.search("botox forehead", knowledge_base="products")
"""
from __future__ import annotations
import base64
from typing import Any, Literal
from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.rag.transport import post

__all__ = ["RAGToolsError", "delete", "get", "list_documents", "search", "upload"]


async def upload(*, name: str, content: bytes, content_type: str = "application/octet-stream", knowledge_base: str = "default", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upload a source. Structured files preserve each complete row as one entity."""
    data = await post("/documents", {
        "name": name, "content_type": content_type, "content_base64": base64.b64encode(content).decode("ascii"),
        "knowledge_base": knowledge_base, "metadata": metadata or {},
    })
    return data if isinstance(data, dict) else {"ok": True}


async def search(query: str, *, limit: int = 5, knowledge_base: str | None = None, mode: Literal["hybrid", "semantic", "lexical"] = "hybrid", filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Search only the current agent database, optionally scoped to one knowledge base."""
    data = await post("/search", {"query": query, "limit": limit, "knowledge_base": knowledge_base, "mode": mode, "filters": filters or {}})
    if isinstance(data, dict) and isinstance(data.get("results"), list): return data["results"]
    return data if isinstance(data, list) else []


async def list_documents() -> list[dict[str, Any]]:
    data = await post("/documents/list", {})
    if isinstance(data, dict) and isinstance(data.get("documents"), list): return data["documents"]
    return data if isinstance(data, list) else []


async def get(document_id: str) -> dict[str, Any]:
    data = await post("/documents/get", {"document_id": document_id})
    return data if isinstance(data, dict) else {"document_id": document_id}


async def delete(document_id: str) -> dict[str, Any]:
    data = await post("/documents/delete", {"document_id": document_id})
    return data if isinstance(data, dict) else {"ok": True, "document_id": document_id}
