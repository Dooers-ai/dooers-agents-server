"""A Maestro delivery gives the handler the production ``AgentIncoming`` (plan 3, task 4).

Production reference: ``HandlerPipeline.execute`` and ``_resolve_content_parts_async`` on
origin/main. The parity tests run the legacy scenario (``run_legacy``) and the same input
through a Maestro delivery (``FakeMaestro`` and the SDK adapter), then compare what the
handler read. The other tests use the loopback fixture of ``test_maestro_context``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
import pickle
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.handlers.pipeline import HandlerPipeline
from dooers.agents.server.maestro import create_text_delivery_adapter
from dooers.agents.server.maestro.artifacts import MaestroArtifactLinkUnavailable, MaestroArtifactsClient, MaestroHydratedArtifact
from dooers.agents.server.maestro.client import MaestroContextError
from dooers.agents.server.maestro.context import (
    MaestroPrivateResources,
    _production_message,
    current_maestro_context,
    maestro_handler_context,
)
from dooers.agents.server.maestro.links import LinkResolver
from dooers.agents.server.protocol.maestro import MaestroContracts
from dooers.agents.server.protocol.models import AudioPart, ContactPart, DocumentPart, ImagePart, TextPart, User
from dooers.tools.rag.runtime import current_execution_context
from tests.parity import fake_maestro as fake_module
from tests.parity.fake_maestro import CALLBACK_BASE_URL, FakeMaestro
from tests.parity.harness import _maestro_content, run_legacy
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import ASSISTANT_NAME, USER, ParityResult, close_capture, open_capture
from tests.parity.normalize import normalize
from tests.parity.scenarios import SCENARIOS
from tests.test_maestro_context import fixture_server, prepare, private

SETTINGS = ("ARTIFACT_LINKS_ENABLED", "MAESTRO_ARTIFACT_LINK_KEY")
LINK = {"url": "https://maestro.example/v1/artifact-links/token.signature", "expires_at": "2099-01-01T00:00:00Z"}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def bundle():
    return MaestroContracts()


def with_links(data: dict[str, Any]) -> None:
    """Maestro with ``ARTIFACT_LINKS_ENABLED`` signs ``artifact.link.issue`` into the capability."""
    data["capability_claims"]["operations"].append("artifact.link.issue")


# -- FakeMaestro deliveries (the person, the thread and the agent of the parity scenarios) --


def _person(maestro: FakeMaestro, thread_id: str) -> dict[str, Any]:
    participant = maestro._participant(thread_id, kind="human", principal_id=USER.user_id)
    return {"kind": "human", "principal_id": USER.user_id, "participant_id": participant["participant_id"], "display_name": USER.user_name}


@pytest.fixture
def agent_only_fields(monkeypatch):
    """Maestro adds the person's e-mail and identity IDs to the agent's delivery context only.

    Stored events (``Public.Event.*``) never carry them; the fake's context gets them here.
    """
    context = FakeMaestro._context

    def enriched(self, request, delivery_id):
        page = context(self, request, delivery_id).json()
        events = [page["trigger_event"], *([page["invocation_event"]] if "invocation_event" in page else []), *page["history"]["items"]]
        for actor in [event["actor"] for event in events]:
            if actor["kind"] == "human" and actor["principal_id"] == USER.user_id:
                actor.update(email=USER.user_email, identity_ids=list(USER.identity_ids))
        return self._reply("Public.ContextResponse", page)

    monkeypatch.setattr(FakeMaestro, "_context", enriched)


def _new_thread(maestro: FakeMaestro, title: str | None) -> tuple[str, dict[str, Any]]:
    """A thread as Maestro creates it for a person: ``thread.created`` is its first event."""
    thread_id = maestro.create_thread(USER, title=title)
    person = _person(maestro, thread_id)
    maestro._append(thread_id, person, {"thread": deepcopy(maestro.threads[thread_id])}, event_type="thread.created")
    return thread_id, person


async def _deliver(maestro: FakeMaestro, adapter, event: dict[str, Any]) -> None:
    for delivery in maestro.deliveries_for(event):
        response = await maestro.deliver(adapter, delivery)
        assert (response.status_code, delivery.state) == (200, "responded"), response.text


def _resources(queues=()) -> Callable:
    async def resolve(scope):
        return MaestroPrivateResources(scope, MemoryPersistence(), {}, {}, queues=queues)

    return resolve


def _media_link(part: dict[str, Any]) -> dict[str, Any]:
    """A URL part of the scenario as the agent's context carries it (``media_link``: no ``size_bytes``)."""
    if part["type"] not in {"image", "audio", "document"}:
        return part
    extra = {key: part[key] for key in ("mime_type", "filename", "duration") if key in part}
    return {"type": "media_link", "kind": part["type"], "url": part["url"], **extra}


async def test_incoming_fields_match_production(agent_only_fields):
    """Same handler, same input as the parity scenario ``incoming_fields``: the handler reads the same values."""
    scenario = SCENARIOS["incoming_fields"]
    spec = scenario.incoming
    legacy = (await run_legacy("incoming_fields")).incoming
    maestro = FakeMaestro(assistant_name=ASSISTANT_NAME)
    adapter = create_text_delivery_adapter(maestro.config, scenario.handler, _resources(spec.queues))
    capture, token = open_capture()
    try:
        with maestro.serving():
            thread_id, person = _new_thread(maestro, spec.thread_title)
            for number, turn in enumerate(spec.turns, start=1):
                content = [_media_link(part) for part in _maestro_content(turn)]
                payload = {"client_event_id": f"client-{number}", "content": content, "mentions": []}
                await _deliver(maestro, adapter, maestro._append(thread_id, person, payload))
    finally:
        close_capture(token)
    expected = deepcopy(legacy)
    # Declared difference 1: a media_link has no size_bytes, and production gives a URL
    # document without size_bytes the size 0.
    assert expected["content"][2]["type"] == "document" and expected["content"][2]["size_bytes"] == 2048
    expected["content"][2]["size_bytes"] = 0
    # Declared difference 2 (plan 5): production takes the user's roles from Core's
    # ConnectionContext; a Maestro delivery has no Core connection context yet, so the roles
    # keep the User defaults.
    roles = ("system_role", "organization_role", "workspace_role")
    assert [expected["context"]["user"][role] for role in roles] == ["admin", "owner", "manager"]
    assert [capture.incoming["context"]["user"][role] for role in roles] == ["user", "member", "member"]
    expected["context"]["user"].update({role: capture.incoming["context"]["user"][role] for role in roles})
    assert normalize(ParityResult([], capture.incoming, {})).incoming == normalize(ParityResult([], expected, {})).incoming
    assert capture.incoming["context"]["queue"] == "suporte" and capture.incoming["rag_context"]["on_behalf"] is True


@pytest.mark.parametrize("name", ["form_response_submit", "form_response_cancel"])
async def test_form_cancel_invokes_handler(name, agent_only_fields):
    """``form.responded`` is a new delivery to the form's author; submit and cancel both run the handler."""
    scenario = SCENARIOS[name]
    legacy = (await run_legacy(name)).incoming
    maestro = FakeMaestro(assistant_name=ASSISTANT_NAME)
    adapter = create_text_delivery_adapter(maestro.config, scenario.handler, _resources())
    capture, token = open_capture()
    try:
        with maestro.serving():
            thread_id, person = _new_thread(maestro, None)
            first = {"client_event_id": "client-1", "content": _maestro_content(scenario.incoming.turns[0]), "mentions": []}
            await _deliver(maestro, adapter, maestro._append(thread_id, person, first))
            (form,) = [event for event in maestro.events[thread_id] if event["type"] == "form.created"]
            data = dict(scenario.incoming.turns[1].data)
            response = {"form_event_id": form["event_id"], "values": data["values"], "cancelled": data["cancelled"]}
            await _deliver(maestro, adapter, maestro._append(thread_id, person, response, event_type="form.responded"))
    finally:
        close_capture(token)
    received = capture.incoming
    assert received["form_event_id"] == form["event_id"]
    assert {key: value for key, value in received.items() if key != "form_event_id"} == {
        key: value for key, value in legacy.items() if key != "form_event_id"
    }
    assert received["form_cancelled"] is data["cancelled"]
    replies = [event["payload"]["content"] for event in maestro.events[thread_id] if event["type"] == "message.created"][-1:]
    assert replies == [[{"type": "text", "text": "Tudo bem, cancelado." if data["cancelled"] else "Cadastro concluído."}]]


async def test_media_part_has_bytes_and_link(monkeypatch, caplog):
    """An uploaded file reaches the handler with its bytes and a temporary link, one link per file."""
    issued: list[str] = []
    issue = FakeMaestro._artifact_link

    def counting(self, request, execution_id, artifact_id):
        issued.append(artifact_id)
        return issue(self, request, execution_id, artifact_id)

    monkeypatch.setattr(FakeMaestro, "_artifact_link", counting)
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        seen["message"], seen["content"] = on.message, list(on.content)
        yield send.text("Recebi.")

    maestro = FakeMaestro()
    adapter = create_text_delivery_adapter(maestro.config, handler, _resources())
    caplog.set_level(logging.DEBUG)
    with maestro.serving():
        thread_id, person = _new_thread(maestro, None)
        image = maestro.upload(USER, b"\x89PNG fixture", filename="foto.png", mime_type="image/png")
        audio = {**maestro.upload(USER, b"RIFF fixture", filename="recado.wav", mime_type="audio/wav"), "duration": 4.0}
        document = maestro.upload(USER, b"%PDF fixture", filename="nota.pdf", mime_type="application/pdf")
        payload = {"client_event_id": "client-1", "content": [image, audio, document, deepcopy(image)], "mentions": []}
        trigger = maestro._append(thread_id, person, payload)
        maestro._link_attachments(trigger)
        await _deliver(maestro, adapter, trigger)

    image_part, audio_part, document_part, again = seen["content"]
    links = f"{CALLBACK_BASE_URL}/artifact-links/"
    assert isinstance(image_part, ImagePart) and image_part.data == b"\x89PNG fixture" and image_part.url.startswith(links)
    assert (image_part.mime_type, image_part.filename, image_part.width, image_part.height) == ("image/png", "foto.png", None, None)
    assert isinstance(audio_part, AudioPart) and audio_part.data == b"RIFF fixture" and audio_part.url.startswith(links)
    assert (audio_part.mime_type, audio_part.filename, audio_part.duration) == ("audio/wav", "recado.wav", 4.0)
    assert isinstance(document_part, DocumentPart) and document_part.data == b"%PDF fixture" and document_part.url.startswith(links)
    assert (document_part.filename, document_part.size_bytes) == ("nota.pdf", len(b"%PDF fixture"))
    assert again == image_part
    # Production message of media without text: one marker per part.
    assert seen["message"] == "[image] [audio] [document: nota.pdf] [image]"
    # One link per file for the execution; the bearer links are never logged.
    assert issued == [image["artifact_id"], audio["artifact_id"], document["artifact_id"]]
    for part in (image_part, audio_part, document_part):
        assert part.url not in caplog.text and part.url.rsplit("/", 1)[1] not in caplog.text


async def test_link_route_off_fails_the_delivery_loudly(monkeypatch, caplog):
    """No link route: the delivery fails and the log names the Maestro settings; the handler never runs."""

    def off(self, request, execution_id, artifact_id):
        raise fake_module._CallbackError(404, "NOT_FOUND")

    monkeypatch.setattr(FakeMaestro, "_artifact_link", off)
    ran = []

    async def handler(on, send, memory, analytics, settings):
        ran.append(on)
        yield send.text("never")

    maestro = FakeMaestro()
    adapter = create_text_delivery_adapter(maestro.config, handler, _resources())
    with maestro.serving():
        thread_id, person = _new_thread(maestro, None)
        note = maestro.upload(USER, b"nota fiscal", filename="nota.txt", mime_type="text/plain")
        trigger = maestro._append(thread_id, person, {"client_event_id": "client-1", "content": [note], "mentions": []})
        maestro._link_attachments(trigger)
        (delivery,) = maestro.deliveries_for(trigger)
        response = await maestro.deliver(adapter, delivery)
    assert ran == []
    assert (response.status_code, response.json()["error"]["code"]) == (422, "UNSUPPORTED_CAPABILITY")
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any(all(setting in message for setting in SETTINGS) for message in errors)


# -- Loopback fixture (``prepare``) --


@contextmanager
def _hydrated(data: bytes = b"hello", mime_type: str = "text/plain"):
    async def hydrate(self, reference, **kwargs):
        return MaestroHydratedArtifact(reference.artifact_id, reference.filename or "attachment", mime_type, data)

    with patch.object(MaestroArtifactsClient, "hydrate", hydrate):
        yield


def _attachment_page(page: dict[str, Any]) -> dict[str, Any]:
    part = {"type": "attachment", "artifact_id": str(uuid4()), "filename": "nota.txt", "mime_type": "text/plain", "size_bytes": 5}
    page["trigger_event"]["payload"]["content"] = [part]
    page["history"]["items"][0]["payload"]["content"] = [deepcopy(part)]
    return part


@pytest.mark.parametrize("route", ["operation_missing", "not_found", "forbidden", "unsupported_capability"])
async def test_link_issue_unavailable_raises_configuration_error(key, bundle, route, caplog):
    change = None if route == "operation_missing" else with_links
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server, change=change)
        _attachment_page(page)
        status = {"not_found": 404, "forbidden": 403, "unsupported_capability": 422}.get(route, 200)
        error = {"error": {"code": "UNSUPPORTED_CAPABILITY", "message": "off", "retryable": False, "request_id": str(uuid4())}}
        server.response = lambda first: error if "/links " in first else page
        server.status = lambda first: status if "/links " in first else 200
        with _hydrated(), pytest.raises(MaestroArtifactLinkUnavailable) as raised:
            async with maestro_handler_context(memory, private):
                pytest.fail("a media part without its link must never reach the handler")
    assert raised.value.code == "UNSUPPORTED_CAPABILITY"
    assert all(setting in str(raised.value) for setting in SETTINGS)
    assert any(all(setting in record.getMessage() for setting in SETTINGS) for record in caplog.records)
    assert any("/links " in first for first, _ in server.requests) is (route != "operation_missing")
    # 403/404 end the reads of the execution, as every other 403/404 of the client does.
    assert memory._client._closed is (route in {"not_found", "forbidden"})


async def test_link_issue_posts_empty_body_with_execution_capability(key, bundle):
    async with fixture_server() as server:
        memory, page, _, token = prepare(key, bundle, server, change=with_links)
        part = _attachment_page(page)
        server.response = lambda first: LINK if "/links " in first else page
        with _hydrated():
            async with maestro_handler_context(memory, private) as prepared:
                assert prepared.on.content[0].url == LINK["url"] and prepared.on.content[0].data == b"hello"
    (first, headers), *_ = [(first, headers) for first, headers in server.requests if "/links " in first]
    execution_id = memory._envelope["execution_id"]
    assert first == f"POST /api/v1/executions/{execution_id}/artifacts/{part['artifact_id']}/links HTTP/1.1"
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Maestro-Execution-Capability"] == memory._client._capability


async def test_link_resolver_refreshes_before_expiry(caplog):
    start = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    clock = [start]
    issued: list[str] = []
    first_file, second_file = str(uuid4()), str(uuid4())

    async def issue(artifact_id):
        issued.append(artifact_id)
        await asyncio.sleep(0)  # yield, so a second concurrent call would ask again without the lock
        expires_at = (clock[0] + timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
        return {"url": f"https://maestro.example/v1/artifact-links/{len(issued)}.secret", "expires_at": expires_at}

    caplog.set_level(logging.DEBUG)
    links = LinkResolver(issue, now=lambda: clock[0])
    link = await links.url_for(first_file)
    clock[0] = start + timedelta(seconds=3540)
    assert await links.url_for(first_file) == link  # 60 s left: still handed out
    clock[0] = start + timedelta(seconds=3541)
    renewed = await links.url_for(first_file)  # 59 s left: a new link
    assert renewed != link and issued == [first_file, first_file]
    assert await links.url_for(second_file) not in {link, renewed}
    third = str(uuid4())
    together = await asyncio.gather(links.url_for(third), links.url_for(third))
    assert together[0] == together[1] and issued.count(third) == 1
    assert "secret" not in repr(links) and "secret" not in caplog.text


async def test_agent_author_gets_empty_user(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        actor = {
            "kind": "agent",
            "principal_id": str(uuid4()),
            "instance_agent_id": str(uuid4()),
            "participant_id": str(uuid4()),
            "display_name": "Outro agente",
        }
        page["trigger_event"]["actor"] = page["history"]["items"][0]["actor"] = actor
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user == User(user_id="")
            assert current_maestro_context().actor == actor
            assert current_execution_context().on_behalf is False


async def test_personal_workspace_is_empty_string(key, bundle):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(key, bundle, server)
        assert memory._envelope["workspace_id"] is None
        async with maestro_handler_context(memory, private) as prepared:
            context = prepared.on.context
            assert context.workspace_id == "" and current_execution_context().workspace_id == ""
            # No thread.created in this context: the production defaults of a web chat thread.
            thread = (context.channel, context.thread_title, context.thread_created_at, context.queue)
            assert thread == ("dooers-platform", None, None, None)
            assert context.queues == [] and context.channel_meta is None


async def test_external_contact_is_the_user_and_never_on_behalf(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        actor = {"kind": "external", "external_id": "+5511999990000", "channel": "whatsapp", "display_name": "Cliente WhatsApp"}
        page["trigger_event"]["actor"] = page["history"]["items"][0]["actor"] = actor
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user == User(user_id="+5511999990000", user_name="Cliente WhatsApp")
            assert prepared.on.context.channel == "whatsapp"
            assert current_execution_context().on_behalf is False and current_execution_context().user_id == ""


async def test_person_email_comes_from_the_participant_when_the_actor_has_none(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["participants"][0].update(email="pessoa@example.com", identity_ids=["idp-pessoa"])
        async with maestro_handler_context(memory, private) as prepared:
            user = prepared.on.context.user
            assert (user.user_id, user.user_name) == (page["trigger_event"]["actor"]["principal_id"], "Pessoa")
            assert (user.user_email, user.identity_ids) == ("pessoa@example.com", ["idp-pessoa"])


async def test_person_without_email_has_none_like_production(key, bundle):
    """Production: ``user_email=ctx.user.email or None``; an empty e-mail is ``None``."""
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["trigger_event"]["actor"]["email"] = ""
        page["participants"][0]["email"] = "outro@example.com"
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user.user_email is None


def _form_response_page(page: dict[str, Any], envelope: dict[str, Any], *, author: str) -> str:
    form_id = str(uuid4())
    form = deepcopy(page["trigger_event"])
    form.update(event_id=form_id, sequence="9", root_event_id=form_id, type="form.created")
    form["actor"] = {
        "kind": "agent",
        "principal_id": author,
        "instance_agent_id": author,
        "participant_id": envelope["recipient_participant_id"],
        "display_name": "Assistente",
    }
    form["payload"] = {"message": "Cadastro", "elements": [], "submit_label": "Enviar", "cancel_label": "Cancelar", "size": "small"}
    trigger = page["trigger_event"]
    trigger.update(type="form.responded", payload={"form_event_id": form_id, "values": {}, "cancelled": True})
    page["history"]["items"] = [form, deepcopy(trigger)]
    return form_id


async def test_form_response_reads_the_form_of_this_agent(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        form_id = _form_response_page(page, memory._envelope, author=memory._envelope["instance_agent_id"])
        async with maestro_handler_context(memory, private) as prepared:
            on = prepared.on
            assert (on.form_data, on.form_cancelled, on.form_event_id) == ({}, True, form_id)
            assert (on.message, on.content) == ("", [TextPart(text="")])


async def test_form_response_to_another_agents_form_is_rejected(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _form_response_page(page, memory._envelope, author=str(uuid4()))
        with pytest.raises(MaestroContextError, match="CONTEXT_INVALID"):
            async with maestro_handler_context(memory, private):
                pytest.fail("not this agent's form")


async def test_form_response_invocation_must_be_the_trigger(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _form_response_page(page, memory._envelope, author=memory._envelope["instance_agent_id"])
        page["invocation_event"] = deepcopy(page["trigger_event"])
        memory._validate_page(page)
        page["invocation_event"]["payload"]["cancelled"] = False
        with pytest.raises(MaestroContextError, match="CONTEXT_INVALID"):
            memory._validate_page(page)


async def test_operator_queue_catalog_reaches_the_handler(key, bundle):
    from tests.parity.model import QUEUES

    async with fixture_server() as server:
        memory, _, _, _ = prepare(key, bundle, server)

        async def resources(scope):
            return MaestroPrivateResources(scope, {}, {}, {}, queues=QUEUES)

        async with maestro_handler_context(memory, resources) as prepared:
            assert prepared.on.context.queues == list(QUEUES)
            assert prepared.on.context.queues[0] is not QUEUES[0]

    async with fixture_server() as server:
        memory, _, _, _ = prepare(key, bundle, server)

        async def invalid(scope):
            return MaestroPrivateResources(scope, {}, {}, {}, queues=({"slug": "suporte"},))

        with pytest.raises(MaestroContextError, match="CONTEXT_INVALID"):
            async with maestro_handler_context(memory, invalid):
                pytest.fail("invalid catalog")


async def test_replace_deepcopy_and_pickle_keep_working(key, bundle):
    """The creator's dataclass operations behave as in production; replace and copies keep context.maestro."""
    async with fixture_server() as server:
        memory, _, _, _ = prepare(key, bundle, server)
        async with maestro_handler_context(memory, private) as prepared:
            on = prepared.on
            metadata = on.context.maestro
            assert metadata is current_maestro_context()
            names = [f.name for f in dataclasses.fields(AgentContext)]
            assert sorted(vars(on.context)) == sorted(names) and isinstance(on.context, AgentContext)
            assert repr(on.context).startswith("AgentContext(thread_id=") and "maestro" not in repr(on)
            assert list(dataclasses.asdict(on)["context"]) == names

            moved = dataclasses.replace(on.context, queue="suporte")
            assert moved.maestro is metadata and moved.queue == "suporte" and moved != on.context
            rag = current_execution_context()
            other = dataclasses.replace(on, message="outra")
            # Still a Maestro context: no second RAG binding (the token stays in maestro_handler_context).
            assert other.context.maestro is metadata and current_execution_context() is rag

            assert copy.copy(on.context).maestro is metadata and copy.copy(on.context) == on.context
            deep = copy.deepcopy(on)
            assert deep == on and deep.context is not on.context and deep.context.maestro is metadata
            assert deep.context.user is not on.context.user

            restored = pickle.loads(pickle.dumps(on))
            # Pickle stores the production object: the metadata only exists during the execution.
            assert restored == on and on == restored
            assert type(restored.context) is AgentContext and restored.context.maestro is None
            assert dataclasses.asdict(restored) == dataclasses.asdict(on)


@pytest.mark.parametrize(
    "content",
    [
        [TextPart(text="Olá"), TextPart(text="  "), TextPart(text="mundo")],
        [TextPart(text=""), ContactPart(display_name=" Bia ")],
        [ContactPart(display_name="")],
        [
            ImagePart(mime_type="image/png"),
            AudioPart(data=b"", mime_type="audio/ogg"),
            DocumentPart(data=b"", mime_type="text/plain", filename="", size_bytes=0),
        ],
        [TextPart(text=""), ImagePart(mime_type="image/png")],
        [],
    ],
)
def test_message_is_the_production_extraction(content):
    assert _production_message(content) == HandlerPipeline._extract_message(None, content)


async def test_message_uses_the_production_function(key, bundle, monkeypatch):
    """No copy of the rule: ``on.message`` is what production's ``_extract_message`` returns."""
    seen = []

    def production(self, content):
        seen.append(list(content))
        return "production rule"

    monkeypatch.setattr(HandlerPipeline, "_extract_message", production)
    async with fixture_server() as server:
        memory, _, _, _ = prepare(key, bundle, server)
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.message == "production rule"
            assert seen == [prepared.on.content]
