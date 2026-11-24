"""Token streaming (text_delta → event.delta → finalized event) and run cancellation."""

from __future__ import annotations

import asyncio

import pytest

from dooers.agents.server.exceptions import HandlerError
from dooers.agents.server.handlers import pipeline as pipeline_module
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.protocol.models import User
from dooers.agents.server.runs import RunRegistry
from tests.fakes import MemoryPersistence


def _pipeline(persistence, payloads, registry=None) -> HandlerPipeline:
    async def capture(agent_id, payload):
        payloads.append(payload)

    return HandlerPipeline(persistence=persistence, broadcast_callback=capture, run_registry=registry)


def _ctx(handler) -> HandlerContext:
    return HandlerContext(
        handler=handler,
        agent_id="agent-1",
        message="oi",
        organization_id="org-1",
        workspace_id="ws-1",
        user=User(user_id="u1", user_name="Ana"),
    )


async def _run(pipe: HandlerPipeline, ctx: HandlerContext):
    result = await pipe.setup(ctx)
    async for _ in pipe.execute(ctx, result):
        pass
    return result


@pytest.fixture(autouse=True)
def _flush_every_delta(monkeypatch):
    monkeypatch.setattr(pipeline_module, "STREAM_FLUSH_SECONDS", 0.0)


async def test_stream_announces_deltas_and_persists_final_text():
    persistence = MemoryPersistence()
    payloads: list[dict] = []

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        sid = send.new_stream_id()
        for piece in ("Olá", ", ", "mundo"):
            yield send.text_delta(piece, stream_id=sid)
        yield send.text("Olá, mundo!", stream_id=sid)
        yield send.run_end()

    result = await _run(_pipeline(persistence, payloads), _ctx(handler))

    appends = [p for p in payloads if p["type"] == "event.append" and p["events"][0].actor == "assistant"]
    deltas = [p for p in payloads if p["type"] == "event.delta"]
    assert appends[0]["events"][0].streaming is True
    assert appends[0]["events"][0].content[0].text == "Olá"
    assert [d["delta"] for d in deltas] == [", ", "mundo"]
    assert [d["seq"] for d in deltas] == [1, 2]
    final = appends[-1]["events"][0]
    assert final.id == appends[0]["events"][0].id == deltas[0]["event_id"]
    assert final.finalized is True and final.streaming is False
    stored = persistence.messages(result.thread.id)
    assert len(stored) == 1 and stored[0].content[0].text == "Olá, mundo!"


async def test_unfinished_stream_is_finalized_at_run_end():
    persistence = MemoryPersistence()
    payloads: list[dict] = []

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        sid = send.new_stream_id()
        yield send.text_delta("parcial ", stream_id=sid)
        yield send.text_delta("completo", stream_id=sid)
        yield send.run_end()

    result = await _run(_pipeline(persistence, payloads), _ctx(handler))
    stored = persistence.messages(result.thread.id)
    assert [e.content[0].text for e in stored] == ["parcial completo"]
    run = next(iter(persistence.runs.values()))
    assert run.status == "succeeded"


async def test_reasoning_stream_is_a_reasoning_event():
    persistence = MemoryPersistence()
    payloads: list[dict] = []

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        sid = send.new_stream_id()
        yield send.reasoning_delta("pensando", stream_id=sid)
        yield send.reasoning("pensando bem", stream_id=sid)
        yield send.text("ok")
        yield send.run_end()

    result = await _run(_pipeline(persistence, payloads), _ctx(handler))
    reasoning = [e for e in persistence.events.values() if e.thread_id == result.thread.id and e.type == "reasoning"]
    assert len(reasoning) == 1 and reasoning[0].data["text"] == "pensando bem"
    assert any(p["type"] == "event.append" and p["events"][0].type == "reasoning" and p["events"][0].streaming for p in payloads)


async def test_cancel_interrupts_awaiting_handler_and_keeps_partial_text():
    persistence = MemoryPersistence()
    payloads: list[dict] = []
    registry = RunRegistry()
    reached = asyncio.Event()
    cleaned = asyncio.Event()

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        sid = send.new_stream_id()
        yield send.text_delta("Começando a resposta", stream_id=sid)
        try:
            reached.set()
            await asyncio.sleep(30)  # e.g. waiting on the LLM stream
            yield send.text_delta(" nunca chega", stream_id=sid)
        finally:
            cleaned.set()

    pipe = _pipeline(persistence, payloads, registry)
    ctx = _ctx(handler)
    result = await pipe.setup(ctx)

    async def consume():
        async for _ in pipe.execute(ctx, result):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(reached.wait(), 2)
    assert registry.is_running(result.thread.id)
    assert registry.cancel(result.thread.id, reason="user_canceled") == 1
    await asyncio.wait_for(task, 2)  # returns normally — no HandlerError, no CancelledError

    assert cleaned.is_set()
    run = next(iter(persistence.runs.values()))
    assert run.status == "canceled" and run.error == "user_canceled"
    stored = persistence.messages(result.thread.id)
    assert stored[0].content[0].text == "Começando a resposta"
    assert stored[0].data and stored[0].data.get("interrupted") is True
    assert not registry.is_running(result.thread.id)
    assert any(p["type"] == "run.upsert" and p["run"].status == "canceled" for p in payloads)


async def test_cancel_between_events_stops_generator():
    persistence = MemoryPersistence()
    registry = RunRegistry()
    seen: list[str] = []

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        registry.cancel(incoming.context.thread_id)
        assert incoming.context.cancelled is True
        seen.append("after-cancel")
        yield send.text("não deve ser salvo")
        seen.append("never")

    result = await _run(_pipeline(persistence, [], registry), _ctx(handler))
    assert seen == ["after-cancel"]
    assert persistence.messages(result.thread.id) == []
    assert next(iter(persistence.runs.values())).status == "canceled"


async def test_handler_error_still_raises_and_finalizes_stream():
    persistence = MemoryPersistence()

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        sid = send.new_stream_id()
        yield send.text_delta("antes do erro", stream_id=sid)
        raise RuntimeError("boom")

    pipe = _pipeline(persistence, [])
    ctx = _ctx(handler)
    result = await pipe.setup(ctx)
    with pytest.raises(HandlerError):
        async for _ in pipe.execute(ctx, result):
            pass
    texts = [e.content[0].text for e in persistence.messages(result.thread.id)]
    assert texts == ["antes do erro"]
    assert next(iter(persistence.runs.values())).status == "failed"


async def test_document_ref_id_is_persisted():
    persistence = MemoryPersistence()

    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        yield send.document("https://x/y.pdf", "y.pdf", "application/pdf", ref_id="art_1", size_bytes=10)
        yield send.run_end()

    result = await _run(_pipeline(persistence, []), _ctx(handler))
    part = persistence.messages(result.thread.id)[0].content[0]
    assert part.ref_id == "art_1" and part.size_bytes == 10
