"""USER fields are personal: one person's value never changes what another person gets."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dooers.agents.server.features.settings.agent_settings import AgentSettings
from dooers.agents.server.features.settings.broadcaster import SettingsBroadcaster
from dooers.agents.server.features.settings.models import (
    SettingsField,
    SettingsFieldType,
    SettingsFieldVisibility,
    SettingsSchema,
    merge_user_values,
    user_settings_key,
)
from dooers.agents.server.handlers.router import Router
from dooers.agents.server.protocol.frames import C2S_SettingsPatch, SettingsPatchPayload
from dooers.agents.server.protocol.models import User
from dooers.agents.server.registry import ConnectionRegistry

AGENT = "agent-1"
SCHEMA = SettingsSchema(
    fields=[
        SettingsField(id="tone", type=SettingsFieldType.TEXT, label="Tom", value="neutro"),
        SettingsField(
            id="persona",
            type=SettingsFieldType.TEXTAREA,
            label="Persona",
            value="",
            visibility=SettingsFieldVisibility.CREATOR,
        ),
    ]
)


class DictPersistence:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get_settings(self, agent_id: str) -> dict[str, Any]:
        return dict(self.rows.get(agent_id, {}))

    async def update_setting(self, agent_id: str, field_id: str, value: Any) -> None:
        self.rows.setdefault(agent_id, {})[field_id] = value

    async def set_settings(self, agent_id: str, values: dict[str, Any]) -> None:
        self.rows[agent_id] = dict(values)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def receive_text(self) -> str:  # pragma: no cover
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        pass


async def _handler(on, send, memory, analytics, settings):  # pragma: no cover
    if False:
        yield


def test_merge_keeps_creator_fields_from_the_agent():
    merged = merge_user_values(
        {"tone": "formal", "persona": "A"}, {"tone": "casual", "persona": "hack"}, SCHEMA
    )
    assert merged == {"tone": "casual", "persona": "A"}


@pytest.mark.asyncio
async def test_handler_settings_are_per_person():
    store = DictPersistence()
    store.rows[AGENT] = {"tone": "formal"}
    store.rows[user_settings_key(AGENT, "ana")] = {"tone": "casual"}
    broadcaster = SettingsBroadcaster(ConnectionRegistry(), {}, {})

    def settings_for(user_id):
        return AgentSettings(AGENT, SCHEMA, store, broadcaster, user_id=user_id)  # type: ignore[arg-type]

    assert (await settings_for("ana").get_all())["tone"] == "casual"
    # Someone who never saved keeps the agent value; a guest never gets personal values.
    assert (await settings_for("bia").get_all())["tone"] == "formal"
    assert await settings_for("guest:abc").get("tone") == "formal"
    assert await settings_for(None).get("tone") == "formal"


def _router(store: DictPersistence, user: User, audience: str) -> tuple[Router, FakeWebSocket]:
    registry = ConnectionRegistry()
    ws_context: dict[str, dict[str, Any]] = {}
    router = Router(
        persistence=store,  # type: ignore[arg-type]
        handler=_handler,
        registry=registry,
        subscriptions={},
        settings_broadcaster=SettingsBroadcaster(registry, {}, ws_context),
        settings_schema=SCHEMA,
        settings_ws_context=ws_context,
    )
    ws = FakeWebSocket()
    router._agent_id = AGENT
    router._user = user
    ws_context[router._ws_id] = {"agent_id": AGENT, "audience": audience, "ws": ws}
    return router, ws


def _patch(field_id: str, value: Any) -> C2S_SettingsPatch:
    return C2S_SettingsPatch(
        id="f1", type="settings.patch", payload=SettingsPatchPayload(field_id=field_id, value=value)
    )


def _acks(ws: FakeWebSocket) -> list[dict[str, Any]]:
    return [f for f in ws.sent if f.get("type") == "ack"]


@pytest.mark.asyncio
async def test_chat_settings_page_saves_only_for_that_person():
    store = DictPersistence()
    store.rows[AGENT] = {"tone": "formal"}
    ana = User(user_id="ana", connection_type="dashboard")
    router, ws = _router(store, ana, "user")

    await router._handle_settings_patch(ws, _patch("tone", "casual"))

    assert _acks(ws)[-1]["payload"]["ok"] is True
    assert store.rows[AGENT] == {"tone": "formal"}
    assert store.rows[user_settings_key(AGENT, "ana")] == {"tone": "casual"}


@pytest.mark.asyncio
async def test_studio_still_sets_the_default_for_everyone():
    store = DictPersistence()
    owner = User(user_id="owner", connection_type="dashboard", system_role="admin")
    router, ws = _router(store, owner, "creator")
    router._agent_owner_user_id = "owner"

    await router._handle_settings_patch(ws, _patch("tone", "formal"))

    assert store.rows[AGENT] == {"tone": "formal"}


@pytest.mark.asyncio
async def test_guests_cannot_save_personal_settings():
    store = DictPersistence()
    guest = User(user_id="guest:abc", connection_type="dashboard")
    router, ws = _router(store, guest, "user")

    await router._handle_settings_patch(ws, _patch("tone", "casual"))

    assert _acks(ws)[-1]["payload"]["ok"] is False
    assert store.rows == {}
