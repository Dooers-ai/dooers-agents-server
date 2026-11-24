"""Reading a public chat visitor back like production (plan 6, final review fixes).

Production reference (origin/main): ``HandlerPipeline.setup`` (``Thread.users=[visitor, agent]``,
``Thread.metadata`` = the first ``event.create`` metadata), ``_enrich_user_from_metadata`` (name and e-mail
of the visitor from ``guest_name``/``guest_email``), ``_public_chat_source_data``/``_assistant_delivery_data``
(``handlers/pipeline.py``). The visitor of Dooers Threads is an ``external`` participant (``guest_profile`` is the
pre-chat the visitor declared); its message actor carries only ``participant_id``, never the profile.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from dooers.agents.server.dooers_threads.context import _EXTERNAL_CHANNEL, dooers_threads_handler_context
from dooers.agents.server.dooers_threads.legacy_model import (
    assistant_delivery_data,
    production_user,
    public_chat_source_data,
    to_thread,
    to_thread_event,
)
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.protocol.models import User
from tests.parity.model import AGENT_ID, ASSISTANT_NAME
from tests.test_dooers_threads_context import fixture_server, prepare, private
from tests.test_dooers_threads_legacy_model import AGENT, DooersThreadsThread, _links, _thread_view
from tests.test_dooers_threads_public_chat import PUBLIC_CHAT_META, _external_actor, _external_participant, _set_public_chat_visitor

VISITOR_ID = "guest:abc123"
GUEST_PROFILE = {"name": "Carlos Lima", "email": "carlos@example.com", "phone": "+5511977776666"}


def _visitor(participant_id: str | None = None, *, joined: str = "1", **overrides: Any) -> dict[str, Any]:
    return _external_participant(participant_id or str(uuid4()), joined_sequence=joined, **{"connection_type": "guest", **overrides})


def _agent_participant(joined: str = "1") -> dict[str, Any]:
    return {
        "participant_id": AGENT["participant_id"],
        "state": "active",
        "joined_sequence": joined,
        "left_sequence": None,
        "kind": "agent",
        "principal_id": AGENT_ID,
        "instance_agent_id": AGENT_ID,
        "role": "member",
        "display_name": ASSISTANT_NAME,
        "availability": "available",
        "capabilities": [],
    }


def _public_chat_view(*participants: dict[str, Any], channel: str | None = "dooers-public-chat", meta: Any = PUBLIC_CHAT_META) -> dict:
    view = _thread_view(str(uuid4()), owner_instance_agent_id=AGENT_ID, channel=channel, channel_meta=meta)
    view["participants"] = list(participants)
    return view


# -- I2: Thread.users order --


@pytest.mark.parametrize("agent_first", [True, False], ids=["agent_listed_first", "visitor_listed_first"])
def test_thread_users_lists_the_visitor_before_the_agent_like_production(agent_first):
    visitor, agent = _visitor(joined="1"), _agent_participant("1")
    view = _public_chat_view(*([agent, visitor] if agent_first else [visitor, agent]))

    thread = to_thread(view, recipient_instance_id=AGENT_ID, assistant_name=ASSISTANT_NAME)

    assert [user.user_id for user in thread.users] == [VISITOR_ID, AGENT_ID]
    assert thread.owner.user_id == VISITOR_ID


def test_the_first_external_to_join_is_the_owner_and_comes_first():
    first, second = _visitor(joined="1", external_id="+5511111110000"), _visitor(joined="1", external_id="+5522222220000")
    first["participant_id"], second["participant_id"] = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
    view = _public_chat_view(_agent_participant("1"), second, first)

    thread = to_thread(view, recipient_instance_id=AGENT_ID, assistant_name=ASSISTANT_NAME)

    assert [user.user_id for user in thread.users] == ["+5511111110000", AGENT_ID, "+5522222220000"]
    assert thread.owner.user_id == "+5511111110000"


def test_a_human_owner_still_comes_first_ahead_of_an_external():
    owner = {**_thread_view(str(uuid4()))["participants"][0]}
    view = _public_chat_view(_visitor(joined="1"), _agent_participant("1"), owner)

    thread = to_thread(view, recipient_instance_id=AGENT_ID, assistant_name=ASSISTANT_NAME)

    assert thread.owner.user_id == owner["principal_id"]
    # A human owner's thread is [owner, agent, added...]: the visitor is one of the added.
    assert [user.user_id for user in thread.users] == [owner["principal_id"], AGENT_ID, VISITOR_ID]


# -- I5/M1: a contact's current name, the connection type --


def test_the_actors_current_name_wins_over_the_name_the_participant_stored_once():
    actor = {"kind": "external", "external_id": "+5511999990000", "channel": "whatsapp", "display_name": "Novo"}
    participant = {**actor, "display_name": "Antigo", "participant_id": str(uuid4()), "role": "member"}

    assert production_user(actor, participant).user_name == "Novo"
    # Without a name on the actor (the visitor's actor never has one) the participant gives it.
    assert production_user({k: v for k, v in actor.items() if k != "display_name"}, participant).user_name == "Antigo"


def test_connection_type_falls_back_to_the_actors_when_the_participant_has_none():
    actor = _external_actor(connection_type="public_authenticated", participant_id="p")
    participant = {"kind": "external", "external_id": VISITOR_ID, "channel": "public_chat", "participant_id": "p", "role": "member"}

    assert production_user(actor, participant).connection_type == "public_authenticated"
    assert production_user(actor, {**participant, "connection_type": "guest"}).connection_type == "guest"


# -- I3: author, name, e-mail and data.source of the visitor's message --


async def _visitor_message(participants: list[dict[str, Any]], actor: dict[str, Any], **conversion: Any):
    thread = DooersThreadsThread(_contracts())
    event = thread.message(actor, [{"type": "text", "text": "Olá, quero um orçamento"}])
    return await to_thread_event(
        event,
        recipient_instance_id=AGENT_ID,
        agent_names={},
        multi_agent=False,
        links=_links(),
        assistant_name=ASSISTANT_NAME,
        participants=participants,
        **conversion,
    )


def _contracts():
    from dooers.agents.server.protocol.dooers_threads import default_contracts

    return default_contracts()


async def test_the_visitors_message_has_the_author_name_and_email_of_its_participant():
    participant = _visitor(guest_profile=dict(GUEST_PROFILE))
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")

    event = await _visitor_message([_agent_participant(), participant], actor)

    assert event.actor == "user"
    assert event.author == "Carlos Lima"
    assert (event.user.user_id, event.user.user_name, event.user.user_email) == (VISITOR_ID, "Carlos Lima", "carlos@example.com")


async def test_a_messages_visitor_without_a_declared_pre_chat_has_no_name():
    participant = _visitor()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")

    event = await _visitor_message([participant], actor)

    assert (event.author, event.user) == (None, User(user_id=VISITOR_ID))


async def test_a_message_actor_unknown_to_the_participants_keeps_its_own_fields():
    actor = _external_actor(participant_id=str(uuid4()), connection_type="guest", display_name="Quem declarou")

    event = await _visitor_message([_visitor()], actor)

    assert event.author == "Quem declarou"


async def test_the_visitors_message_carries_the_inbound_source_like_production():
    participant = _visitor(guest_profile=dict(GUEST_PROFILE))
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")

    event = await _visitor_message([participant], actor, channel="dooers-public-chat", channel_meta=dict(PUBLIC_CHAT_META))

    assert event.data == {"source": {"channel": "dooers-public-chat", "public_chat_label": "Site institucional"}}
    # The agent's own message gets the delivery data instead, never the source.
    assert event.user.user_id == VISITOR_ID


async def test_the_source_is_what_production_writes_for_the_same_channel_data():
    """``HandlerPipeline._public_chat_source_data`` is the reference (it reads only ``channel`` and ``channel_meta``)."""
    cases = [
        ("dooers-public-chat", {"public_chat_label": "Site"}),
        ("dooers-public-chat", {"public_chat": {"label": " Aninhado "}}),
        ("dooers-public-chat", {"public_chat_label": "  ", "public_chat": {"label": "Aninhado"}}),
        ("dooers-public-chat", {}),
        ("dooers-public-chat", None),
        ("dooers-platform", None),
        ("", None),
        (None, None),
    ]
    for channel, meta in cases:
        context = HandlerContext(
            handler=None, agent_id=AGENT_ID, message="", channel=channel or "", channel_meta=meta, user=User(user_id=VISITOR_ID)
        )
        assert public_chat_source_data(channel, meta) == HandlerPipeline._inbound_source_data(context), (channel, meta)


async def test_a_message_outside_a_public_chat_gets_no_source_from_the_thread():
    """A WhatsApp inbound's source has per-message values (``wa_message_id``): they stay on the message's own data."""
    participant = _visitor(channel="whatsapp", external_id="+5511999990000")
    actor = {"kind": "external", "external_id": "+5511999990000", "channel": "whatsapp", "participant_id": participant["participant_id"]}
    meta = {"whatsapp": {"instance_id": "inst-1", "from_e164": "+5511999990000", "wa_message_id": "wamid.1"}}

    event = await _visitor_message([participant], actor, channel="whatsapp", channel_meta=meta)

    assert event.data is None


# -- M2: the nested label of the delivery data --


def test_assistant_delivery_data_reads_the_nested_public_chat_label_like_production():
    assert assistant_delivery_data("dooers-public-chat", {"public_chat": {"label": " Aninhado "}}) == {
        "delivery": {"channel": "dooers-public-chat", "public_chat_label": "Aninhado"}
    }
    # The flat label wins; an empty one gives way to the nested one.
    assert assistant_delivery_data("dooers-public-chat", {"public_chat_label": "Plano", "public_chat": {"label": "Aninhado"}}) == {
        "delivery": {"channel": "dooers-public-chat", "public_chat_label": "Plano"}
    }
    assert assistant_delivery_data("dooers-public-chat", {"public_chat_label": " ", "public_chat": {"label": "Aninhado"}}) == {
        "delivery": {"channel": "dooers-public-chat", "public_chat_label": "Aninhado"}
    }
    assert assistant_delivery_data("dooers-public-chat", {"public_chat": "x"}) == {"delivery": {"channel": "dooers-public-chat"}}


# -- I4: Thread.metadata --


def test_the_public_chat_thread_metadata_is_what_production_stores():
    visitor = _visitor(guest_profile=dict(GUEST_PROFILE))

    thread = to_thread(_public_chat_view(visitor, _agent_participant()), recipient_instance_id=AGENT_ID, assistant_name=ASSISTANT_NAME)

    assert thread.metadata == {
        "channel": "dooers-public-chat",
        "channel_meta": {"public_chat_label": "Site institucional"},
        "guest_name": "Carlos Lima",
        "guest_email": "carlos@example.com",
        "guest_phone": "+5511977776666",
    }


def test_the_public_chat_thread_metadata_keeps_only_what_the_visitor_declared():
    visitor = _visitor(guest_profile={"name": "Só Nome"})
    authenticated = _visitor(connection_type="public_authenticated")

    declared = to_thread(_public_chat_view(visitor, _agent_participant()))
    bare = to_thread(_public_chat_view(authenticated, _agent_participant()))
    no_meta = to_thread(_public_chat_view(authenticated, _agent_participant(), meta=None))

    assert declared.metadata == {
        "channel": "dooers-public-chat",
        "channel_meta": {"public_chat_label": "Site institucional"},
        "guest_name": "Só Nome",
    }
    assert bare.metadata == {"channel": "dooers-public-chat", "channel_meta": {"public_chat_label": "Site institucional"}}
    assert no_meta.metadata == {"channel": "dooers-public-chat"}


@pytest.mark.parametrize("channel", ["dooers-platform", "whatsapp", None])
def test_other_channels_keep_no_thread_metadata(channel):
    thread = to_thread(_public_chat_view(_visitor(guest_profile=dict(GUEST_PROFILE)), _agent_participant(), channel=channel))

    assert thread.metadata is None


# -- M4: the external channel map --


def test_external_channel_map_is_production_names():
    assert _EXTERNAL_CHANNEL == {"whatsapp": "whatsapp", "public_chat": "dooers-public-chat"}


@pytest.mark.parametrize(
    ("wire", "expected"),
    [("public_chat", "dooers-public-chat"), ("whatsapp", "whatsapp")],
)
async def test_a_contact_on_a_default_channel_thread_gets_its_production_channel(key, bundle, wire, expected):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        participant_id = page["participants"][0]["participant_id"]
        actor = _external_actor(participant_id=participant_id, channel=wire, connection_type="guest")
        page["trigger_event"]["actor"] = page["history"]["items"][0]["actor"] = actor
        page["participants"][0] = _external_participant(participant_id, channel=wire, connection_type="guest")
        page["thread"].update(channel="dooers-platform", channel_meta=None)
        async with dooers_threads_handler_context(memory, private) as prepared:
            assert prepared.on.context.channel == expected


async def test_a_thread_that_names_its_channel_keeps_it_for_a_contact(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _set_public_chat_visitor(page, connection_type="guest")
        async with dooers_threads_handler_context(memory, private) as prepared:
            assert prepared.on.context.channel == "dooers-public-chat"


@pytest.fixture(scope="module")
def key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def bundle():
    from dooers.agents.server.protocol.dooers_threads import default_contracts

    return default_contracts()
