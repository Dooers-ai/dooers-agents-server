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
