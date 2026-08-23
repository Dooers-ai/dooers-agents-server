from __future__ import annotations

import base64

import pytest

from dooers.tools import rag


@pytest.mark.asyncio
async def test_upload_encodes_content_and_options(monkeypatch):
    captured = {}

    async def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"id": "doc-1"}

    monkeypatch.setattr(rag, "post", fake_post)
    result = await rag.upload(
        name="catalog.csv",
        content=b"hello",
        content_type="text/csv",
        knowledge_base="products",
        strategy="structured",
        exclude_embedding_fields=["preco"],
    )

    assert result["id"] == "doc-1"
    assert captured["path"] == "/documents"
    assert captured["payload"]["content_base64"] == base64.b64encode(b"hello").decode("ascii")
    assert captured["payload"]["knowledge_base"] == "products"
    assert captured["payload"]["strategy"] == "structured"
    assert captured["payload"]["exclude_embedding_fields"] == ["preco"]


@pytest.mark.asyncio
async def test_search_supports_multiple_knowledge_bases(monkeypatch):
    async def fake_post(path, payload):
        assert path == "/search"
        assert payload["query"] == "hello"
        assert payload["knowledge_bases"] == ["products", "faq"]
        return {"results": [{"text": "world", "score": 0.9}]}

    monkeypatch.setattr(rag, "post", fake_post)
    assert await rag.search("hello", knowledge_bases=["products", "faq"]) == [
        {"text": "world", "score": 0.9}
    ]


@pytest.mark.asyncio
async def test_search_rejects_single_and_multi_kb_at_once():
    with pytest.raises(ValueError):
        await rag.search("hello", knowledge_base="a", knowledge_bases=["b"])


@pytest.mark.asyncio
async def test_list_knowledge_bases(monkeypatch):
    async def fake_post(path, payload):
        assert path == "/knowledge-bases/list"
        return {"knowledge_bases": [{"name": "products"}]}

    monkeypatch.setattr(rag, "post", fake_post)
    assert await rag.list_knowledge_bases() == [{"name": "products"}]
