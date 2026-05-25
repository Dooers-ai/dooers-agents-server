"""Tests for the handler-facing AgentContext exposing organization/workspace/agent metadata."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.auth_validation import AuthValidationClient, AuthValidationResult
from dooers.handlers.context import AgentContext
from dooers.handlers.router import Router
from dooers.protocol.frames import (
    C2S_Connect,
    C2S_EventCreate,
    ConnectPayload,
    EventCreateEventPayload,
    EventCreatePayload,
)
from dooers.protocol.models import User, WireC2S_TextPart
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


class StubAuthValidator(AuthValidationClient):
    """AuthValidationClient stub returning a fixed AuthValidationResult."""

    def __init__(self, result: AuthValidationResult) -> None:
        # Skip parent __init__ — we don't want to create an httpx client.
        self._result = result

    async def close(self) -> None:  # pragma: no cover - unused
        return

    async def validate(self, **kwargs: Any) -> AuthValidationResult:
        return self._result


def _make_stub_validation_result() -> AuthValidationResult:
    user = User(
        user_id="usr_1",
        user_name="Alice",
        user_email="alice@example.com",
        system_role="user",
        organization_role="owner",
        workspace_role="manager",
        connection_type="dashboard",
        metadata={"sso_claims": {"groups": ["eng"]}},
    )
    return AuthValidationResult(
        valid=True,
        user=user,
        rate_limits={},
        thread_ttl_hours=None,
        organization_id="org_1",
        workspace_id="ws_1",
        connection_type="dashboard",
        agent_id="wkr_1",
        agent_owner_user_id="usr_1",
        organization_plan="pro",
        organization_metadata={"settings": {"plan": "pro"}, "name": "Acme"},
        workspace_metadata={"name": "Team A"},
        agent_metadata={"blueprint_id": "bp_1", "blueprint_name": "Test Blueprint"},
    )


@pytest.mark.asyncio
async def test_handler_receives_organization_workspace_agent_metadata() -> None:
    """Handler should see organization, workspace, and agent dataclasses populated
    from the AuthValidationResult, including their metadata bags."""

    captured: dict[str, AgentContext] = {}

    async def capturing_handler(on, send, memory, analytics, settings):
        captured["ctx"] = on.context
        yield send.text("ok")

    validator = StubAuthValidator(_make_stub_validation_result())

    # In-memory persistence stub via AsyncMock + thread store side-effect.
    persistence = AsyncMock()
    created_threads: list = []

    async def _create_thread(thread):
        created_threads.append(thread)

    async def _get_thread(thread_id):
        for t in created_threads:
            if t.id == thread_id:
                return t
        return None

    persistence.create_thread.side_effect = _create_thread
    persistence.get_thread.side_effect = _get_thread
    persistence.create_event.return_value = None
    persistence.update_event.return_value = None
    persistence.update_thread.return_value = None
    persistence.upsert_thread_participant.return_value = None

    registry = ConnectionRegistry()
    subscriptions: dict[str, set[str]] = {}

    router = Router(
        persistence=persistence,
        handler=capturing_handler,
        registry=registry,
        subscriptions=subscriptions,
        auth_validator=validator,
    )

    ws = FakeWebSocket()

    # Connect — the stub validator returns the populated result regardless of inputs.
    connect_frame = C2S_Connect(
        id="connect-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="wkr_1",
            organization_id="org_1",
            workspace_id="ws_1",
            user=User(user_id=""),
            auth_token="tok",
        ),
    )
    await router._handle_connect(ws, connect_frame)

    event_frame = C2S_EventCreate(
        id="event-1",
        type="event.create",
        payload=EventCreatePayload(
            thread_id=None,
            client_event_id="cli-1",
            event=EventCreateEventPayload(
                type="message",
                actor="user",
                content=[WireC2S_TextPart(text="hello")],
            ),
            metadata=None,
        ),
    )
    await router._handle_event_create(ws, event_frame)

    ctx = captured.get("ctx")
    assert ctx is not None, "handler was not invoked"

    # Organization assertions
    assert ctx.organization.id == "org_1"
    assert ctx.organization.role == "owner"
    assert ctx.organization.plan == "pro"
    assert ctx.organization.metadata["settings"]["plan"] == "pro"
    assert ctx.organization.metadata["name"] == "Acme"

    # Workspace assertions
    assert ctx.workspace.id == "ws_1"
    assert ctx.workspace.role == "manager"
    assert ctx.workspace.metadata["name"] == "Team A"

    # Agent assertions
    assert ctx.agent.id == "wkr_1"
    assert ctx.agent.owner_user_id == "usr_1"
    assert ctx.agent.metadata["blueprint_id"] == "bp_1"
    assert ctx.agent.metadata["blueprint_name"] == "Test Blueprint"

    # User metadata assertion
    assert ctx.user.metadata["sso_claims"] == {"groups": ["eng"]}
