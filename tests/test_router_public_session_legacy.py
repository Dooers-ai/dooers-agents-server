"""Legacy (production) public chat: the guest token is validated only on connect."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.agents.server.auth_validation import AuthValidationResult
from dooers.agents.server.handlers.router import Router
from dooers.agents.server.protocol.frames import (
    C2S_Connect,
    C2S_Ping,
    C2S_ThreadList,
    ConnectPayload,
    ThreadListPayload,
)
from dooers.agents.server.protocol.models import User
from dooers.agents.server.registry import ConnectionRegistry


class CountingValidator:
    def __init__(self, connection_type: str = "guest") -> None:
        self.connection_type = connection_type
        self.calls = 0

    async def validate(self, **kwargs: Any) -> AuthValidationResult:
        self.calls += 1
        user = User(user_id="guest:abc", connection_type=self.connection_type)
        return AuthValidationResult(
            valid=True,
            user=user,
            connection_type=self.connection_type,
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="ws-1",
        )


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
        yield


def _make_router(validator: CountingValidator) -> Router:
    persistence = AsyncMock()
    persistence.list_threads.return_value = []
    return Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
        auth_validator=validator,  # type: ignore[arg-type]
    )


async def _connected_guest(validator: CountingValidator) -> tuple[Router, FakeWebSocket]:
    router = _make_router(validator)
    ws = FakeWebSocket()
    frame = C2S_Connect(
        id="connect-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="ws-1",
            user=User(user_id=""),
            auth_token="tok",
        ),
    )
    await router.route(ws, frame)
    assert json.loads(ws.sent[-1])["payload"]["ok"] is True
    return router, ws


async def test_guest_session_validates_only_on_connect():
    validator = CountingValidator(connection_type="guest")
    router, ws = await _connected_guest(validator)
    assert validator.calls == 1
    await router.route(ws, C2S_ThreadList(id="list-1", type="thread.list", payload=ThreadListPayload()))
    await router.route(ws, C2S_Ping(id="ping-1", type="ping", payload={}))
    assert validator.calls == 1
    assert json.loads(ws.sent[-1])["type"] == "pong"


async def test_guest_broadcast_does_not_revalidate():
    validator = CountingValidator(connection_type="guest")
    router, ws = await _connected_guest(validator)
    delivered = await router._registry.broadcast("agent-1", "broadcast-frame")
    assert delivered == 1
    assert validator.calls == 1
    assert ws.sent[-1] == "broadcast-frame"
