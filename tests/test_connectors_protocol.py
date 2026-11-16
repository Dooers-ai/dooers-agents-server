from __future__ import annotations

import json
import logging
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


def test_handler_context_repr_hides_presence_token():
    ctx = HandlerContext(handler=None, agent_id="a", message="m", presence_token="jwt-secret")  # type: ignore[arg-type]
    assert ctx.presence_token == "jwt-secret"
    assert "jwt-secret" not in repr(ctx)


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


def test_event_create_repr_hides_presence_token():
    p = EventCreatePayload.model_validate({"thread_id": "t", "event": _EVENT, "presence_token": "jwt-secret"})
    assert "jwt-secret" not in repr(p) and "jwt-secret" not in str(p)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"presence_token": "jwt-secret"},  # missing "event": the error's input is this whole payload
        {"event": {**_EVENT, "actor": "agent"}, "presence_token": "jwt-secret"},
    ],
    ids=["missing_event", "invalid_actor"],
)
async def test_frame_that_fails_to_parse_is_logged_without_its_input(caplog, payload):
    from unittest.mock import AsyncMock

    from dooers.agents.server.config import AgentConfig
    from dooers.agents.server.server import AgentServer

    class _OneFrame:
        def __init__(self, frame: str) -> None:
            self.frames = [frame]

        async def receive_text(self) -> str:
            return self.frames.pop(0)

        async def send_text(self, data: str) -> None:  # pragma: no cover
            pass

        async def close(self, code: int = 1000) -> None:  # pragma: no cover
            pass

    server = AgentServer(AgentConfig(database_type="postgres"))
    server._ensure_initialized = AsyncMock(return_value=AsyncMock())
    frame = json.dumps({"id": "f1", "type": "event.create", "payload": payload})

    with caplog.at_level(logging.DEBUG):
        await server.handle(_OneFrame(frame), lambda *a: None)

    assert any("failed to parse WebSocket C2S frame" in r.getMessage() for r in caplog.records)
    assert "jwt-secret" not in caplog.text
