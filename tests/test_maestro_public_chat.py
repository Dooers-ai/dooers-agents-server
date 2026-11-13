"""The SDK exposes a public chat visitor to the agent handler like production (plan 6, Task 5).

Production reference (origin/main): ``HandlerPipeline._assistant_delivery_data`` (``pipeline.py``,
around line 1230) and ``tools/rag/runtime.py`` (``on_behalf`` requires channel ``dooers-platform``).
Field map: ``.superpowers/sdd/2026-11-11-paridade-sdk-maestro-06-chat-publico/task-3b-report.md``,
section "What plan 6 Tasks 4 and 5 must read" — the external participant of
``Public.ContextResponse.participants`` (``kind: external``, ``channel: public_chat``) carries
``connection_type``, ``display_name``, ``email``, ``identity_ids``, the three roles and
``guest_profile {name?, email?, phone?}``.

Two corrections to the plan text (binding, see the task-5 brief): the production channel for
public chat is ``dooers-public-chat``, not ``dooers-platform``; and ``on_behalf`` is ``False`` for
both ``guest`` and ``public_authenticated`` (production requires channel ``dooers-platform`` for
``on_behalf=True``, which public chat never has).
"""

from __future__ import annotations

import pytest

from dooers.agents.server.maestro.context import maestro_handler_context
from dooers.agents.server.maestro.legacy_model import assistant_delivery_data
from dooers.agents.server.maestro.persistence import MaestroAgentMemory
from dooers.agents.server.protocol.maestro import default_contracts
from dooers.tools.rag.runtime import current_execution_context
from tests.parity.model import AGENT_ID, ASSISTANT_NAME
from tests.test_maestro_context import fixture_server, prepare, private
from tests.test_maestro_legacy_model import AGENT, MaestroThread
from tests.test_maestro_memory_parity import ContextMemory

PUBLIC_CHAT_META = {"public_chat_label": "Site institucional"}


@pytest.fixture(scope="module")
def key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def bundle():
    return default_contracts()


def _external_actor(**overrides) -> dict:
    base = {"kind": "external", "external_id": "guest:abc123", "channel": "public_chat"}
    base.update(overrides)
    return base


def _external_participant(participant_id: str, **overrides) -> dict:
    base = {
        "participant_id": participant_id,
        "state": "active",
        "joined_sequence": "1",
        "left_sequence": None,
        "external_id": "guest:abc123",
        "channel": "public_chat",
        "kind": "external",
        "role": "member",
    }
    base.update(overrides)
    return base


def _set_public_chat_visitor(
    page: dict, *, connection_type: str, actor_overrides: dict | None = None, participant_overrides: dict | None = None
) -> None:
    """Replace ``page``'s human actor/participant with a public-chat external visitor."""
    participant_id = page["participants"][0]["participant_id"]
    page["thread"].update(channel="dooers-public-chat", channel_meta=dict(PUBLIC_CHAT_META))
    actor = _external_actor(participant_id=participant_id, connection_type=connection_type, **(actor_overrides or {}))
    page["trigger_event"]["actor"] = page["history"]["items"][0]["actor"] = actor
    page["participants"][0] = _external_participant(participant_id, connection_type=connection_type, **(participant_overrides or {}))


# -- context.user / context.channel / on_behalf --


async def test_guest_visitor_user_and_on_behalf(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _set_public_chat_visitor(
            page,
            connection_type="guest",
            participant_overrides={
                "display_name": "Visitante",
                "system_role": "guest",
                "organization_role": "guest",
                "workspace_role": "guest",
                "guest_profile": {"name": "Visitante Declarado", "email": "visitante@example.com", "phone": "+5511988887777"},
            },
        )
        async with maestro_handler_context(memory, private) as prepared:
            user = prepared.on.context.user
            assert user.user_id == "guest:abc123"
            assert user.connection_type == "guest"
            assert user.user_name == "Visitante"  # core display_name wins over guest_profile.name
            assert (user.system_role, user.organization_role, user.workspace_role) == ("guest", "guest", "guest")
            assert prepared.on.context.channel == "dooers-public-chat"
            assert prepared.on.context.channel_meta == PUBLIC_CHAT_META
            execution = current_execution_context()
            assert execution.on_behalf is False and execution.user_id == ""


async def test_public_authenticated_visitor_is_never_on_behalf(key, bundle):
    """The plan text wrongly says ``on_behalf`` is true for ``public_authenticated``; it is not (correction 2)."""
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _set_public_chat_visitor(
            page,
            connection_type="public_authenticated",
            participant_overrides={"display_name": "Cliente Autenticado", "email": "cliente@example.com"},
        )
        async with maestro_handler_context(memory, private) as prepared:
            user = prepared.on.context.user
            assert user.connection_type == "public_authenticated"
            assert user.user_name == "Cliente Autenticado"
            assert user.user_email == "cliente@example.com"
            execution = current_execution_context()
            assert execution.on_behalf is False and execution.user_id == ""


async def test_core_name_wins_over_guest_profile_name(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _set_public_chat_visitor(
            page,
            connection_type="guest",
            participant_overrides={"display_name": "Nome Core", "guest_profile": {"name": "Nome Declarado"}},
        )
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user.user_name == "Nome Core"


async def test_guest_profile_name_used_when_no_core_name(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _set_public_chat_visitor(
            page,
            connection_type="guest",
            participant_overrides={"guest_profile": {"name": "Nome Declarado"}},
        )
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user.user_name == "Nome Declarado"


async def test_no_name_anywhere_has_no_name(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _set_public_chat_visitor(page, connection_type="guest")
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user.user_name is None


# -- data.delivery (F7) --


async def test_assistant_message_in_public_chat_thread_carries_delivery_data():
    thread = MaestroThread(default_contracts())
    thread.message(AGENT, [{"type": "text", "text": "Olá! Como posso ajudar?"}])
    thread_view = {
        "thread_id": thread.thread_id,
        "title": "Chat",
        "queue": None,
        "channel": "dooers-public-chat",
        "channel_meta": dict(PUBLIC_CHAT_META),
        "created_at": "2026-11-12T10:00:00Z",
        "owner_instance_agent_id": AGENT_ID,
    }
    context = ContextMemory(thread, thread_view=thread_view)
    memory = MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME)

    events = await memory.get_history_raw()

    assert events[0].data == {"delivery": {"channel": "dooers-public-chat", "public_chat_label": "Site institucional"}}


async def test_dooers_platform_thread_has_no_delivery_data():
    """Parity: production adds no ``data.delivery`` on ``dooers-platform`` (plain web chat)."""
    thread = MaestroThread(default_contracts())
    thread.message(AGENT, [{"type": "text", "text": "Olá!"}])
    thread_view = {
        "thread_id": thread.thread_id,
        "title": "Chat",
        "queue": None,
        "channel": "dooers-platform",
        "channel_meta": None,
        "created_at": "2026-11-12T10:00:00Z",
        "owner_instance_agent_id": AGENT_ID,
    }
    context = ContextMemory(thread, thread_view=thread_view)
    memory = MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME)

    events = await memory.get_history_raw()

    assert events[0].data is None


def test_assistant_delivery_data_matches_production_shape_and_skips_the_default_channel():
    assert assistant_delivery_data(None, None) is None
    assert assistant_delivery_data("dooers-platform", None) is None
    assert assistant_delivery_data("dooers-public-chat", None) == {"delivery": {"channel": "dooers-public-chat"}}
    assert assistant_delivery_data("dooers-public-chat", {"public_chat_label": "Site"}) == {
        "delivery": {"channel": "dooers-public-chat", "public_chat_label": "Site"}
    }
    assert assistant_delivery_data("dooers-public-chat", {"public_chat_label": "  "}) == {"delivery": {"channel": "dooers-public-chat"}}
