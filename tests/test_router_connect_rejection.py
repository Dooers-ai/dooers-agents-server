from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx

from dooers.agents.server.auth_validation import AuthValidationClient
from dooers.agents.server.handlers.router import Router
from dooers.agents.server.protocol.frames import (
    C2S_Connect,
    C2S_EventCreate,
    C2S_ThreadList,
    ConnectPayload,
    EventCreateEventPayload,
    EventCreatePayload,
    ThreadListPayload,
)
from dooers.agents.server.protocol.models import User
from dooers.agents.server.registry import ConnectionRegistry


class FakeWebSocket:
    def __init__(self, raise_on_close: bool = False) -> None:
        self.sent: list[str] = []
        self.closed_with: int | None = None
        self._raise_on_close = raise_on_close

    async def receive_text(self) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code
        if self._raise_on_close:
            raise RuntimeError("Cannot call close once a close message has been sent")


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


@pytest_asyncio.fixture
async def validator():
    v = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
    try:
        yield v
    finally:
        await v.close()


def _make_router(auth_validator: AuthValidationClient | None) -> Router:
    return Router(
        persistence=AsyncMock(),
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
        auth_validator=auth_validator,
    )


def _connect_frame(user_id: str = "", token: str = "tok") -> C2S_Connect:
    return C2S_Connect(
        id="frame-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="ws-1",
            user=User(user_id=user_id),
            auth_token=token,
        ),
    )


def _last_ack(ws: FakeWebSocket) -> dict[str, Any]:
    assert ws.sent, "expected at least one frame"
    return json.loads(ws.sent[-1])


def _reject_mock(mock: respx.MockRouter) -> None:
    mock.post("https://core.test/validate").mock(return_value=httpx.Response(200, json={"valid": False, "reason": "invalid_token"}))


def _accept_mock(mock: respx.MockRouter) -> None:
    mock.post("https://core.test/validate").mock(
        return_value=httpx.Response(
            200,
            json={
                "valid": True,
                "user": {"user_id": "guest:abc", "user_email": "", "identity_ids": [], "roles": []},
            },
        )
    )


@pytest.mark.asyncio
async def test_rejected_dashboard_connect_leaves_no_session(validator):
    router = _make_router(validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        _reject_mock(mock)
        await router._handle_connect(ws, _connect_frame(user_id="user-1"))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "CONNECTION_REJECTED"
    assert router._agent_id is None
    assert router._user is None
    assert router._registry.get_connection_count("agent-1") == 0
    assert ws.closed_with == 1008


@pytest.mark.asyncio
async def test_rejected_anonymous_connect_closes_socket():
    router = _make_router(None)
    ws = FakeWebSocket()

    await router._handle_connect(ws, _connect_frame(user_id=""))

    assert _last_ack(ws)["payload"]["error"]["code"] == "ANONYMOUS_NOT_ALLOWED"
    assert ws.closed_with == 1008
    assert router._agent_id is None


@pytest.mark.asyncio
async def test_frames_after_rejection_are_not_connected(validator):
    router = _make_router(validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        _reject_mock(mock)
        await router._handle_connect(ws, _connect_frame(user_id="user-1"))

    await router.route(ws, C2S_ThreadList(id="f2", type="thread.list", payload=ThreadListPayload()))
    ack = _last_ack(ws)
    assert ack["type"] == "ack"
    assert ack["payload"]["error"]["code"] == "NOT_CONNECTED"
    router._persistence.list_threads.assert_not_called()

    await router.route(
        ws,
        C2S_EventCreate(
            id="f3",
            type="event.create",
            payload=EventCreatePayload(
                event=EventCreateEventPayload(type="message", actor="user", content=[{"type": "text", "text": "hi"}])
            ),
        ),
    )
    ack = _last_ack(ws)
    assert ack["payload"]["error"]["code"] == "NOT_CONNECTED"
    router._persistence.create_thread.assert_not_called()


@pytest.mark.asyncio
async def test_second_rejected_connect_drops_previous_session(validator):
    router = _make_router(validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        _accept_mock(mock)
        await router._handle_connect(ws, _connect_frame(user_id=""))
    assert _last_ack(ws)["payload"]["ok"] is True
    assert router._registry.get_connection_count("agent-1") == 1
    router._analytics_subscriptions.setdefault("agent-1", set()).add(router._ws_id)
    router._settings_subscriptions.setdefault("agent-1", set()).add(router._ws_id)
    router._settings_ws_context[router._ws_id] = {}

    with respx.mock() as mock:
        _reject_mock(mock)
        await router._handle_connect(ws, _connect_frame(user_id=""))

    assert router._registry.get_connection_count("agent-1") == 0
    assert router._agent_id is None
    assert router._ws_id not in router._subscriptions
    assert router._analytics_subscriptions == {}
    assert router._settings_subscriptions == {}
    assert router._ws_id not in router._settings_ws_context


@pytest.mark.asyncio
async def test_rejection_survives_a_socket_that_is_already_closed(validator):
    router = _make_router(validator)
    ws = FakeWebSocket(raise_on_close=True)

    with respx.mock() as mock:
        _reject_mock(mock)
        await router._handle_connect(ws, _connect_frame(user_id="user-1"))

    assert _last_ack(ws)["payload"]["ok"] is False


@pytest.mark.asyncio
async def test_rejection_is_logged_without_token(validator, caplog):
    router = _make_router(validator)
    ws = FakeWebSocket()

    with caplog.at_level(logging.INFO, logger="agents"), respx.mock() as mock:
        _reject_mock(mock)
        await router._handle_connect(ws, _connect_frame(user_id="user-1", token="secret-token-value"))

    messages = [r.getMessage() for r in caplog.records]
    assert any("connect rejected" in m and "agent-1" in m and "invalid_token" in m for m in messages)
    assert not any("secret-token-value" in m for m in messages)
