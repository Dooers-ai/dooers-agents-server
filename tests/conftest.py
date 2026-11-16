"""Shared fixtures. Connector tasks extend this file (pipeline_harness, svc, turn...)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.agents.server.handlers.router import Router
from dooers.agents.server.protocol.frames import C2S_EventCreate, EventCreatePayload
from dooers.agents.server.protocol.models import User
from dooers.agents.server.registry import ConnectionRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:  # pragma: no cover
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        pass


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


@dataclass
class RouterHarness:
    router: Router
    ws: _FakeWS
    persistence: AsyncMock
    contexts: list[Any] = field(default_factory=list)

    async def send_event_create(self, payload: dict[str, Any]) -> None:
        frame = C2S_EventCreate(
            id="frame-1",
            type="event.create",
            payload=EventCreatePayload.model_validate(payload),
        )
        await self.router._handle_event_create(self.ws, frame)


@pytest.fixture
def router_harness() -> RouterHarness:
    """A connected Router with a mock persistence; the real pipeline.setup runs,
    pipeline.execute is replaced by a recorder of the HandlerContext it receives."""
    persistence = AsyncMock()
    router = Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
    )
    router._agent_id = "agent-1"
    router._organization_id = "org-1"
    router._workspace_id = "ws-1"
    router._user = User(user_id="u1")
    router._ws_id = "ws-id-1"
    router._subscriptions["ws-id-1"] = set()
    harness = RouterHarness(router=router, ws=_FakeWS(), persistence=persistence)

    async def _execute(context, result):
        harness.contexts.append(context)
        return
        yield  # pragma: no cover

    router._pipeline.execute = _execute  # type: ignore[method-assign]
    return harness
