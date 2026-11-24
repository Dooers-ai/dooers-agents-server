from __future__ import annotations

import pytest

from dooers.agents.server.features.memory import (
    InMemoryMemoryStore,
    LongTermMemory,
    MemoryOwner,
    MemoryUnavailableError,
    format_memories_for_prompt,
)


def _api(store, user="u1", workspace="w1", notifier=None) -> LongTermMemory:
    return LongTermMemory(store, MemoryOwner(agent_id="a1", organization_id="o1", workspace_id=workspace, user_id=user), notifier=notifier)


async def test_remember_dedupes_and_recalls_by_relevance():
    store = InMemoryMemoryStore()
    api = _api(store)
    first = await api.remember("Prefere respostas curtas e em português", kind="preference", tags=["Estilo"])
    again = await api.remember("prefere respostas CURTAS e em português!", tags=["tom"])
    await api.remember("Trabalha no time de finanças da Acme")
    assert again.id == first.id
    assert set(again.tags) == {"estilo", "tom"}
    results = await api.recall("em qual time de finanças ele trabalha?")
    assert results[0].content.startswith("Trabalha no time")
    listed, has_more = await api.list()
    assert len(listed) == 2 and has_more is False


async def test_user_memories_are_private_and_workspace_memories_shared():
    store = InMemoryMemoryStore()
    alice, bob = _api(store, "alice"), _api(store, "bob")
    await alice.remember("Alice é alérgica a amendoim")
    shared = await alice.remember("O fiscal do workspace fecha dia 25", scope="workspace")
    assert shared.user_id == ""
    bob_view, _ = await bob.list()
    assert [m.content for m in bob_view] == ["O fiscal do workspace fecha dia 25"]
    assert await bob.get(shared.id) is not None
    private = (await alice.list(scope="user"))[0][0]
    assert await bob.get(private.id) is None
    assert await bob.forget(private.id) is False
    other_ws = _api(store, "alice", workspace="w2")
    assert (await other_ws.list())[0] == []


async def test_personal_chats_have_no_workspace_scope():
    store = InMemoryMemoryStore()
    api = _api(store, workspace="")
    record = await api.remember("nota pessoal", scope="workspace")
    assert record.scope == "user"


async def test_guest_and_missing_store_are_unavailable():
    assert _api(InMemoryMemoryStore(), user="guest:abc").available is False
    with pytest.raises(MemoryUnavailableError):
        await _api(None).remember("x")
    assert await _api(None).recall("x") == []
    prefs = await _api(None).preferences()
    assert prefs.enabled is False


async def test_update_forget_and_notifications():
    events: list[tuple[str, str]] = []

    async def notify(action, record, user_id):
        events.append((action, record.id))

    store = InMemoryMemoryStore()
    api = _api(store, notifier=notify)
    record = await api.remember("Gosta de café")
    updated = await api.update(record.id, content="Gosta de café sem açúcar", pinned=True)
    assert updated and updated.pinned and updated.content == "Gosta de café sem açúcar"
    assert await api.forget(record.id) is True
    assert await api.get(record.id) is None
    assert [a for a, _ in events] == ["upsert", "upsert", "deleted"]


async def test_preferences_round_trip_and_prompt_format():
    store = InMemoryMemoryStore()
    api = _api(store)
    prefs = await api.set_preferences(custom_instructions="Responda em tópicos", about_me="Sou engenheira")
    assert prefs.custom_instructions == "Responda em tópicos"
    assert (await api.preferences()).about_me == "Sou engenheira"
    record = await api.remember("Mora em Curitiba", pinned=True)
    text = format_memories_for_prompt([record])
    assert record.id in text and "pinned" in text


async def test_content_validation():
    api = _api(InMemoryMemoryStore())
    with pytest.raises(ValueError):
        await api.remember("   ")
    with pytest.raises(ValueError):
        await api.remember("x" * 5000)
