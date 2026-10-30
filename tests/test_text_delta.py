"""Live text transport: ephemeral fragments are not chat history records."""
from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.protocol.frames import S2C_TextDelta, TextDeltaPayload


def test_send_text_delta_is_transient():
    event = AgentSend().text_delta("Olá")
    assert event.send_type == "text_delta"
    assert event.data == {"text": "Olá"}


def test_live_text_frame_round_trip():
    data = S2C_TextDelta(
        id="frame-1",
        payload=TextDeltaPayload(
            thread_id="thread-1", run_id="run-1", sequence=1, text="test",
        ),
    ).model_dump()
    assert data["type"] == "text.delta"
    assert data["payload"]["sequence"] == 1
    assert data["payload"]["text"] == "test"


def test_live_fragment_reaches_only_authorized_thread_viewers():
    import asyncio
    import json
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, Mock

    from dooers.agents.server.handlers.router import Router
    from dooers.agents.server.protocol.models import Thread, User
    from dooers.agents.server.registry import ConnectionSession

    owner = User(user_id="owner")
    thread = Thread(
        id="thread-1", agent_id="agent-1", owner=owner, users=[owner],
        organization_id="org", workspace_id="ws",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        last_event_at=datetime.now(UTC),
    )
    registry = Mock()
    allowed = Mock()
    blocked = Mock()
    registry.get_connections.return_value = {allowed, blocked}
    registry.session_for.side_effect = lambda ws: ConnectionSession(
        user=owner if ws is allowed else User(user_id="stranger"),
        workspace_id="ws",
    )
    registry._safe_send = AsyncMock(return_value=True)
    persistence = Mock()
    persistence.get_thread = AsyncMock(return_value=thread)
    router = Router(
        persistence=persistence, handler=Mock(),
        registry=registry, subscriptions={},
    )
    asyncio.run(router._broadcast_to_agent_dict("agent-1", {
        "type": "text.delta", "thread_id": "thread-1", "run_id": "run-1",
        "sequence": 1, "text": "hello",
    }))
    assert registry._safe_send.await_count == 1
    ws, payload = registry._safe_send.await_args.args
    assert ws is allowed
    frame = json.loads(payload)
    assert frame["type"] == "text.delta"
    assert frame["payload"]["text"] == "hello"
