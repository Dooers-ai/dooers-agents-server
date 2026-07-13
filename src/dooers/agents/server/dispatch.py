from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from dooers.agents.server.handlers.send import AgentEvent

if TYPE_CHECKING:
    from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline, PipelineResult
    from dooers.agents.server.observability.otel import _NoOpTracker, _OtelTracker


class DispatchStream:
    """Async-iterable stream of handler events from a programmatic dispatch.

    Properties ``thread_id``, ``event_id``, and ``is_new_thread`` are
    available immediately (before iteration), because the pipeline setup
    phase runs before this object is returned.

    The AgentOps trace (when enabled) is started before this object is
    returned and ended automatically when the stream is fully consumed
    or an error occurs.
    """

    def __init__(
        self,
        pipeline: HandlerPipeline,
        context: HandlerContext,
        result: PipelineResult,
        tracker: _OtelTracker | _NoOpTracker | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._context = context
        self._result = result
        self._tracker = tracker

    @property
    def thread_id(self) -> str:
        return self._result.thread.id

    @property
    def event_id(self) -> str:
        return self._result.user_event.id

    @property
    def is_new_thread(self) -> bool:
        return self._result.is_new_thread

    async def __aiter__(self) -> AsyncGenerator[AgentEvent, None]:  # type: ignore[override]
        tracker = self._tracker
        try:
            async for event in self._pipeline.execute(self._context, self._result):
                yield event
        except Exception as e:
            if tracker:
                tracker.fail()
                tracker.record_error(e)
            raise
        finally:
            if tracker:
                tracker.end()

    async def collect(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        async for event in self:
            events.append(event)
        return events
