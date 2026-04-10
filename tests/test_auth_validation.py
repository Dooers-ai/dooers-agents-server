import httpx
import pytest
import respx

from dooers.auth_validation import AuthValidationClient


@pytest.mark.asyncio
async def test_returns_valid_when_webhook_says_valid():
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
        client = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
        result = await client.validate(
            auth_token="tok", agent_id="agent-1", guest_user_id="guest:abc"
        )
    assert result.valid is True
    assert result.user is not None
    assert result.user.user_id == "guest:abc"
    assert result.rate_limits["messagesPerMinute"] == 20
    assert result.thread_ttl_hours == 24


@pytest.mark.asyncio
async def test_returns_invalid_on_false():
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(200, json={"valid": False, "reason": "expired"})
        )
        client = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
        result = await client.validate(auth_token="tok", agent_id="a", guest_user_id="g")
    assert result.valid is False
    assert result.reason == "expired"


@pytest.mark.asyncio
async def test_fails_closed_on_network_error():
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            side_effect=httpx.ConnectError("boom")
        )
        client = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
        result = await client.validate(auth_token="tok", agent_id="a", guest_user_id="g")
    assert result.valid is False
    assert result.reason == "transport_error"


@pytest.mark.asyncio
async def test_fails_closed_on_5xx():
    with respx.mock() as mock:
        mock.post("https://core.test/validate").mock(
            return_value=httpx.Response(503, text="bad")
        )
        client = AuthValidationClient(url="https://core.test/validate", timeout=2.0)
        result = await client.validate(auth_token="tok", agent_id="a", guest_user_id="g")
    assert result.valid is False
    assert result.reason is not None
    assert "503" in result.reason
