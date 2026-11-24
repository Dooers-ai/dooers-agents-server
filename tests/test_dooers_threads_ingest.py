"""``AgentServer.ingest`` and ``AgentServer.chat_upload`` in Dooers Threads mode (plan 5, task 6a).

Each comparison runs the same call twice: on a production ``AgentServer`` (``thread_management="agent"``, rows kept as
``PostgresPersistence`` keeps them by ``tests/parity/memory_persistence.py``) and on a Dooers Threads-mode ``AgentServer`` over the
loopback agent API (``tests/parity/fake_agent_api.py``). The ``Thread`` and the ``ThreadEvent`` each one reads back are compared.
What differs is declared here (and in "ingest e chat_upload pela API do agente" of the Dooers Threads README):

- times are the server's clock (``<time>``) and event ids are labels (the SDK event id is the Dooers Threads ``client_event_id``);
- the order of ``Thread.users`` follows the Dooers Threads join order: the agent comes before the contact of a channel thread
  (production lists the contact first);
- a file of ``ref_id`` is an artifact in Dooers Threads: the part has the artifact id as ``ref_id``, the temporary link as ``url`` and the
  MIME type Dooers Threads returns (production: the local id, no URL and the caller's MIME type);
- a URL part Dooers Threads could not fetch is a ``media_link``: no ``ref_id`` and no ``size_bytes``;
- the author of a user event comes from the person (an ``author`` argument is not stored);
- ``ingest(actor="assistant")`` raises the production ``ValidationError`` (F13); production has already created the thread and
  Dooers Threads writes nothing;
- no analytics event, no WebSocket broadcast (Dooers Threads is the authority), no guest metadata enrichment (plan 6);
- the channels Dooers Threads has an external contact for are ``whatsapp`` and ``dooers-public-chat``; any other channel, and a person
  without an id, raise ``DooersThreadsAgentUnsupported`` before anything is written.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

import dooers.agents.server.dooers_threads.ingress as ingress_module
import dooers.agents.server.observability.otel as otel
import dooers.agents.server.server as server_module
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.dooers_threads.agent_api import DooersThreadsAgentApi, DooersThreadsAgentApiError, DooersThreadsAgentScopeError
from dooers.agents.server.dooers_threads.agent_persistence import DooersThreadsAgentUnsupported
from dooers.agents.server.dooers_threads.media_fetch import FetchedMedia, MediaFetchError
from dooers.agents.server.features.settings.models import SettingsSchema
from dooers.agents.server.handlers.pipeline import UploadReferenceError
from dooers.agents.server.protocol.models import User
from dooers.agents.server.server import AgentServer
from tests.parity.fake_agent_api import AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, FakeAgentApi
from tests.parity.memory_persistence import MemoryPersistence
from tests.test_dooers_threads_agent_persistence import ANA, BIA, core_person, plain

ASSISTANT = "Assistente"
PHONE = "+5511999990000"
KEY = f"wa:inst:i1:{PHONE}"
CONTACT = User(user_id=PHONE, user_name="Maria")
META = {"whatsapp": {"instance_id": "i1", "from_e164": PHONE, "agent_phone_e164": "+5511888880000", "wa_message_id": "wamid.in.1"}}
PNG = b"\x89PNG\r\n\x1a\ndooers-threads-ingest"
PHOTO_URL = "https://cdn.example.com/foto.png"
COMMON = {"organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID}


class CanonicalMimeApi(FakeAgentApi):
    """Dooers Threads stores the canonical MIME type of a file and returns it: here ``image/png`` is stored as ``image/webp``."""

    def _reply(self, schema: str, value: Any, status: int, path: str):  # type: ignore[no-untyped-def]
        if schema == "Public.AgentArtifact" and value["mime_type"] == "image/png":
            value = {**value, "mime_type": "image/webp"}
        return super()._reply(schema, value, status, path)


def agent_config(**values: Any) -> AgentConfig:
    base: dict[str, Any] = {
        "database_type": "postgres",
        "database_host": "localhost",
        "database_port": 5432,
        "database_user": "ingest",
        "database_name": "ingest",
        "database_password": "",
        "database_key": "",
        "database_ssl": False,
        "database_instance_uri": "",
        "assistant_name": ASSISTANT,
        "analytics_enabled": False,
        "guest_thread_cleanup_interval_seconds": 0,
        "agent_seed_secret": "",
        "store_chat_uploads": False,
        "chat_storage_service": "none",
        "gcp_storage_bucket": "",
        "dooers_storage_prefix": "",
        "azure_storage_connection_string": "",
        "azure_storage_container": "",
        "agent_core_base_url": "https://core.example.test",
        "otel_service_url": "",
        "otel_service_name": "",
    }
    return AgentConfig(**{**base, **values})


class World:
    """A production ``AgentServer`` and a Dooers Threads-mode one, each over its own thread authority."""

    def __init__(self, fake: FakeAgentApi, prod_db: MemoryPersistence, local: MemoryPersistence, **config: Any) -> None:
        self.fake, self.prod_db, self.local = fake, prod_db, local
        self.prod = AgentServer(agent_config(**config))
        self.dooers_threads = AgentServer(agent_config(thread_management="dooers_threads", dooers_threads=fake.config(), **config))

    def posts(self, suffix: str = "") -> list[str]:
        return [r.path for r in self.fake.requests if r.method == "POST" and r.path.endswith(suffix)]

    def dooers_threads_thread(self, key: str):  # type: ignore[no-untyped-def]
        return next(t for t in self.fake.threads.values() if t.external_key == key)

    async def read(self, server: AgentServer, thread_id: str) -> tuple[Any, Any]:
        repository = await server.repository()
        return await repository.get_thread(thread_id), await repository.list_events({"thread_id": thread_id}, order={"direction": "asc"})


@pytest.fixture
async def fake():
    server = await CanonicalMimeApi(workspace_id=WORKSPACE_ID).start()
    for person in (ANA, BIA):
        server.core_people[person.user_id] = core_person(person)
    try:
        yield server
        assert server.failures == []  # a response of the fake that is not contract-valid is a bug of the fake
    finally:
        await server.stop()


async def make_world(monkeypatch, fake: FakeAgentApi, **config: Any):
    prod_db, local = MemoryPersistence(), MemoryPersistence()
    monkeypatch.setattr(server_module, "_build_persistence", lambda c: local if c.thread_management == "dooers_threads" else prod_db)
    monkeypatch.setattr(otel, "init_otel", lambda **_kwargs: None)
    monkeypatch.setattr(otel, "_otel_enabled", False)
    return World(fake, prod_db, local, **config)


@pytest.fixture
async def world(monkeypatch, fake):
    built = await make_world(monkeypatch, fake)
    yield built
    await built.prod.close()
    await built.dooers_threads.close()


def labelled(thread: Any, events: Any) -> dict[str, Any]:
    labels = {thread.id: "thread", **{event.id: f"event-{number}" for number, event in enumerate(events, start=1)}}
    return {"thread": plain(thread, labels), "events": plain(events, labels)}


@contextmanager
def media_served(media: dict[str, tuple[bytes, str]] | None = None) -> Iterator[None]:
    """Media the SDK can fetch: the served URLs answer, any other fails like an unreachable host (``fetch_media`` has its own tests)."""
    media = media or {}

    async def fetch(url: str, *, hint_mime: str | None = None, hint_filename: str | None = None, **_limits: Any) -> FetchedMedia:
        if url not in media:
            raise MediaFetchError("network", "offline test: no DNS")
        data, content_type = media[url]
        return FetchedMedia(data=data, mime_type=hint_mime or content_type, filename=hint_filename or urlsplit(url).path.rsplit("/", 1)[-1])

    with patch.object(ingress_module, "fetch_media", fetch):
        yield


# -- the thread id --


async def test_ingest_returns_callers_thread_id(world):
    first = await world.dooers_threads.ingest(
        AGENT_ID, "Oi", user=CONTACT, thread_id=KEY, actor="user", channel="whatsapp", channel_meta=META, **COMMON
    )
    again = await world.dooers_threads.ingest(
        AGENT_ID, "De novo", user=CONTACT, thread_id=KEY, actor="user", channel="whatsapp", channel_meta=META, **COMMON
    )
    assert first == again == KEY
    # The key is the thread's external key: one thread, two messages. The SDK id of a caller without one is the key too.
    assert [t.external_key for t in world.fake.threads.values()] == [KEY]
    assert len(world.dooers_threads_thread(KEY).events) == 2
    generated = await world.dooers_threads.ingest(AGENT_ID, "Sem id", user=ANA, actor="user", **COMMON)
    assert generated != KEY and world.dooers_threads_thread(generated).external_key == generated
    # A Dooers Threads id of a thread that has no key (made by the web chat) continues that thread.
    web = await world.dooers_threads._dooers_threads_agent_persistence()._api.create_thread(workspace_id=WORKSPACE_ID)
    assert web["external_key"] is None
    assert (
        await world.dooers_threads.ingest(AGENT_ID, "Na web", user=ANA, thread_id=web["thread_id"], actor="user", **COMMON)
        == web["thread_id"]
    )
    assert len(world.fake.threads) == 3 and len(world.fake.threads[web["thread_id"]].events) == 2  # participant.added + the message


async def test_ingest_matches_production_on_a_platform_thread(world):
    for server in (world.prod, world.dooers_threads):
        for user, text in ((ANA, "Primeira"), (ANA, "Segunda"), (BIA, "Outra pessoa")):
            returned = await server.ingest(
                AGENT_ID, text, user=user, thread_id="atendimento-1", thread_title="Atendimento", actor="user", **COMMON
            )
            assert returned == "atendimento-1"
    production = labelled(*await world.read(world.prod, "atendimento-1"))
    dooers_threads = labelled(*await world.read(world.dooers_threads, "atendimento-1"))
    assert dooers_threads == production
    # The comparison is not between two empty values.
    assert [e["content"][0]["text"] for e in production["events"]] == ["Primeira", "Segunda", "Outra pessoa"]
    assert [u["user_id"] for u in production["thread"]["users"]] == [ANA.user_id, AGENT_ID, BIA.user_id]
    assert production["thread"]["title"] == "Atendimento" and production["thread"]["owner"]["user_id"] == ANA.user_id
    assert [e["author"] for e in production["events"]] == ["Ana Souza", "Ana Souza", "Bia Lima"]


async def test_ingest_without_thread_id_is_a_new_thread_with_the_sdk_id(world):
    prod_id = await world.prod.ingest(AGENT_ID, "Oi", user=ANA, actor="user", **COMMON)
    dooers_threads_id = await world.dooers_threads.ingest(AGENT_ID, "Oi", user=ANA, actor="user", **COMMON)
    production = labelled(*await world.read(world.prod, prod_id))
    dooers_threads = labelled(*await world.read(world.dooers_threads, dooers_threads_id))
    assert dooers_threads == production


# -- WhatsApp --


async def test_ingest_on_a_whatsapp_channel_creates_the_external_participant(world):
    await world.dooers_threads.ingest(
        AGENT_ID, "Oi, tudo bem?", user=CONTACT, thread_id=KEY, actor="user", channel="whatsapp", channel_meta=META, **COMMON
    )
    thread = world.dooers_threads_thread(KEY)
    contacts = [p for p in thread.participants if p["kind"] == "external"]
    assert [(p["channel"], p["external_id"], p["display_name"]) for p in contacts] == [("whatsapp", PHONE, "Maria")]
    assert thread.channel == "whatsapp" and thread.channel_meta == META
    message = next(e for e in thread.events if e["type"] == "message.created")
    assert message["actor"]["kind"] == "external" and message["actor"]["external_id"] == PHONE
    assert message["actor"]["participant_id"] == contacts[0]["participant_id"]
    # Created with the thread: no add_participant, and the same contact again adds nobody.
    await world.dooers_threads.ingest(
        AGENT_ID, "De novo", user=CONTACT, thread_id=KEY, actor="user", channel="whatsapp", channel_meta=META, **COMMON
    )
    assert world.posts("/participants") == []
    # Another contact in the same thread joins it (production: upsert_thread_participant).
    other = User(user_id="+5511977770000", user_name="João")
    await world.dooers_threads.ingest(
        AGENT_ID, "Sou outro", user=other, thread_id=KEY, actor="user", channel="whatsapp", channel_meta=META, **COMMON
    )
    assert len(world.posts("/participants")) == 1
    assert {p["external_id"] for p in thread.participants if p["kind"] == "external"} == {PHONE, other.user_id}


async def test_ingest_whatsapp_matches_production(world):
    for server in (world.prod, world.dooers_threads):
        await server.ingest(
            AGENT_ID,
            "Mensagem do cliente",
            user=CONTACT,
            thread_id=KEY,
            actor="user",
            data={"origem": "whatsapp_peer"},
            channel="whatsapp",
            channel_meta=copy.deepcopy(META),
            **COMMON,
        )
    production = labelled(*await world.read(world.prod, KEY))
    dooers_threads = labelled(*await world.read(world.dooers_threads, KEY))
    # Declared: production lists the contact before the agent; Dooers Threads orders by join.
    for view in (production, dooers_threads):
        view["thread"]["users"].sort(key=lambda user: user["user_id"])
    assert dooers_threads == production
    assert production["thread"]["owner"]["user_id"] == PHONE and production["events"][0]["user"]["user_name"] == "Maria"


# -- data, source and the production event --


async def test_ingest_stores_data_and_source_as_production_does(world):
    meta = copy.deepcopy(META)
    meta["whatsapp"].update(from_me=True, self_chat=False)
    for server in (world.prod, world.dooers_threads):
        await server.ingest(
            AGENT_ID,
            "Resposta do atendente",
            user=CONTACT,
            thread_id=KEY,
            actor="user",
            author="Maria",
            data={"origem": "whatsapp_peer", "n": 1},
            channel="whatsapp",
            channel_meta=copy.deepcopy(meta),
            **COMMON,
        )
    production_events = (await world.read(world.prod, KEY))[1]
    dooers_threads_events = (await world.read(world.dooers_threads, KEY))[1]
    expected = {
        "origem": "whatsapp_peer",
        "n": 1,
        "source": {
            "channel": "whatsapp",
            "from_e164": PHONE,
            "to_e164": "+5511888880000",
            "instance_id": "i1",
            "from_me": True,
            "self_chat": False,
            "wa_message_id": "wamid.in.1",
        },
    }
    assert production_events[0].data == expected
    assert dooers_threads_events[0].data == expected
    assert dooers_threads_events[0].model_dump(exclude={"id", "created_at"}) == production_events[0].model_dump(
        exclude={"id", "created_at"}
    )
    # The platform channel has no ``source``; the caller's data is all there is, and none is no ``data``.
    for server in (world.prod, world.dooers_threads):
        await server.ingest(AGENT_ID, "Com dados", user=ANA, thread_id="t-data", actor="user", data={"a": [1, {"b": None}]}, **COMMON)
        await server.ingest(AGENT_ID, "Sem dados", user=ANA, thread_id="t-data", actor="user", **COMMON)
    prod_data = [e.data for e in (await world.read(world.prod, "t-data"))[1]]
    dooers_threads_data = [e.data for e in (await world.read(world.dooers_threads, "t-data"))[1]]
    assert prod_data == dooers_threads_data == [{"a": [1, {"b": None}]}, None]


@pytest.mark.parametrize("actor", ["assistant", "system", "tool", "robot"])
async def test_ingest_of_a_non_user_actor_raises_the_production_validation_error(world, actor):
    with pytest.raises(ValidationError) as production:
        await world.prod.ingest(AGENT_ID, "Resposta", user=ANA, thread_id="t-f13", actor=actor, **COMMON)
    with pytest.raises(ValidationError) as dooers_threads:
        await world.dooers_threads.ingest(AGENT_ID, "Resposta", user=ANA, thread_id="t-f13", actor=actor, **COMMON)
    assert dooers_threads.value.errors() == production.value.errors()
    assert dooers_threads.value.title == production.value.title == "ThreadEvent"
    # Declared: Dooers Threads writes nothing (production has created the empty thread).
    assert world.fake.requests == [] and world.fake.threads == {}
    assert await world.prod_db.get_thread("t-f13") is not None


async def test_ingest_default_actor_is_the_production_error_too(world):
    # production's docstring says "assistant" is the default actor; F13 stays a user decision (reported, not fixed).
    with pytest.raises(ValidationError) as production:
        await world.prod.ingest(AGENT_ID, "Resposta", user=ANA, thread_id="t-f13", author="Maria", **COMMON)
    with pytest.raises(ValidationError) as dooers_threads:
        await world.dooers_threads.ingest(AGENT_ID, "Resposta", user=ANA, thread_id="t-f13", author="Maria", **COMMON)
    assert dooers_threads.value.errors() == production.value.errors()


# -- the agent --


@pytest.mark.parametrize("agent_id", ["", AGENT_ID, AGENT_ID.upper()])
async def test_ingest_accepts_this_agent(world, agent_id):
    assert await world.dooers_threads.ingest(agent_id, "Oi", user=ANA, thread_id="t-agent", actor="user", **COMMON) == "t-agent"
    assert len(world.dooers_threads_thread("t-agent").events) == 1
    assert world.dooers_threads_thread("t-agent").events[-1]["payload"]["content"] == [{"type": "text", "text": "Oi"}]


async def test_ingest_of_another_agent_is_a_scope_error(world):
    for call in (
        {"agent_id": "w1"},
        {"agent_id": "44444444-4444-4444-8444-444444444444"},
        {"agent_id": AGENT_ID, "organization_id": "11111111-1111-4111-8111-111111111111"},
        {"agent_id": AGENT_ID, "workspace_id": ""},
    ):
        with pytest.raises(DooersThreadsAgentScopeError):
            await world.dooers_threads.ingest(**{"message": "Oi", "user": ANA, "actor": "user", **COMMON, **call})
    assert world.fake.requests == []


# -- what Dooers Threads cannot express: loud, and nothing written --


@pytest.mark.parametrize(
    "call",
    [
        {"user": User(user_id=""), "actor": "user"},
        {"user": None, "actor": "user"},
        {"user": CONTACT, "actor": "user", "channel": "slack"},
        {"user": User(user_id="", user_email="ana@example.com"), "actor": "user", "channel": "whatsapp"},
    ],
)
async def test_ingest_without_a_person_or_with_an_unknown_channel_is_unsupported(world, call):
    with pytest.raises(DooersThreadsAgentUnsupported):
        await world.dooers_threads.ingest(AGENT_ID, "Oi", thread_id="t-loud", **COMMON, **call)
    assert world.fake.requests == [] and world.fake.threads == {}


# -- files --


async def test_ingest_ref_id_part_is_uploaded_with_the_mime_type_dooers_threads_returns(world):
    upload = await world.dooers_threads.chat_upload(data=PNG, filename="foto.png", mime_type="image/png", agent_id=AGENT_ID)
    content = [
        {"type": "text", "text": "Segue a foto"},
        {"type": "image", "ref_id": upload["ref_id"], "filename": "foto.png", "mime_type": "image/png"},
    ]
    assert world.fake.requests == []  # chat_upload sends nothing to Dooers Threads
    await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-file", actor="user", content=content, **COMMON)
    thread = world.dooers_threads_thread("t-file")
    (artifact,) = world.fake.artifacts.values()
    assert artifact["thread_id"] == thread.id and artifact["data"] == PNG
    message = thread.events[-1]
    assert message["payload"]["content"] == [
        {"type": "text", "text": "Segue a foto"},
        {
            "type": "attachment",
            "artifact_id": artifact["artifact_id"],
            "mime_type": "image/webp",
            "filename": "foto.png",
            "size_bytes": len(PNG),
        },
    ]
    # The file left the local store: a ref_id is single use, as in production.
    assert world.dooers_threads.upload_store.consume(upload["ref_id"]) is None
    # Read back, the part is the person's upload: the artifact id is its ref_id and Dooers Threads' MIME type is its type.
    _, events = await world.read(world.dooers_threads, "t-file")
    image = events[0].content[1]
    assert (image.type, image.mime_type, image.filename, image.ref_id) == ("image", "image/webp", "foto.png", artifact["artifact_id"])
    assert image.url and events[0].content[0].text == "Segue a foto"


async def test_ingest_ref_id_part_matches_production_but_for_the_declared_file_fields(world):
    contents = {}
    for name, server in (("production", world.prod), ("dooers_threads", world.dooers_threads)):
        upload = await server.chat_upload(data=PNG, filename="foto.png", mime_type="image/png", agent_id=AGENT_ID)
        contents[name] = [{"type": "image", "ref_id": upload["ref_id"], "filename": "foto.png", "mime_type": "image/png"}]
        await server.ingest(AGENT_ID, "Foto", user=ANA, thread_id="t-compare", actor="user", content=contents[name], **COMMON)
    production = labelled(*await world.read(world.prod, "t-compare"))
    dooers_threads = labelled(*await world.read(world.dooers_threads, "t-compare"))
    for view in (production, dooers_threads):
        (part,) = view["events"][0]["content"]
        assert part["ref_id"] and part["type"] == "image"
        # Declared: the id of the file (a local id or an artifact id), the temporary link (production: none) and Dooers Threads'
        # canonical type.
        assert (part["url"] is not None) == (view is dooers_threads)
        part.update(ref_id="<file>", url=None, mime_type="<mime>")
    assert dooers_threads == production


async def test_ingest_unknown_ref_id_is_the_production_error(world):
    content = [{"type": "image", "ref_id": "00000000-0000-4000-8000-0000000000ff", "filename": "foto.png"}]
    with pytest.raises(UploadReferenceError) as production:
        await world.prod.ingest(AGENT_ID, "", user=ANA, thread_id="t-ref", actor="user", content=copy.deepcopy(content), **COMMON)
    with pytest.raises(UploadReferenceError) as dooers_threads:
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-ref", actor="user", content=copy.deepcopy(content), **COMMON)
    assert str(dooers_threads.value) == str(production.value)
    assert world.fake.requests == []


async def test_ingest_bytes_and_contact_parts(world):
    content = [
        {"type": "document", "data": b"%PDF-1.7 dooers_threads", "filename": "nota.pdf", "mime_type": "application/pdf"},
        {"type": "contact", "display_name": "Suporte", "vcard": "BEGIN:VCARD\nEND:VCARD", "phones": [{"phone": "+551130000000"}]},
    ]
    await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-bytes", actor="user", content=content, **COMMON)
    parts = world.dooers_threads_thread("t-bytes").events[-1]["payload"]["content"]
    assert [part["type"] for part in parts] == ["attachment", "contact"]
    assert parts[0]["mime_type"] == "application/pdf" and parts[0]["filename"] == "nota.pdf"
    assert parts[1] == {
        "type": "contact",
        "display_name": "Suporte",
        "vcard": "BEGIN:VCARD\nEND:VCARD",
        "phones": [{"phone": "+551130000000"}],
    }


async def test_ingest_media_url_part_is_fetched_and_uploaded(world):
    content = [{"type": "image", "url": PHOTO_URL, "mime_type": "image/png", "filename": "foto.png"}]
    with media_served({PHOTO_URL: (PNG, "image/png")}):
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-url", actor="user", content=content, **COMMON)
    (artifact,) = world.fake.artifacts.values()
    assert artifact["data"] == PNG
    (part,) = world.dooers_threads_thread("t-url").events[-1]["payload"]["content"]
    assert part == {
        "type": "attachment",
        "artifact_id": artifact["artifact_id"],
        "mime_type": "image/webp",
        "filename": "foto.png",
        "size_bytes": len(PNG),
    }


@pytest.mark.parametrize("failure", ["unreachable", "refused_by_dooers_threads", "type_outside_the_policy"])
async def test_ingest_media_url_that_cannot_be_uploaded_becomes_a_media_link(world, failure):
    content = [{"type": "image", "url": PHOTO_URL, "mime_type": "image/png", "filename": "foto.png"}]
    media = {PHOTO_URL: (PNG, "image/png")}
    if failure == "unreachable":
        media = {}  # no DNS: the fetch fails
    elif failure == "refused_by_dooers_threads":
        # The fake Dooers Threads is told to refuse a gif with VALIDATION_FAILED.
        world.fake.refused_mime_types.add("image/gif")
        media = {PHOTO_URL: (b"GIF89a", "image/gif")}
        content = [{"type": "image", "url": PHOTO_URL, "mime_type": "image/gif", "filename": "foto.gif"}]
    else:
        media = {PHOTO_URL: (b"MZ\x90\x00", "application/x-msdownload")}
        content = [{"type": "document", "url": PHOTO_URL, "filename": "programa.exe"}]
    with media_served(media):
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-link", actor="user", content=content, **COMMON)
    assert world.fake.artifacts == {}
    (part,) = world.dooers_threads_thread("t-link").events[-1]["payload"]["content"]
    assert part["type"] == "media_link" and part["url"] == PHOTO_URL and part["kind"] == content[0]["type"]
    # The conversation continues and the read-back is the production URL-only part.
    _, events = await world.read(world.dooers_threads, "t-link")
    assert events[0].content[0].url == PHOTO_URL and events[0].content[0].ref_id is None


@pytest.mark.parametrize(
    "url",
    ["ftp://files.example.com/foto.png", "data:image/png;base64,@@@@", "file:///etc/passwd"],
)
async def test_ingest_media_url_that_cannot_be_fetched_keeps_the_url(world, url):
    """Production stores any URL text: one that is neither http(s) nor a data URL that decodes is a ``media_link`` as it came."""
    content = [{"type": "image", "url": url, "filename": "foto.png"}]
    with media_served():
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-ftp", actor="user", content=content, **COMMON)
    assert world.fake.artifacts == {}
    (part,) = world.dooers_threads_thread("t-ftp").events[-1]["payload"]["content"]
    assert part == {"type": "media_link", "kind": "image", "url": url, "filename": "foto.png"}


async def test_ingest_file_of_any_type_is_uploaded_as_production_stores_it(world):
    """Attachments sweep (R1): production stages any file (``UploadStore``); a ``.exe`` is a document attachment."""
    upload = await world.dooers_threads.chat_upload(
        data=b"MZ\x90\x00", filename="programa.exe", mime_type="application/x-msdownload", agent_id=AGENT_ID
    )
    content = [{"type": "document", "ref_id": upload["ref_id"], "filename": "programa.exe", "mime_type": "application/x-msdownload"}]
    await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-exe", actor="user", content=content, **COMMON)
    (artifact,) = world.fake.artifacts.values()
    assert artifact["data"] == b"MZ\x90\x00" and artifact["mime_type"] == "application/x-msdownload"
    (part,) = world.dooers_threads_thread("t-exe").events[-1]["payload"]["content"]
    assert part["type"] == "attachment" and part["filename"] == "programa.exe"


async def test_ingest_file_outside_a_dooers_threads_allowlist_is_refused_before_any_write_and_keeps_the_file(monkeypatch, fake):
    world = await make_world(monkeypatch, fake)
    world.dooers_threads._config.dooers_threads = fake.config(artifact_mime_types=("image/png",))
    try:
        upload = await world.dooers_threads.chat_upload(
            data=b"MZ\x90\x00", filename="programa.exe", mime_type="application/x-msdownload", agent_id=AGENT_ID
        )
        content = [{"type": "document", "ref_id": upload["ref_id"], "filename": "programa.exe", "mime_type": "application/x-msdownload"}]
        with pytest.raises(DooersThreadsAgentApiError, match="REQUEST_INVALID"):
            await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-exe", actor="user", content=content, **COMMON)
        assert world.fake.requests == [] and upload["ref_id"] in world.dooers_threads.upload_store._store
    finally:
        await world.prod.close()
        await world.dooers_threads.close()


# -- a failed ingest does not lose the staged file --


async def stage(world, name: str = "foto.png") -> tuple[str, list[dict[str, Any]]]:
    upload = await world.dooers_threads.chat_upload(data=PNG, filename=name, mime_type="image/png", agent_id=AGENT_ID)
    return upload["ref_id"], [{"type": "image", "ref_id": upload["ref_id"], "filename": name, "mime_type": "image/png"}]


def fail_once(monkeypatch, name: str) -> None:
    original = getattr(DooersThreadsAgentApi, name)
    calls: list[int] = []

    async def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not calls:
            calls.append(1)
            raise DooersThreadsAgentApiError("DEPENDENCY_UNAVAILABLE", status=503, retryable=True)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(DooersThreadsAgentApi, name, wrapper)


@pytest.mark.parametrize("failing_call", ["create_thread", "upload_artifact", "publish_event"])
async def test_failed_ingest_gives_the_staged_file_back_and_the_retry_ends_with_one_message(world, monkeypatch, failing_call):
    ref_id, content = await stage(world)
    fail_once(monkeypatch, failing_call)
    with pytest.raises(DooersThreadsAgentApiError, match="DEPENDENCY_UNAVAILABLE"):
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-retry", actor="user", content=content, **COMMON)
    assert ref_id in world.dooers_threads.upload_store._store  # the same ref_id is still usable
    assert (
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-retry", actor="user", content=content, **COMMON) == "t-retry"
    )
    messages = [e for e in world.dooers_threads_thread("t-retry").events if e["type"] == "message.created"]
    assert len(messages) == 1 and messages[0]["payload"]["content"][0]["type"] == "attachment"
    assert len([t for t in world.fake.threads.values() if t.external_key == "t-retry"]) == 1
    assert world.dooers_threads.upload_store.consume(ref_id) is None  # used by the retry, single use again


async def test_failed_add_participant_gives_the_staged_file_back(world, monkeypatch):
    await world.dooers_threads.ingest(AGENT_ID, "Oi", user=ANA, thread_id="t-member", actor="user", **COMMON)
    ref_id, content = await stage(world)
    fail_once(monkeypatch, "add_participant")
    with pytest.raises(DooersThreadsAgentApiError):
        await world.dooers_threads.ingest(AGENT_ID, "", user=BIA, thread_id="t-member", actor="user", content=content, **COMMON)
    assert ref_id in world.dooers_threads.upload_store._store
    await world.dooers_threads.ingest(AGENT_ID, "", user=BIA, thread_id="t-member", actor="user", content=content, **COMMON)
    assert len([e for e in world.dooers_threads_thread("t-member").events if e["type"] == "message.created"]) == 2


async def test_ingest_of_an_unknown_channel_does_not_consume_the_staged_file(world):
    ref_id, content = await stage(world)
    with pytest.raises(DooersThreadsAgentUnsupported):
        await world.dooers_threads.ingest(
            AGENT_ID, "", user=CONTACT, thread_id=KEY, actor="user", channel="slack", content=content, **COMMON
        )
    assert world.fake.requests == [] and ref_id in world.dooers_threads.upload_store._store
    await world.dooers_threads.ingest(
        AGENT_ID, "", user=CONTACT, thread_id=KEY, actor="user", channel="whatsapp", channel_meta=META, content=content, **COMMON
    )
    assert world.dooers_threads_thread(KEY).events[-1]["payload"]["content"][0]["type"] == "attachment"


async def test_ingest_with_the_production_validation_error_keeps_the_staged_file(world):
    ref_id, content = await stage(world)
    with pytest.raises(ValidationError):
        await world.dooers_threads.ingest(AGENT_ID, "", user=ANA, thread_id="t-f13", actor="assistant", content=content, **COMMON)
    assert world.fake.requests == [] and ref_id in world.dooers_threads.upload_store._store


# -- chat_upload --


async def test_chat_upload_returns_production_fields(world):
    base = {"data": PNG, "filename": "foto.png", "mime_type": "image/png", "agent_id": AGENT_ID}
    production = await world.prod.chat_upload(**base)
    dooers_threads = await world.dooers_threads.chat_upload(**base, thread_id=KEY, source="whatsapp", run_id="run-1")
    assert list(dooers_threads) == list(production) == ["ref_id", "mime_type", "filename", "size_bytes", "size", "public_url"]
    assert {**dooers_threads, "ref_id": None} == {**production, "ref_id": None}
    assert dooers_threads["size_bytes"] == dooers_threads["size"] == len(PNG) and dooers_threads["public_url"] is None
    # The ref_id is the local one: the bytes are in the local store, and Dooers Threads was not called (even for a thread that exists).
    entry = world.dooers_threads.upload_store.consume(dooers_threads["ref_id"])
    assert (entry.data, entry.filename, entry.mime_type, entry.size_bytes) == (PNG, "foto.png", "image/png", len(PNG))
    assert world.fake.requests == [] and world.fake.artifacts == {}


async def test_chat_upload_errors_are_production_errors(world):
    base = {"data": PNG, "filename": "foto.png", "mime_type": "image/png", "agent_id": AGENT_ID}
    limit = 25 * 1024 * 1024
    assert limit == 26214400
    cases = {
        "empty_file": {"data": b""},
        "blank_agent_id": {"agent_id": "  "},
        "over_25_mib": {"data": bytes(limit + 1)},
    }
    for label, change in cases.items():
        raised = []
        for server in (world.prod, world.dooers_threads):
            with pytest.raises(ValueError) as error:
                await server.chat_upload(**{**base, **change})
            raised.append(str(error.value))
        assert raised[0] == raised[1], label
    assert raised[0] == f"File exceeds maximum size of {limit} bytes"
    # Exactly 25 MiB is accepted by both.
    for server in (world.prod, world.dooers_threads):
        assert (await server.chat_upload(**{**base, "data": bytes(limit)}))["size_bytes"] == limit


async def test_chat_upload_enforces_the_file_policy_when_the_creator_persists_attachments(monkeypatch, fake):
    built = await make_world(monkeypatch, fake, settings_schema=SettingsSchema(fields=[]), allowed_content_types=["image"])
    try:
        for db in (built.prod_db, built.local):
            await db.set_settings(AGENT_ID, {"persist_chat_attachments": True})
        raised = []
        for server in (built.prod, built.dooers_threads):
            with pytest.raises(ValueError) as error:
                await server.chat_upload(data=b"%PDF", filename="nota.pdf", mime_type="application/pdf", agent_id=AGENT_ID)
            raised.append(str(error.value))
            assert (await server.chat_upload(data=PNG, filename="foto.png", mime_type="image/png", agent_id=AGENT_ID))["size_bytes"]
        assert raised[0] == raised[1] and "«document»" in raised[0]
    finally:
        await built.prod.close()
        await built.dooers_threads.close()


async def test_chat_upload_of_another_agent_is_a_scope_error(world):
    with pytest.raises(DooersThreadsAgentScopeError):
        await world.dooers_threads.chat_upload(data=PNG, filename="foto.png", mime_type="image/png", agent_id="w1")
    assert world.fake.requests == []
    # Any case of this agent's id is this agent (the ref_id is stored; the agent id is the instance id).
    assert (await world.dooers_threads.chat_upload(data=PNG, filename="foto.png", mime_type="image/png", agent_id=AGENT_ID.upper()))[
        "ref_id"
    ]
