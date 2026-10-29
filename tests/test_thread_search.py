from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.agents.server.handlers.router import Router
from dooers.agents.server.persistence.postgres import _ilike_pattern
from dooers.agents.server.protocol.frames import C2S_ThreadSearch, ThreadSearchPayload
from dooers.agents.server.protocol.models import User
from dooers.agents.server.registry import ConnectionRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


def _router() -> tuple[Router, AsyncMock, FakeWebSocket]:
    persistence = AsyncMock()
    persistence.list_threads.return_value = []
    persistence.count_threads.return_value = 0
    router = Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
    )
    router._agent_id = "agent-1"
    router._organization_id = "org-1"
    router._workspace_id = "ws-1"
    router._user = User(user_id="u1")
    return router, persistence, FakeWebSocket()


def _frames(ws: FakeWebSocket) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in ws.sent]


def test_ilike_pattern_matches_wildcards_literally() -> None:
    assert _ilike_pattern("50%_off\\") == "%50\\%\\_off\\\\%"


@pytest.mark.asyncio
async def test_thread_search_filters_by_title_and_echoes_the_query() -> None:
    router, persistence, ws = _router()

    await router._handle_thread_search(
        ws,
        C2S_ThreadSearch(id="f1", type="thread.search", payload=ThreadSearchPayload(query="  pedido ")),
    )

    assert persistence.list_threads.await_args.kwargs["title_query"] == "pedido"
    assert persistence.count_threads.await_args.kwargs["title_query"] == "pedido"
    result = next(f for f in _frames(ws) if f["type"] == "thread.search.result")
    assert result["payload"]["query"] == "pedido"
    assert result["payload"]["threads"] == []


@pytest.mark.asyncio
async def test_thread_search_refuses_an_empty_query() -> None:
    router, persistence, ws = _router()

    await router._handle_thread_search(
        ws,
        C2S_ThreadSearch(id="f1", type="thread.search", payload=ThreadSearchPayload(query="   ")),
    )

    persistence.list_threads.assert_not_called()
    ack = next(f for f in _frames(ws) if f["type"] == "ack")
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "INVALID_QUERY"


@pytest.mark.asyncio
async def test_thread_search_failure_answers_instead_of_closing_the_socket() -> None:
    router, persistence, ws = _router()
    persistence.list_threads.side_effect = RuntimeError("invalid byte sequence")

    await router._handle_thread_search(
        ws,
        C2S_ThreadSearch(id="f1", type="thread.search", payload=ThreadSearchPayload(query="pe\x00dido")),
    )

    ack = next(f for f in _frames(ws) if f["type"] == "ack")
    assert ack["payload"]["ok"] is False
    assert ack["payload"]["error"]["code"] == "SEARCH_FAILED"
    assert persistence.list_threads.await_args.kwargs["title_query"] == "pedido"
