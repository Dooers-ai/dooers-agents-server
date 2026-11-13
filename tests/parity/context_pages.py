"""The pages of ``GET /deliveries/:id/context`` as Maestro serves them, forward and backward.

Port of ``context()`` in ``src/modules/executions/callbacks.ts`` (Maestro 5466bde) and of its cursor
(``context-cursor.ts``), over a list of stored events:

- Forward (default): events after ``after``, ascending, up to the cutoff.
- Backward (``direction=backward``): events before ``before`` (the cutoff + 1 at the tip), descending.
- Without a cursor, or with the envelope's ``context_cursor``, a read starts at the tip of its direction.
- A cursor belongs to the direction that issued it: the other direction refuses it (``CURSOR_INVALID``).
- A page is contiguous: a missing sequence is ``DEPENDENCY_UNAVAILABLE`` (a gap).
- The response holds at most 1 MiB, counting the trigger and the participants; a page keeps at least one
  event of the history, even if that one is over the cap.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

MAX_PAGE_BYTES = 1024 * 1024
DEFAULT_LIMIT = 50


class CursorInvalid(ValueError):
    """``CURSOR_INVALID`` (HTTP 400)."""


class ContextGap(RuntimeError):
    """``DEPENDENCY_UNAVAILABLE``: the stored sequences are not contiguous."""


def cursor_for(base: str, direction: str, position: int) -> str:
    """The cursor after ``position`` (forward: the last sequence read) or before it (backward: the oldest read)."""
    return f"{base}.{'b' if direction == 'backward' else ''}{position}"


def _position(base: str, cursor: str | None, direction: str) -> int | None:
    if cursor is None or cursor == base:
        return None
    prefix = base + "."
    tail = cursor[len(prefix) :] if cursor.startswith(prefix) else ""
    if direction == "backward":
        tail = tail[1:] if tail.startswith("b") else ""
    if not (tail.isascii() and tail.isdecimal()):
        raise CursorInvalid(cursor)
    return int(tail)


def serve_context(
    events: list[dict[str, Any]],
    *,
    cutoff: int,
    base: str,
    cursor: str | None,
    limit: int | None,
    direction: str,
    page: Callable[[list[dict[str, Any]], str | None, bool], dict[str, Any]],
) -> dict[str, Any]:
    """One context page. ``events`` are the thread's events in sequence order (they may stop short of the cutoff).

    ``page(items, next_cursor, has_more)`` builds the response around the history items (trigger,
    participants, ids); the size cap is measured on it.
    """
    limit = DEFAULT_LIMIT if limit is None else limit
    backward = direction == "backward"
    position = _position(base, cursor, direction)
    visible = [event for event in events if int(event["sequence"]) <= cutoff]
    if backward:
        before = cutoff + 1 if position is None else position
        rows = sorted((event for event in visible if int(event["sequence"]) < before), key=lambda e: -int(e["sequence"]))[: limit + 1]
        selected = rows[:limit]
        expected = before - 1
        for row in selected:
            if int(row["sequence"]) != expected:
                raise ContextGap
            expected -= 1
        if len(rows) <= limit and expected != 0:
            raise ContextGap
    else:
        after = 0 if position is None else position
        rows = [event for event in visible if int(event["sequence"]) > after][: limit + 1]
        selected = rows[:limit]
        expected = after + 1
        for row in selected:
            if int(row["sequence"]) != expected:
                raise ContextGap
            expected += 1
        if len(rows) <= limit and expected - 1 != cutoff:
            raise ContextGap

    def build() -> dict[str, Any]:
        more = len(rows) > len(selected)
        tail = int(selected[-1]["sequence"]) if selected else (position if position is not None else (cutoff + 1 if backward else 0))
        return page(selected, cursor_for(base, direction, tail) if more else None, more)

    candidate = build()
    # The cap cuts the end of the page (the newest forward, the oldest backward), never below one event.
    while len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PAGE_BYTES and len(selected) > 1:
        selected.pop()
        candidate = build()
    return candidate


class ThreadPages:
    """The delivery context of a thread, served page by page without HTTP (``requests`` records each call).

    ``events`` are the stored events in sequence order, ``participants`` the context participants;
    the context stops at the ``cutoff`` (a sequence; default the last event, which is the trigger).
    ``window()`` is the real ``MaestroContextWindow`` over these pages.
    """

    def __init__(self, events: list[dict[str, Any]], participants: list[dict[str, Any]], *, cutoff: int | None = None) -> None:
        self.events, self.participants = events, participants
        self.cutoff = len(events) if cutoff is None else cutoff
        self.base = "ctx.test"
        self.requests: list[tuple[str, int, str]] = []
        #: Reads of the lease check (the real client checks it on every use of the window).
        self.checks = 0
        #: Seconds each page takes (lets two reads overlap, as two handler tasks do).
        self.delay = 0.0

    async def fetch(self, cursor: str, limit: int, direction: str) -> dict[str, Any]:
        self.requests.append((cursor, limit, direction))
        if self.delay:
            await asyncio.sleep(self.delay)
        trigger = next(event for event in self.events if int(event["sequence"]) == self.cutoff)

        def page(items: list[dict[str, Any]], next_cursor: str | None, has_more: bool) -> dict[str, Any]:
            return {
                "delivery_id": "delivery",
                "trigger_event": trigger,
                "participants": self.participants,
                "context_cursor": self.base,
                "history": {"items": list(items), "next_cursor": next_cursor, "has_more": has_more, "watermark": str(self.cutoff)},
            }

        return serve_context(self.events, cutoff=self.cutoff, base=self.base, cursor=cursor, limit=limit, direction=direction, page=page)

    def _check(self) -> None:
        self.checks += 1

    def window(self, *, page_size: int = 100, max_pages: int = 20):
        from dooers.agents.server.maestro.window import MaestroContextWindow

        return MaestroContextWindow(self.fetch, cursor=self.base, check_current=self._check, page_size=page_size, max_pages=max_pages)
