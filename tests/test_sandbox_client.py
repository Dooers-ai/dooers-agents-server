"""dooers.tools.sandbox: the create request carries the thread and the person the turn runs for."""

from __future__ import annotations

import json

import httpx
import pytest

from dooers.tools import sandbox
from dooers.tools.rag.runtime import bind_execution_context


@pytest.fixture
def captured(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": json.loads(request.content or b"{}"), "auth": request.headers.get("authorization")})
        return httpx.Response(201, json={"id": "sbx_1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(sandbox.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def bearer(context):
        return "core-token"

    monkeypatch.setattr(sandbox, "_bearer", bearer)
    monkeypatch.setenv("DOOERS_SANDBOX_SERVICE_URL", "https://sandbox.test")
    monkeypatch.setattr(sandbox, "_sessions", {})
    return seen


async def test_open_sends_thread_and_authenticated_user(captured):
    bind_execution_context(
        agent_id="agent-1", organization_id="org-1", workspace_id="ws-1", user_id="ana", channel="dooers-platform", thread_id="t1"
    )
    box = await sandbox.open(profile="documents")
    assert box.id == "sbx_1"
    body = captured[0]["body"]
    assert captured[0]["path"] == "/v1/sandboxes" and captured[0]["auth"] == "Bearer core-token"
    assert body["thread_id"] == "t1" and body["user_id"] == "ana" and body["profile"] == "documents"


async def test_guests_and_channels_are_not_recorded_as_users(captured):
    bind_execution_context(agent_id="agent-1", organization_id="org-1", user_id="guest:abc", channel="dooers-platform", thread_id="t2")
    await sandbox.open(profile="documents")
    assert captured[0]["body"]["user_id"] is None
