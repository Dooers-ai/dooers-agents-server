from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dooers.agents.server.handlers.pipeline import HandlerContext
from dooers.agents.server.protocol.frames import EventCreatePayload
from dooers.agents.server.protocol.models import ThreadEvent

_EVENT = {"type": "message", "actor": "user", "content": []}


def test_event_create_accepts_presence_token():
    p = EventCreatePayload.model_validate({"thread_id": "t", "event": _EVENT, "presence_token": "jwt"})
    assert p.presence_token == "jwt"


def test_event_create_presence_token_optional():
    assert EventCreatePayload.model_validate({"thread_id": "t", "event": _EVENT}).presence_token is None


def test_handler_context_presence_token_defaults_none():
    assert HandlerContext.__dataclass_fields__["presence_token"].default is None


def test_thread_event_accepts_connector_interaction():
    ev = ThreadEvent(
        id="e",
        thread_id="t",
        type="connector.interaction",
        actor="system",
        data={},
        created_at=datetime.now(UTC),
    )
    assert ev.type == "connector.interaction"


@pytest.mark.asyncio
async def test_presence_token_reaches_context_but_is_not_persisted(router_harness):
    await router_harness.send_event_create(
        {
            "event": {"type": "message", "actor": "user", "content": [{"type": "text", "text": "oi"}]},
            "metadata": {"k": "v"},
            "presence_token": "jwt-secret",
        }
    )
    assert [c.presence_token for c in router_harness.contexts] == ["jwt-secret"]
    assert "jwt-secret" not in str(router_harness.persistence.mock_calls)
    assert router_harness.persistence.create_event.await_count >= 1
    assert "jwt-secret" not in str(router_harness.ws.sent)
    ctx = router_harness.contexts[0]
    assert "jwt-secret" not in str(ctx.metadata) and "jwt-secret" not in str(ctx.data)
