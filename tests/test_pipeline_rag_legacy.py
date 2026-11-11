"""Legacy (production) RAG context: bound at AgentIncoming creation, never reset by the pipeline."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.send import AgentSend
from dooers.agents.server.protocol.models import User
from dooers.tools.rag.runtime import current_execution_context


def _pipeline() -> HandlerPipeline:
    persistence = AsyncMock()
    persistence.get_thread.return_value = None
    persistence.get_settings.return_value = {}
    return HandlerPipeline(persistence=persistence)


def _context(handler) -> HandlerContext:
    return HandlerContext(
        handler=handler,
        agent_id="agent-rag",
        organization_id="org-1",
        workspace_id="ws-1",
        message="hi",
        user=User(user_id="user-1"),
    )


async def test_legacy_handler_sees_rag_context():
    seen: list = []

    async def handler(on, send, memory, analytics, settings):
        seen.append(current_execution_context())
        yield send.run_start()
        yield send.run_end()

    pipeline = _pipeline()
    context = _context(handler)
    result = await pipeline.setup(context)
    async for _ in pipeline.execute(context, result):
        pass

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].agent_id == "agent-rag"


async def test_legacy_execute_closed_from_other_task_does_not_raise():
    async def handler(on, send, memory, analytics, settings):
        yield send.run_start()
        await asyncio.sleep(10)
        yield send.run_end()

    pipeline = _pipeline()
    context = _context(handler)
    result = await pipeline.setup(context)
    stream = pipeline.execute(context, result)
    first = await stream.__anext__()
    assert first.send_type == "run_start"

    errors: list[BaseException] = []

    async def close_elsewhere():
        try:
            await stream.aclose()
        except BaseException as exc:  # noqa: BLE001 - the test inspects any failure
            errors.append(exc)

    await asyncio.create_task(close_elsewhere())

    def chain(exc: BaseException | None):
        while exc is not None:
            yield exc
            exc = exc.__cause__ or exc.__context__

    # Production never resets the RAG context in the pipeline, so no error in the
    # chain may mention it. Other close-time errors (contextvar tokens) are out of scope.
    assert not [e for e in errors for link in chain(e) if "RAG_CONTEXT_TOKEN_INVALID" in str(link)]
