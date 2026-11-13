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

import base64
import inspect
import json
import logging
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.exceptions import ThreadManagementModeError
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.maestro import create_durable_text_delivery_adapter, create_text_delivery_adapter
from dooers.agents.server.maestro.artifacts import ARTIFACT_LINKS_UNAVAILABLE
from dooers.agents.server.maestro.client import MaestroContextError
from dooers.agents.server.maestro.config import MaestroConfigurationError
from dooers.agents.server.maestro.journal_store import PostgresEffectJournal
from dooers.agents.server.maestro.links import ArtifactLinkMissing, LinkResolver
from dooers.agents.server.maestro.memory import MaestroArtifactReference, MaestroMemory
from dooers.agents.server.maestro.persistence import MaestroAgentMemory, MaestroExecutionPersistence
from dooers.agents.server.maestro.window import MaestroContextWindow
from dooers.agents.server.persistence.base import Persistence
from dooers.agents.server.protocol.models import Thread, ThreadEvent, User, WireS2C_TextPart
from tests.parity import fake_maestro as fake_module
from tests.parity import samples
from tests.parity.context_pages import ThreadPages
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

    ``envelope``, ``links`` (the execution's ``LinkResolver``) and ``window()``: the real
    ``MaestroContextWindow`` over pages of the thread up to the cutoff (``cutoff``: the number of
    thread events the context holds, default all; ``page_size`` events per page). ``pages`` serves and
    records the requests (``pages.requests``; ``pages.checks`` counts the lease checks).
    ``snapshot()`` is a new forward read of the whole thread (``snapshots``); ``list_artifacts`` is
    the real ``MaestroMemory`` method over it.
    """

    list_artifacts = MaestroMemory.list_artifacts

    def __init__(
        self,
        thread: MaestroThread,
        participants=PARTICIPANTS,
        *,
        links: LinkResolver | None = None,
        cutoff: int | None = None,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> None:
        self._envelope = {
            "thread_id": thread.thread_id,
            "organization_id": ORG_ID,
            "workspace_id": WORKSPACE_ID,
            "instance_agent_id": AGENT_ID,
        }
        self.links = links or _links()
        self._thread, self._participants, self._cutoff = thread, list(participants), cutoff
        self.pages = ThreadPages(thread.events, list(participants), cutoff=cutoff)
        self._window = self.pages.window(page_size=page_size, max_pages=max_pages)
        self.snapshots = 0
        self.hydrated: list[MaestroArtifactReference] = []

    @property
    def envelope(self) -> dict[str, Any]:
        return self._envelope

    def window(self) -> MaestroContextWindow:
        return self._window

    async def snapshot(self) -> dict[str, Any]:
        self.snapshots += 1
        events = deepcopy(self._thread.events[: self._cutoff])
        return {"trigger_event": events[-1], "participants": deepcopy(self._participants), "events": events}

    async def hydrate_artifact(self, reference: MaestroArtifactReference) -> str:
        self.hydrated.append(reference)
        return "hydrated"


@dataclass
class Published:
    """The sink's side of read-your-writes: the echoed events and the URLs of the files it uploaded."""

    published_events: list[dict[str, Any]] = field(default_factory=list)
    published_media_urls: dict[str, str] = field(default_factory=dict)


def _memory(thread: MaestroThread, participants=PARTICIPANTS, *, published: Published | None = None, **options: Any) -> MaestroAgentMemory:
    return MaestroAgentMemory(ContextMemory(thread, participants, **options), assistant_name=ASSISTANT_NAME, published=published)


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
#: The current turn's outputs, read back in the same execution (read-your-writes).
WRITES_TURN = _text_turn(
    "Pode me passar para o suporte?",
    lambda send, trigger_id: [
        send.run_start(),
        send.text("Claro."),
        send.update_user_event(trigger_id, [_text("Pode me passar para o suporte, por favor?")]),
        send.update_thread(title="Pedido 42", queue="suporte"),
        send.add_participant("user-2", user_name="Bia Lima"),
        send.reasoning("transferindo"),
        send.text("Pronto, Bia vai te ajudar."),
    ],
)


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
            allowed_queues=["suporte"],
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


def _in_execution(turns: tuple[Turn, ...], **options: Any) -> MaestroAgentMemory:
    """The memory of the last turn's execution after it yielded its outputs.

    The context stops at the last person message (the trigger); the outputs after it are the
    events the sink echoed, as production stored them before the next read.
    """
    thread = _maestro(turns)
    cutoff = max(index for index, event in enumerate(thread.events) if event["actor"]["kind"] == "human") + 1
    published = Published(published_events=deepcopy(thread.events[cutoff:]))
    return _memory(thread, cutoff=cutoff, published=published, **options)


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
    memory = _memory(_maestro((PHOTO_TURN, FILES_TURN, LAST_TURN)))

    langchain = await memory.get_history(format="langchain")
    completions = await memory.get_history(format="openai_completions")
    responses = await memory.get_history(format="openai_responses")

    image = {"type": "image", "url": _link(IMAGE), "mime_type": "image/png", "filename": "foto.png"}
    assert langchain[0] == {"role": "user", "content": [_text("Veja esta foto"), image]}
    assert completions[0]["content"][1] == {"type": "image_url", "image_url": {"url": _link(IMAGE)}}
    assert responses[0]["content"][1] == {"type": "input_image", "image_url": _link(IMAGE)}
    # Each file keeps its own link (the text formats put it in the text).
    assert completions[2]["content"][1:] == [
        {"type": "text", "text": f"[document: contrato.pdf] {_link(DOCUMENT)}"},
        {"type": "text", "text": f"[audio: recado.wav (4.0s)] {_link(AUDIO)}"},
    ]
    assert responses[2]["content"][1:] == [
        {"type": "input_text", "text": f"[document: contrato.pdf] {_link(DOCUMENT)}"},
        {"type": "input_text", "text": f"[audio: recado.wav (4.0s)] {_link(AUDIO)}"},
    ]


async def test_a_file_without_link_goes_without_url(caplog):
    """403/404 for one file (removed, not linked up to the cutoff): only that part has no URL."""
    missing: list[str] = []

    async def issue(artifact_id: str) -> dict[str, Any]:
        if artifact_id == IMAGE:
            missing.append(artifact_id)
            raise ArtifactLinkMissing()
        return {"url": _link(artifact_id), "expires_at": "2099-01-01T00:00:00Z"}

    memory = _memory(_maestro((PHOTO_TURN, FILES_TURN, LAST_TURN)), links=LinkResolver(issue))
    caplog.set_level(logging.WARNING)

    first = await memory.get_history(format="langchain")
    again = await memory.get_history(format="openai_completions")

    # Production's rule for a part without a fetchable URL: the "[image: <name>]" marker.
    assert first[0] == {"role": "user", "content": "Veja esta foto\n[image: foto.png]"}
    assert first[2]["content"][1]["url"] == _link(DOCUMENT)
    assert again[0] == {"role": "user", "content": "Veja esta foto\n[image: foto.png]"}
    assert missing == [IMAGE]  # the miss is kept for the execution
    assert IMAGE in caplog.text and "artifact-links" not in caplog.text


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


async def test_one_snapshot_per_execution_and_a_lease_check_per_read():
    """The context below the cutoff does not change: every read uses the execution window.

    ``window()`` reads the first page once and checks the lease on every use
    (``tests/test_maestro_context.py``); here each read goes through it, and nothing else reads.
    """
    context = ContextMemory(_maestro(TURNS))
    memory = MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME)

    await memory.get_history()
    await memory.get_history_raw()
    await memory.get_thread()
    artifacts, _, _ = await memory.list_artifacts()

    # The thread fits the first page: one request for all the reads, a lease check for each of them
    # (get_history, get_history_raw, get_thread, then list_artifacts: get_thread and two get_events batches).
    assert (len(context.pages.requests), context.snapshots) == (1, 0)
    assert context.pages.checks >= 6
    assert len(artifacts) == 4


@pytest.mark.parametrize("fmt", HISTORY_FORMATS)
async def test_reads_after_yield_see_the_outputs_like_production(fmt):
    """Production stores each yielded event before the next read; Maestro adds the sink's echoes."""
    production = await _production((PHOTO_TURN, WRITES_TURN))
    memory = _in_execution((PHOTO_TURN, WRITES_TURN))

    for options in ({}, {"limit": 2}, {"filters": {"actor": "user"}}):
        expected = await production.get_history(format=fmt, **options)
        assert _normalized(await memory.get_history(format=fmt, **options)) == _normalized(expected), options
    assert len(await production.get_history(format=fmt)) == 5


async def test_raw_events_and_thread_after_yield_equal_production():
    turns = (_text_turn("Oi", lambda send, _: [send.run_start(), send.text("Olá!"), send.run_end()]), WRITES_TURN)
    production = await _production(turns)
    # Maestro lists the context participants by a random UUID: here the agent comes before the owner,
    # and the SDK still gives production's order, [owner, agent, added].
    memory = _in_execution(turns, participants=tuple(reversed(PARTICIPANTS)))

    raw = await memory.get_history_raw(limit=100)
    expected = await production.get_history_raw(limit=100)
    thread, expected_thread = await memory.get_thread(), await production.get_thread()

    assert _normalized([e.model_dump() for e in raw]) == _normalized([e.model_dump() for e in expected])
    # The trigger has the run_id of the current run (the echo of run_start: run.updated running).
    trigger = next(e for e in raw if e.content and e.content[0].text.startswith("Pode me passar"))
    assert trigger.run_id is not None and trigger.run_id == raw[-1].run_id

    # Users without the roles: a Maestro delivery has no Core roles yet (plan 5; README). A person the
    # agent added keeps only the principal in Maestro (participant.add): no name ("Humano"), e-mail or
    # identity IDs, until Core's connection context reaches the delivery (plan 5).
    def people(users):
        return [(u.user_id, u.user_name, u.user_email, u.identity_ids) for u in users]

    added = ("user-2", "Bia Lima", None, [])
    assert people(expected_thread.users)[-1] == added
    assert (thread.title, thread.queue, people(thread.users)) == (
        expected_thread.title,
        expected_thread.queue,
        [*people(expected_thread.users)[:-1], ("user-2", "Humano", None, [])],
    )
    assert [user.user_id for user in thread.users] == [USER.user_id, AGENT_ID, "user-2"]


async def test_own_uploaded_files_keep_their_url_in_the_execution():
    """A file this execution uploaded is past the cutoff: Maestro has no link for it, production has the URL."""
    thread = MaestroThread(shared_contracts())
    trigger = thread.message(PERSON, [_text("Me mande o gráfico")])
    artifact = str(uuid4())
    echo = thread.message(MAESTRO_AGENT, [{"type": "attachment", "artifact_id": artifact, "mime_type": "image/png", "filename": "g.png"}])
    issued: list[str] = []
    published = Published([echo], {artifact: "https://cdn.example.com/grafico.png"})
    memory = _memory(thread, cutoff=1, published=published, links=_links(issued))

    raw = await memory.get_history_raw()
    artifacts, _, _ = await memory.list_artifacts(direction="out")

    assert [event.id for event in raw] == [trigger["event_id"], echo["event_id"]]
    assert raw[1].content[0].url == "https://cdn.example.com/grafico.png"
    # Production's send.image part: the URL, no ref_id.
    assert [(a.url, a.ref_id) for a in artifacts] == [("https://cdn.example.com/grafico.png", None)]
    assert issued == []


async def test_an_echo_already_read_is_read_once():
    """An echo with the id of an event the snapshot has, or a repeated echo, is read once."""
    thread = MaestroThread(shared_contracts())
    trigger = thread.message(PERSON, [_text("Oi")])
    reply = thread.message(MAESTRO_AGENT, [_text("Olá!")])
    memory = _memory(thread, cutoff=1, published=Published([deepcopy(trigger), deepcopy(reply), deepcopy(reply)]))

    assert [event.id for event in await memory.get_history_raw()] == [trigger["event_id"], reply["event_id"]]


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
    # The person's e-mail and identity IDs stay in the agent's context (ThreadArtifact has neither).
    assert all("email" not in r.actor and "identity_ids" not in r.actor for r in references)
    assert references[0].actor["principal_id"] == PERSON["principal_id"]
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
    # Called as a production caller calls it: the Protocol's required arguments.
    parameters = list(inspect.signature(getattr(Persistence, name)).parameters.values())[1:]
    arguments = [None for p in parameters if p.default is inspect.Parameter.empty and p.kind is p.POSITIONAL_OR_KEYWORD]

    with pytest.raises(ThreadManagementModeError, match="THREAD_MANAGEMENT_MISMATCH"):
        await method(*arguments)
    assert (context.pages.checks, len(context.pages.requests), context.snapshots) == (0, 0, 0)


def test_the_class_defines_every_persistence_method():
    # No Protocol stub (body ``...``, returns None) is inherited by MaestroExecutionPersistence.
    assert {"create_event", "update_event", "delete_event", "create_run", "update_thread", "upsert_thread_participant"} <= set(
        _OTHER_METHODS
    )
    assert all(name in vars(MaestroExecutionPersistence) for name in [*_OTHER_METHODS, "get_thread", "get_events"])


# -- through the executor --


def _agent_config(fake: FakeMaestro, **values: Any) -> AgentConfig:
    """The AgentConfig of a Maestro-mode agent (the parity suite's legacy config, as harness._config)."""
    return AgentConfig(
        database_type="postgres",
        thread_management=values.pop("thread_management", "maestro"),
        maestro=fake.config,
        assistant_name=values.pop("assistant_name", ASSISTANT_NAME),
        **values,
    )


async def _deliver_turns(
    fake: FakeMaestro, handler, turns: list[list[dict[str, Any]]], *, config: Any = None, dispatch: bool = False
) -> list[Any]:
    """Each turn through a Maestro delivery: the person in the web chat, or (``dispatch``) the agent's
    own publication of the person's message, Maestro's side of production ``AgentServer.dispatch``."""
    adapter = create_text_delivery_adapter(config or fake.config, handler, private_resources)
    thread_id = fake.create_thread(USER, title=None)
    publish = fake.agent_publish if dispatch else fake.publish
    responses = []
    for number, content in enumerate(turns, start=1):
        trigger = publish(thread_id, USER, content, client_event_id=f"client-{number}")
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
        image = fake.upload(USER, samples.png_bytes(), filename="foto.png", mime_type="image/png")
        # AgentConfig.assistant_name reaches the adapter (the Maestro name differs).
        responses = await _deliver_turns(
            fake, handler, [[_text("Veja esta foto"), image], [_text("Resuma a conversa")]], config=_agent_config(fake)
        )

    assert [response.json()["state"] for response in responses] == ["responded", "responded"]
    assert isinstance(seen["memory"], MaestroAgentMemory) and isinstance(seen["memory"], AgentMemory)
    production = await _production((PHOTO_TURN, LAST_TURN))
    for fmt in HISTORY_FORMATS:
        assert _normalized(seen["histories"][fmt]) == _normalized(await production.get_history(format=fmt)), fmt
    # A web chat person has no name in Maestro's agent context ("Humano") until Core's context reaches it (plan 5).
    person = fake_module.WEB_CHAT_PERSON_NAME
    assert [(e.actor, e.author) for e in seen["raw"]] == [("user", person), ("assistant", ASSISTANT_NAME), ("user", person)]
    assert seen["raw"][0].content[1].url.startswith(f"{fake_module.CALLBACK_BASE_URL}/artifact-links/")
    assert isinstance(seen["thread"], Thread) and seen["thread"].id == seen["raw"][0].thread_id


async def _read_after_yield(on, send, memory, seen):
    yield send.run_start()
    yield send.text("Primeira parte.")
    yield send.update_user_event(on.context.event_id, [_text("Oi (editado)")])
    yield send.update_thread(queue="suporte")
    seen["raw"] = [event.model_dump() for event in await memory.get_history_raw()]
    seen["histories"] = {fmt: await memory.get_history(format=fmt) for fmt in HISTORY_FORMATS}
    seen["queue"] = (await memory.get_thread()).queue
    yield send.text("Segunda parte.")
    yield send.run_end()


async def test_executor_reads_after_yield_like_production():
    fake = FakeMaestro()
    maestro: dict[str, Any] = {}
    production: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        async for event in _read_after_yield(on, send, memory, maestro):
            yield event

    async def production_handler(on, send, memory, analytics, settings):
        async for event in _read_after_yield(on, send, memory, production):
            yield event

    with fake.serving():
        (response,) = await _deliver_turns(fake, handler, [[_text("Oi")]], config=_agent_config(fake), dispatch=True)
    persistence = MemoryPersistence()
    pipeline = HandlerPipeline(persistence=persistence, assistant_name=ASSISTANT_NAME)
    context = HandlerContext(
        handler=production_handler,
        agent_id=AGENT_ID,
        organization_id=ORG_ID,
        workspace_id=WORKSPACE_ID,
        message="Oi",
        user=USER,
        allowed_queues=["suporte"],
    )
    async for _ in pipeline.execute(context, await pipeline.setup(context)):
        pass

    assert response.json()["state"] == "responded"
    assert _normalized(maestro) == _normalized(production)
    assert [(e["type"], e["actor"]) for e in maestro["raw"]] == [("message", "user"), ("message", "assistant"), ("thread.update", "system")]
    assert maestro["raw"][0]["run_id"] == maestro["raw"][1]["run_id"] is not None
    assert maestro["raw"][0]["content"] == [{"type": "text", "text": "Oi (editado)"}]
    # The echo of run_start is the run.updated (running) that names the trigger.
    thread_id = next(iter(fake.threads))
    start = next(event for event in fake.events[thread_id] if event["type"] == "run.updated")
    trigger = next(event for event in fake.events[thread_id] if event["type"] == "message.created")
    assert (start["payload"]["status"], start["payload"]["trigger_event_id"]) == ("running", trigger["event_id"])


async def test_echoes_follow_the_maestro_order_with_an_open_stream():
    """A stream keeps its ordinal while open: a text persisted meanwhile is read before the stream's message."""
    fake = FakeMaestro(product_capabilities=("stream.delta",))
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        stream = send.open_text()
        await stream.delta("Resposta em partes")
        yield send.text("Antes do fim do stream.")
        await stream.finish()
        seen["texts"] = [event.content[0].text for event in await memory.get_history_raw() if event.actor == "assistant"]

    with fake.serving():
        (response,) = await _deliver_turns(fake, handler, [[_text("Oi")]], dispatch=True)

    assert response.json()["state"] == "responded"
    (thread_id,) = fake.threads
    stored = [event["payload"]["content"][0]["text"] for event in fake.events[thread_id] if event["actor"]["kind"] == "agent"]
    assert seen["texts"] == stored == ["Antes do fim do stream.", "Resposta em partes"]


async def test_a_tolerated_refusal_has_no_echo():
    """``already_participant``: the handler goes on, and the refused output is not read back."""
    fake = FakeMaestro()
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        yield send.add_participant(USER.user_id, user_name=USER.user_name)
        seen["users"] = [user.user_id for user in (await memory.get_thread()).users]
        seen["raw"] = [event.type for event in await memory.get_history_raw()]
        yield send.text("Você já está na conversa.")

    with fake.serving():
        (response,) = await _deliver_turns(fake, handler, [[_text("Me adicione")]], dispatch=True)

    assert response.json()["state"] == "responded"
    assert fake.rejections == [("outputs", "VALIDATION_FAILED")]
    assert seen == {"users": [USER.user_id, AGENT_ID], "raw": ["message"]}


async def _edit_own_message(on, send, memory, seen):
    yield send.text("Rascunho")
    own = (await memory.get_history_raw())[-1]
    yield send.update_user_event(own.id, [_text("Versão final")])
    seen["raw"] = [event.model_dump() for event in await memory.get_history_raw()]
    seen["history"] = await memory.get_history(format="openai")


async def test_an_edit_of_its_own_echo_reads_like_production():
    """``update_user_event`` on the agent's own message of this execution: the next read has the edit."""
    fake = FakeMaestro()
    maestro: dict[str, Any] = {}
    production: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        async for event in _edit_own_message(on, send, memory, maestro):
            yield event

    async def production_handler(on, send, memory, analytics, settings):
        async for event in _edit_own_message(on, send, memory, production):
            yield event

    with fake.serving():
        (response,) = await _deliver_turns(fake, handler, [[_text("Oi")]], config=_agent_config(fake), dispatch=True)
    pipeline = HandlerPipeline(persistence=MemoryPersistence(), assistant_name=ASSISTANT_NAME)
    context = HandlerContext(
        handler=production_handler, agent_id=AGENT_ID, organization_id=ORG_ID, workspace_id=WORKSPACE_ID, message="Oi", user=USER
    )
    async for _ in pipeline.execute(context, await pipeline.setup(context)):
        pass

    assert response.json()["state"] == "responded"
    assert _normalized(maestro) == _normalized(production)
    assert maestro["history"][-1] == {"role": "assistant", "content": "Versão final"}


async def test_own_uploaded_file_is_read_back_with_its_url(monkeypatch):
    fake = FakeMaestro()
    seen: dict[str, Any] = {}
    issue = FakeMaestro._artifact_link
    links: list[str] = []

    def counting(self, request, execution_id, artifact_id):
        links.append(artifact_id)
        return issue(self, request, execution_id, artifact_id)

    monkeypatch.setattr(FakeMaestro, "_artifact_link", counting)
    data_url = "data:image/png;base64," + base64.b64encode(samples.png_bytes()).decode()

    async def handler(on, send, memory, analytics, settings):
        yield send.image(data_url, mime_type="image/png")
        seen["raw"] = await memory.get_history_raw()

    with fake.serving():
        (response,) = await _deliver_turns(fake, handler, [[_text("Me mande a imagem")]])

    assert response.json()["state"] == "responded"
    image = seen["raw"][-1].content[0]
    # Production keeps the URL send.image received, without ref_id; Maestro has no link past the cutoff.
    assert (image.url, image.ref_id, links) == (data_url, None, [])


async def test_messages_after_the_cutoff_stay_out():
    fake = FakeMaestro()
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        seen["history"] = await memory.get_history()
        yield send.text("Ok.")

    adapter = create_text_delivery_adapter(fake.config, handler, private_resources)
    with fake.serving():
        thread_id = fake.create_thread(USER, title=None)
        first = fake.publish(thread_id, USER, [_text("Primeira")], client_event_id="client-1")
        (delivery,) = fake.deliveries_for(first)
        fake.publish(thread_id, USER, [_text("Chegou depois do corte")], client_event_id="client-2")
        response = await fake.deliver(adapter, delivery)

    assert response.json()["state"] == "responded"
    assert seen["history"] == [{"role": "user", "content": "Primeira"}]


async def test_history_file_without_link_degrades_and_the_handler_replies(monkeypatch, caplog):
    """A 404 for one old file: that part goes without URL, the client stays open, the reply goes out."""
    fake = FakeMaestro()
    seen: dict[str, Any] = {}
    issue = FakeMaestro._artifact_link

    def missing_after_first_turn(self, request, execution_id, artifact_id):
        if len(self.deliveries) > 1:
            raise fake_module._CallbackError(404, "NOT_FOUND")
        return issue(self, request, execution_id, artifact_id)

    monkeypatch.setattr(FakeMaestro, "_artifact_link", missing_after_first_turn)

    async def handler(on, send, memory, analytics, settings):
        async for event in _photo_then_summary(send, on, memory, seen):
            yield event

    caplog.set_level(logging.WARNING)
    with fake.serving():
        image = fake.upload(USER, samples.png_bytes(), filename="foto.png", mime_type="image/png")
        first, second = await _deliver_turns(fake, handler, [[_text("Veja esta foto"), image], [_text("Resuma a conversa")]])

    assert [first.json()["state"], second.json()["state"]] == ["responded", "responded"]
    assert seen["histories"]["langchain"][0] == {"role": "user", "content": "Veja esta foto\n[image: foto.png]"}
    assert seen["raw"][0].content[1].url is None
    assert image["artifact_id"] in caplog.text and ARTIFACT_LINKS_UNAVAILABLE not in caplog.text


async def test_history_without_the_link_route_fails_as_configuration_error(monkeypatch, caplog):
    """The route is off (Maestro answers UNSUPPORTED_CAPABILITY): the delivery fails loudly."""
    fake = FakeMaestro()
    seen: dict[str, Any] = {}
    issue = FakeMaestro._artifact_link

    def off_after_first_turn(self, request, execution_id, artifact_id):
        if len(self.deliveries) > 1:
            raise fake_module._CallbackError(422, "UNSUPPORTED_CAPABILITY")
        return issue(self, request, execution_id, artifact_id)

    monkeypatch.setattr(FakeMaestro, "_artifact_link", off_after_first_turn)

    async def handler(on, send, memory, analytics, settings):
        async for event in _photo_then_summary(send, on, memory, seen):
            yield event

    caplog.set_level(logging.ERROR)
    with fake.serving():
        image = fake.upload(USER, samples.png_bytes(), filename="foto.png", mime_type="image/png")
        first, second = await _deliver_turns(fake, handler, [[_text("Veja esta foto"), image], [_text("Resuma a conversa")]])

    assert first.json()["state"] == "responded"
    assert (second.status_code, second.json()["error"]["code"]) == (422, "UNSUPPORTED_CAPABILITY")
    assert "histories" not in seen
    assert ARTIFACT_LINKS_UNAVAILABLE in caplog.text


async def test_context_limit_inside_the_handler_fails_as_payload_too_large():
    fake = FakeMaestro()

    async def handler(on, send, memory, analytics, settings):
        raise MaestroContextError("CONTEXT_LIMIT")
        yield send.text("never")

    with fake.serving():
        (response,) = await _deliver_turns(fake, handler, [[_text("Oi")]])

    assert (response.status_code, response.json()["error"]["code"]) == (413, "PAYLOAD_TOO_LARGE")


# -- AgentConfig.assistant_name reaches both adapters --


def _journal(config: Any) -> PostgresEffectJournal:
    """A journal that never touches PostgreSQL (``tests/test_maestro_forms_readiness.py``)."""

    async def authorize(*_args):
        raise AssertionError("not used")

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace()

    return PostgresEffectJournal(
        SimpleNamespace(transaction=transaction),
        generation=str(uuid4()),
        organization_id=config.organization_id,
        workspace_id=config.workspace_id,
        instance_agent_id=config.instance_agent_id,
        fingerprint_keys={"1": b"x" * 32},
        active_key_version="1",
        authorize=authorize,
    )


async def _never(*_args):
    raise AssertionError("not used")


def test_adapters_take_the_assistant_name_from_agent_config():
    fake = FakeMaestro()
    agent_config = _agent_config(fake, assistant_name="Ana Bot")
    journal = _journal(fake.config)

    text = create_text_delivery_adapter(agent_config, _never, _never)
    durable = create_durable_text_delivery_adapter(agent_config, _never, _never, journal=journal)
    explicit = create_text_delivery_adapter(agent_config, _never, _never, assistant_name="Outro")
    plain = create_text_delivery_adapter(fake.config, _never, _never)
    plain_durable = create_durable_text_delivery_adapter(fake.config, _never, _never, journal=journal)

    assert [adapter._executor.assistant_name for adapter in (text, durable, explicit, plain, plain_durable)] == [
        "Ana Bot",
        "Ana Bot",
        "Outro",
        "Assistant",
        "Assistant",
    ]
    assert text.config is durable.config is fake.config
    # Only a Maestro-mode AgentConfig configures a Maestro adapter.
    legacy = _agent_config(fake, thread_management="agent")
    for factory, options in ((create_text_delivery_adapter, {}), (create_durable_text_delivery_adapter, {"journal": journal})):
        with pytest.raises(MaestroConfigurationError, match="MAESTRO_CONFIG_REQUIRED"):
            factory(legacy, _never, _never, **options)
