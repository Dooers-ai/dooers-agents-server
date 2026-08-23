"""Managed RAG tools for Dooers agent handlers.

    from dooers.tools import rag

    await rag.upload(name="catalog.csv", content=data, knowledge_base="products")
    results = await rag.search("botox forehead", knowledge_bases=["products"])
"""
from __future__ import annotations

import base64
from typing import Any, Literal

from dooers.tools.rag.errors import RAGToolsError
from dooers.tools.rag.transport import post

__all__ = [
    "RAGToolsError",
    "delete",
    "get",
    "list_documents",
    "list_knowledge_bases",
    "search",
    "upload",
]


async def upload(
    *,
    name: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    knowledge_base: str = "default",
    metadata: dict[str, Any] | None = None,
    strategy: Literal["auto", "structured", "document"] = "auto",
    store_original: bool = True,
    embedding_fields: list[str] | None = None,
    exclude_embedding_fields: list[str] | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> dict[str, Any]:
    """Upload a source. Structured files preserve each complete row as one entity."""
    data = await post(
        "/documents",
        {
            "name": name,
            "content_type": content_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "knowledge_base": knowledge_base,
            "metadata": metadata or {},
            "strategy": strategy,
            "store_original": store_original,
            "embedding_fields": embedding_fields,
            "exclude_embedding_fields": exclude_embedding_fields or [],
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
    )
    return data if isinstance(data, dict) else {"ok": True}


async def search(
    query: str,
    *,
    limit: int = 5,
    knowledge_base: str | None = None,
    knowledge_bases: list[str] | None = None,
    mode: Literal["hybrid", "semantic", "lexical"] = "hybrid",
    filters: dict[str, Any] | None = None,
    candidate_k: int = 50,
) -> list[dict[str, Any]]:
    """Search KBs authorized for the current deterministic execution context."""
    if knowledge_base and knowledge_bases:
        raise ValueError("use either knowledge_base or knowledge_bases, not both")
    selected = knowledge_bases if knowledge_bases is not None else ([knowledge_base] if knowledge_base else None)
    data = await post(
        "/search",
        {
            "query": query,
            "limit": limit,
            "knowledge_bases": selected,
            "mode": mode,
            "filters": filters or {},
            "candidate_k": candidate_k,
        },
    )
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    return data if isinstance(data, list) else []


async def list_knowledge_bases() -> list[dict[str, Any]]:
    """List only KBs visible to the current agent/workspace/user context."""
    data = await post("/knowledge-bases/list", {})
    if isinstance(data, dict) and isinstance(data.get("knowledge_bases"), list):
        return data["knowledge_bases"]
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
    """Delete a source only when it belongs to a KB scoped to this agent."""
    data = await post("/documents/delete", {"document_id": document_id})
    return data if isinstance(data, dict) else {"ok": True, "document_id": document_id}
