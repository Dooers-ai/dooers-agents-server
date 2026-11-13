"""The newest events of a delivery context, read backward from the cutoff.

Production reads the latest rows of a thread (``get_events`` with a ``limit``); it never reads the
whole thread. ``MaestroContextWindow`` does the same over ``GET /deliveries/:id/context`` in its
``direction=backward`` mode: the first page holds the events up to the cutoff, newest first, and
each ``next_cursor`` continues before the oldest event read. The events read so far are the
*window*: a suffix of the thread in chronological order. One window lives for one execution (the
context below the cutoff does not change), grows only when a read needs older events, and checks
the lease on every use.

The window also keeps the first event of the thread (a forward page of one event), read only
when asked: ``thread.created`` has the channel and creation time, and the queue the thread had
before the window.

``max_pages`` is how far back the window may go (backward pages, newest first): an execution never
holds more. A read that needs events older than that raises ``MaestroHistoryLimitError`` (not an
HTTP error: the handler may catch it). What was read stays, so a read that fits the window still works.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, Literal

from .client import MaestroContextError

Direction = Literal["forward", "backward"]
#: ``(cursor, limit, direction)`` -> a validated ``Public.ContextResponse``.
PageFetcher = Callable[[str, int, Direction], Awaitable[dict[str, Any]]]


class MaestroHistoryLimitError(RuntimeError):
    """A history read needs events older than the ``max_pages`` pages the window may hold."""

    def __init__(self, max_pages: int) -> None:
        self.max_pages = max_pages
        super().__init__(
            f"MAESTRO_HISTORY_LIMIT: the read needs events older than the {max_pages} context pages kept back from the newest "
            "event; narrow it (smaller limit, order desc, after_event_id near the end) or raise MaestroConfig.max_history_pages"
        )


def _key(event: dict[str, Any]) -> tuple[int, str]:
    return len(event["sequence"]), event["sequence"]


class MaestroContextWindow:
    """The events of one delivery context, newest first on the wire and in order here.

    ``fetch`` reads one validated page; ``cursor`` is the envelope's ``context_cursor`` (it starts a
    read at the tip of either direction); ``check_current`` is the client's lease, capability and
    cancellation check, run on every public call. ``events`` is shared: read it, never change it.
    """

    def __init__(
        self,
        fetch: PageFetcher,
        *,
        cursor: str,
        check_current: Callable[[], Any],
        page_size: int = 100,
        max_pages: int = 20,
    ) -> None:
        self._fetch, self._cursor, self._check_current = fetch, cursor, check_current
        self._page_size, self.max_pages = page_size, max_pages
        self._head: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._next: str | None = None
        self._origin: dict[str, Any] | None = None
        self._origin_read = False
        self._back_pages = 0
        self.pages = 0
        # Two handler tasks may read at once (``asyncio.gather``): one page is fetched and merged at a time.
        self._lock = asyncio.Lock()

    @property
    def events(self) -> list[dict[str, Any]]:
        """The window, oldest first. Empty before :meth:`load`."""
        return self._events

    @property
    def exhausted(self) -> bool:
        """The window starts at the first event of the thread: nothing older exists."""
        return self._head is not None and self._next is None

    async def load(self) -> dict[str, Any]:
        """The context head (``trigger_event``, ``invocation_event``?, ``participants``), reading the first page once."""
        if self._head is None:
            async with self._lock:
                if self._head is None:  # a concurrent caller may have read it while this one waited
                    self._check_current()
                    page = await self._fetch(self._cursor, self._page_size, "backward")
                    self._take(page)  # a page that does not hold fails before the window has a head
                    self.pages += 1
                    self._back_pages += 1
                    self._head = {
                        "trigger_event": deepcopy(page["trigger_event"]),
                        **({"invocation_event": deepcopy(page["invocation_event"])} if "invocation_event" in page else {}),
                        "participants": deepcopy(page["participants"]),
                    }
        self._check_current()
        return self._head

    def _take(self, page: dict[str, Any]) -> None:
        history = page["history"]
        newest_first = history["items"]
        if self._events and newest_first and _key(newest_first[0]) >= _key(self._events[0]):
            raise _invalid()
        known = {event["event_id"] for event in self._events}
        if any(event["event_id"] in known for event in newest_first):
            raise _invalid()
        self._events[:0] = reversed(newest_first)
        self._next = history["next_cursor"]

    async def read_back(self) -> bool:
        """Read one page of older events; false when the window already starts at the thread's first event.

        With ``max_pages`` pages held, a page more is ``MaestroHistoryLimitError``. A call that waited for
        another read of the same page finds the window already grown and returns true without fetching:
        the caller looks at the window again, as after any growth.
        """
        await self.load()
        wanted = self._next
        async with self._lock:
            if self._next != wanted:
                return True
            if self._next is None:
                return False
            if self._back_pages >= self.max_pages:
                raise MaestroHistoryLimitError(self.max_pages)
            self._check_current()
            page = await self._fetch(self._next, self._page_size, "backward")
            head = self._head or {}
            if page["trigger_event"] != head["trigger_event"] or page.get("invocation_event") != head.get("invocation_event"):
                raise _invalid()
            self._take(page)
            self.pages += 1
            self._back_pages += 1
            return True

    async def read_until(self, done: Callable[[], bool]) -> None:
        """Go back page by page until ``done()`` (called on the current window) or the thread's first event."""
        await self.load()
        while not done() and await self.read_back():
            pass

    async def find_event(self, event_id: str) -> dict[str, Any] | None:
        """The event with this id, reading back until it is found.

        ``None`` if the thread has no such event, or if it is older than the ``max_pages`` pages the
        window may hold (the caller treats both as "not there", as production does for an id it does not find).
        """

        def found() -> bool:
            return any(event["event_id"] == event_id for event in self._events)

        try:
            await self.read_until(found)
        except MaestroHistoryLimitError:
            return None
        return next((event for event in self._events if event["event_id"] == event_id), None)

    async def origin(self) -> dict[str, Any] | None:
        """The first event of the thread (sequence 1), read once: a forward page of one event."""
        await self.load()
        if not self._origin_read:
            async with self._lock:
                if not self._origin_read:
                    if self.exhausted:
                        self._origin = self._events[0] if self._events else None
                    else:
                        self._check_current()
                        page = await self._fetch(self._cursor, 1, "forward")
                        self.pages += 1
                        items = page["history"]["items"]
                        self._origin = deepcopy(items[0]) if items else None
                    self._origin_read = True
        self._check_current()
        return self._origin

    async def thread_events(self) -> list[dict[str, Any]]:
        """The window with the first event of the thread in front when the window starts later.

        What ``thread_at_cutoff`` reads: ``thread.created`` (channel, creation time, the first title
        and queue), then the changes the window holds. Title and queue changes older than the window
        are not seen.
        """
        origin = await self.origin()
        return list(self._events) if self.exhausted or origin is None else [origin, *self._events]


def _invalid() -> MaestroContextError:
    return MaestroContextError("CONTEXT_INVALID")
