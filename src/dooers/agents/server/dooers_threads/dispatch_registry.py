"""The handlers of ``AgentServer.dispatch`` in Dooers Threads mode, kept by the process that called it (plan 5, task 6b, ruling F5).

In production ``dispatch(handler, ...)`` runs the handler it receives, in the calling process. In Dooers Threads mode the handler runs
inside the Dooers Threads executor, when Dooers Threads delivers the trigger event to the agent. Two things keep the handler of a
``dispatch`` call honest:

- **In this process** the call registers its handler under the SDK event id (``DispatchStream.event_id``, the
  ``client_event_id`` of the trigger event: one id for one trigger event and known before the event is published, so a delivery
  that arrives at once finds it). The executor of this process runs that handler instead of the one it mounts, and records the
  events the handler yields, so the stream hands back the handler's own ``AgentEvent`` (F14).
- **In another process** the registry is empty. When the handler of the call is not the one mounted there, the trigger event
  carries a marker (the fingerprint of the handler) and the executor that receives the delivery **fails it visibly** (the
  execution ends ``failed`` with the error text in the thread) instead of running a different handler. A custom dispatch handler
  therefore needs a single process: the one that calls ``dispatch`` is the one that gets the delivery.

The marker is only written when it can matter: not when the handler of the call is the one this process mounts for the same
agent (``mount``), where no delivery can run a different handler.
"""

from __future__ import annotations

import functools
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dooers.agents.server.handlers.send import AgentEvent

#: ``data`` key of the trigger event that carries the fingerprint of a dispatch handler (written only when needed).
MARKER_KEY = "dooers_dispatch_handler"
#: Entries kept at most, and for how long (an entry the stream never finished is dropped by the next registration).
MAX_ENTRIES = 1024
ENTRY_TTL_SECONDS = 3600.0


def handler_fingerprint(handler: Callable[..., Any]) -> str:
    """A name of a handler that two processes of one deployment can compare: ``module:qualname``.

    A handler without a stable name (a lambda, a closure, a ``partial``, a callable object) has none: its fingerprint is unique to
    this object, so no other process matches it (running another handler under that name would be silent).
    """
    target = handler.func if isinstance(handler, functools.partial) else handler
    target = getattr(target, "__func__", target)
    module, qualname = getattr(target, "__module__", None), getattr(target, "__qualname__", None)
    if (
        isinstance(handler, functools.partial)
        or not isinstance(module, str)
        or not isinstance(qualname, str)
        or "<locals>" in qualname
        or "<lambda>" in qualname
    ):
        return f"object:{uuid.uuid4().hex}"
    return f"{module}:{qualname}"


class DispatchHandlerUnavailable(RuntimeError):
    """The delivery was dispatched with a handler this process does not have (it fails the execution with this text)."""

    def __init__(self, fingerprint: str) -> None:
        super().__init__(
            f"This message was dispatched with the handler {fingerprint!r}, which is not the handler mounted in this process. "
            "A custom handler of AgentServer.dispatch runs only in the process that called dispatch; "
            "call dispatch with the mounted handler to run it in another process."
        )
        self.fingerprint = fingerprint


@dataclass
class DispatchEntry:
    """One ``dispatch`` call: its handler and what the executors that ran it recorded (``runs``: execution id -> events)."""

    event_id: str
    handler: Callable[..., Any]
    fingerprint: str
    created: float = field(default_factory=time.monotonic)
    #: The events each execution of this process yielded, in order (only events the Dooers Threads sink accepted).
    runs: dict[str, list[AgentEvent]] = field(default_factory=dict)
    #: The exception the handler raised in an execution of this process.
    failures: dict[str, Exception] = field(default_factory=dict)

    def claimed(self, execution_id: str) -> bool:
        return execution_id in self.runs


class DispatchRegistry:
    """The dispatch handlers of this process, by trigger event id; bounded in size and age."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, DispatchEntry] = OrderedDict()
        #: agent id -> the handler the executor of this process mounts for it.
        self._mounted: dict[str, Callable[..., Any]] = {}

    # -- the mounted handler --

    def mount(self, instance_agent_id: str, handler: Callable[..., Any]) -> None:
        """The executor of this process mounts ``handler`` for the agent (the last one wins)."""
        with self._lock:
            self._mounted[instance_agent_id] = handler

    def marker_for(self, instance_agent_id: str, handler: Callable[..., Any]) -> str | None:
        """The fingerprint the trigger event must carry, or ``None`` when no delivery can run a different handler.

        ``None`` only when this process mounts this very handler for the agent. Without a mounted handler here (the executor is
        in another process) or with another one, the fingerprint is written: the process that gets the delivery compares it.
        """
        with self._lock:
            if self._mounted.get(instance_agent_id) is handler:
                return None
        return handler_fingerprint(handler)

    # -- the calls --

    def register(self, event_id: str, handler: Callable[..., Any]) -> DispatchEntry:
        entry = DispatchEntry(event_id, handler, handler_fingerprint(handler))
        now = time.monotonic()
        with self._lock:
            for key in [k for k, e in self._entries.items() if now - e.created > ENTRY_TTL_SECONDS]:
                del self._entries[key]
            self._entries[event_id] = entry
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)
        return entry

    def forget(self, event_id: str) -> None:
        with self._lock:
            self._entries.pop(event_id, None)

    def claim(self, event_id: str | None, execution_id: str) -> DispatchEntry | None:
        """The entry of a delivery that lands in this process; marks ``execution_id`` as run here. ``None``: not a call of this process."""
        if not event_id:
            return None
        with self._lock:
            entry = self._entries.get(event_id)
            if entry is not None:
                entry.runs.setdefault(execution_id, [])
        return entry

    def clear(self) -> None:
        """Test helper: forget every entry and mounted handler."""
        with self._lock:
            self._entries.clear()
            self._mounted.clear()


#: The registry of this process.
DISPATCHES = DispatchRegistry()


async def recorded(entry: DispatchEntry, execution_id: str, events: Any) -> Any:
    """The async iterator of a handler's events, recording what the executor accepts and the handler's own failure."""
    log = entry.runs.setdefault(execution_id, [])
    try:
        async for event in events:
            yield event
            # Here only when the executor took the event and asked for the next one: the sink accepted it.
            log.append(event)
    except Exception as error:
        entry.failures[execution_id] = error
        raise
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()
