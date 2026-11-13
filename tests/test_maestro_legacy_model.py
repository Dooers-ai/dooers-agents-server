"""Maestro events as the production thread models (plan 3, task 5).

Production reference (origin/main): ``HandlerPipeline.setup``/``execute`` write each event,
``PostgresPersistence._row_to_event`` reads it back (``tests/parity/memory_persistence.py``
keeps the same rows). The round-trip tests run a handler through the production pipeline
and the same ``AgentEvent`` list through the SDK's Maestro outputs (``outputs.output_for``,
stored as Maestro stores them), then compare the production ``get_events``/``list_runs``
with ``fold_events`` + ``to_thread_event`` and ``to_runs``. Every Maestro event the tests
build is validated against the vendored contract (``Public.Event.*``).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.maestro.callbacks import STORED_EVENT_TYPES
from dooers.agents.server.maestro.legacy_model import fold_events, to_run, to_runs, to_thread, to_thread_event
from dooers.agents.server.maestro.links import LinkResolver
from dooers.agents.server.maestro.outputs import RunTracker, media_output, output_for
from dooers.agents.server.protocol.maestro import MaestroContracts
from dooers.agents.server.protocol.models import (
    Run,
    Thread,
    ThreadEvent,
    User,
    WireS2C_AudioPart,
    WireS2C_ContactPart,
    WireS2C_DocumentPart,
    WireS2C_ImagePart,
    WireS2C_TextPart,
)
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import AGENT_ID, ASSISTANT_NAME, ORG_ID, OTHER_AGENT_ID, USER, WORKSPACE_ID, ParityResult
from tests.parity.normalize import normalize

OTHER_AGENT_NAME = "Cobrança"
#: The person as the agent's delivery context carries it: e-mail and identity IDs are agent-only.
PERSON = {
    "kind": "human",
    "principal_id": USER.user_id,
    "participant_id": str(uuid4()),
    "display_name": USER.user_name,
    "email": USER.user_email,
    "identity_ids": list(USER.identity_ids),
}
AGENT = {
    "kind": "agent",
    "principal_id": AGENT_ID,
    "instance_agent_id": AGENT_ID,
    "participant_id": str(uuid4()),
    "display_name": ASSISTANT_NAME,
}
OTHER = {
    "kind": "agent",
    "principal_id": OTHER_AGENT_ID,
    "instance_agent_id": OTHER_AGENT_ID,
    "participant_id": str(uuid4()),
    "display_name": "Agente de cobrança",
}
CONTACT = {"kind": "external", "external_id": "+5511999990000", "channel": "whatsapp", "display_name": "Cliente"}
#: What production reads back from its events table for the person (``_row_to_event``).
STORED_PERSON = User(user_id=USER.user_id, user_name=USER.user_name, user_email=USER.user_email)
MEDIA_SENDS = frozenset({"image", "audio", "document"})


@pytest.fixture(scope="module")
def contracts() -> MaestroContracts:
    return MaestroContracts()


def _public(event: dict[str, Any]) -> dict[str, Any]:
    """The event without the agent-only actor fields, as ``Public.Event.*`` has it."""
    copy = deepcopy(event)
    copy["actor"].pop("email", None)
    copy["actor"].pop("identity_ids", None)
    return copy


class MaestroThread:
    """Events of one Maestro thread, appended as Maestro stores them (``Public.Event.*``)."""

    def __init__(self, contracts: MaestroContracts) -> None:
        self.contracts = contracts
        self.thread_id = str(uuid4())
        self.events: list[dict[str, Any]] = []
        self.state = {"title": "Nova conversa", "queue": None, "version": 1}
        self._clock = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)

    def append(self, event_type: str, actor: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        event_id = str(uuid4())
        self._clock += timedelta(seconds=1)
        event = {
            "event_id": event_id,
            "thread_id": self.thread_id,
            "sequence": str(len(self.events) + 1),
            "schema_version": 1,
            "actor": deepcopy(actor),
            "created_at": self._clock.isoformat().replace("+00:00", "Z"),
            "causation_event_id": None,
            "root_event_id": event_id,
            "type": event_type,
            "payload": payload,
        }
        self.contracts.validate(f"Public.Event.{event_type}", _public(event))
        self.events.append(event)
        return event

    def message(self, actor: dict[str, Any], content: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return self.append("message.created", actor, {"content": content, "mentions": [], **extra})

    def publish(self, agent_events: list[AgentEvent], trigger: dict[str, Any], actor: dict[str, Any] = AGENT) -> None:
        """The SDK's outputs for ``agent_events`` (sink rules), stored with Maestro's additions."""
        runs = RunTracker()
        for ordinal, event in enumerate(agent_events):
            if event.send_type in MEDIA_SENDS:
                # The URL could not be uploaded: the sink publishes a media_link (index 4.3).
                part = {"type": "media_link", "kind": event.send_type, "url": event.data["url"]} | {
                    key: event.data[key] for key in ("mime_type", "filename", "alt", "duration") if event.data.get(key) is not None
                }
                output = media_output(event, part, runs=runs)
            else:
                output = output_for(event, ordinal, runs=runs)
            if output is not None:
                self.append(STORED_EVENT_TYPES[output.type], actor, self._stored(output.type, deepcopy(output.payload), trigger, actor))

    def _stored(self, kind: str, payload: dict[str, Any], trigger: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        if kind == "run.update":
            # sdk-outputs.ts: agent_id defaults to the executing agent; the trigger opens the run.
            return {"agent_id": actor["instance_agent_id"], **payload, "trigger_event_id": trigger["event_id"]}
        if kind == "thread.update":
            self.state.update({key: payload[key] for key in ("title", "queue") if key in payload})
            self.state["version"] += 1
            run = {"run_id": payload["run_id"]} if "run_id" in payload else {}
            return {"thread_id": self.thread_id, "state": "active", **self.state, **run}
        if kind == "participant.add":
            # addHumanAsAgent keeps only the principal; Maestro names a person "Humano" (participantView).
            user = payload.pop("user")
            participant = {
                "participant_id": str(uuid4()),
                "display_name": "Humano",
                "state": "active",
                "joined_sequence": str(len(self.events) + 1),
                "left_sequence": None,
                "kind": "human",
                "principal_id": user["user_id"],
                "role": "member",
            }
            return {**payload, "participant": participant, "history_policy": "full"}
        return payload


def _links(issued: list[str] | None = None) -> LinkResolver:
    async def issue(artifact_id: str) -> dict[str, Any]:
        if issued is not None:
            issued.append(artifact_id)
        return {"url": f"https://maestro.example/v1/artifact-links/{artifact_id}.sig", "expires_at": "2099-01-01T00:00:00Z"}

    return LinkResolver(issue)


async def _convert(events: list[dict[str, Any]], *, multi_agent: bool = False, links: LinkResolver | None = None) -> list[ThreadEvent]:
    links = links or _links()
    converted = [
        await to_thread_event(
            event,
            recipient_instance_id=AGENT_ID,
            agent_names={OTHER_AGENT_ID: OTHER_AGENT_NAME},
            multi_agent=multi_agent,
            links=links,
            assistant_name=ASSISTANT_NAME,
        )
        for event in fold_events(events)
    ]
    return [event for event in converted if event is not None]


async def _one(event: dict[str, Any], **options: Any) -> ThreadEvent | None:
    (folded,) = fold_events([event])
    return await to_thread_event(
        folded,
        recipient_instance_id=AGENT_ID,
        agent_names=options.pop("agent_names", {OTHER_AGENT_ID: OTHER_AGENT_NAME}),
        multi_agent=options.pop("multi_agent", False),
        links=options.pop("links", None) or _links(),
        assistant_name=ASSISTANT_NAME,
    )


def _at(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))


# -- message.created --


async def test_message_created_from_human_maps_to_user_message(contracts):
    thread = MaestroThread(contracts)
    event = thread.message(PERSON, [{"type": "text", "text": "Oi"}], client_event_id="client-1")

    converted = await _one(event)

    # As production reads it back (_row_to_event): user id, name and e-mail; no client_event_id column.
    assert converted == ThreadEvent(
        id=event["event_id"],
        thread_id=thread.thread_id,
        type="message",
        actor="user",
        author=USER.user_name,
        user=STORED_PERSON,
        content=[WireS2C_TextPart(text="Oi")],
        created_at=_at(event),
    )


async def test_channel_contact_message_is_a_user_message(contracts):
    thread = MaestroThread(contracts)
    contact = thread.message(CONTACT, [{"type": "text", "text": "Olá"}])

    converted = await _one(contact)

    # Production: the WhatsApp contact is the User (its number) and the author is its name.
    assert (converted.actor, converted.author) == ("user", "Cliente")
    assert converted.user == User(user_id="+5511999990000", user_name="Cliente")


@pytest.mark.parametrize("multi_agent", [False, True])
async def test_own_agent_message_maps_to_assistant_without_prefix(contracts, multi_agent):
    thread = MaestroThread(contracts)
    plain = thread.message(AGENT, [{"type": "text", "text": "Resposta"}])
    authored = thread.message(AGENT, [{"type": "text", "text": "Assinada"}], author="Bot X")

    first, second = await _convert([plain, authored], multi_agent=multi_agent)

    assert (first.actor, first.author, first.user, first.content) == (
        "assistant",
        ASSISTANT_NAME,
        User(user_id=""),
        [WireS2C_TextPart(text="Resposta")],
    )
    assert (second.actor, second.author, second.content) == ("assistant", "Bot X", [WireS2C_TextPart(text="Assinada")])


async def test_other_agent_message_gets_prefix_only_in_multi_agent(contracts):
    thread = MaestroThread(contracts)
    text = thread.message(OTHER, [{"type": "text", "text": "Fatura enviada."}], author="Bot Y")
    image_only = thread.message(OTHER, [{"type": "media_link", "kind": "image", "url": "https://cdn.example/f.png"}])

    single = await _convert([text, image_only], multi_agent=False)
    multi = await _convert([text, image_only], multi_agent=True)

    # One agent left in the thread: production has only assistant messages, without prefix.
    assert [(e.actor, e.author) for e in single] == [("assistant", "Bot Y"), ("assistant", OTHER["display_name"])]
    assert single[0].content == [WireS2C_TextPart(text="Fatura enviada.")]
    # Several agents: the other agent speaks as a user named after it, with the prefix (design 7.5).
    assert [(e.actor, e.author, e.user) for e in multi] == [("user", OTHER_AGENT_NAME, User(user_id=""))] * 2
    assert multi[0].content == [WireS2C_TextPart(text=f"[agente: {OTHER_AGENT_NAME}] Fatura enviada.")]
    assert multi[1].content == [
        WireS2C_TextPart(text=f"[agente: {OTHER_AGENT_NAME}]"),
        WireS2C_ImagePart(url="https://cdn.example/f.png"),
    ]


async def test_other_agent_without_a_known_name_uses_its_display_name(contracts):
    thread = MaestroThread(contracts)
    event = thread.message(OTHER, [{"type": "text", "text": "Oi"}])

    converted = await _one(event, multi_agent=True, agent_names={})

    assert converted.author == OTHER["display_name"]
    assert converted.content == [WireS2C_TextPart(text=f"[agente: {OTHER['display_name']}] Oi")]


#: The executing agent as Maestro names it: not ``AgentConfig.assistant_name``.
RENAMED_AGENT = {**AGENT, "display_name": "Nome no Maestro"}


async def test_own_agent_events_use_the_assistant_name_not_the_maestro_name(contracts):
    """Production writes ``AgentConfig.assistant_name`` (``pipeline.py``), whatever Maestro calls the agent."""
    thread = MaestroThread(contracts)
    thread.append("thread.created", PERSON, {"thread": _thread_view(thread.thread_id)})
    plain = thread.message(RENAMED_AGENT, [{"type": "text", "text": "Oi"}])
    thread.message(RENAMED_AGENT, [{"type": "text", "text": "Assinada"}], author="Bot X")
    thread.append("reasoning.created", RENAMED_AGENT, {"text": "pensando"})
    thread.append(
        "chart.created",
        RENAMED_AGENT,
        {"title": "T", "message": "", "chart_type": "bar", "size": "medium", "x_key": "x", "y_keys": ["y"], "data": [], "series": []},
    )
    form = {"message": "Dados", "elements": [], "submit_label": "Enviar", "cancel_label": "Cancelar", "size": "small"}
    thread.append("form.created", RENAMED_AGENT, form)
    view = {"thread_id": thread.thread_id, "state": "active", "title": "Nova conversa"}
    thread.append("thread.updated", RENAMED_AGENT, {**view, "version": 2, "queue": "suporte"})
    # Another agent keeps its own name (single-agent view: an assistant message).
    thread.message(OTHER, [{"type": "text", "text": "Fatura enviada."}])

    converted = await _convert(thread.events)

    assert [(event.type, event.author) for event in converted] == [
        ("message", ASSISTANT_NAME),
        ("message", "Bot X"),
        ("reasoning", ASSISTANT_NAME),
        ("chart", ASSISTANT_NAME),
        ("form", ASSISTANT_NAME),
        ("thread.update", ASSISTANT_NAME),
        ("message", OTHER["display_name"]),
    ]
    assert converted[5].user == User(user_id=AGENT_ID, user_name=ASSISTANT_NAME)
    # Without a name, the production default (AgentConfig.assistant_name = "Assistant").
    default = await to_thread_event(plain, recipient_instance_id=AGENT_ID, agent_names={}, multi_agent=False, links=_links())
    assert default.author == "Assistant"


async def test_attachments_get_the_temporary_link_and_keep_their_metadata(contracts):
    thread = MaestroThread(contracts)
    image, audio, document = str(uuid4()), str(uuid4()), str(uuid4())
    event = thread.message(
        PERSON,
        [
            {
                "type": "attachment",
                "artifact_id": image,
                "mime_type": "image/png",
                "filename": "a.png",
                "alt": "logo",
                "width": 4,
                "height": 3,
            },
            {"type": "attachment", "artifact_id": audio, "mime_type": "audio/wav", "filename": "a.wav", "size_bytes": 10, "duration": 2.5},
            {"type": "attachment", "artifact_id": document, "mime_type": "application/pdf", "filename": "a.pdf", "size_bytes": 12},
            {"type": "attachment", "artifact_id": image, "mime_type": "image/png", "filename": "a.png"},
        ],
    )
    issued: list[str] = []

    converted = await _one(event, links=_links(issued))

    url = "https://maestro.example/v1/artifact-links/{}.sig".format
    assert converted.content == [
        WireS2C_ImagePart(url=url(image), mime_type="image/png", filename="a.png", alt="logo", width=4, height=3, ref_id=image),
        WireS2C_AudioPart(url=url(audio), mime_type="audio/wav", filename="a.wav", duration=2.5, ref_id=audio),
        WireS2C_DocumentPart(url=url(document), mime_type="application/pdf", filename="a.pdf", size_bytes=12, ref_id=document),
        WireS2C_ImagePart(url=url(image), mime_type="image/png", filename="a.png", ref_id=image),
    ]
    # One link per artifact for the whole conversion (LinkResolver cache).
    assert issued == [image, audio, document]


async def test_agent_attachments_are_the_production_send_media_parts(contracts):
    """An agent's file is the URL of its ``send.image``/``audio``/``document``, which the SDK uploaded.

    Production (``HandlerPipeline.execute``) stores that URL with the send's metadata only: no
    ``ref_id``, and no file name for an image or an audio (the history text says ``[image: image]``).
    """
    thread = MaestroThread(contracts)
    image, audio, document = str(uuid4()), str(uuid4()), str(uuid4())
    event = thread.message(
        AGENT,
        [
            # The sink uploads with the file name of the URL and adds the send's alt/duration.
            {"type": "attachment", "artifact_id": image, "mime_type": "image/png", "filename": "cat.png", "size_bytes": 9, "alt": "Gato"},
            {"type": "attachment", "artifact_id": audio, "mime_type": "audio/ogg", "filename": "a.ogg", "size_bytes": 7, "duration": 3.5},
            {"type": "attachment", "artifact_id": document, "mime_type": "application/pdf", "filename": "a.pdf", "size_bytes": 12},
        ],
    )

    converted = await _one(event, links=_links())

    url = "https://maestro.example/v1/artifact-links/{}.sig".format
    assert converted.content == [
        WireS2C_ImagePart(url=url(image), mime_type="image/png", alt="Gato"),
        WireS2C_AudioPart(url=url(audio), mime_type="audio/ogg", duration=3.5),
        WireS2C_DocumentPart(url=url(document), mime_type="application/pdf", filename="a.pdf"),
    ]


async def test_media_link_and_contact_parts_are_the_production_url_parts(contracts):
    thread = MaestroThread(contracts)
    event = thread.message(
        AGENT,
        [
            {"type": "media_link", "kind": "image", "url": "https://cdn.example/i.png", "mime_type": "image/png", "alt": "i"},
            {"type": "media_link", "kind": "audio", "url": "https://cdn.example/a.ogg", "mime_type": "audio/ogg", "duration": 3},
            {
                "type": "media_link",
                "kind": "document",
                "url": "https://cdn.example/d.pdf",
                "mime_type": "application/pdf",
                "filename": "d.pdf",
            },
            {"type": "contact", "display_name": "Ana", "vcard": "BEGIN:VCARD\nEND:VCARD", "phones": [{"phone": "+55", "type": "CELL"}]},
            {"type": "contact", "display_name": "Sem fone"},
        ],
    )

    converted = await _one(event)

    assert converted.content == [
        WireS2C_ImagePart(url="https://cdn.example/i.png", mime_type="image/png", alt="i"),
        WireS2C_AudioPart(url="https://cdn.example/a.ogg", mime_type="audio/ogg", duration=3),
        # No size: production send.document stores none either (WireS2C_DocumentPart.size_bytes is None).
        WireS2C_DocumentPart(url="https://cdn.example/d.pdf", mime_type="application/pdf", filename="d.pdf"),
        WireS2C_ContactPart(display_name="Ana", vcard="BEGIN:VCARD\nEND:VCARD", phones=[{"phone": "+55", "type": "CELL"}]),
        WireS2C_ContactPart(display_name="Sem fone"),
    ]


async def test_template_is_stored_in_the_production_shape(contracts):
    thread = MaestroThread(contracts)
    components = [{"type": "body", "parameters": [{"type": "text", "text": "Ana"}]}]
    event = thread.message(
        AGENT,
        [{"type": "text", "text": "[template: boas_vindas (pt_BR)]"}],
        template={"template_name": "boas_vindas", "template_language": "pt_BR", "template_components": components},
    )

    converted = await _one(event)

    assert converted.data == {"template": {"name": "boas_vindas", "language": "pt_BR", "components": components}}


# -- fold_events --


def test_fold_applies_update_and_drops_removed(contracts):
    thread = MaestroThread(contracts)
    first = thread.message(PERSON, [{"type": "text", "text": "Original"}])
    second = thread.message(PERSON, [{"type": "text", "text": "Apagada"}])
    update = thread.append("message.updated", AGENT, {"event_id": first["event_id"], "content": [{"type": "text", "text": "v1"}]})
    again = thread.append("message.updated", AGENT, {"event_id": first["event_id"], "content": [{"type": "text", "text": "v2"}]})
    removed = thread.append("message.removed", AGENT, {"event_id": second["event_id"]})
    late = thread.append("message.updated", AGENT, {"event_id": second["event_id"], "content": [{"type": "text", "text": "x"}]})
    third = thread.message(AGENT, [{"type": "text", "text": "Depois"}])
    before = deepcopy(thread.events)

    folded = fold_events(thread.events)

    assert [event["event_id"] for event in folded] == [first["event_id"], third["event_id"]]
    assert folded[0]["payload"]["content"] == [{"type": "text", "text": "v2"}]
    assert {key: folded[0][key] for key in folded[0] if key != "payload"} == {key: first[key] for key in first if key != "payload"}
    assert folded[1] == third
    assert all(event["event_id"] not in {e["event_id"] for e in folded} for event in (update, again, removed, late))
    assert thread.events == before  # the input list and its events are not changed


def test_fold_drops_control_events_without_a_target(contracts):
    thread = MaestroThread(contracts)
    orphan = thread.append("message.updated", AGENT, {"event_id": str(uuid4()), "content": [{"type": "text", "text": "x"}]})
    gone = thread.append("message.removed", AGENT, {"event_id": str(uuid4())})

    assert fold_events([orphan, gone]) == []


# -- other event types --


def _payloads(thread: MaestroThread) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """One Maestro event of each type of index 4.2 with a production equivalent: (actor, payload)."""
    run = str(uuid4())
    return {
        "reasoning.created": (AGENT, {"text": "pensando", "run_id": run}),
        "tool_call.created": (AGENT, {"id": "call-1", "name": "buscar", "args": {"q": "x"}, "run_id": run}),
        "tool_result.created": (AGENT, {"id": "call-1", "name": "buscar", "result": {"ok": True}, "run_id": run}),
        "tool_transaction.created": (AGENT, {"id": "tx-1", "name": "pagar", "args": {"v": 1}, "result": {"ok": True}}),
        "chart.created": (
            AGENT,
            {"title": "T", "message": "", "chart_type": "bar", "size": "medium", "x_key": "x", "y_keys": ["y"], "data": [], "series": []},
        ),
        "form.created": (
            AGENT,
            {"message": "Dados", "elements": [], "submit_label": "Enviar", "cancel_label": "Cancelar", "size": "small"},
        ),
        "form.responded": (PERSON, {"form_event_id": str(uuid4()), "values": {"nome": "Ana"}, "cancelled": False}),
        "thread.updated": (
            AGENT,
            {"thread_id": thread.thread_id, "title": "Nova conversa", "state": "active", "version": 2, "queue": "suporte"},
        ),
        "message.created": (PERSON, {"content": [{"type": "text", "text": "Oi"}], "mentions": []}),
    }


PRODUCTION_TYPES = {
    "reasoning.created": ("reasoning", "assistant"),
    "tool_call.created": ("tool.call", "assistant"),
    "tool_result.created": ("tool.result", "tool"),
    "tool_transaction.created": ("tool.transaction", "assistant"),
    "chart.created": ("chart", "assistant"),
    "form.created": ("form", "assistant"),
    "form.responded": ("form.response", "user"),
    "thread.updated": ("thread.update", "system"),
    "message.created": ("message", "user"),
}


@pytest.mark.parametrize("maestro_type", sorted(PRODUCTION_TYPES))
async def test_each_new_event_type_maps_to_production_type(contracts, maestro_type):
    thread = MaestroThread(contracts)
    actor, payload = _payloads(thread)[maestro_type]
    event = thread.append(maestro_type, actor, payload)

    converted = await _one(event)

    assert (converted.type, converted.actor) == PRODUCTION_TYPES[maestro_type]
    assert (converted.id, converted.thread_id, converted.created_at) == (event["event_id"], thread.thread_id, _at(event))
    assert converted.run_id == payload.get("run_id")


def _without_equivalent(thread: MaestroThread) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    target = str(uuid4())
    return {
        "run.updated": (
            AGENT,
            {"run_id": str(uuid4()), "agent_id": AGENT_ID, "status": "running", "trigger_event_id": target},
        ),
        "participant.added": (
            AGENT,
            {
                "history_policy": "full",
                "participant": {
                    "participant_id": str(uuid4()),
                    "display_name": "Bia",
                    "state": "active",
                    "joined_sequence": "1",
                    "left_sequence": None,
                    "kind": "human",
                    "principal_id": "user-2",
                    "role": "member",
                },
            },
        ),
        "channel_delivery.updated": (AGENT, {"event_id": target, "channel": "whatsapp", "status": "delivered"}),
        "thread.archived": (PERSON, {"thread_id": thread.thread_id, "version": 3}),
        "execution.started": (
            AGENT,
            {
                "completed_at": None,
                "deadline_at": "2026-11-12T10:05:00Z",
                "delivery_id": str(uuid4()),
                "execution_id": str(uuid4()),
                "outcome": None,
                "participant_id": AGENT["participant_id"],
                "phase_id": "initial",
                "reason_code": None,
                "state": "running",
            },
        ),
        # A rename: production writes no event for a title change.
        "thread.updated": (
            AGENT,
            {"thread_id": thread.thread_id, "title": "Outro título", "state": "active", "version": 2, "queue": None},
        ),
    }


@pytest.mark.parametrize(
    "maestro_type",
    sorted(["run.updated", "participant.added", "channel_delivery.updated", "thread.archived", "execution.started", "thread.updated"]),
)
async def test_events_without_production_equivalent_map_to_none(contracts, maestro_type):
    thread = MaestroThread(contracts)
    actor, payload = _without_equivalent(thread)[maestro_type]
    event = thread.append(maestro_type, actor, payload)

    assert await _one(event) is None


async def test_unfolded_control_events_map_to_none(contracts):
    thread = MaestroThread(contracts)
    target = thread.message(PERSON, [{"type": "text", "text": "Oi"}])
    update = thread.append("message.updated", AGENT, {"event_id": target["event_id"], "content": [{"type": "text", "text": "x"}]})
    removed = thread.append("message.removed", AGENT, {"event_id": target["event_id"]})

    for event in (update, removed):
        assert await to_thread_event(event, recipient_instance_id=AGENT_ID, agent_names={}, multi_agent=False, links=_links()) is None


async def test_form_response_keeps_the_production_data(contracts):
    thread = MaestroThread(contracts)
    form = thread.append(
        "form.created",
        AGENT,
        {"message": "Dados", "elements": [], "submit_label": "Enviar", "cancel_label": "Cancelar", "size": "small"},
    )
    response = thread.append("form.responded", PERSON, {"form_event_id": form["event_id"], "values": {}, "cancelled": True})

    _, converted = await _convert([form, response])

    assert converted == ThreadEvent(
        id=response["event_id"],
        thread_id=thread.thread_id,
        type="form.response",
        actor="user",
        author=USER.user_name,
        user=STORED_PERSON,
        content=None,
        data={"form_event_id": form["event_id"], "values": {}, "cancelled": True},
        created_at=_at(response),
    )


async def test_queue_change_is_a_thread_update_from_the_previous_queue(contracts):
    thread = MaestroThread(contracts)
    view = {"thread_id": thread.thread_id, "state": "active"}
    created = thread.append("thread.created", PERSON, {"thread": _thread_view(thread.thread_id, queue="vendas")})
    rename = thread.append("thread.updated", AGENT, {**view, "title": "Novo", "version": 2, "queue": "vendas"})
    by_agent = thread.append("thread.updated", AGENT, {**view, "title": "Novo", "version": 3, "queue": "suporte"})
    by_person = thread.append("thread.updated", PERSON, {**view, "title": "Novo", "version": 4, "queue": None})

    converted = await _convert([created, rename, by_agent, by_person])

    assert converted == [
        ThreadEvent(
            id=by_agent["event_id"],
            thread_id=thread.thread_id,
            type="thread.update",
            actor="system",
            author=ASSISTANT_NAME,
            user=User(user_id=AGENT_ID, user_name=ASSISTANT_NAME),
            data={"field": "queue", "from": "vendas", "to": "suporte"},
            created_at=_at(by_agent),
        ),
        # A person's queue change (router.py, thread.upsert): the person is the author and the user.
        ThreadEvent(
            id=by_person["event_id"],
            thread_id=thread.thread_id,
            type="thread.update",
            actor="system",
            author=USER.user_name,
            user=STORED_PERSON,
            data={"field": "queue", "from": "suporte", "to": None},
            created_at=_at(by_person),
        ),
    ]


async def test_form_elements_are_normalized_like_production(contracts):
    thread = MaestroThread(contracts)
    elements = [
        AgentSend.form_text("nome", label="Nome", required=True),
        {"type": "select_input", "name": "plano", "label": "Plano", "options": [{"value": "pro", "label": "Pro"}]},
    ]
    event = thread.append(
        "form.created",
        AGENT,
        {"message": "Dados", "elements": elements, "submit_label": "Enviar", "cancel_label": "Cancelar", "size": "small"},
    )
    unknown = deepcopy(event)
    unknown["payload"]["elements"].append({"type": "rating_input", "name": "nota", "stars": 5})

    converted = await _one(event)
    with_unknown = await to_thread_event(unknown, recipient_instance_id=AGENT_ID, agent_names={}, multi_agent=False, links=_links())

    # Set keys only (pydantic exclude_unset): the builder sets every key, the hand-written element only some.
    assert converted.data["elements"] == [
        elements[0],
        {"type": "select_input", "name": "plano", "label": "Plano", "options": [{"value": "pro", "label": "Pro"}]},
    ]
    assert with_unknown.data["elements"][-1] == {"type": "rating_input", "name": "nota", "stars": 5}


# -- run_id linkage and runs --


async def test_trigger_event_gets_the_run_id_of_its_run(contracts):
    """Scenario ``text``: the person's event and the reply have the same ``run_id`` (index 4.3.1)."""
    thread = MaestroThread(contracts)
    trigger = thread.message(PERSON, [{"type": "text", "text": "Oi"}])
    send = AgentSend()
    thread.publish([send.run_start(), send.text("Olá!"), send.run_end()], trigger)

    person, reply = await _convert(thread.events)

    run_id = thread.events[1]["payload"]["run_id"]
    assert (person.id, person.run_id) == (trigger["event_id"], run_id)
    assert (reply.actor, reply.run_id) == ("assistant", run_id)
    assert "run_id" not in trigger["payload"]  # the input event is not changed


async def test_trigger_keeps_the_last_run_and_agent_events_keep_their_own(contracts):
    thread = MaestroThread(contracts)
    trigger = thread.message(PERSON, [{"type": "text", "text": "Oi"}])
    send = AgentSend()
    thread.publish([send.run_start(), send.text("1"), send.run_end(), send.run_start(), send.text("2"), send.run_end()], trigger)
    # Another agent's reply, sent inside its own run, triggers this agent: its run_id stays.
    answer = thread.message(OTHER, [{"type": "text", "text": "Outra"}], run_id=str(uuid4()))
    thread.publish([send.run_start(), send.text("3"), send.run_end()], answer)

    person, one, two, other, three = await _convert(thread.events)

    first_run, second_run = thread.events[1]["payload"]["run_id"], thread.events[4]["payload"]["run_id"]
    assert (person.run_id, one.run_id, two.run_id) == (second_run, first_run, second_run)
    assert other.run_id == answer["payload"]["run_id"]
    assert three.run_id not in (first_run, second_run, other.run_id)


def test_to_run_maps_one_run_update(contracts):
    thread = MaestroThread(contracts)
    run_id = str(uuid4())
    running = thread.append(
        "run.updated", AGENT, {"run_id": run_id, "agent_id": OTHER_AGENT_ID, "status": "running", "trigger_event_id": str(uuid4())}
    )
    failed = thread.append(
        "run.updated",
        AGENT,
        {"run_id": run_id, "agent_id": AGENT_ID, "status": "failed", "error": "falhou", "trigger_event_id": str(uuid4())},
    )

    assert to_run(running) == Run(id=run_id, thread_id=thread.thread_id, agent_id=OTHER_AGENT_ID, status="running", started_at=_at(running))
    # The executing agent's own id is production's send.run_start() without agent_id: None.
    assert to_run(failed) == Run(
        id=run_id, thread_id=thread.thread_id, agent_id=None, status="failed", started_at=_at(failed), ended_at=_at(failed), error="falhou"
    )


def test_to_runs_keeps_agent_and_start_of_the_first_update(contracts):
    thread = MaestroThread(contracts)
    trigger = thread.message(PERSON, [{"type": "text", "text": "Oi"}])
    send = AgentSend()
    thread.publish(
        [
            send.run_start(agent_id=OTHER_AGENT_ID),
            send.text("a"),
            send.run_end(status="failed", error="caiu"),
            send.run_start(),
            send.text("b"),
            send.run_end(),
            send.run_start(),
        ],
        trigger,
    )
    updates = [event for event in thread.events if event["type"] == "run.updated"]

    first, second, open_run = to_runs(thread.events)

    assert first == Run(
        id=updates[0]["payload"]["run_id"],
        thread_id=thread.thread_id,
        agent_id=OTHER_AGENT_ID,
        status="failed",
        started_at=_at(updates[0]),
        ended_at=_at(updates[1]),
        error="caiu",
    )
    assert (second.agent_id, second.status, second.started_at, second.ended_at) == (None, "succeeded", _at(updates[2]), _at(updates[3]))
    assert (open_run.status, open_run.ended_at) == ("running", None)


# -- to_thread --


def _thread_view(thread_id: str, *, queue: str | None = None, **changes: Any) -> dict[str, Any]:
    participant = {"joined_sequence": "1", "left_sequence": None, "state": "active"}
    return {
        "thread_id": thread_id,
        "organization_id": ORG_ID,
        "scope_kind": "workspace",
        "workspace_id": WORKSPACE_ID,
        "mode": "maestro",
        "state": "active",
        "title": "Nova conversa",
        "queue": queue,
        "version": 1,
        "policy_version": 1,
        "supervision_policy": "strict",
        "capabilities": [],
        "cursor": f"thread.{thread_id}.1",
        "last_sequence": "1",
        "created_at": "2026-11-12T10:00:00Z",
        "updated_at": "2026-11-12T10:05:00Z",
        "permissions": dict.fromkeys(
            ("archive", "cancel", "export", "feedback", "manage", "read", "remove_participants", "supervise", "write"), False
        ),
        "participants": [
            {**participant, **{k: PERSON[k] for k in ("kind", "principal_id", "participant_id", "display_name")}, "role": "owner"},
            {
                **participant,
                **{k: AGENT[k] for k in ("kind", "principal_id", "instance_agent_id", "participant_id", "display_name")},
                "role": "member",
                "availability": "available",
                "capabilities": [],
            },
            {
                **participant,
                "kind": "human",
                "principal_id": "user-2",
                "participant_id": str(uuid4()),
                "display_name": "Bia",
                "role": "member",
                "state": "removed",
            },
        ],
        **changes,
    }


def test_to_thread_maps_the_view_to_the_production_thread(contracts):
    thread_id = str(uuid4())
    view = _thread_view(thread_id, queue="suporte")
    contracts.validate("Public.Thread", view)
    # The agent's context adds the person's e-mail and identity IDs to the participant.
    view["participants"][0].update(email=USER.user_email, identity_ids=list(USER.identity_ids))
    person = User(user_id=USER.user_id, user_name=USER.user_name, user_email=USER.user_email, identity_ids=list(USER.identity_ids))
    updated = datetime(2026, 11, 12, 10, 5, tzinfo=UTC)

    assert to_thread(view) == Thread(
        id=thread_id,
        agent_id=AGENT_ID,
        organization_id=ORG_ID,
        workspace_id=WORKSPACE_ID,
        owner=person,
        users=[person, User(user_id=AGENT_ID, user_name=ASSISTANT_NAME)],
        title="Nova conversa",
        queue="suporte",
        created_at=datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
        updated_at=updated,
        last_event_at=updated,
    )
    last = datetime(2026, 11, 12, 10, 9, tzinfo=UTC)
    assert to_thread(view, last_event_at=last).last_event_at == last


@pytest.mark.parametrize("order", [(0, 1, 2, 3), (3, 2, 1, 0), (1, 3, 0, 2), (2, 0, 3, 1)])
def test_to_thread_orders_users_as_production_whatever_order_maestro_returns(contracts, order):
    """Maestro lists the context participants by a random UUID; production lists [owner, agent, added...]."""
    view = _thread_view(str(uuid4()))
    owner, agent, _removed = view["participants"]

    def added(user_id: str, sequence: str) -> dict[str, Any]:
        return {
            **owner,
            "principal_id": user_id,
            "participant_id": str(uuid4()),
            "display_name": "Humano",
            "role": "member",
            "joined_sequence": sequence,
        }

    # A sequence is a decimal string: "12" comes after "7" (a text sort would not).
    arranged = [owner, agent, added("user-3", "7"), added("user-4", "12")]
    view["participants"] = [arranged[index] for index in order]
    contracts.validate("Public.Thread", view)

    thread = to_thread(view, recipient_instance_id=AGENT_ID, assistant_name=ASSISTANT_NAME)

    assert [user.user_id for user in thread.users] == [PERSON["principal_id"], AGENT_ID, "user-3", "user-4"]
    assert thread.owner.user_id == PERSON["principal_id"]


def test_to_thread_lists_the_reading_agent_with_the_assistant_name(contracts):
    view = _thread_view(str(uuid4()))
    view["participants"][1]["display_name"] = "Nome no Maestro"
    contracts.validate("Public.Thread", view)

    # Production lists its agent as User(agent_id, AgentConfig.assistant_name) (``thread_access.agent_participant``).
    assert to_thread(view, recipient_instance_id=AGENT_ID, assistant_name=ASSISTANT_NAME).users[1] == User(
        user_id=AGENT_ID, user_name=ASSISTANT_NAME
    )
    # Without the reading agent, every agent keeps its Maestro name.
    assert to_thread(view).users[1] == User(user_id=AGENT_ID, user_name="Nome no Maestro")


def test_to_thread_defaults_of_personal_untitled_and_channel_threads(contracts):
    thread_id = str(uuid4())
    personal = _thread_view(thread_id, scope_kind="personal", workspace_id=None, title="")
    contracts.validate("Public.Thread", personal)
    channel = _thread_view(thread_id, owner_instance_agent_id=OTHER_AGENT_ID)
    contracts.validate("Public.Thread", channel)
    channel["participants"][0] = {
        "kind": "external",
        "external_id": "+5511999990000",
        "channel": "whatsapp",
        "display_name": "Cliente",
        "participant_id": str(uuid4()),
        "joined_sequence": "1",
        "left_sequence": None,
        "role": "member",
        "state": "active",
    }

    first = to_thread(personal)
    second = to_thread(channel)

    assert (first.workspace_id, first.title, first.queue) == ("", None, None)
    # The agent that created the thread owns it; a channel thread's owner is the contact (production: the WhatsApp user).
    assert (second.agent_id, second.owner) == (OTHER_AGENT_ID, User(user_id="+5511999990000", user_name="Cliente"))


# -- Round trip: production pipeline vs Maestro outputs --


Build = Callable[[AgentSend, str], list[AgentEvent]]


async def _production(build: Build) -> tuple[list[ThreadEvent], list[Run]]:
    persistence = MemoryPersistence()
    pipeline = HandlerPipeline(persistence=persistence, assistant_name=ASSISTANT_NAME)

    async def handler(on, send, memory, analytics, settings):
        for event in build(send, on.context.event_id):
            yield event

    context = HandlerContext(
        handler=handler,
        agent_id=AGENT_ID,
        organization_id=ORG_ID,
        workspace_id=WORKSPACE_ID,
        message="Oi",
        user=USER,
        allowed_queues=["suporte", "vendas"],
    )
    result = await pipeline.setup(context)
    async for _ in pipeline.execute(context, result):
        pass
    runs = await persistence.list_runs(thread_id=result.thread.id, limit=100)
    return await persistence.get_events(result.thread.id, limit=1000), list(reversed(runs))


async def _maestro(contracts: MaestroContracts, build: Build, actor: dict[str, Any] = AGENT) -> tuple[list[ThreadEvent], list[Run]]:
    thread = MaestroThread(contracts)
    trigger = thread.message(PERSON, [{"type": "text", "text": "Oi"}])
    thread.publish(build(AgentSend(), trigger["event_id"]), trigger, actor=actor)
    return await _convert(thread.events), to_runs(thread.events)


def _comparable(events: list[ThreadEvent], runs: list[Run]) -> ParityResult:
    return normalize(
        ParityResult(
            events=[event.model_dump() for event in events], incoming={}, histories={}, outputs={"runs": [r.model_dump() for r in runs]}
        )
    )


def _every_send(send: AgentSend, trigger_id: str) -> list[AgentEvent]:
    options = [{"value": "basico", "label": "Básico"}, {"value": "pro", "label": "Pro"}]
    return [
        send.run_start(),
        send.text("Olá", author="Bot X"),
        send.text("Sem autor"),
        send.image("https://cdn.example/a.png", mime_type="image/png", alt="logo"),
        send.audio("https://cdn.example/a.ogg", mime_type="audio/ogg", duration=3.5),
        send.document("https://cdn.example/a.pdf", filename="a.pdf", mime_type="application/pdf"),
        send.contact(display_name="Ana", vcard="BEGIN:VCARD\nEND:VCARD", phones=[{"phone": "+5511", "type": "CELL"}]),
        send.template("boas_vindas", "pt_BR", template_components=[{"type": "body", "parameters": []}]),
        send.reasoning("pensando", author="Raciocínio"),
        send.reasoning("de novo"),
        send.tool_call("buscar", {"q": "x"}, display_name="Buscar", id="call-1"),
        send.tool_result("buscar", {"ok": True}, id="call-1"),
        send.tool_transaction("pagar", {"v": 1}, {"ok": True}, display_name="Pagar", id="tx-1"),
        send.form(
            "Complete seu cadastro",
            [
                send.form_text("nome", label="Nome", required=True, placeholder="Seu nome", input_type="email"),
                send.form_textarea("obs", label="Obs", rows=4, default="Nada"),
                send.form_select("plano", label="Plano", options=options, default="pro", placeholder="Escolha"),
                send.form_radio("contato", label="Contato", options=options, variant="button"),
                send.form_checkbox("extras", label="Extras", options=options, default=["pro"]),
                send.form_file("anexo", label="Anexo", upload_url="/uploads", accept="application/pdf", multiple=True),
            ],
            submit_label="Enviar",
            cancel_label="Cancelar",
            size="small",
        ),
        send.chart(
            chart_type="bar",
            data=[{"mes": "jan", "v": 1}],
            x_key="mes",
            y_keys=["v"],
            title="Vendas",
            series=[send.chart_series("v", label="Valor"), {"key": "w"}],
        ),
        send.chart(chart_type="pie", data=[], x_key="x", y_keys=["y"], x_label="X", y_label="Y", size="large"),
        send.update_user_event(trigger_id, [{"type": "text", "text": "Oi (editado)"}]),
        send.update_thread(title="Pedido 42", queue="suporte"),
        send.update_thread(title="Pedido 43"),
        send.add_participant("user-2", user_name="Bia Lima"),
        send.run_end(),
    ]


def _runs_only(send: AgentSend, trigger_id: str) -> list[AgentEvent]:
    return [
        send.run_start(),
        send.text("Não consegui."),
        send.run_end(status="failed", error="Serviço indisponível"),
        send.run_start(agent_id=OTHER_AGENT_ID),
        send.text("Aqui é a cobrança."),
        send.run_end(),
    ]


@pytest.mark.parametrize("actor", [AGENT, RENAMED_AGENT], ids=["same_name", "maestro_name"])
@pytest.mark.parametrize("build", [_every_send, _runs_only], ids=["every_send", "runs"])
async def test_send_types_round_trip_to_the_production_thread_events(contracts, build, actor):
    # ``maestro_name``: Maestro calls the agent something else; production writes ASSISTANT_NAME.
    production = await _production(build)
    maestro = await _maestro(contracts, build, actor)

    assert len(maestro[0]) == len(production[0])
    assert _comparable(*maestro) == _comparable(*production)
