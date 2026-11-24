"""The context pages FakeDooersThreads serves: Dooers Threads' forward and backward modes (``context_pages.py``)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.parity.context_pages import ContextGap, CursorInvalid, serve_context
from tests.parity.fake_dooers_threads import FakeDooersThreads, shared_contracts
from tests.parity.model import USER

BASE = "ctx.delivery"


def _events(count: int, *, size: int = 0) -> list[dict[str, Any]]:
    return [{"sequence": str(n), "event_id": f"e{n}", "payload": {"text": "x" * size}} for n in range(1, count + 1)]


def _page(items: list[dict[str, Any]], next_cursor: str | None, has_more: bool) -> dict[str, Any]:
    return {"history": {"items": [event["sequence"] for event in items], "next_cursor": next_cursor, "has_more": has_more}}


def _read(events, *, cutoff, cursor=None, limit=3, direction="backward", page=_page):
    return serve_context(events, cutoff=cutoff, base=BASE, cursor=cursor, limit=limit, direction=direction, page=page)


def _items(response: dict[str, Any]) -> list[str]:
    return response["history"]["items"]


def test_backward_pages_go_from_the_cutoff_down_to_the_first_event_newest_first():
    events = _events(10)

    first = _read(events, cutoff=8)
    second = _read(events, cutoff=8, cursor=first["history"]["next_cursor"])
    third = _read(events, cutoff=8, cursor=second["history"]["next_cursor"])

    assert _items(first) == ["8", "7", "6"] and first["history"]["has_more"] is True
    assert _items(second) == ["5", "4", "3"] and second["history"]["has_more"] is True
    assert _items(third) == ["2", "1"] and third["history"] == {"items": ["2", "1"], "next_cursor": None, "has_more": False}


def test_a_read_starts_at_the_tip_without_a_cursor_or_with_the_context_cursor():
    events = _events(6)

    assert _items(_read(events, cutoff=6)) == _items(_read(events, cutoff=6, cursor=BASE)) == ["6", "5", "4"]
    assert _items(_read(events, cutoff=6, direction="forward")) == _items(_read(events, cutoff=6, cursor=BASE, direction="forward"))
    assert _items(_read(events, cutoff=6, direction="forward")) == ["1", "2", "3"]


def test_events_after_the_cutoff_are_not_in_either_direction():
    events = _events(10)

    assert _items(_read(events, cutoff=4, limit=10)) == ["4", "3", "2", "1"]
    assert _items(_read(events, cutoff=4, limit=10, direction="forward")) == ["1", "2", "3", "4"]


def test_a_cursor_belongs_to_its_direction():
    events = _events(10)
    backward = _read(events, cutoff=10)["history"]["next_cursor"]
    forward = _read(events, cutoff=10, direction="forward")["history"]["next_cursor"]

    for cursor, direction in ((backward, "forward"), (forward, "backward"), ("nonsense", "backward"), (f"{BASE}.", "forward")):
        with pytest.raises(CursorInvalid):
            _read(events, cutoff=10, cursor=cursor, direction=direction)
    assert _items(_read(events, cutoff=10, cursor=backward)) == ["7", "6", "5"]
    assert _items(_read(events, cutoff=10, cursor=forward, direction="forward")) == ["4", "5", "6"]


def test_a_gap_is_dependency_unavailable_in_both_directions():
    events = [event for event in _events(10) if event["sequence"] != "6"]

    with pytest.raises(ContextGap):
        _read(events, cutoff=10, limit=10)
    with pytest.raises(ContextGap):
        _read(events, cutoff=10, limit=10, direction="forward")
    # A page that does not reach the gap is fine.
    assert _items(_read(events, cutoff=10, limit=3)) == ["10", "9", "8"]
    # The first event missing: the thread would not end at sequence 1.
    with pytest.raises(ContextGap):
        _read(events[1:], cutoff=10, limit=100)


def test_the_page_cap_cuts_the_oldest_backward_and_the_newest_forward_but_keeps_one_event():
    events = _events(10, size=400_000)

    backward = serve_context(events, cutoff=10, base=BASE, cursor=None, limit=10, direction="backward", page=_full)
    forward = serve_context(events, cutoff=10, base=BASE, cursor=None, limit=10, direction="forward", page=_full)

    assert [event["sequence"] for event in backward["history"]["items"]] == ["10", "9"]
    assert backward["history"]["has_more"] is True and backward["history"]["next_cursor"] == f"{BASE}.b9"
    assert [event["sequence"] for event in forward["history"]["items"]] == ["1", "2"]
    assert forward["history"]["next_cursor"] == f"{BASE}.2"
    # An event over the cap alone still goes (every accepted event reaches the agent).
    huge = _events(3, size=1_200_000)
    single = serve_context(huge, cutoff=3, base=BASE, cursor=None, limit=3, direction="backward", page=_full)
    assert [event["sequence"] for event in single["history"]["items"]] == ["3"] and single["history"]["has_more"] is True


def _full(items: list[dict[str, Any]], next_cursor: str | None, has_more: bool) -> dict[str, Any]:
    return {"history": {"items": items, "next_cursor": next_cursor, "has_more": has_more}}


async def _context_request(fake: FakeDooersThreads, delivery, **params: Any) -> httpx.Response:
    headers = {"Authorization": f"Bearer {fake.core_token}", "Dooers-Threads-Execution-Capability": delivery.capability}
    with fake.serving() as _:
        async with httpx.AsyncClient() as client:
            return await client.get(
                f"{fake.config.callback_base_url}/deliveries/{delivery.delivery_id}/context", params=params, headers=headers
            )


async def test_fake_dooers_threads_serves_the_context_both_ways_over_http():
    fake = FakeDooersThreads()
    thread_id = fake.create_thread(USER, title=None)
    for number in range(1, 8):
        trigger = fake.publish(thread_id, USER, [{"type": "text", "text": f"m{number}"}], client_event_id=f"c{number}")
    (delivery,) = fake.deliveries_for(trigger)
    assert shared_contracts() is not None

    async def read(**params):
        response = await _context_request(fake, delivery, **params)
        return response.status_code, json.loads(response.content)

    cursor = delivery.context_cursor
    status, back = await read(cursor=cursor, direction="backward", limit=3)
    assert status == 200
    assert [event["sequence"] for event in back["history"]["items"]] == ["8", "7", "6"] and back["history"]["has_more"] is True
    status, rest = await read(cursor=back["history"]["next_cursor"], direction="backward", limit=10)
    assert [event["sequence"] for event in rest["history"]["items"]] == ["5", "4", "3", "2", "1"] and rest["history"]["next_cursor"] is None
    # No direction is forward, from the first event; no cursor starts at the tip.
    status, ahead = await read(cursor=cursor, limit=2)
    assert [event["sequence"] for event in ahead["history"]["items"]] == ["1", "2"]
    status, tip = await read(direction="backward", limit=1)
    assert [event["sequence"] for event in tip["history"]["items"]] == ["8"]
    # The direction is bound to the cursor.
    assert (await read(cursor=back["history"]["next_cursor"], limit=2))[0] == 400
    assert (await read(cursor=ahead["history"]["next_cursor"], direction="backward", limit=2))[0] == 400
    assert delivery.context_requests[0] == (cursor, 3, "backward")
