from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.agents.server.handlers.router import _THREAD_SNAPSHOT_LIMIT, Router
from dooers.agents.server.protocol.frames import C2S_ThreadSubscribe, ThreadSubscribePayload
from dooers.agents.server.protocol.models import Thread, ThreadEvent, User
from dooers.agents.server.registry import ConnectionRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def receive_text(self) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - unused
        pass


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


def _event(event_id: str, created_at: datetime) -> ThreadEvent:
    return ThreadEvent(
        id=event_id,
        thread_id="thread-1",
        type="message",
        actor="assistant",
        created_at=created_at,
    )


def _thread(user: User) -> Thread:
    now = datetime.now(UTC)
    return Thread(
        id="thread-1",
        agent_id="agent-1",
        organization_id="org-1",
        workspace_id="ws-1",
        owner=user,
        users=[user],
        created_at=now,
        updated_at=now,
        last_event_at=now,
    )


def _router_with_events(events: list[ThreadEvent]) -> tuple[Router, FakeWebSocket, User]:
    user = User(user_id="user-1")
    persistence = AsyncMock()
    persistence.get_thread = AsyncMock(return_value=_thread(user))

    async def get_events(
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: str = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        rows = list(events)
        if after_event_id:
            index = next(i for i, event in enumerate(rows) if event.id == after_event_id)
            rows = rows[index + 1 :]
        if order == "desc":
            rows = list(reversed(rows))
        return rows[:limit]

    persistence.get_events = get_events
    router = Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
    )
    router._agent_id = "agent-1"
    router._user = user
    router._workspace_id = "ws-1"
    router._subscriptions[router._ws_id] = set()
    ws = FakeWebSocket()
    return router, ws, user


def _snapshot(ws: FakeWebSocket) -> dict[str, Any]:
    frames = [json.loads(raw) for raw in ws.sent]
    snapshot = next(frame for frame in frames if frame["type"] == "thread.snapshot")
    return snapshot["payload"]


@pytest.mark.asyncio
async def test_subscribe_opens_on_latest_events_not_oldest():
    start = datetime(2026, 10, 3, 16, 35, tzinfo=UTC)
    events = [_event(f"e{i}", start + timedelta(seconds=i)) for i in range(327)]
    router, ws, _user = _router_with_events(events)

    await router._handle_thread_subscribe(
        ws,
        C2S_ThreadSubscribe(
            id="sub-1",
            type="thread.subscribe",
            payload=ThreadSubscribePayload(thread_id="thread-1"),
        ),
    )

    payload = _snapshot(ws)
    ids = [event["id"] for event in payload["events"]]
    assert ids[0] == "e0"
    assert ids[-1] == "e326"
    assert len(ids) == 327
    assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_subscribe_pages_from_the_tail_when_history_exceeds_limit():
    start = datetime(2026, 10, 3, 16, 35, tzinfo=UTC)
    total = _THREAD_SNAPSHOT_LIMIT + 40
    events = [_event(f"e{i}", start + timedelta(seconds=i)) for i in range(total)]
    router, ws, _user = _router_with_events(events)

    await router._handle_thread_subscribe(
        ws,
        C2S_ThreadSubscribe(
            id="sub-1",
            type="thread.subscribe",
            payload=ThreadSubscribePayload(thread_id="thread-1"),
        ),
    )

    payload = _snapshot(ws)
    ids = [event["id"] for event in payload["events"]]
    assert ids[0] == f"e{total - _THREAD_SNAPSHOT_LIMIT}"
    assert ids[-1] == f"e{total - 1}"
    assert len(ids) == _THREAD_SNAPSHOT_LIMIT
    assert payload["has_more"] is True


@pytest.mark.asyncio
async def test_subscribe_after_event_id_only_fills_the_gap():
    start = datetime(2026, 10, 3, 16, 35, tzinfo=UTC)
    events = [_event(f"e{i}", start + timedelta(seconds=i)) for i in range(5)]
    router, ws, _user = _router_with_events(events)

    await router._handle_thread_subscribe(
        ws,
        C2S_ThreadSubscribe(
            id="sub-1",
            type="thread.subscribe",
            payload=ThreadSubscribePayload(thread_id="thread-1", after_event_id="e2"),
        ),
    )

    payload = _snapshot(ws)
    assert [event["id"] for event in payload["events"]] == ["e3", "e4"]
    assert payload["has_more"] is False
