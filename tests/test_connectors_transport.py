from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from dooers.tools.connectors import presence
from dooers.tools.connectors.errors import ConnectorError
from dooers.tools.connectors.presence import connector_session
from dooers.tools.connectors.runtime import TurnContext, bind_turn
from dooers.tools.connectors.transport import ConnectorTransport

CORE = "https://core.test"
SERVICE = "https://connectors.test"
TOKEN_URL = f"{CORE}/api/v2/identity/service-token/agent"
SESSION_URL = f"{CORE}/api/v2/identity/connector-session"


class _Persistence:
    def __init__(self, key: str | None = "runtime-key") -> None:
        self.key = key

    async def get_service_secrets(self, agent_id: str) -> dict:
        return {"dooers_runtime_api_key": self.key} if self.key else {}


def _turn(presence_token: str | None = "presence-1", *, agent_id="agent-1", user_id="u1", key="runtime-key") -> TurnContext:
    return TurnContext(
        agent_id=agent_id,
        organization_id="org-1",
        workspace_id="ws-1",
        thread_id="th-1",
        user_id=user_id,
        channel="web",
        presence_token=presence_token,
        persistence=_Persistence(key),  # type: ignore[arg-type]
        emit=None,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_CORE_BASE_URL", CORE)
    monkeypatch.setenv("DOOERS_CONNECTOR_SERVICE_URL", SERVICE)
    monkeypatch.delenv("AGENT_SEED_SECRET", raising=False)


def bound(turn: TurnContext) -> None:
    # Each async test runs in its own task context, so the binding never leaks to other tests.
    bind_turn(turn)


def _token_ok(token="svc-jwt"):
    return httpx.Response(200, json={"success": True, "data": {"accessToken": token, "expiresIn": 300}})


def _session_ok(token="sess-1", expires_at="2030-01-01T00:10:00Z"):
    return httpx.Response(200, json={"success": True, "data": {"token": token, "expiresAt": expires_at}})


@pytest.mark.asyncio
async def test_post_carries_context_session_and_service_bearer():
    bound(_turn())
    with respx.mock() as mock:
        token_route = mock.post(TOKEN_URL).mock(return_value=_token_ok("svc-jwt"))
        mock.post(SESSION_URL).mock(return_value=_session_ok("sess-1"))
        route = mock.post(f"{SERVICE}/v1/tools/list").mock(return_value=httpx.Response(200, json={"tools": []}))
        result = await ConnectorTransport().post("/v1/tools/list", {"only": None})

    assert result == {"tools": []}
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer svc-jwt"
    body = json.loads(req.content)
    assert body["context"] == {"agent_id": "agent-1", "workspace_id": "ws-1", "thread_id": "th-1", "channel": "web"}
    assert body["connector_session"] == "sess-1"
    assert body["on_behalf_of"] is None
    assert body["only"] is None
    minted = json.loads(token_route.calls.last.request.content)
    assert minted["audience"] == "connector-service"
    assert minted["scopes"] == ["connectors:read", "connectors:execute"]
    assert minted["workerId"] == "agent-1"


@pytest.mark.asyncio
async def test_post_on_behalf_of_only_when_explicit():
    bound(_turn(presence_token=None))
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_ok())
        route = mock.post(f"{SERVICE}/v1/tools/execute").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        await ConnectorTransport().post("/v1/tools/execute", {"name": "x"}, on_behalf_of="user-9")

    body = json.loads(route.calls.last.request.content)
    assert body["on_behalf_of"] == "user-9"
    assert body["connector_session"] is None


@pytest.mark.asyncio
async def test_session_exchange_is_cached_within_the_turn():
    bound(_turn())
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_ok())
        session = mock.post(SESSION_URL).mock(return_value=_session_ok("sess-1"))
        mock.post(f"{SERVICE}/v1/tools/list").mock(return_value=httpx.Response(200, json={"tools": []}))
        t = ConnectorTransport()
        await t.post("/v1/tools/list", {})
        await t.post("/v1/tools/list", {})

    assert session.call_count == 1
    sent = json.loads(session.calls.last.request.content)
    assert sent == {"presenceToken": "presence-1", "agentId": "agent-1", "runtimeApiKey": "runtime-key"}


@pytest.mark.asyncio
async def test_session_cache_expires_30s_before_expires_at(monkeypatch):
    now = [1_900_000_000.0]
    monkeypatch.setattr(presence, "_now", lambda: now[0])
    # expiresAt = now + 600
    exp = "2030-03-17T17:56:40Z"  # 1_900_000_600
    turn = _turn()
    with respx.mock() as mock:
        session = mock.post(SESSION_URL).mock(return_value=_session_ok("sess-1", exp))
        assert await connector_session(turn) == "sess-1"
        now[0] += 560  # 40s left: still cached
        assert await connector_session(turn) == "sess-1"
        assert session.call_count == 1
        now[0] += 15  # 25s left: inside the 30s margin
        await connector_session(turn)
        assert session.call_count == 2


@pytest.mark.asyncio
async def test_session_cache_never_crosses_people_or_agents():
    with respx.mock() as mock:
        session = mock.post(SESSION_URL).mock(side_effect=[_session_ok("sess-a"), _session_ok("sess-b"), _session_ok("sess-c")])
        a = await connector_session(_turn("presence-a"))
        b = await connector_session(_turn("presence-b", user_id="u2"))
        c = await connector_session(_turn("presence-a", agent_id="agent-2"))

    assert (a, b, c) == ("sess-a", "sess-b", "sess-c")
    assert session.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_session_exchange_denied_returns_none(status, caplog):
    with respx.mock() as mock:
        mock.post(SESSION_URL).mock(return_value=httpx.Response(status, json={"success": False}))
        with caplog.at_level("WARNING"):
            assert await connector_session(_turn()) is None
    assert "presence-1" not in caplog.text
    assert "runtime-key" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="oops"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"success": True}),
        httpx.Response(200, json={"success": True, "data": []}),
        httpx.Response(200, json={"success": True, "data": {"token": 123, "expiresAt": "2030-01-01T00:10:00Z"}}),
        httpx.Response(200, json={"success": True, "data": {"token": "", "expiresAt": "2030-01-01T00:10:00Z"}}),
    ],
    ids=["500", "502", "not_json", "no_data", "data_not_object", "token_not_string", "token_empty"],
)
async def test_session_exchange_5xx_or_malformed_returns_none(response):
    with respx.mock() as mock:
        mock.post(SESSION_URL).mock(return_value=response)
        assert await connector_session(_turn()) is None
    assert presence._sessions == {}


@pytest.mark.asyncio
async def test_parallel_misses_share_one_exchange():
    """Tools running in parallel in one turn all miss the cache at once; only one exchange reaches the core."""

    async def slow_session(request):
        await asyncio.sleep(0.01)  # the first exchange is still in flight when the others ask
        return _session_ok("sess-1")

    turn = _turn()
    with respx.mock() as mock:
        session = mock.post(SESSION_URL).mock(side_effect=slow_session)
        results = await asyncio.gather(*(connector_session(turn) for _ in range(3)))

    assert results == ["sess-1", "sess-1", "sess-1"]
    assert session.call_count == 1


@pytest.mark.asyncio
async def test_session_cache_prunes_expired_entries_on_insert(monkeypatch):
    now = [1_900_000_000.0]
    monkeypatch.setattr(presence, "_now", lambda: now[0])
    with respx.mock() as mock:
        mock.post(SESSION_URL).mock(
            side_effect=[_session_ok("sess-a", "2030-03-17T17:56:40Z"), _session_ok("sess-b", "2030-03-17T18:56:40Z")]
        )
        await connector_session(_turn("presence-a"))
        now[0] += 600  # sess-a is past its expiry
        await connector_session(_turn("presence-b", user_id="u2"))

    assert [session for session, _ in presence._sessions.values()] == ["sess-b"]


@pytest.mark.asyncio
async def test_payload_cannot_override_context_session_or_person():
    bound(_turn())
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_ok())
        mock.post(SESSION_URL).mock(return_value=_session_ok("sess-1"))
        route = mock.post(f"{SERVICE}/v1/tools/execute").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        await ConnectorTransport().post(
            "/v1/tools/execute",
            {"name": "x", "context": {"agent_id": "other"}, "connector_session": "forged", "on_behalf_of": "u-other"},
        )

    body = json.loads(route.calls.last.request.content)
    assert body["context"]["agent_id"] == "agent-1"
    assert body["connector_session"] == "sess-1"
    assert body["on_behalf_of"] is None
    assert body["name"] == "x"


@pytest.mark.asyncio
async def test_session_exchange_network_error_returns_none():
    with respx.mock() as mock:
        mock.post(SESSION_URL).mock(side_effect=httpx.ConnectError("boom"))
        assert await connector_session(_turn()) is None


@pytest.mark.asyncio
async def test_no_presence_token_means_no_exchange():
    with respx.mock(assert_all_called=False) as mock:
        session = mock.post(SESSION_URL).mock(return_value=_session_ok())
        assert await connector_session(_turn(presence_token=None)) is None
    assert session.call_count == 0


@pytest.mark.asyncio
async def test_missing_service_url_has_clear_message(monkeypatch):
    bound(_turn())
    monkeypatch.delenv("DOOERS_CONNECTOR_SERVICE_URL")
    with pytest.raises(ConnectorError, match="DOOERS_CONNECTOR_SERVICE_URL não configurada"):
        await ConnectorTransport().post("/v1/tools/list", {})


@pytest.mark.asyncio
async def test_no_active_turn_has_clear_message():
    with pytest.raises(ConnectorError, match="connectors precisa de um turno ativo do handler"):
        await ConnectorTransport().post("/v1/tools/list", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(401, text="no"), "denied"),
        (httpx.Response(403, text="no"), "denied"),
        (httpx.Response(500, text="oops"), "provider_error"),
        (httpx.Response(502, text="oops"), "provider_error"),
        (httpx.Response(422, text="bad"), "provider_error"),
    ],
)
async def test_service_http_errors_map_to_status(response, status):
    bound(_turn(presence_token=None))
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_ok())
        mock.post(f"{SERVICE}/v1/tools/list").mock(return_value=response)
        with pytest.raises(ConnectorError) as exc:
            await ConnectorTransport().post("/v1/tools/list", {})
    assert exc.value.status == status


@pytest.mark.asyncio
async def test_service_network_error_is_provider_error():
    bound(_turn(presence_token=None))
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=_token_ok())
        mock.post(f"{SERVICE}/v1/tools/list").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ConnectorError) as exc:
            await ConnectorTransport().post("/v1/tools/list", {})
    assert exc.value.status == "provider_error"


@pytest.mark.asyncio
async def test_missing_runtime_key_is_denied():
    bound(_turn(presence_token=None, key=None))
    with pytest.raises(ConnectorError) as exc:
        await ConnectorTransport().post("/v1/tools/list", {})
    assert exc.value.status == "denied"


@pytest.mark.asyncio
async def test_token_mint_failure_is_denied():
    bound(_turn(presence_token=None))
    with respx.mock() as mock:
        mock.post(TOKEN_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(ConnectorError) as exc:
            await ConnectorTransport().post("/v1/tools/list", {})
    assert exc.value.status == "denied"
