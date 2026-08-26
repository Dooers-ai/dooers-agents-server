import httpx
import pytest
import pytest_asyncio
import respx

from dooers.agents.server.observability.service_token import ServiceTokenClient

CORE_URL = "https://core.test"
TOKEN_URL = f"{CORE_URL}/api/v2/identity/service-token/agent"


@pytest_asyncio.fixture
async def client():
    c = ServiceTokenClient(core_base_url=CORE_URL)
    try:
        yield c
    finally:
        await c.aclose()


def _token_response(access_token: str = "jwt-1", expires_in: int = 300) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "data": {"accessToken": access_token, "expiresIn": expires_in}}
    )


@pytest.mark.asyncio
async def test_get_token_fetches_and_returns_access_token(client):
    with respx.mock() as mock:
        route = mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-1"))
        token = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    assert token == "jwt-1"
    assert route.called
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body == {
        "audience": "otel-service",
        "workerId": "agent-1",
        "workspaceId": "ws-1",
        "runtimeApiKey": "key-1",
        "scopes": ["otel:write"],
    }


@pytest.mark.asyncio
async def test_get_token_supports_rag_audience_and_scopes(client):
    with respx.mock() as mock:
        route = mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-rag"))
        token = await client.get_token(
            agent_id="agent-1",
            workspace_id="ws-1",
            runtime_api_key="key-1",
            audience="rag-service",
            scopes=["rag:read", "rag:write"],
        )

    assert token == "jwt-rag"
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["audience"] == "rag-service"
    assert body["scopes"] == ["rag:read", "rag:write"]
    assert route.called


@pytest.mark.asyncio
async def test_get_token_caches_separately_per_audience(client):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(
            side_effect=[_token_response("jwt-otel"), _token_response("jwt-rag")]
        )
        otel = await client.get_token(
            agent_id="agent-1",
            workspace_id="ws-1",
            runtime_api_key="key-1",
            audience="otel-service",
            scopes=["otel:write"],
        )
        rag = await client.get_token(
            agent_id="agent-1",
            workspace_id="ws-1",
            runtime_api_key="key-1",
            audience="rag-service",
            scopes=["rag:read", "rag:write"],
        )

    assert otel == "jwt-otel"
    assert rag == "jwt-rag"


@pytest.mark.asyncio
async def test_get_token_omits_workspace_id_for_personal_chat(client):
    with respx.mock() as mock:
        route = mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-personal"))
        token = await client.get_token(agent_id="agent-1", workspace_id="", runtime_api_key="key-1")

    assert token == "jwt-personal"
    import json

    body = json.loads(route.calls.last.request.content)
    assert "workspaceId" not in body
    assert body["workerId"] == "agent-1"


@pytest.mark.asyncio
async def test_get_token_uses_cache_without_refetching(client):
    with respx.mock() as mock:
        route = mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-1"))
        first = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")
        second = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    assert first == second == "jwt-1"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_get_token_refreshes_when_close_to_expiry(client):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-stale", expires_in=30))
        await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    # 30s TTL is inside the 60s refresh margin — the next call must refetch, not reuse the cache.
    with respx.mock() as mock:
        route = mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-fresh", expires_in=300))
        token = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    assert token == "jwt-fresh"
    assert route.called


@pytest.mark.asyncio
async def test_different_agents_get_independent_tokens(client):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(
            side_effect=[_token_response("jwt-agent-1"), _token_response("jwt-agent-2")]
        )
        token_1 = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")
        token_2 = await client.get_token(agent_id="agent-2", workspace_id="ws-2", runtime_api_key="key-2")

    assert token_1 == "jwt-agent-1"
    assert token_2 == "jwt-agent-2"


@pytest.mark.asyncio
async def test_get_token_returns_none_on_transport_error_without_cache(client):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))
        token = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    assert token is None


@pytest.mark.asyncio
async def test_get_token_reuses_stale_token_when_renewal_fails(client):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_response("jwt-1", expires_in=30))
        await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        token = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    # Core is down for renewal — better to retry with a maybe-expired token than skip entirely.
    assert token == "jwt-1"


@pytest.mark.asyncio
async def test_get_token_returns_none_on_malformed_response(client):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"success": True}))
        token = await client.get_token(agent_id="agent-1", workspace_id="ws-1", runtime_api_key="key-1")

    assert token is None

class _FakeSecretsPersistence:
    def __init__(self, secrets: dict | None = None, *, fail: bool = False):
        self._secrets = secrets or {}
        self._fail = fail

    async def get_service_secrets(self, agent_id: str) -> dict:
        if self._fail:
            raise RuntimeError("db down")
        return self._secrets


@pytest.mark.asyncio
async def test_resolve_runtime_api_key_prefers_service_secrets(monkeypatch):
    from dooers.agents.server.observability.service_token import (
        RUNTIME_API_KEY_SECRET_NAME,
        resolve_runtime_api_key,
    )

    monkeypatch.setenv("AGENT_SEED_SECRET", "env-key-should-not-win")
    persistence = _FakeSecretsPersistence({RUNTIME_API_KEY_SECRET_NAME: "db-key"})
    assert await resolve_runtime_api_key(persistence, "agent-1") == "db-key"


@pytest.mark.asyncio
async def test_resolve_runtime_api_key_falls_back_to_agent_seed_secret(monkeypatch):
    from dooers.agents.server.observability.service_token import resolve_runtime_api_key

    monkeypatch.setenv("AGENT_SEED_SECRET", "env-runtime-key")
    persistence = _FakeSecretsPersistence({})
    assert await resolve_runtime_api_key(persistence, "agent-1") == "env-runtime-key"


@pytest.mark.asyncio
async def test_resolve_runtime_api_key_returns_none_when_missing(monkeypatch):
    from dooers.agents.server.observability.service_token import resolve_runtime_api_key

    monkeypatch.delenv("AGENT_SEED_SECRET", raising=False)
    persistence = _FakeSecretsPersistence({})
    assert await resolve_runtime_api_key(persistence, "agent-1") is None


@pytest.mark.asyncio
async def test_resolve_runtime_api_key_falls_back_when_secrets_read_fails(monkeypatch):
    from dooers.agents.server.observability.service_token import resolve_runtime_api_key

    monkeypatch.setenv("AGENT_SEED_SECRET", "env-after-db-error")
    persistence = _FakeSecretsPersistence(fail=True)
    assert await resolve_runtime_api_key(persistence, "agent-1") == "env-after-db-error"


@pytest.mark.asyncio
async def test_resolve_uses_configured_process_seed_when_env_empty(monkeypatch):
    from dooers.agents.server.observability import service_token as st

    monkeypatch.delenv("AGENT_SEED_SECRET", raising=False)
    st.configure_process_seed_secret("from-agent-config")
    try:
        persistence = _FakeSecretsPersistence({})
        assert await st.resolve_runtime_api_key(persistence, "agent-1") == "from-agent-config"
    finally:
        st.configure_process_seed_secret("")
