"""The production ``AgentMemory`` over the Maestro delivery context (plan 3, task 6).

Production reference (origin/main): ``AgentMemory`` (``handlers/memory.py``) over the rows
``PostgresPersistence`` writes and reads (``tests/parity/memory_persistence.py`` keeps the
same rows; the SDK has no SQLite backend). The production side runs each turn through
``HandlerPipeline``. The Maestro side stores the same turns as Maestro events
(``MaestroThread`` of ``tests/test_maestro_legacy_model.py``: every event is validated against
the vendored contract) and reads them as a handler does in an execution:
``MaestroAgentMemory`` over ``MaestroExecutionPersistence``.

The agent's name in Maestro ("Nome no Maestro") differs from ``AgentConfig.assistant_name``
(``ASSISTANT_NAME``) on purpose: production writes the configured name.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from dooers.agents.server.exceptions import ThreadManagementModeError
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.maestro import create_text_delivery_adapter
from dooers.agents.server.maestro.artifacts import ARTIFACT_LINKS_UNAVAILABLE
from dooers.agents.server.maestro.links import LinkResolver
from dooers.agents.server.maestro.memory import MaestroArtifactReference, MaestroMemory
from dooers.agents.server.maestro.persistence import MaestroAgentMemory, MaestroExecutionPersistence
from dooers.agents.server.persistence.base import Persistence
from dooers.agents.server.protocol.models import Thread, ThreadEvent, User, WireS2C_TextPart
from tests.parity import fake_maestro as fake_module
from tests.parity.fake_maestro import FakeMaestro, private_resources, shared_contracts
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import AGENT_ID, ASSISTANT_NAME, ORG_ID, USER, WORKSPACE_ID, ParityResult
from tests.parity.normalize import normalize
from tests.test_maestro_legacy_model import AGENT, OTHER, PERSON, MaestroThread, _thread_view

HISTORY_FORMATS = ("openai", "anthropic", "google", "cohere", "voyage", "langchain", "openai_completions", "openai_responses")
#: The executing agent as Maestro names it: not ``AgentConfig.assistant_name``.
MAESTRO_AGENT = {**AGENT, "display_name": "Nome no Maestro"}
IMAGE, DOCUMENT, AUDIO = str(uuid4()), str(uuid4()), str(uuid4())


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _link(artifact_id: str) -> str:
    return f"https://maestro.example/v1/artifact-links/{artifact_id}.sig"


def _links(issued: list[str] | None = None) -> LinkResolver:
    async def issue(artifact_id: str) -> dict[str, Any]:
        if issued is not None:
            issued.append(artifact_id)
        return {"url": _link(artifact_id), "expires_at": "2099-01-01T00:00:00Z"}

    return LinkResolver(issue)


def _participant(actor: dict[str, Any], *, role: str = "member", state: str = "active") -> dict[str, Any]:
    """A participant of the delivery context (``Public.ContextResponse.participants``)."""
    participant = {
        "participant_id": actor["participant_id"],
        "display_name": actor["display_name"],
        "state": state,
        "joined_sequence": "1",
        "left_sequence": None,
        "kind": actor["kind"],
        "principal_id": actor["principal_id"],
        "role": role,
    }
    if actor["kind"] == "agent":
        participant.update(instance_agent_id=actor["instance_agent_id"], capabilities=[], availability="available")
    else:
        # The agent's context adds the person's e-mail and identity IDs.
        participant.update(email=actor["email"], identity_ids=list(actor["identity_ids"]))
    return participant


PARTICIPANTS = (_participant(PERSON, role="owner"), _participant(MAESTRO_AGENT))


class ContextMemory:
    """What ``MaestroExecutionPersistence`` reads of ``MaestroMemory``.

    The envelope, the execution's ``LinkResolver`` and a fresh snapshot of the delivery
    context on each call. ``list_artifacts`` is the real ``MaestroMemory`` method over it.
    """

    list_artifacts = MaestroMemory.list_artifacts

    def __init__(self, thread: MaestroThread, participants=PARTICIPANTS, *, links: LinkResolver | None = None) -> None:
        self._envelope = {
            "thread_id": thread.thread_id,
            "organization_id": ORG_ID,
            "workspace_id": WORKSPACE_ID,
            "instance_agent_id": AGENT_ID,
        }
        self._links = links or _links()
        self._thread, self._participants = thread, list(participants)
        self.snapshots = 0
        self.hydrated: list[MaestroArtifactReference] = []

    async def snapshot(self) -> dict[str, Any]:
        self.snapshots += 1
        events = deepcopy(self._thread.events)
        return {"trigger_event": events[-1], "participants": deepcopy(self._participants), "events": events}

    async def hydrate_artifact(self, reference: MaestroArtifactReference) -> str:
        self.hydrated.append(reference)
        return "hydrated"


def _memory(thread: MaestroThread, participants=PARTICIPANTS, **options: Any) -> MaestroAgentMemory:
    return MaestroAgentMemory(ContextMemory(thread, participants, **options), assistant_name=ASSISTANT_NAME)


Reply = Callable[[AgentSend, str], list[AgentEvent]]


def _no_reply(send: AgentSend, trigger_id: str) -> list[AgentEvent]:
    return []


@dataclass(frozen=True)
class Turn:
    """One person message (production wire parts, Maestro parts) and the agent's reply to it."""

    production: tuple[dict[str, Any], ...]
    maestro: tuple[dict[str, Any], ...]
    reply: Reply = _no_reply


def _text_turn(text: str, reply: Reply = _no_reply) -> Turn:
    return Turn((_text(text),), (_text(text),), reply)


PHOTO_TURN = Turn(
    production=(
        _text("Veja esta foto"),
        {"type": "image", "url": "https://cdn.example.com/foto.png", "mime_type": "image/png", "filename": "foto.png"},
    ),
    maestro=(
        _text("Veja esta foto"),
        {"type": "attachment", "artifact_id": IMAGE, "mime_type": "image/png", "filename": "foto.png", "size_bytes": 10},
    ),
    reply=lambda send, _: [send.run_start(), send.text("Recebi a foto."), send.run_end()],
)
FILES_TURN = Turn(
    production=(
        _text("Seguem o contrato e um recado"),
        {
            "type": "document",
            "url": "https://cdn.example.com/contrato.pdf",
            "mime_type": "application/pdf",
            "filename": "contrato.pdf",
            "size_bytes": 2048,
        },
        {"type": "audio", "url": "https://cdn.example.com/recado.wav", "mime_type": "audio/wav", "filename": "recado.wav", "duration": 4.0},
    ),
    maestro=(
        _text("Seguem o contrato e um recado"),
        {"type": "attachment", "artifact_id": DOCUMENT, "mime_type": "application/pdf", "filename": "contrato.pdf", "size_bytes": 2048},
        {"type": "attachment", "artifact_id": AUDIO, "mime_type": "audio/wav", "filename": "recado.wav", "size_bytes": 64, "duration": 4.0},
    ),
    reply=lambda send, _: [
        send.run_start(),
        send.reasoning("lendo o contrato"),
        send.tool_call("ler_pdf", {"arquivo": "contrato.pdf"}, id="call-1"),
        send.tool_result("ler_pdf", {"paginas": 3}, id="call-1"),
        send.text("Li o contrato.", author="Leitor"),
        send.image("https://cdn.example.com/resumo.png", mime_type="image/png", alt="Resumo"),
        send.contact(display_name="Ana", phones=[{"phone": "+5511999990000", "type": "CELL"}]),
        send.run_end(),
    ],
)
EDIT_TURN = _text_turn(
    "Mensagem com erro",
    lambda send, trigger_id: [send.update_user_event(trigger_id, [_text("Mensagem corrigida")]), send.text("Corrigido.")],
)
LAST_TURN = _text_turn("Resuma a conversa")
TURNS = (PHOTO_TURN, FILES_TURN, EDIT_TURN, LAST_TURN)


async def _production(turns: tuple[Turn, ...]) -> AgentMemory:
    """Production after ``turns``: the memory its handler reads in the last turn (it replies nothing)."""
    persistence = MemoryPersistence()
    pipeline = HandlerPipeline(persistence=persistence, assistant_name=ASSISTANT_NAME)
    thread_id = None
    for turn in turns:

        async def handler(on, send, memory, analytics, settings, reply=turn.reply):
            for event in reply(send, on.context.event_id):
                yield event

        context = HandlerContext(
            handler=handler,
            agent_id=AGENT_ID,
            organization_id=ORG_ID,
            workspace_id=WORKSPACE_ID,
            message="",
            content=[dict(part) for part in turn.production],
            user=USER,
            thread_id=thread_id,
        )
        result = await pipeline.setup(context)
        async for _ in pipeline.execute(context, result):
            pass
        thread_id = result.thread.id
    return AgentMemory(thread_id, persistence)


def _maestro(turns: tuple[Turn, ...], *, agent: dict[str, Any] = MAESTRO_AGENT) -> MaestroThread:
    """The same turns as Maestro stores them; the last person message is the delivery trigger."""
    thread = MaestroThread(shared_contracts())
    for turn in turns:
        trigger = thread.message(PERSON, [dict(part) for part in turn.maestro])
        thread.publish(turn.reply(AgentSend(), trigger["event_id"]), trigger, actor=agent)
    return thread


_URL = re.compile(r"https?://[^\s\"]+")


def _normalized(value: Any) -> Any:
    """Ids and times as markers (``tests/parity/normalize.py``), and each URL as ``<url:N>``.

    URLs are numbered in order of first appearance, inside text too: ``openai_completions`` and
    ``openai_responses`` put a document's or an audio's URL in the text. Each side is numbered alone.
    """
    urls: dict[str, str] = {}
    text = _URL.sub(lambda match: urls.setdefault(match.group(), f"<url:{len(urls) + 1}>"), json.dumps(value, default=str))
    return normalize(ParityResult(events=[], incoming={}, histories={"value": json.loads(text)}, outputs={})).histories


def _at(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))


# -- get_history --


@pytest.mark.parametrize("fmt", HISTORY_FORMATS)
async def test_get_history_equals_production(fmt):
    production = await _production(TURNS)
    maestro = _memory(_maestro(TURNS))

    for options in ({}, {"limit": 2}, {"limit": 3, "order": "asc"}, {"filters": {"actor": "user"}}):
        expected = await production.get_history(format=fmt, **options)
        assert _normalized(await maestro.get_history(format=fmt, **options)) == _normalized(expected), options
    # The whole thread: 8 messages (the contact-only reply has no text, in production too).
    assert len(await production.get_history(format=fmt)) == 8


async def test_history_image_is_multimodal_with_link():
    memory = _memory(_maestro((PHOTO_TURN, LAST_TURN)))

    langchain = await memory.get_history(format="langchain")
    completions = await memory.get_history(format="openai_completions")
    responses = await memory.get_history(format="openai_responses")

    image = {"type": "image", "url": _link(IMAGE), "mime_type": "image/png", "filename": "foto.png"}
    assert langchain[0] == {"role": "user", "content": [_text("Veja esta foto"), image]}
    assert completions[0]["content"][1] == {"type": "image_url", "image_url": {"url": _link(IMAGE)}}
    assert responses[0]["content"][1] == {"type": "input_image", "image_url": _link(IMAGE)}


@pytest.mark.parametrize("fmt", HISTORY_FORMATS)
async def test_multi_agent_history_prefixes_only_other_agent(fmt):
    thread = MaestroThread(shared_contracts())
    question = thread.message(PERSON, [_text("Minha fatura chegou?")])
    thread.message(OTHER, [_text("Fatura enviada.")])
    thread.publish([AgentSend().text("Posso ajudar em algo mais?")], question, actor=MAESTRO_AGENT)
    participants = (*PARTICIPANTS, _participant(OTHER))

    history = await _memory(thread, participants).get_history(format=fmt)

    # Production with the other agent's message as a person's message with the prefix: the
    # person's and the agent's own messages are production's, without prefix.
    production = await _production(
        (
            _text_turn("Minha fatura chegou?"),
            _text_turn(f"[agente: {OTHER['display_name']}] Fatura enviada.", lambda send, _: [send.text("Posso ajudar em algo mais?")]),
        )
    )
    assert history == await production.get_history(format=fmt)
    assert len(history) == 3


async def test_other_agent_without_a_second_active_agent_is_the_assistant():
    thread = MaestroThread(shared_contracts())
    thread.message(PERSON, [_text("Oi")])
    thread.message(OTHER, [_text("Fatura enviada.")])
    removed = _participant(OTHER, state="removed")

    history = await _memory(thread, (*PARTICIPANTS, removed)).get_history()

    assert history == [{"role": "user", "content": "Oi"}, {"role": "assistant", "content": "Fatura enviada."}]


# -- get_history_raw and get_events --


async def test_get_history_raw_returns_thread_events():
    raw = await _memory(_maestro(TURNS)).get_history_raw(limit=100)

    assert raw and all(isinstance(event, ThreadEvent) for event in raw)
    # The author of the agent's own events is AgentConfig.assistant_name, as production writes it.
    assert [(event.type, event.author) for event in raw if event.actor == "assistant"] == [
        ("message", ASSISTANT_NAME),
        ("reasoning", ASSISTANT_NAME),
        ("tool.call", None),
        ("message", "Leitor"),
        ("message", ASSISTANT_NAME),
        ("message", ASSISTANT_NAME),
        ("message", ASSISTANT_NAME),
    ]


async def test_get_history_raw_equals_production_rows():
    # Text only: a production URL part has no ref_id, and a Maestro upload has one.
    turns = (
        _text_turn(
            "Oi", lambda send, _: [send.run_start(), send.reasoning("pensando"), send.text("Olá! Como posso ajudar?"), send.run_end()]
        ),
        EDIT_TURN,
        LAST_TURN,
    )
    production = await (await _production(turns)).get_history_raw(limit=100)
    maestro = await _memory(_maestro(turns)).get_history_raw(limit=100)

    assert len(maestro) == len(production) == 6
    assert _normalized([e.model_dump() for e in maestro]) == _normalized([e.model_dump() for e in production])


async def test_selection_runs_after_the_fold():
    """Edits and removals apply before ``limit``, ``order`` and ``filters`` (they can cross a page)."""
    thread = MaestroThread(shared_contracts())
    first = thread.message(PERSON, [_text("Original")])
    second = thread.message(PERSON, [_text("Apagada")])
    answer = thread.message(MAESTRO_AGENT, [_text("Resposta")])
    thread.append("message.updated", MAESTRO_AGENT, {"event_id": first["event_id"], "content": [_text("Editada")]})
    thread.append("message.removed", MAESTRO_AGENT, {"event_id": second["event_id"]})
    memory = _memory(thread)
    persistence = MaestroExecutionPersistence(ContextMemory(thread), assistant_name=ASSISTANT_NAME)

    newest = await memory.get_history_raw(limit=2, order="desc")

    assert [(event.id, event.content) for event in newest] == [
        (answer["event_id"], [WireS2C_TextPart(text="Resposta")]),
        (first["event_id"], [WireS2C_TextPart(text="Editada")]),
    ]
    assert await memory.get_history(limit=1) == [{"role": "assistant", "content": "Resposta"}]
    # Production filter columns only (an unknown key is ignored, as the SQL query ignores it).
    assert [e.id for e in await memory.get_history_raw(filters={"actor": "user"})] == [first["event_id"]]
    assert [e.id for e in await memory.get_history_raw(filters={"user_email": USER.user_email, "other": "x"})] == [first["event_id"]]
    assert [e.id for e in await memory.get_history_raw(filters={"user_id": ""})] == []
    # after_event_id and before_event_id, as PostgresPersistence.get_events reads them.
    thread_id = thread.thread_id
    assert [e.id for e in await persistence.get_events(thread_id, after_event_id=first["event_id"])] == [answer["event_id"]]
    assert [e.id for e in await persistence.get_events(thread_id, after_event_id=answer["event_id"], order="desc")] == [first["event_id"]]
    assert [e.id for e in await persistence.get_events(thread_id, before_event_id=answer["event_id"], order="desc")] == [first["event_id"]]
    assert len(await persistence.get_events(thread_id, before_event_id=str(uuid4()))) == 2


async def test_links_are_issued_only_for_the_events_returned():
    thread = MaestroThread(shared_contracts())
    images = [str(uuid4()) for _ in range(3)]
    for image in images:
        thread.message(PERSON, [{"type": "attachment", "artifact_id": image, "mime_type": "image/png", "filename": "a.png"}])
    issued: list[str] = []
    memory = _memory(thread, links=_links(issued))

    (newest,) = await memory.get_history_raw(limit=1, order="desc")

    assert newest.content[0].url == _link(images[-1])
    assert issued == [images[-1]]


async def test_each_read_takes_a_fresh_snapshot():
    context = ContextMemory(_maestro(TURNS))
    memory = MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME)

    await memory.get_history()
    await memory.get_history_raw()
    await memory.get_thread()

    assert context.snapshots == 3


# -- get_thread --


async def test_get_thread_returns_thread():
    thread = MaestroThread(shared_contracts())
    thread.append("thread.created", PERSON, {"thread": _thread_view(thread.thread_id, queue="vendas")})
    thread.message(PERSON, [_text("Oi")])
    view = {"thread_id": thread.thread_id, "state": "active"}
    thread.append("thread.updated", MAESTRO_AGENT, {**view, "title": "Pedido 42", "version": 2, "queue": "suporte"})
    last = thread.message(MAESTRO_AGENT, [_text("Olá")])

    result = await _memory(thread).get_thread()

    person = User(user_id=USER.user_id, user_name=USER.user_name, user_email=USER.user_email, identity_ids=list(USER.identity_ids))
    assert isinstance(result, Thread)
    assert result == Thread(
        id=thread.thread_id,
        agent_id=AGENT_ID,
        organization_id=ORG_ID,
        workspace_id=WORKSPACE_ID,
        owner=person,
        # The agent as production lists it: AgentConfig.assistant_name, not the Maestro name.
        users=[person, User(user_id=AGENT_ID, user_name=ASSISTANT_NAME)],
        title="Pedido 42",
        queue="suporte",
        created_at=datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
        updated_at=_at(last),
        last_event_at=_at(last),
    )


async def test_get_thread_without_thread_created_keeps_production_defaults():
    thread = _maestro((LAST_TURN,))

    result = await _memory(thread).get_thread()

    assert (result.id, result.title, result.queue) == (thread.thread_id, None, None)
    assert result.created_at == result.last_event_at == _at(thread.events[0])


async def test_another_thread_is_not_read():
    context = ContextMemory(_maestro(TURNS))
    persistence = MaestroExecutionPersistence(context, assistant_name=ASSISTANT_NAME)

    assert await persistence.get_thread("outra-thread") is None
    assert await persistence.get_events("outra-thread") == []
    assert await AgentMemory("outra-thread", persistence).get_history() == []
    assert context.snapshots == 0


# -- list_artifacts and the Maestro extras --


async def test_list_artifacts_is_the_production_index_with_links():
    memory = _memory(_maestro(TURNS))

    artifacts, cursor, has_more = await memory.list_artifacts()
    page, next_cursor, more = await memory.list_artifacts(limit=1, cursor="1")
    outgoing, _, _ = await memory.list_artifacts(direction="out")

    assert [(a.kind, a.direction, a.url, a.ref_id, a.user_id) for a in artifacts] == [
        ("image", "out", "https://cdn.example.com/resumo.png", None, None),
        ("document", "in", _link(DOCUMENT), DOCUMENT, USER.user_id),
        ("audio", "in", _link(AUDIO), AUDIO, USER.user_id),
        ("image", "in", _link(IMAGE), IMAGE, USER.user_id),
    ]
    assert (cursor, has_more) == (None, False)
    assert ([a.ref_id for a in page], next_cursor, more) == ([DOCUMENT], "2", True)
    assert [a.url for a in outgoing] == ["https://cdn.example.com/resumo.png"]


async def test_maestro_extras_read_the_maestro_context():
    thread = _maestro(TURNS)
    context = ContextMemory(thread)
    memory = MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME)

    references, cursor, has_more = await memory.list_maestro_artifacts(direction="in")
    hydrated = await memory.hydrate_artifact(references[0])

    assert [(r.artifact_id, r.filename) for r in references] == [(DOCUMENT, "contrato.pdf"), (AUDIO, "recado.wav"), (IMAGE, "foto.png")]
    assert all(isinstance(r, MaestroArtifactReference) for r in references) and (cursor, has_more) == (None, False)
    assert (hydrated, context.hydrated) == ("hydrated", [references[0]])


# -- writes --

#: Every ``Persistence`` method except the two reads ``AgentMemory`` uses.
_OTHER_METHODS = sorted(
    name
    for name, _ in inspect.getmembers(Persistence, inspect.isfunction)
    if not name.startswith("_") and name not in {"get_thread", "get_events"}
)


@pytest.mark.parametrize("name", _OTHER_METHODS)
async def test_every_other_persistence_method_raises_thread_management_mode_error(name):
    context = ContextMemory(_maestro(TURNS))
    method = getattr(MaestroExecutionPersistence(context, assistant_name=ASSISTANT_NAME), name)
    required = [
        p
        for p in inspect.signature(method).parameters.values()
        if p.default is inspect.Parameter.empty and p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]

    with pytest.raises(ThreadManagementModeError, match="THREAD_MANAGEMENT_MISMATCH"):
        await method(*[None] * len(required))
    assert context.snapshots == 0


def test_the_write_list_covers_the_persistence_protocol():
    assert {"create_event", "update_event", "delete_event", "create_run", "update_thread", "upsert_thread_participant"} <= set(
        _OTHER_METHODS
    )


# -- through the executor --


async def _deliver_turns(fake: FakeMaestro, handler, turns: list[list[dict[str, Any]]], **adapter_options: Any) -> list[Any]:
    adapter = create_text_delivery_adapter(fake.config, handler, private_resources, **adapter_options)
    thread_id = fake.create_thread(USER, title=None)
    responses = []
    for number, content in enumerate(turns, start=1):
        trigger = fake.publish(thread_id, USER, content, client_event_id=f"client-{number}")
        (delivery,) = fake.deliveries_for(trigger)
        responses.append(await fake.deliver(adapter, delivery))
    return responses


async def _photo_then_summary(send: AgentSend, on: Any, memory: Any, seen: dict[str, Any]):
    if on.message != "Resuma a conversa":
        yield send.run_start()
        yield send.text("Recebi a foto.")
        yield send.run_end()
        return
    seen["memory"] = memory
    seen["histories"] = {fmt: await memory.get_history(format=fmt) for fmt in HISTORY_FORMATS}
    seen["raw"] = await memory.get_history_raw()
    seen["thread"] = await memory.get_thread()
    yield send.text("Resumo pronto.")


async def test_executor_gives_the_handler_the_production_memory():
    fake = FakeMaestro(assistant_name="Nome no Maestro")
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        async for event in _photo_then_summary(send, on, memory, seen):
            yield event

    with fake.serving():
        image = fake.upload(USER, b"\x89PNG fixture", filename="foto.png", mime_type="image/png")
        responses = await _deliver_turns(
            fake, handler, [[_text("Veja esta foto"), image], [_text("Resuma a conversa")]], assistant_name=ASSISTANT_NAME
        )

    assert [response.json()["state"] for response in responses] == ["responded", "responded"]
    assert isinstance(seen["memory"], MaestroAgentMemory) and isinstance(seen["memory"], AgentMemory)
    production = await _production((PHOTO_TURN, LAST_TURN))
    for fmt in HISTORY_FORMATS:
        assert _normalized(seen["histories"][fmt]) == _normalized(await production.get_history(format=fmt)), fmt
    assert [(e.actor, e.author) for e in seen["raw"]] == [("user", USER.user_name), ("assistant", ASSISTANT_NAME), ("user", USER.user_name)]
    assert seen["raw"][0].content[1].url.startswith(f"{fake_module.CALLBACK_BASE_URL}/artifact-links/")
    assert isinstance(seen["thread"], Thread) and seen["thread"].id == seen["raw"][0].thread_id


async def test_history_without_the_link_route_fails_as_configuration_error(monkeypatch, caplog):
    """The history never hands the LLM a part without its link: the delivery fails loudly."""
    fake = FakeMaestro()
    seen: dict[str, Any] = {}
    issue = FakeMaestro._artifact_link

    def off_after_first_turn(self, request, execution_id, artifact_id):
        if len(self.deliveries) > 1:
            raise fake_module._CallbackError(404, "NOT_FOUND")
        return issue(self, request, execution_id, artifact_id)

    monkeypatch.setattr(FakeMaestro, "_artifact_link", off_after_first_turn)

    async def handler(on, send, memory, analytics, settings):
        async for event in _photo_then_summary(send, on, memory, seen):
            yield event

    caplog.set_level(logging.ERROR)
    with fake.serving():
        image = fake.upload(USER, b"\x89PNG fixture", filename="foto.png", mime_type="image/png")
        first, second = await _deliver_turns(fake, handler, [[_text("Veja esta foto"), image], [_text("Resuma a conversa")]])

    assert first.json()["state"] == "responded"
    assert (second.status_code, second.json()["error"]["code"]) == (422, "UNSUPPORTED_CAPABILITY")
    assert "histories" not in seen
    assert ARTIFACT_LINKS_UNAVAILABLE in caplog.text
