from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx

from dooers.auth_validation import AuthValidationClient
from dooers.handlers.router import Router
from dooers.protocol.frames import C2S_Connect, ConnectPayload
from dooers.protocol.models import User
from dooers.registry import ConnectionRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def receive_text(self) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - unused
        pass


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield  # keep it an async generator


@pytest_asyncio.fixture
async def validator():
    v = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
    try:
        yield v
    finally:
        await v.close()


def _make_router(auth_validator: AuthValidationClient | None) -> Router:
    persistence = AsyncMock()
    registry = ConnectionRegistry()
    subscriptions: dict[str, set[str]] = {}
    return Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=registry,
        subscriptions=subscriptions,
        auth_validator=auth_validator,
    )


def _make_connect_frame(user_id: str = "") -> C2S_Connect:
    return C2S_Connect(
        id="frame-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="ws-1",
            user=User(user_id=user_id),
            auth_token="tok",
        ),
    )


def _last_ack(ws: FakeWebSocket) -> dict[str, Any]:
    assert ws.sent, "expected at least one frame"
    return json.loads(ws.sent[-1])


@pytest.mark.asyncio
async def test_anonymous_without_url_is_refused():
    router = _make_router(auth_validator=None)
    ws = FakeWebSocket()
    await router._handle_connect(ws, _make_connect_frame(user_id=""))

    ack = _last_ack(ws)
    assert ack["type"] == "ack"
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "ANONYMOUS_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_anonymous_with_valid_webhook_is_accepted(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": True,
                    "user": {
                        "user_id": "guest:abc",
                        "user_email": "",
                        "identity_ids": [],
                        "roles": [],
                    },
                    "rateLimits": {"messagesPerMinute": 15},
                    "threadTtlHours": 24,
                },
            )
        )
        await router._handle_connect(ws, _make_connect_frame(user_id=""))

    ack = _last_ack(ws)
    assert ack["type"] == "ack"
    assert ack["payload"]["ok"] is True
    assert router._user is not None
    assert router._user.user_id == "guest:abc"
    # Hard-coded guest roles — webhook cannot escalate.
    assert router._user.organization_role == "member"
    assert router._user.workspace_role == "member"
    assert router._rate_limits == {"messagesPerMinute": 15}


@pytest.mark.asyncio
async def test_anonymous_with_invalid_webhook_is_rejected(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(200, json={"valid": False, "reason": "expired"})
        )
        await router._handle_connect(ws, _make_connect_frame(user_id=""))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "CONNECTION_REJECTED"


@pytest.mark.asyncio
async def test_anonymous_fails_closed_on_upstream_5xx(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(503, text="bad")
        )
        await router._handle_connect(ws, _make_connect_frame(user_id=""))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "CONNECTION_REJECTED"


@pytest.mark.asyncio
async def test_authenticated_user_skips_webhook(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    # No respx mock configured — if the router called the webhook, httpx would try
    # a real request. Using respx.mock(assert_all_called=False) ensures no leakage.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(500)
        )
        await router._handle_connect(ws, _make_connect_frame(user_id="user-123"))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._user is not None
    assert router._user.user_id == "user-123"
    assert router._rate_limits == {}
    assert route.call_count == 0

    # Ensure full setup happened on the authenticated path.
    assert router._agent_id == "agent-1"
    assert router._ws_id in router._subscriptions

    # upsert_thread_participant is only called on event.create, not on connect.
    router._persistence.upsert_thread_participant.assert_not_called()
