"""Scenarios for what the handler reads: ``AgentIncoming`` and ``memory.get_history``."""

from __future__ import annotations

from tests.parity.model import QUEUES, Scenario, ScenarioInput, Turn, capture_histories, capture_incoming

HISTORY_FORMATS = (
    "openai",
    "anthropic",
    "google",
    "cohere",
    "voyage",
    "langchain",
    "openai_completions",
    "openai_responses",
)

ALL_PART_TYPES = (
    {"type": "text", "text": "Segue o pedido"},
    {"type": "image", "url": "https://cdn.example.com/pedido.png", "mime_type": "image/png", "filename": "pedido.png"},
    {
        "type": "document",
        "url": "https://cdn.example.com/nota.pdf",
        "mime_type": "application/pdf",
        "filename": "nota.pdf",
        "size_bytes": 2048,
    },
    {
        "type": "audio",
        "url": "https://cdn.example.com/recado.ogg",
        "mime_type": "audio/ogg",
        "duration": 4.0,
        "filename": "recado.ogg",
    },
    {"type": "contact", "display_name": "Bia Lima", "phones": [{"phone": "+5511955554444", "type": "CELL"}]},
)


async def _incoming_fields(on, send, memory, analytics, settings):
    """Turn 1 moves the thread to a queue; turn 2 (all part types) is the last capture."""
    capture_incoming(on)
    yield send.run_start()
    if len(on.content) == 1:
        yield send.update_thread(queue="suporte")
        yield send.text("Vou te passar para o suporte.")
    else:
        yield send.text("Recebi seus anexos.")
    yield send.run_end()


async def _history(on, send, memory, analytics, settings):
    yield send.run_start()
    if on.message == "Resuma a conversa":
        await capture_histories(memory)
        yield send.text("Resumo pronto.")
    else:
        yield send.text("Recebi a foto.")
    yield send.run_end()


HISTORY_INPUT = ScenarioInput(
    turns=(
        Turn(
            message="",
            content=(
                {"type": "text", "text": "Veja esta foto"},
                {"type": "image", "url": "https://cdn.example.com/foto.png", "mime_type": "image/png", "filename": "foto.png"},
            ),
        ),
        Turn(message="Resuma a conversa"),
    )
)

INCOMING_SCENARIOS: dict[str, Scenario] = {
    "incoming_fields": Scenario(
        handler=_incoming_fields,
        incoming=ScenarioInput(
            thread_title="Pedido 42",
            queues=QUEUES,
            turns=(Turn(message="Preciso de ajuda"), Turn(message="", content=ALL_PART_TYPES)),
        ),
    ),
    **{f"history_{fmt}": Scenario(handler=_history, incoming=HISTORY_INPUT, history_formats=(fmt,)) for fmt in HISTORY_FORMATS},
}
