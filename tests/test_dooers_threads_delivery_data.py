"""Data of an inbound channel message and the profile of an external actor, read inside a delivery.

Production reference: ``HandlerPipeline._inbound_source_data`` / ``_with_inbound_source_data`` (``handlers/pipeline.py``)
stamps ``data.source`` on every inbound event; the Dooers Threads ingress writes it on the message's ``payload.data`` (a
WhatsApp message carries per-message values: ``wa_message_id``, ``from_me``...). A public chat visitor's message has no
``payload.data``: its source is derived from the thread's ``channel``/``channel_meta``. ``to_thread_event`` (what
``memory.get_events``/``get_history`` call inside a delivery) must give the same ``data`` that ``_ThreadReads._expose`` gave
outside one.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from dooers.agents.server.dooers_threads.legacy_model import fold_events, to_thread_event
from dooers.agents.server.protocol.dooers_threads import default_contracts
from tests.parity.model import AGENT_ID, ASSISTANT_NAME
from tests.test_dooers_threads_legacy_model import AGENT, DooersThreadsThread, _links
from tests.test_dooers_threads_public_chat import PUBLIC_CHAT_META, _external_actor, _external_participant

WA_NUMBER = "+5511999990000"
WA_SOURCE = {
    "channel": "whatsapp",
    "from_e164": WA_NUMBER,
    "to_e164": "+5511888880000",
    "instance_id": "inst-1",
    "from_me": False,
    "self_chat": False,
    "wa_message_id": "wamid.ABC",
}
WA_META = {"whatsapp": {"instance_id": "inst-1", "from_e164": WA_NUMBER, "to_e164": "+5511888880000", "wa_message_id": "wamid.LAST"}}
GUEST_PROFILE = {"name": "Carlos Lima", "email": "carlos@example.com", "phone": "+5511977776666"}


def _contact_participant() -> dict[str, Any]:
    return _external_participant(str(uuid4()), external_id=WA_NUMBER, channel="whatsapp")


def _contact_actor(participant: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"kind": "external", "external_id": WA_NUMBER, "channel": "whatsapp", "participant_id": participant["participant_id"], **extra}


def _visitor_participant() -> dict[str, Any]:
    return _external_participant(str(uuid4()), connection_type="guest", guest_profile=dict(GUEST_PROFILE))


async def _read(event: dict[str, Any], **conversion: Any):
    return await to_thread_event(
        event,
        recipient_instance_id=AGENT_ID,
        agent_names={},
        multi_agent=False,
        links=_links(),
        assistant_name=ASSISTANT_NAME,
        **conversion,
    )


# -- finding 1: payload.data of the external user's message --


async def test_a_whatsapp_inbound_message_keeps_its_own_data_inside_a_delivery():
    participant = _contact_participant()
    thread = DooersThreadsThread(default_contracts())
    event = thread.message(_contact_actor(participant), [{"type": "text", "text": "Oi"}], data={"source": dict(WA_SOURCE)})

    converted = await _read(event, participants=[participant], channel="whatsapp", channel_meta=WA_META)

    assert converted.data == {"source": WA_SOURCE}


async def test_the_data_does_not_depend_on_the_thread_having_a_channel_view():
    participant = _contact_participant()
    event = DooersThreadsThread(default_contracts()).message(
        _contact_actor(participant), [{"type": "text", "text": "Oi"}], data={"source": dict(WA_SOURCE)}
    )

    assert (await _read(event, participants=[participant])).data == {"source": WA_SOURCE}


async def test_the_callers_other_data_is_kept_next_to_the_source():
    participant = _contact_participant()
    event = DooersThreadsThread(default_contracts()).message(
        _contact_actor(participant), [{"type": "text", "text": "Oi"}], data={"source": dict(WA_SOURCE), "custom": {"a": 1}}
    )

    assert (await _read(event, participants=[participant])).data == {"source": WA_SOURCE, "custom": {"a": 1}}


async def test_a_message_data_is_a_copy():
    participant = _contact_participant()
    event = DooersThreadsThread(default_contracts()).message(
        _contact_actor(participant), [{"type": "text", "text": "Oi"}], data={"source": dict(WA_SOURCE)}
    )

    converted = await _read(event, participants=[participant])
    converted.data["source"]["from_me"] = True

    assert event["payload"]["data"]["source"]["from_me"] is False


async def test_a_public_chat_message_with_a_source_of_its_own_keeps_that_one():
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")
    own = {"source": {"channel": "dooers-public-chat", "public_chat_label": "Rótulo gravado"}}
    event = DooersThreadsThread(default_contracts()).message(actor, [{"type": "text", "text": "Oi"}], data=own)

    converted = await _read(event, participants=[participant], channel="dooers-public-chat", channel_meta=dict(PUBLIC_CHAT_META))

    assert converted.data == own


async def test_a_public_chat_message_without_data_still_derives_its_source():
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")
    event = DooersThreadsThread(default_contracts()).message(actor, [{"type": "text", "text": "Oi"}])

    converted = await _read(event, participants=[participant], channel="dooers-public-chat", channel_meta=dict(PUBLIC_CHAT_META))

    assert converted.data == {"source": {"channel": "dooers-public-chat", "public_chat_label": "Site institucional"}}


async def test_a_public_chat_message_with_other_data_and_no_source_gets_the_derived_source_next_to_it():
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")
    event = DooersThreadsThread(default_contracts()).message(actor, [{"type": "text", "text": "Oi"}], data={"custom": 1})

    converted = await _read(event, participants=[participant], channel="dooers-public-chat", channel_meta=dict(PUBLIC_CHAT_META))

    assert converted.data == {"source": {"channel": "dooers-public-chat", "public_chat_label": "Site institucional"}, "custom": 1}


async def test_a_message_without_data_outside_a_channel_has_none():
    participant = _contact_participant()
    event = DooersThreadsThread(default_contracts()).message(_contact_actor(participant), [{"type": "text", "text": "Oi"}])

    assert (await _read(event, participants=[participant], channel="whatsapp", channel_meta=WA_META)).data is None


# -- finding 2: the profile of an external actor on a form answer and on a queue change --


async def _external_form_response(actor: dict[str, Any], participant: dict[str, Any], **conversion: Any):
    thread = DooersThreadsThread(default_contracts())
    form = thread.append(
        "form.created", AGENT, {"message": "Dados", "elements": [], "submit_label": "Enviar", "cancel_label": "Cancelar", "size": "small"}
    )
    response = thread.append("form.responded", actor, {"form_event_id": form["event_id"], "values": {"nome": "Ana"}, "cancelled": False})
    return await _read(response, participants=[participant], **conversion)


async def test_a_visitors_form_answer_has_its_participants_name_and_email():
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")

    converted = await _external_form_response(actor, participant)

    assert converted.type == "form.response"
    assert converted.author == "Carlos Lima"
    assert (converted.user.user_id, converted.user.user_name, converted.user.user_email) == (
        "guest:abc123",
        "Carlos Lima",
        "carlos@example.com",
    )


async def test_a_form_answer_of_an_actor_unknown_to_the_participants_keeps_its_own_fields():
    actor = _external_actor(participant_id=str(uuid4()), connection_type="guest", display_name="Quem declarou")

    converted = await _external_form_response(actor, _visitor_participant())

    assert converted.author == "Quem declarou"
    assert converted.user.user_email is None


async def test_a_visitors_queue_change_has_its_participants_name_and_email():
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")
    thread = DooersThreadsThread(default_contracts())
    view = {"thread_id": thread.thread_id, "state": "active", "title": "Novo", "version": 2}
    thread.append("thread.updated", AGENT, {**view, "queue": "vendas"})
    change = thread.append("thread.updated", actor, {**view, "version": 3, "queue": "suporte"})
    folded = fold_events(thread.events)

    converted = await _read(next(event for event in folded if event["event_id"] == change["event_id"]), participants=[participant])

    assert converted is not None
    assert converted.author == "Carlos Lima"
    assert (converted.user.user_id, converted.user.user_name, converted.user.user_email) == (
        "guest:abc123",
        "Carlos Lima",
        "carlos@example.com",
    )
    assert converted.data == {"field": "queue", "from": "vendas", "to": "suporte"}


async def test_a_visitors_form_answer_carries_the_inbound_source_like_production():
    """``_with_inbound_source_data`` stamps ``data.source`` on every inbound event, a form answer included."""
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")

    converted = await _external_form_response(actor, participant, channel="dooers-public-chat", channel_meta=dict(PUBLIC_CHAT_META))

    assert converted.data["source"] == {"channel": "dooers-public-chat", "public_chat_label": "Site institucional"}
    assert converted.data["values"] == {"nome": "Ana"}


async def test_a_form_answer_outside_a_public_chat_gets_no_source():
    participant = _visitor_participant()
    actor = _external_actor(participant_id=participant["participant_id"], connection_type="guest")

    converted = await _external_form_response(actor, participant, channel="dooers-platform", channel_meta=None)

    assert "source" not in converted.data
