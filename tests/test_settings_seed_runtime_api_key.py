"""settings.seed persists dooers_runtime_api_key for OTEL token minting."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dooers.agents.server.handlers.router import Router
from dooers.agents.server.observability.service_token import RUNTIME_API_KEY_SECRET_NAME
from dooers.agents.server.protocol.frames import C2S_SettingsSeed, SettingsSeedPayload
from dooers.agents.server.registry import ConnectionRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


async def _noop_handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


def _make_router(persistence: AsyncMock) -> Router:
    return Router(
        persistence=persistence,
        handler=_noop_handler,
        registry=ConnectionRegistry(),
        subscriptions={},
        auth_validator=None,
    )


def _last_ack(ws: FakeWebSocket) -> dict[str, Any]:
    assert ws.sent, "expected at least one frame"
    return json.loads(ws.sent[-1])


@pytest.mark.asyncio
async def test_settings_seed_persists_runtime_api_key_in_service_secrets():
    persistence = AsyncMock()
    persistence.get_worker_seed_hash_bytes = AsyncMock(return_value=None)
    persistence.set_settings = AsyncMock()
    persistence.set_worker_seed_hash_bytes = AsyncMock()
    persistence.merge_service_secrets = AsyncMock()

    router = _make_router(persistence)
    ws = FakeWebSocket()
    frame = C2S_SettingsSeed(
        id="seed-1",
        type="settings.seed",
        payload=SettingsSeedPayload(
            worker_id="worker-1",
            values={"greeting": "hi"},
            seed_secret="runtime-api-key-from-core",
        ),
    )

    await router._handle_settings_seed(ws, frame)

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    persistence.merge_service_secrets.assert_awaited_once_with(
        "worker-1", {RUNTIME_API_KEY_SECRET_NAME: "runtime-api-key-from-core"}
    )
    persistence.set_worker_seed_hash_bytes.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_seed_rotation_stores_next_seed_secret():
    persistence = AsyncMock()
    persistence.get_worker_seed_hash_bytes = AsyncMock(return_value=None)
    persistence.set_settings = AsyncMock()
    persistence.set_worker_seed_hash_bytes = AsyncMock()
    persistence.merge_service_secrets = AsyncMock()

    router = _make_router(persistence)
    ws = FakeWebSocket()
    frame = C2S_SettingsSeed(
        id="seed-2",
        type="settings.seed",
        payload=SettingsSeedPayload(
            worker_id="worker-1",
            values={},
            seed_secret="old-key",
            next_seed_secret="new-key-after-rotation",
        ),
    )

    await router._handle_settings_seed(ws, frame)

    ack = _last_ack(ws)
    assert ack["payload"]["ok"] is True
    persistence.merge_service_secrets.assert_awaited_once_with(
        "worker-1", {RUNTIME_API_KEY_SECRET_NAME: "new-key-after-rotation"}
    )
