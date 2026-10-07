"""Core-issued JWTs are validated against the configured core, never a URL taken from the token."""

import base64
import json

import httpx
import pytest
import respx

from dooers.agents.server.auth_validation import (
    DASHBOARD_VALIDATION_PATH,
    PUBLIC_CHAT_VALIDATION_PATH,
    AuthValidationClient,
)

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


# --- Backwards compatibility when AGENT_CORE_BASE_URL is not configured -------------------------

DEV_CORE = "https://api-v2.dev.dooers.ai"
PROD_DASHBOARD_URL = f"https://api.dooers.ai{DASHBOARD_VALIDATION_PATH}"


@pytest.mark.asyncio
@pytest.mark.parametrize("core_origin", ["https://api.dooers.ai", DEV_CORE, "https://api.dev.dooers.ai"])
async def test_unconfigured_client_keeps_using_a_known_dooers_core_from_the_token(core_origin):
    """Agents without AGENT_CORE_BASE_URL (e.g. `dooers run` against dev) keep working."""
    client = AuthValidationClient(url=LEGACY)
    expected = f"{core_origin}{DASHBOARD_VALIDATION_PATH}"
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1", "validation_url": expected})
    try:
        with respx.mock() as mock:
            route = mock.post(expected).mock(return_value=httpx.Response(200, json=_dashboard_context()))
            result = await _validate(client, token)
        assert route.called
        assert result.valid is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unconfigured_client_keeps_public_chat_on_a_known_dev_core():
    client = AuthValidationClient(url=LEGACY)
    expected = f"{DEV_CORE}{PUBLIC_CHAT_VALIDATION_PATH}"
    token = _jwt({"iss": "dooers-service-core", "session_token": "s", "agent_id": "agent-1", "validation_url": expected})
    try:
        with respx.mock() as mock:
            route = mock.post(expected).mock(return_value=httpx.Response(200, json={"valid": False, "reason": "expired"}))
            await _validate(client, token)
        assert route.called
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claimed",
    [
        "https://elsewhere.test/api/v2/identity/validate-agent-session",
        "http://api.dooers.ai/api/v2/identity/validate-agent-session",
        "https://api.dooers.ai:8443/api/v2/identity/validate-agent-session",
        "https://api.dooers.ai/some/other/path",
        "https://user@api.dooers.ai/api/v2/identity/validate-agent-session",
    ],
)
async def test_unconfigured_client_sends_anything_else_to_the_default_core(claimed):
    client = AuthValidationClient(url=LEGACY)
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1", "validation_url": claimed})
    try:
        with respx.mock(assert_all_called=False) as mock:
            default = mock.post(PROD_DASHBOARD_URL).mock(return_value=httpx.Response(200, json={"valid": False, "reason": "invalid_token"}))
            other = mock.route().mock(return_value=httpx.Response(200, json=_dashboard_context()))
            result = await _validate(client, token)
        assert default.called
        assert not other.called
        assert result.valid is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_configured_core_wins_over_another_known_core_in_the_token():
    client = AuthValidationClient(url=LEGACY, core_base_url="https://api.dooers.ai")
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1", "validation_url": f"{DEV_CORE}{DASHBOARD_VALIDATION_PATH}"})
    try:
        with respx.mock(assert_all_called=False) as mock:
            prod = mock.post(PROD_DASHBOARD_URL).mock(return_value=httpx.Response(200, json={"valid": False}))
            dev = mock.post(f"{DEV_CORE}{DASHBOARD_VALIDATION_PATH}").mock(return_value=httpx.Response(200, json=_dashboard_context()))
            await _validate(client, token)
        assert prod.called
        assert not dev.called
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_local_runtime_binding_still_applies(monkeypatch):
    monkeypatch.setenv("DOOERS_RUNTIME_ID", "rt-1")
    client = AuthValidationClient(url=LEGACY, core_base_url=CORE)
    token = _jwt({"iss": "dooers-service-core", "worker_id": "agent-1", "runtime_id": "rt-other"})
    try:
        with respx.mock() as mock:
            mock.post(DASHBOARD_URL).mock(return_value=httpx.Response(200, json=_dashboard_context()))
            result = await _validate(client, token)
        assert result.valid is False
        assert result.reason == "runtime_mismatch"
    finally:
        await client.close()
