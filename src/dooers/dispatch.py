from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from dooers.handlers.send import WorkerEvent

if TYPE_CHECKING:
    from dooers.handlers.pipeline import HandlerContext, HandlerPipeline, PipelineResult


class DispatchStream:
    """Async-iterable stream of handler events from a programmatic dispatch.

    Properties ``thread_id``, ``event_id``, and ``is_new_thread`` are
    available immediately (before iteration), because the pipeline setup
    phase runs before this object is returned.
    """

    def __init__(
        self,
        pipeline: HandlerPipeline,
        context: HandlerContext,
        result: PipelineResult,
    ) -> None:
        self._pipeline = pipeline
        self._context = context
        self._result = result

    @property
    def thread_id(self) -> str:
        return self._result.thread.id

    @property
    def event_id(self) -> str:
        return self._result.user_event.id

    @property
    def is_new_thread(self) -> bool:
        return self._result.is_new_thread

    def __aiter__(self) -> AsyncGenerator[WorkerEvent, None]:
        return self._pipeline.execute(self._context, self._result)

    async def collect(self) -> list[WorkerEvent]:
        events: list[WorkerEvent] = []
        async for event in self:
            events.append(event)
        return events
