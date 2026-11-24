"""Process-local registry of handler executions, so a run can be stopped from another socket.

Cancellation is cooperative with a safety net:

* the pipeline checks :attr:`ActiveRun.cancel_requested` between handler events, and
* when the handler is awaiting something (an LLM stream, a tool), the executing task is
  cancelled so the ``await`` is interrupted right away. Only that window is interrupted —
  never the pipeline's own persistence/broadcast work — which keeps the thread consistent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ActiveRun:
    thread_id: str
    agent_id: str
    task: asyncio.Task | None
    run_id: str | None = None
    #: True while the pipeline awaits the handler's next event (safe to interrupt).
    in_handler: bool = False
    cancel_requested: bool = False
    cancel_reason: str | None = None
    canceled_by: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def request_cancel(self, *, reason: str | None = None, by: str | None = None) -> None:
        if self.cancel_requested:
            return
        self.cancel_requested = True
        self.cancel_reason = reason
        self.canceled_by = by
        self.event.set()
        # Interrupt only a task parked inside handler code. A handler stopping itself (same
        # task) or a task busy in pipeline bookkeeping just sees the flag at the next event.
        if self.in_handler and self.task is not None and not self.task.done() and self.task is not _current_task():
            self.task.cancel()


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class RunRegistry:
    def __init__(self) -> None:
        self._by_thread: dict[str, list[ActiveRun]] = {}

    def register(self, *, thread_id: str, agent_id: str, task: asyncio.Task | None) -> ActiveRun:
        run = ActiveRun(thread_id=thread_id, agent_id=agent_id, task=task)
        self._by_thread.setdefault(thread_id, []).append(run)
        return run

    def unregister(self, run: ActiveRun) -> None:
        runs = self._by_thread.get(run.thread_id)
        if not runs:
            return
        if run in runs:
            runs.remove(run)
        if not runs:
            self._by_thread.pop(run.thread_id, None)

    def active(self, thread_id: str) -> list[ActiveRun]:
        return list(self._by_thread.get(thread_id, []))

    def is_running(self, thread_id: str) -> bool:
        return bool(self._by_thread.get(thread_id))

    def cancel(self, thread_id: str, *, run_id: str | None = None, reason: str | None = None, by: str | None = None) -> int:
        """Request cancellation; returns how many executions were signalled."""
        count = 0
        for run in self.active(thread_id):
            if run_id and run.run_id and run.run_id != run_id:
                continue
            run.request_cancel(reason=reason, by=by)
            count += 1
        return count
