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


@pytest.mark.asyncio
async def test_jwt_validation_threads_metadata(client):
    """JWT response with metadata at each level is folded into the result."""
    import base64
    import json as _json

    payload = {"validation_url": "https://validate.dooers.ai/api/v2/identity/validate-agent-session"}
    fake_jwt = "x." + base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=") + ".y"

    with respx.mock() as mock:
        mock.post("https://validate.dooers.ai/api/v2/identity/validate-agent-session").mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": True,
                    "connection_type": "dashboard",
                    "user": {
                        "id": "usr_1", "email": "a@b.test", "name": "Ada",
                        "identity_ids": ["acc_1"], "system_role": "user",
                        "metadata": {
                            "email_verified": True,
                            "member_id": "mem_1",
                            "sso_claims": {"groups": ["eng"]},
                        },
                    },
                    "organization": {
                        "id": "org_1", "role": "owner", "plan": "pro",
                        "metadata": {"name": "Acme", "settings": {"plan": "pro"}},
                    },
                    "workspace": {
                        "id": "ws_1", "role": "manager",
                        "metadata": {"name": "Team A", "is_public": False},
                    },
                    "agent": {
                        "id": "wkr_1", "owner_user_id": "usr_1",
                        "metadata": {"display_name": "AgentX", "blueprint_id": "bp_1"},
                    },
                    "policies": {"rate_limit_msgs_per_min": 60, "thread_ttl_hours": 24},
                },
            )
        )
        result = await client.validate(auth_token=fake_jwt, agent_id="wkr_1", guest_user_id="")

    assert result.valid is True
    assert result.user is not None
    assert result.user.metadata == {
        "email_verified": True,
        "member_id": "mem_1",
        "sso_claims": {"groups": ["eng"]},
    }
    assert result.organization_metadata == {"name": "Acme", "settings": {"plan": "pro"}}
    assert result.workspace_metadata == {"name": "Team A", "is_public": False}
    assert result.agent_metadata == {"display_name": "AgentX", "blueprint_id": "bp_1"}


@pytest.mark.asyncio
async def test_legacy_nested_response_propagates_connection_type_to_user(client):
    """Regression: opaque-token guest webhooks must surface connection_type on User.

    The router gates the guest display-field merge and the inbound metadata merge on
    self._user.connection_type == 'guest'. Without this propagation, every public-chat
    guest connection in production looks like a dashboard connection to the router.
    """
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": True,
                    "connection_type": "guest",
                    "user": {
                        "id": "guest:abc", "email": "", "name": "",
                        "identity_ids": [], "system_role": "guest",
                    },
                    "organization": {"id": "org_1", "role": "guest", "plan": "free"},
                    "workspace": {"id": "ws_1", "role": "guest"},
                    "agent": {"id": "wkr_1", "owner_user_id": None},
                    "policies": {"rate_limit_msgs_per_min": 20, "thread_ttl_hours": 24},
                },
            )
        )
        result = await client.validate(
            auth_token="opaque-session", agent_id="wkr_1", guest_user_id="guest:abc"
        )

    assert result.valid is True
    assert result.user is not None
    assert result.user.connection_type == "guest"


@pytest.mark.asyncio
async def test_legacy_flat_response_defaults_metadata_to_empty(client):
    """Legacy (no nested organization key) responses set metadata to {} without raising."""
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": True,
                    "user": {
                        "user_id": "guest:abc", "user_email": "", "identity_ids": [], "roles": [],
                    },
                    "rateLimits": {},
                    "threadTtlHours": 24,
                },
            )
        )
        result = await client.validate(auth_token="opaque", agent_id="wkr_1", guest_user_id="guest:abc")

    assert result.valid is True
    assert result.user is not None
    assert result.user.metadata == {}
    assert result.organization_metadata == {}
    assert result.workspace_metadata == {}
    assert result.agent_metadata == {}
