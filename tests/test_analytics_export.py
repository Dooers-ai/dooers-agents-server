import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from dooers.agents.server.features.analytics.collector import AnalyticsCollector
from dooers.agents.server.features.analytics.export import export_agent_feedback, message_text
from dooers.agents.server.features.analytics.models import AnalyticsBatch, AnalyticsEventPayload
from dooers.agents.server.observability.service_token import ServiceTokenClient
from dooers.agents.server.protocol.models import ThreadEvent, WireS2C_TextPart

CORE = "https://core.test"
TOKEN_URL = f"{CORE}/api/v2/identity/service-token/agent"
FEEDBACK_URL = f"{CORE}/api/v2/analytics/agent/feedback"
WEBHOOK = "https://core.test/api/v2/webhooks/analytics"


class Memory:
    def __init__(self, event: ThreadEvent | None = None, secret: str = "runtime-key"):
        self.event = event
        self.secret = secret

    async def get_service_secrets(self, agent_id: str) -> dict:
        return {"dooers_runtime_api_key": self.secret}

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        if self.event and self.event.id == event_id:
            return self.event
        return None


def _event() -> ThreadEvent:
    return ThreadEvent(
        id="evt-1",
        thread_id="thread-1",
        type="message",
        actor="assistant",
        content=[WireS2C_TextPart(text="The invoice total is 10.")],
        created_at=datetime(2026, 11, 20, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_export_joins_the_message_and_refuses_the_wrong_secret():
    event = _event()
    memory = Memory(event)
    tokens = ServiceTokenClient(core_base_url=CORE)
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as mock:
            token_route = mock.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"success": True, "data": {"accessToken": "jwt-read", "expiresIn": 300}},
                )
            )
            feedback = mock.get(FEEDBACK_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [
                                {
                                    "id": 4,
                                    "event": "feedback.dislike",
                                    "timestamp": "2026-11-20T12:00:00.000Z",
                                    "user_id": "user-1",
                                    "target_type": "event",
                                    "target_id": "evt-1",
                                    "classification": "wrong answer",
                                    "reason": "total was off",
                                }
                            ],
                            "next_cursor": None,
                        },
                    },
                )
            )
            denied = await export_agent_feedback(
                persistence=memory,  # type: ignore[arg-type]
                agent_id="agent-1",
                authorization="Bearer nope",
                export_secret="export-secret",
                core_base_url=CORE,
                http_client=http,
                token_client=tokens,
            )
            status, body = await export_agent_feedback(
                persistence=memory,  # type: ignore[arg-type]
                agent_id="agent-1",
                authorization="Bearer export-secret",
                export_secret="export-secret",
                core_base_url=CORE,
                http_client=http,
                token_client=tokens,
            )

    assert denied == (401, {"error": "invalid export credential"})
    assert status == 200
    assert body["items"][0]["thread_id"] == "thread-1"
    assert body["items"][0]["message"] == "The invoice total is 10."
    assert body["items"][0]["classification"] == "wrong answer"
    assert feedback.calls.last.request.headers["authorization"] == "Bearer jwt-read"
    token_body = json.loads(token_route.calls.last.request.content)
    assert token_body["scopes"] == ["analytics:read"]
    assert token_body["runtimeApiKey"] == "runtime-key"
    await tokens.aclose()


@pytest.mark.asyncio
async def test_feedback_snapshots_the_thread_and_the_message():
    collector = AnalyticsCollector(
        webhook_url=WEBHOOK,
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        subscriptions={},
        persistence=Memory(_event()),  # type: ignore[arg-type]
    )
    await collector.feedback(
        feedback_type="dislike",
        target_type="event",
        target_id="evt-1",
        agent_id="agent-1",
        reason="total was off",
        classification="wrong answer",
    )
    payload = collector._buffer[0]
    assert payload.thread_id == "thread-1"
    assert payload.data is not None
    assert payload.data["message"] == "The invoice total is 10."
    assert payload.data["target_id"] == "evt-1"


@pytest.mark.asyncio
async def test_clear_records_a_clear_event_for_the_same_message():
    collector = AnalyticsCollector(
        webhook_url=WEBHOOK,
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        subscriptions={},
        persistence=Memory(_event()),  # type: ignore[arg-type]
    )
    await collector.feedback(
        feedback_type="clear",
        target_type="event",
        target_id="evt-1",
        agent_id="agent-1",
    )
    payload = collector._buffer[0]
    assert payload.event == "feedback.clear"
    assert payload.thread_id == "thread-1"
    assert payload.data is not None
    assert payload.data["target_id"] == "evt-1"


@pytest.mark.asyncio
async def test_export_keeps_a_snapshot_already_stored_in_core():
    memory = Memory(_event())
    tokens = ServiceTokenClient(core_base_url=CORE)
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"success": True, "data": {"accessToken": "jwt-read", "expiresIn": 300}},
                )
            )
            mock.get(FEEDBACK_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "items": [
                                {
                                    "target_id": "evt-1",
                                    "thread_id": "thread-from-core",
                                    "message": "snapshot from the vote",
                                }
                            ],
                            "next_cursor": None,
                        },
                    },
                )
            )
            status, body = await export_agent_feedback(
                persistence=memory,  # type: ignore[arg-type]
                agent_id="agent-1",
                authorization="Bearer export-secret",
                export_secret="export-secret",
                core_base_url=CORE,
                http_client=http,
                token_client=tokens,
            )
    assert status == 200
    assert body["items"][0]["thread_id"] == "thread-from-core"
    assert body["items"][0]["message"] == "snapshot from the vote"
    await tokens.aclose()


def test_message_text_joins_text_parts_only():
    event = _event()
    event.content = [WireS2C_TextPart(text="a"), WireS2C_TextPart(text="b")]
    assert message_text(event) == "a\nb"
    assert message_text(None) is None


@pytest.mark.asyncio
async def test_webhook_sends_write_token_and_falls_back_when_mint_fails():
    memory = Memory()
    tokens = ServiceTokenClient(core_base_url=CORE)
    collector = AnalyticsCollector(
        webhook_url=WEBHOOK,
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        subscriptions={},
        persistence=memory,  # type: ignore[arg-type]
        core_base_url=CORE,
        token_client=tokens,
    )
    collector._http_client = httpx.AsyncClient()
    batch = AnalyticsBatch(
        batch_id="batch-1",
        agent_id="agent-1",
        events=[
            AnalyticsEventPayload(
                event="feedback.like",
                timestamp=datetime(2026, 11, 20, tzinfo=UTC),
                agent_id="agent-1",
                created_at=datetime(2026, 11, 20, tzinfo=UTC),
            )
        ],
        sent_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    try:
        with respx.mock() as mock:
            token_route = mock.post(TOKEN_URL).mock(return_value=httpx.Response(403, json={"success": False}))
            hook = mock.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"received": True}))
            await collector._send_to_webhook(batch)
        assert token_route.called
        assert hook.called
        assert "authorization" not in {k.lower() for k in hook.calls.last.request.headers}
    finally:
        await collector._http_client.aclose()
        await tokens.aclose()


@pytest.mark.asyncio
async def test_webhook_attaches_the_write_token():
    memory = Memory()
    tokens = ServiceTokenClient(core_base_url=CORE)
    collector = AnalyticsCollector(
        webhook_url=WEBHOOK,
        registry=SimpleNamespace(),  # type: ignore[arg-type]
        subscriptions={},
        persistence=memory,  # type: ignore[arg-type]
        token_client=tokens,
    )
    collector._http_client = httpx.AsyncClient()
    batch = AnalyticsBatch(
        batch_id="batch-1",
        agent_id="agent-1",
        events=[
            AnalyticsEventPayload(
                event="feedback.like",
                timestamp=datetime(2026, 11, 20, tzinfo=UTC),
                agent_id="agent-1",
                created_at=datetime(2026, 11, 20, tzinfo=UTC),
            )
        ],
        sent_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    try:
        with respx.mock() as mock:
            token_route = mock.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    200, json={"success": True, "data": {"accessToken": "jwt-write", "expiresIn": 300}}
                )
            )
            hook = mock.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"received": True}))
            await collector._send_to_webhook(batch)
        assert hook.calls.last.request.headers["authorization"] == "Bearer jwt-write"
        body = json.loads(token_route.calls.last.request.content)
        assert body["scopes"] == ["analytics:write"]
    finally:
        await collector._http_client.aclose()
        await tokens.aclose()
