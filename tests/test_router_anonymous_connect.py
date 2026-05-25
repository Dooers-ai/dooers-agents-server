from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx

from dooers.auth_validation import AuthValidationClient, AuthValidationResult
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
        mock.post("https://core.test/validate").mock(return_value=httpx.Response(200, json={"valid": False, "reason": "expired"}))
        await router._handle_connect(ws, _make_connect_frame(user_id=""))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "CONNECTION_REJECTED"


@pytest.mark.asyncio
async def test_anonymous_fails_closed_on_upstream_5xx(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(return_value=httpx.Response(503, text="bad"))
        await router._handle_connect(ws, _make_connect_frame(user_id=""))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "CONNECTION_REJECTED"


@pytest.mark.asyncio
async def test_webhook_org_workspace_override_frame_values(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    frame = C2S_Connect(
        id="frame-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="fake-org",
            workspace_id="fake-ws",
            user=User(user_id=""),
            auth_token="tok",
        ),
    )

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
                    "organizationId": "real-org",
                    "workspaceId": "real-ws",
                },
            )
        )
        await router._handle_connect(ws, frame)

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._organization_id == "real-org"
    assert router._workspace_id == "real-ws"


@pytest.mark.asyncio
async def test_frame_values_used_when_webhook_omits_ids(validator):
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    frame = C2S_Connect(
        id="frame-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="frame-org",
            workspace_id="frame-ws",
            user=User(user_id=""),
            auth_token="tok",
        ),
    )

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
                },
            )
        )
        await router._handle_connect(ws, frame)

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._organization_id == "frame-org"
    assert router._workspace_id == "frame-ws"


@pytest.mark.asyncio
async def test_authenticated_user_without_validator_uses_frame_identity():
    """When no auth validator is configured, authenticated users fall back to frame identity."""
    router = _make_router(auth_validator=None)
    ws = FakeWebSocket()

    await router._handle_connect(ws, _make_connect_frame(user_id="user-123"))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._user is not None
    assert router._user.user_id == "user-123"
    assert router._rate_limits == {}

    # Ensure full setup happened on the authenticated path.
    assert router._agent_id == "agent-1"
    assert router._ws_id in router._subscriptions

    # upsert_thread_participant is only called on event.create, not on connect.
    router._persistence.upsert_thread_participant.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_user_with_valid_webhook(validator):
    """Authenticated users go through validation; successful response is sole source of truth."""
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": True,
                    "user": {
                        "user_id": "user-123",
                        "user_email": "user@test.com",
                        "identity_ids": [],
                        "system_role": "user",
                        "organization_role": "owner",
                        "workspace_role": "manager",
                    },
                    "rateLimits": {},
                    "organizationId": "org-1",
                    "workspaceId": "ws-1",
                },
            )
        )
        await router._handle_connect(ws, _make_connect_frame(user_id="user-123"))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._user is not None
    assert router._user.user_id == "user-123"
    assert router._user.organization_role == "owner"

    assert router._agent_id == "agent-1"
    assert router._ws_id in router._subscriptions


@pytest.mark.asyncio
async def test_authenticated_user_with_failed_webhook_is_rejected(validator):
    """Authenticated users are rejected when the validator returns an error."""
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(return_value=httpx.Response(500))
        await router._handle_connect(ws, _make_connect_frame(user_id="user-123"))

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "CONNECTION_REJECTED"


@pytest.mark.asyncio
async def test_guest_connect_merges_frame_metadata_into_user(validator):
    """Guest connections merge frame.payload.metadata into the webhook-seeded user.metadata.

    The merge is additive: webhook-supplied keys are preserved unless the frame
    overrides them. Frame-supplied keys win on ties.
    """
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    frame = C2S_Connect(
        id="frame-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="ws-1",
            user=User(user_id=""),
            auth_token="tok",
            metadata={"phone": "+15555550123", "company": "Acme"},
        ),
    )

    # Frame must actually carry metadata (otherwise Pydantic silently drops it
    # and there's nothing to merge or discard).
    assert frame.payload.metadata == {"phone": "+15555550123", "company": "Acme"}

    # Stub the validator: webhook returns a guest with a pre-existing metadata
    # bag (e.g. seeded by the public-chat-link resolver).
    validator.validate = AsyncMock(
        return_value=AuthValidationResult(
            valid=True,
            user=User(
                user_id="guest:abc",
                user_email="",
                connection_type="guest",
                metadata={"link_id": "pcl_uuid"},
            ),
            organization_id="org-1",
            workspace_id="ws-1",
            connection_type="guest",
        )
    )

    await router._handle_connect(ws, frame)

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._user is not None
    assert router._user.connection_type == "guest"
    # Frame-supplied keys are merged in.
    assert router._user.metadata["phone"] == "+15555550123"
    assert router._user.metadata["company"] == "Acme"
    # Webhook-supplied keys are preserved (additive merge).
    assert router._user.metadata["link_id"] == "pcl_uuid"


@pytest.mark.asyncio
async def test_dashboard_connect_discards_frame_metadata(validator):
    """Dashboard connections silently discard frame.payload.metadata.

    The webhook is the sole source of truth for authenticated sessions —
    a client-supplied metadata bag must not leak into the user state.
    """
    router = _make_router(auth_validator=validator)
    ws = FakeWebSocket()

    frame = C2S_Connect(
        id="frame-1",
        type="connect",
        payload=ConnectPayload(
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="ws-1",
            user=User(user_id="user-123"),
            auth_token="tok",
            metadata={"phone": "EVIL"},
        ),
    )

    # Frame must actually carry metadata so the discard policy has something
    # to ignore. If Pydantic drops the field silently, the test asserts the
    # field shape and fails early — the protection must be explicit.
    assert frame.payload.metadata == {"phone": "EVIL"}

    # Stub the validator: webhook returns a dashboard user with its own
    # metadata bag — the source of truth.
    validator.validate = AsyncMock(
        return_value=AuthValidationResult(
            valid=True,
            user=User(
                user_id="user-123",
                user_email="user@test.com",
                organization_role="owner",
                workspace_role="manager",
                connection_type="dashboard",
                metadata={"email_verified": True},
            ),
            organization_id="org-1",
            workspace_id="ws-1",
            connection_type="dashboard",
        )
    )

    await router._handle_connect(ws, frame)

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    assert router._user is not None
    assert router._user.connection_type == "dashboard"
    # Webhook-supplied metadata is intact.
    assert router._user.metadata.get("email_verified") is True
    # Frame-supplied metadata was discarded.
    assert "phone" not in router._user.metadata
