"""Core-issued JWTs are validated against the configured core, never a URL taken from the token."""

import base64
import json

import httpx
import pytest
import respx

from dooers.agents.server.auth_validation import AuthValidationClient

CORE = "https://core.test"
LEGACY = "https://legacy.test/validate-connection"
DASHBOARD_URL = f"{CORE}/api/v2/identity/validate-agent-session"
PUBLIC_CHAT_URL = f"{CORE}/api/v2/public-chats/validate-session"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt(payload: dict) -> str:
    return f"{_b64({'alg': 'HS256'})}.{_b64(payload)}.signature"


def _dashboard_context(agent_id: str = "agent-1") -> dict:
    return {
        "valid": True,
        "connection_type": "dashboard",
        "user": {"id": "u1", "email": "u1@test", "name": "U1", "identity_ids": [], "system_role": "user"},
        "organization": {"id": "org-1", "role": "member", "plan": "free"},
        "workspace": {"id": "ws-1", "role": "member"},
        "agent": {"id": agent_id, "owner_user_id": "owner", "can_configure_settings": False},
        "policies": {"rate_limit_msgs_per_min": None, "thread_ttl_hours": None},
    }


async def _validate(client: AuthValidationClient, token: str):
    return await client.validate(auth_token=token, agent_id="agent-1", guest_user_id="", user_id="u1")


@pytest.mark.asyncio
async def test_dashboard_token_uses_configured_core_not_claim():
    client = AuthValidationClient(url=LEGACY, core_base_url=CORE)
    token = _jwt(
        {
            "iss": "dooers-service-core",
            "worker_id": "agent-1",
            "validation_url": "https://elsewhere.test/validate",
        }
    )
    try:
        with respx.mock(assert_all_called=False) as mock:
            core = mock.post(DASHBOARD_URL).mock(return_value=httpx.Response(200, json={"valid": False, "reason": "invalid_token"}))
            foreign = mock.post("https://elsewhere.test/validate").mock(return_value=httpx.Response(200, json=_dashboard_context()))
            result = await _validate(client, token)
        assert core.called
        assert not foreign.called
        assert result.valid is False
        assert result.reason == "invalid_token"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dashboard_token_valid_response_from_core_is_parsed():
    client = AuthValidationClient(url=LEGACY, core_base_url=CORE)
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1", "validation_url": DASHBOARD_URL})
    try:
        with respx.mock() as mock:
            mock.post(DASHBOARD_URL).mock(return_value=httpx.Response(200, json=_dashboard_context()))
            result = await _validate(client, token)
        assert result.valid is True
        assert result.user is not None and result.user.user_id == "u1"
        assert result.agent_id == "agent-1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_chat_session_token_goes_to_configured_core_public_chat_endpoint():
    client = AuthValidationClient(url=LEGACY, core_base_url=CORE)
    token = _jwt(
        {
            "iss": "dooers-service-core",
            "session_token": "opaque",
            "agent_id": "agent-1",
            "validation_url": "https://elsewhere.test/validate-session",
        }
    )
    try:
        with respx.mock() as mock:
            route = mock.post(PUBLIC_CHAT_URL).mock(return_value=httpx.Response(200, json={"valid": False, "reason": "expired"}))
            result = await _validate(client, token)
        assert route.called
        assert json.loads(route.calls.last.request.content) == {"token": token}
        assert result.reason == "expired"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_core_base_url_trailing_slash_is_normalized():
    client = AuthValidationClient(url=LEGACY, core_base_url=f"{CORE}/")
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1"})
    try:
        with respx.mock() as mock:
            route = mock.post(DASHBOARD_URL).mock(return_value=httpx.Response(200, json={"valid": False}))
            await _validate(client, token)
        assert route.called
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_core_token_uses_operator_configured_legacy_url():
    client = AuthValidationClient(url=LEGACY, core_base_url=CORE)
    token = _jwt({"iss": "someone-else", "validation_url": "https://elsewhere.test/other"})
    try:
        with respx.mock() as mock:
            route = mock.post(LEGACY).mock(return_value=httpx.Response(200, json={"valid": False, "reason": "nope"}))
            result = await _validate(client, token)
        assert route.called
        assert result.reason == "nope"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_base", ["", "   ", "ftp://core.test", "core.test"])
async def test_invalid_core_base_url_fails_closed_without_network(bad_base):
    client = AuthValidationClient(url=LEGACY, core_base_url=bad_base)
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1", "validation_url": DASHBOARD_URL})
    try:
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(200, json=_dashboard_context()))
            result = await _validate(client, token)
        assert not anything.called
        assert result.valid is False
        assert result.reason == "core_base_url_not_configured"
    finally:
        await client.close()


def test_default_core_base_url_is_platform_core():
    client = AuthValidationClient(url=LEGACY)
    assert client.core_base_url == "https://api.dooers.ai"
