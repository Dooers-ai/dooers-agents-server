from __future__ import annotations

import base64

import pytest

from dooers.tools import rag


@pytest.mark.asyncio
async def test_upload_encodes_content(monkeypatch):
    captured = {}

    async def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"id": "doc-1"}

    monkeypatch.setattr(rag, "post", fake_post)
    result = await rag.upload(name="a.txt", content=b"hello", content_type="text/plain")

    assert result["id"] == "doc-1"
    assert captured["path"] == "/documents"
    assert captured["payload"]["content_base64"] == base64.b64encode(b"hello").decode("ascii")


@pytest.mark.asyncio
async def test_search_unwraps_results(monkeypatch):
    async def fake_post(path, payload):
        assert path == "/search"
        assert payload["query"] == "hello"
        return {"results": [{"text": "world", "score": 0.9}]}

    monkeypatch.setattr(rag, "post", fake_post)
    assert await rag.search("hello") == [{"text": "world", "score": 0.9}]
