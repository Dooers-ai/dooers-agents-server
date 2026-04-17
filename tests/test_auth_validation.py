import httpx
import pytest
import pytest_asyncio
import respx

from dooers.auth_validation import AuthValidationClient


@pytest_asyncio.fixture
async def client():
    c = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_returns_valid_when_webhook_says_valid(client):
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
                    "rateLimits": {"messagesPerMinute": 20},
                    "threadTtlHours": 24,
                },
            )
        )
        result = await client.validate(auth_token="tok", agent_id="agent-1", guest_user_id="guest:abc")
    assert result.valid is True
    assert result.user is not None
    assert result.user.user_id == "guest:abc"
    # Guests are always member-scope; the webhook cannot escalate.
    assert result.user.system_role == "user"
    assert result.user.organization_role == "member"
    assert result.user.workspace_role == "member"
    assert result.rate_limits["messagesPerMinute"] == 20
    assert result.thread_ttl_hours == 24


@pytest.mark.asyncio
async def test_returns_invalid_on_false(client):
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(return_value=httpx.Response(200, json={"valid": False, "reason": "expired"}))
        result = await client.validate(auth_token="tok", agent_id="a", guest_user_id="g")
    assert result.valid is False
    assert result.reason == "expired"


@pytest.mark.asyncio
async def test_fails_closed_on_network_error(client):
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(side_effect=httpx.ConnectError("boom"))
        result = await client.validate(auth_token="tok", agent_id="a", guest_user_id="g")
    assert result.valid is False
    assert result.reason == "transport_error"


@pytest.mark.asyncio
async def test_fails_closed_on_5xx(client):
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(return_value=httpx.Response(503, text="bad"))
        result = await client.validate(auth_token="tok", agent_id="a", guest_user_id="g")
    assert result.valid is False
    assert result.reason == "upstream_503"


@pytest.mark.asyncio
async def test_parses_organization_and_workspace_ids(client):
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
                    "rateLimits": {"messagesPerMinute": 20},
                    "threadTtlHours": 24,
                    "organizationId": "real-org",
                    "workspaceId": "real-ws",
                },
            )
        )
        result = await client.validate(auth_token="tok", agent_id="agent-1", guest_user_id="guest:abc")
    assert result.valid is True
    assert result.organization_id == "real-org"
    assert result.workspace_id == "real-ws"


@pytest.mark.asyncio
async def test_organization_and_workspace_default_to_none(client):
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
        result = await client.validate(auth_token="tok", agent_id="agent-1", guest_user_id="guest:abc")
    assert result.valid is True
    assert result.organization_id is None
    assert result.workspace_id is None


@pytest.mark.asyncio
async def test_fails_closed_on_non_200():
    c = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
    try:
        with respx.mock() as mock:
            mock.post("https://core.test/validate").mock(return_value=httpx.Response(404, text="no"))
            result = await c.validate(auth_token="tok", agent_id="a", guest_user_id="g")
        assert result.valid is False
        assert result.reason == "status_404"
    finally:
        await c.close()
