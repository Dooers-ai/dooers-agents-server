"""``MaestroAgentPersistence``: the production ``Repository`` and ``AgentMemory`` over the Maestro agent API.

Each case runs the same calls twice: on the production persistence (``tests/parity/memory_persistence.py``, which
keeps rows as ``PostgresPersistence`` does; the list filters below port ``_build_scope_conditions``) and on
``MaestroAgentPersistence`` over the loopback agent API (``tests/parity/fake_agent_api.py``), and asserts that the
results are equal. What differs is declared in ``DECLARED`` (and in the cases that name a difference):

- times are the server's clock (``<time>``);
- Maestro ``Thread.users`` lists the agent too (it is always a participant); production ``Repository.create_thread``
  stores only the owner;
- a user message has the author derived from the person, as ``HandlerPipeline`` writes it; an assistant message
  without an author has ``assistant_name``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.maestro.agent_api import MaestroAgentApi, MaestroAgentApiError, MaestroAgentScopeError
from dooers.agents.server.maestro.agent_persistence import MaestroAgentPersistence, MaestroAgentUnsupported
from dooers.agents.server.maestro.window import MaestroHistoryLimitError
from dooers.agents.server.persistence.base import FILTERABLE_FIELDS
from dooers.agents.server.protocol.models import Run, Thread, ThreadEvent, User, WireS2C_ImagePart, WireS2C_TextPart
from dooers.agents.server.repository import Repository
from dooers.agents.server.server import AgentServer
from dooers.agents.server.thread_queue import is_unqueued_list_filter
from tests.parity.fake_agent_api import AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, FakeAgentApi
from tests.parity.memory_persistence import MemoryPersistence

ASSISTANT = "Assistente"
ANA = User(
    user_id="user-1",
    user_name="Ana Souza",
    user_email="ana@example.com",
    identity_ids=["idp-ana"],
    system_role="user",
    organization_role="owner",
    workspace_role="manager",
)
BIA = User(user_id="user-2", user_name="Bia Lima", user_email="bia@example.com", identity_ids=["idp-bia"], system_role="user")
CARLA = User(user_id="user-3", user_name="Carla Dias", user_email="carla@example.com", system_role="user")
PHONE = "+5511999990000"
EXTERNAL_KEY = f"wa:inst:i1:{PHONE}"
RUN = "55555555-5555-4555-8555-555555555555"
FAILED_RUN = "66666666-6666-4666-8666-666666666666"
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TIME_FIELDS = {"created_at", "updated_at", "last_event_at", "started_at", "ended_at"}
CORE_FIELDS = ("user_name", "user_email", "identity_ids", "system_role", "organization_role", "workspace_role")


def core_person(user: User) -> dict[str, Any]:
    """What the core tells Maestro about a person (the fake ``core_people`` format)."""
    return {"display_name": user.user_name, "email": user.user_email, **{k: getattr(user, k) for k in CORE_FIELDS[2:]}}


# -- the production reference for the list filters (``PostgresPersistence._build_scope_conditions``) --


def _ilike(value: str, pattern: str) -> bool:
    return pattern.lower() in (value or "").lower()


class ProductionThreads(MemoryPersistence):
    """``MemoryPersistence`` with the thread list filters of ``PostgresPersistence`` (scope, queue, title, cursor)."""

    def _matching(
        self,
        agent_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        scope: str,
        user_email: str | None,
        identity_ids: list[str] | None,
        queue: str | None,
        title_query: str | None,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in self._threads.values():
            if row["agent_id"] != agent_id:
                continue
            users = json.loads(row["users"]) if row["users"] else []
            if scope == "organization":
                if row["organization_id"] != organization_id:
                    continue
                if (workspace_id or "").strip():
                    if row["workspace_id"] != workspace_id:
                        continue
                elif (row["workspace_id"] or "") != "":
                    continue
            elif scope in ("workspace", "member"):
                if row["organization_id"] != organization_id or row["workspace_id"] != workspace_id:
                    continue
                if scope == "member":
                    matches: list[bool] = []
                    if user_id:
                        matches.append(any(u.get("user_id") == user_id for u in users))
                    if user_email:
                        matches.append(any(u.get("user_email") == user_email for u in users))
                    if identity_ids:
                        matches.append(
                            any(u.get("user_id") in identity_ids for u in users)
                            or any(iid in identity_ids for u in users for iid in (u.get("identity_ids") or []))
                        )
                    if matches and not any(matches):
                        continue
            if is_unqueued_list_filter(queue):
                if row["queue"]:
                    continue
            elif queue and row["queue"] != queue:
                continue
            if title_query and not _ilike(row["title"], title_query):
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["last_event_at"], r["id"]), reverse=True)
        return rows

    async def list_threads(  # type: ignore[override]
        self,
        agent_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        cursor: str | None,
        limit: int,
        scope: str = "member",
        user_email: str | None = None,
        identity_ids: list[str] | None = None,
        queue: str | None = None,
        title_query: str | None = None,
    ) -> list[Thread]:
        rows = self._matching(agent_id, organization_id, workspace_id, user_id, scope, user_email, identity_ids, queue, title_query)
        if cursor:
            stamp, thread_id = cursor.rsplit("|", 1)
            at = datetime.fromisoformat(stamp)
            rows = [r for r in rows if r["last_event_at"] < at or (r["last_event_at"] == at and r["id"] < thread_id)]
        return [self._row_to_thread(r) for r in rows[:limit]]

    async def count_threads(  # type: ignore[override]
        self,
        agent_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        scope: str = "member",
        user_email: str | None = None,
        identity_ids: list[str] | None = None,
        queue: str | None = None,
        title_query: str | None = None,
    ) -> int:
        return len(self._matching(agent_id, organization_id, workspace_id, user_id, scope, user_email, identity_ids, queue, title_query))


# -- fixtures --


@pytest.fixture
async def fake():
    server = await FakeAgentApi(workspace_id=WORKSPACE_ID).start()
    for person in (ANA, BIA, CARLA):
        server.core_people[person.user_id] = core_person(person)
    try:
        yield server
        # A response of the fake that is not contract-valid is a bug of the fake: fail loudly.
        assert server.failures == []
    finally:
        await server.stop()


def persistence_for(fake: FakeAgentApi, local: MemoryPersistence | None = None, **options: Any) -> MaestroAgentPersistence:
    return MaestroAgentPersistence(
        MaestroAgentApi(fake.config()), assistant_name=ASSISTANT, local=local if local is not None else MemoryPersistence(), **options
    )


class World:
    """The production persistence and the Maestro one, each under its own ``Repository``."""

    def __init__(self, fake: FakeAgentApi) -> None:
        self.fake = fake
        self.prod_db = ProductionThreads()
        self.prod = Repository(self.prod_db)
        self.maestro_db = persistence_for(fake)
        self.maestro = Repository(self.maestro_db)

    def maestro_id(self, key: str) -> str:
        """The Maestro id of the thread the SDK made with this key."""
        return next(t.id for t in self.fake.threads.values() if t.external_key == key)


@pytest.fixture
def world(fake):
    return World(fake)


def plain(value: Any, labels: dict[str, str] | None = None) -> Any:
    """A model (or a list of them) as JSON values: times are ``<time>``, known ids are their label."""
    labels = labels or {}
    if isinstance(value, list):
        return [plain(item, labels) for item in value]
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"), labels)
    if isinstance(value, dict):
        return {k: "<time>" if k in TIME_FIELDS and v is not None else plain(v, labels) for k, v in value.items()}
    if isinstance(value, str):
        return labels.get(value, value)
    return value


def without_agent(thread: dict[str, Any] | None) -> dict[str, Any] | None:
    """Declared: Maestro lists the agent in ``users``. Checked and removed, so the rest compares equal."""
    if thread is None:
        return None
    agent = {"user_id": AGENT_ID, "user_name": ASSISTANT}
    assert {k: v for k, v in thread["users"][-1].items() if k in agent} == agent
    return {**thread, "users": thread["users"][:-1]}


# -- test_repository_crud_matches_production --


async def crud_script(repo: Repository, add_runs, *, maestro: bool) -> dict[str, Any]:
    """The call sequence of the parity scenario ``repository_crud`` (``tests/parity/scenarios_server.py``)."""
    labels: dict[str, str] = {}
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA, title="Via repositório", queue="suporte")
    labels[thread.id] = "thread-1"
    fetched = await repo.get_thread(thread.id)
    updated = await repo.update_thread(thread.id, title="Título novo", queue="vendas")
    missing_update = await repo.update_thread("thread-inexistente", title="x")
    first = await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Oi")], user=ANA, author="Ana Souza")
    second = await repo.create_event(
        thread.id,
        "message",
        "assistant",
        content=[WireS2C_TextPart(text="Olá, Ana")],
        data={"origem": "crm"},
        author=ASSISTANT,
    )
    draft = await repo.create_event(thread.id, "message", "assistant", content=[WireS2C_TextPart(text="Rascunho")])
    await repo.remove_event(draft.id)
    labels |= {first.id: "event-1", second.id: "event-2", draft.id: "event-3"}
    newest_first = await repo.list_events({"thread_id": thread.id})
    from_user = await repo.list_events({"thread_id": thread.id, "actor": "user"}, order={"direction": "asc"})
    oldest_two = await repo.list_events({"thread_id": thread.id}, order={"direction": "asc"}, limit=2)
    fetched_event = await repo.get_event(first.id)
    removed_event = await repo.get_event(draft.id)
    try:
        await repo.list_events({})
        list_events_error = None
    except ValueError as exc:
        list_events_error = str(exc)
    temporary = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, title="Temporária")
    labels[temporary.id] = "thread-2"
    await repo.remove_thread(temporary.id)
    removed = await repo.get_thread(temporary.id)
    listed = await repo.list_threads(
        {"agent_id": AGENT_ID, "organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID, "user_id": ANA.user_id}
    )
    await add_runs(thread.id)
    runs = await repo.list_runs({"thread_id": thread.id})
    run = await repo.get_run(RUN)
    missing_run = await repo.get_run("run-inexistente")
    unknown_run = await repo.get_run(str(uuid4()))
    await repo.update_settings(AGENT_ID, {"tom": "formal"})
    settings = await repo.get_settings(AGENT_ID)

    def thread_view(value: Thread | None, *, read: bool = True) -> dict[str, Any] | None:
        view = plain(value, labels) if value else None
        # ``create_thread`` returns the ``Thread`` the Repository built (no agent in ``users``); the reads list the agent.
        return without_agent(view) if maestro and read else view

    return {
        "create_thread": thread_view(thread, read=False),
        "get_thread": thread_view(fetched),
        "update_thread": thread_view(updated),
        "update_missing_thread": missing_update,
        "create_event": plain([first, second], labels),
        "list_events_default": plain(newest_first, labels),
        "list_events_user_asc": plain(from_user, labels),
        "list_events_oldest_two": plain(oldest_two, labels),
        "get_event": plain(fetched_event, labels),
        "get_removed_event": removed_event,
        "list_events_without_thread": list_events_error,
        "get_removed_thread": removed,
        "list_threads": [thread_view(t) for t in listed],
        "list_runs": plain(runs, labels),
        "get_run": plain(run, labels),
        "get_missing_run": missing_run,
        "get_unknown_run": unknown_run,
        "get_settings": settings,
    }


async def test_repository_crud_matches_production(world):
    async def add_prod_runs(thread_id: str) -> None:
        await world.prod_db.create_run(
            Run(id=RUN, thread_id=thread_id, status="succeeded", started_at=T0, ended_at=T0 + timedelta(seconds=2))
        )
        await world.prod_db.create_run(
            Run(
                id=FAILED_RUN,
                thread_id=thread_id,
                status="failed",
                started_at=T0 + timedelta(seconds=5),
                ended_at=T0,
                error="content_policy",
            )
        )

    async def add_maestro_runs(thread_id: str) -> None:
        maestro_id = world.maestro_id(thread_id)
        world.fake.add_run(maestro_id, status="succeeded", run_id=RUN)
        world.fake.add_run(maestro_id, status="failed", run_id=FAILED_RUN)

    production = await crud_script(world.prod, add_prod_runs, maestro=False)
    maestro = await crud_script(world.maestro, add_maestro_runs, maestro=True)

    assert maestro == production
    # The results above are not empty values that match by accident.
    assert production["create_thread"]["owner"]["user_id"] == ANA.user_id
    assert [e["id"] for e in production["list_events_default"]] == ["event-2", "event-1"]
    assert [r["id"] for r in production["list_runs"]] == [FAILED_RUN, RUN]
    assert production["get_event"]["id"] == "event-1" and production["get_removed_event"] is None
    assert production["get_missing_run"] is None and production["get_removed_thread"] is None


async def test_declared_differences_are_the_only_ones(world):
    """The agent is in ``Thread.users``; a user message's author comes from the person; an assistant's from the name."""
    for repo in (world.prod, world.maestro):
        thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
        await repo.create_event(thread.id, "message", "assistant", content=[WireS2C_TextPart(text="Sem autor")])
        await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Oi")], user=ANA)
        fetched = await repo.get_thread(thread.id)
        events = await repo.list_events({"thread_id": thread.id}, order={"direction": "asc"})
        if repo is world.prod:
            assert fetched.users == [ANA]
            assert [(e.actor, e.author) for e in events] == [("assistant", None), ("user", None)]
        else:
            assert fetched.users == [ANA, User(user_id=AGENT_ID, user_name=ASSISTANT)]
            assert [(e.actor, e.author) for e in events] == [("assistant", ASSISTANT), ("user", "Ana Souza")]


# -- test_memory_by_external_thread_id --


async def test_memory_by_external_thread_id(world):
    key = EXTERNAL_KEY
    contact = User(user_id=PHONE, user_name="Maria")
    texts = [("user", "Oi, tudo bem?"), ("assistant", "Tudo ótimo. Em que ajudo?"), ("user", "Preciso da 2ª via do boleto")]
    # Production: a channel thread whose id is the caller's key.
    now = T0
    await world.prod_db.create_thread(
        Thread(
            id=key, agent_id=AGENT_ID, organization_id=ORGANIZATION_ID, workspace_id=WORKSPACE_ID, owner=contact, users=[contact],
            created_at=now, updated_at=now, last_event_at=now,
        )
    )  # fmt: skip
    for index, (actor, text) in enumerate(texts):
        await world.prod_db.create_event(
            ThreadEvent(
                id=f"e{index}",
                thread_id=key,
                type="message",
                actor=actor,
                author=ASSISTANT if actor == "assistant" else "Maria",
                user=contact if actor == "user" else User(user_id=""),
                content=[WireS2C_TextPart(text=text)],
                created_at=now + timedelta(seconds=index),
            )
        )
    # Maestro: what ``dispatch`` and the WhatsApp ingress do: a thread with the contact, then the messages (agent API).
    api = MaestroAgentApi(world.fake.config())
    thread = await api.create_thread(
        external_key=key,
        channel="whatsapp",
        channel_meta={"instance_id": "i1"},
        participants=[{"kind": "external", "channel": "whatsapp", "external_id": PHONE, "display_name": "Maria"}],
        workspace_id=WORKSPACE_ID,
    )
    for index, (actor, text) in enumerate(texts):
        who = (
            {"kind": "self"}
            if actor == "assistant"
            else {"kind": "external", "channel": "whatsapp", "external_id": PHONE, "display_name": "Maria"}
        )
        await api.publish_event(
            thread["thread_id"],
            actor=who,
            payload={"content": [{"type": "text", "text": text}]},
            trigger=False,
            idempotency_key=f"e{index}",
        )

    production = AgentMemory(key, world.prod_db)
    maestro = AgentMemory(key, world.maestro_db)
    for history_format in ("openai", "anthropic", "google", "cohere", "voyage", "langchain", "openai_completions", "openai_responses"):
        expected = await production.get_history(limit=10, format=history_format)
        assert expected, history_format
        assert await maestro.get_history(limit=10, format=history_format) == expected
        assert await maestro.get_history(limit=10, format=history_format, order="asc") == await production.get_history(
            limit=10, format=history_format, order="asc"
        )
    assert plain(await maestro.get_history_raw(limit=10)) == plain(await production.get_history_raw(limit=10), {})
    assert await maestro.get_history(limit=1) == await production.get_history(limit=1)
    assert plain(await maestro.get_history_raw(filters={"actor": "user"})) == plain(
        await production.get_history_raw(filters={"actor": "user"})
    )
    assert (await maestro.get_thread()).id == key
    # ``thread.users`` and ``owner`` are the contact (the channel thread belongs to it), and the agent is the Maestro extra.
    assert (await maestro.get_thread()).owner == contact
    assert (await maestro.get_thread()).workspace_id == WORKSPACE_ID

    # A thread that does not exist: empty, as production.
    unknown_production, unknown_maestro = (
        AgentMemory("wa:inst:i1:+550000", world.prod_db),
        AgentMemory("wa:inst:i1:+550000", world.maestro_db),
    )
    assert await unknown_maestro.get_thread() is None and await unknown_production.get_thread() is None
    assert await unknown_maestro.get_history() == await unknown_production.get_history() == []
    assert await unknown_maestro.get_history_raw() == await unknown_production.get_history_raw() == []
    assert await unknown_maestro.list_artifacts() == await unknown_production.list_artifacts() == ([], None, False)


async def test_memory_reads_the_events_after_an_edit_and_the_run_of_the_input(world):
    """The fold of Maestro events (``message.updated``, ``run.updated``) is the one a handler's memory has."""
    thread = await world.maestro.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    maestro_id = world.maestro_id(thread.id)
    sent = await world.maestro.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Texto velho")], user=ANA)
    trigger = world.fake.threads[maestro_id].events[-1]["event_id"]
    world.fake.inject_event(
        maestro_id, "run.updated", {"run_id": RUN, "agent_id": AGENT_ID, "status": "running", "trigger_event_id": trigger}
    )
    world.fake.inject_event(maestro_id, "message.updated", {"event_id": trigger, "content": [{"type": "text", "text": "Texto novo"}]})
    events = await world.maestro.list_events({"thread_id": thread.id}, order={"direction": "asc"})
    assert [(e.id, e.run_id, e.content[0].text) for e in events] == [(sent.id, RUN, "Texto novo")]
    assert [e.id for e in await world.maestro.list_events({"thread_id": thread.id, "run_id": RUN})] == [sent.id]
    assert (await world.maestro.get_event(sent.id)).content[0].text == "Texto novo"


# -- test_thread_id_returned_is_callers_key (F8) --


async def test_thread_id_returned_is_callers_key(world):
    repo, fake = world.maestro, world.fake
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA, title="Chave")
    maestro_id = world.maestro_id(thread.id)
    assert maestro_id != thread.id

    # create_thread sends the SDK id as the external key and a key made from the SDK id (not from the external key's text).
    create = next(r for r in fake.requests if r.method == "POST" and r.path.endswith("/agent/threads"))
    assert create.body["external_key"] == thread.id
    assert create.headers["idempotency-key"] == f"thread.create:{thread.id}"

    event = await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Oi")], user=ANA)
    fake.add_run(maestro_id, run_id=RUN)
    # The caller's id comes back from every read.
    assert (await repo.get_thread(thread.id)).id == thread.id
    assert [t.id for t in await repo.list_threads({"workspace_id": WORKSPACE_ID, "organization_id": ORGANIZATION_ID})] == [thread.id]
    assert [e.thread_id for e in await repo.list_events({"thread_id": thread.id})] == [thread.id]
    assert (await repo.get_event(event.id)).thread_id == thread.id
    assert [r.thread_id for r in await repo.list_runs({"thread_id": thread.id})] == [thread.id]
    assert (await repo.get_run(RUN)).thread_id == thread.id
    assert (await repo.update_thread(thread.id, title="Outro")).id == thread.id

    # The Maestro id works too, and then it is what the caller gave.
    by_maestro_id = await repo.get_thread(maestro_id)
    assert by_maestro_id is not None and by_maestro_id.id == maestro_id and by_maestro_id.title == "Outro"
    assert [e.thread_id for e in await repo.list_events({"thread_id": maestro_id})] == [maestro_id]

    # A thread id that is not a UUID is an external key (the path of the request is that key).
    wa = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, title="WhatsApp")
    await world.maestro_db._api.create_thread(external_key=EXTERNAL_KEY, workspace_id=WORKSPACE_ID)  # as dispatch makes it
    fake.requests.clear()
    thread_by_key = await repo.get_thread(EXTERNAL_KEY)
    assert thread_by_key is not None and thread_by_key.id == EXTERNAL_KEY
    assert fake.requests[-1].path.endswith("/agent/threads/wa%3Ainst%3Ai1%3A%2B5511999990000")
    assert wa.id != EXTERNAL_KEY

    # create_event sends the SDK event id as the idempotency key (``client_event_id``): the same event is not made twice.
    assert await repo._persistence.create_event(event) is None
    assert len([e for e in fake.threads[maestro_id].events if e["type"] == "message.created"]) == 1
    published = [r for r in fake.requests if r.method == "POST" and r.path.endswith("/events")]
    assert [r.body["idempotency_key"] for r in published] == [event.id]  # the replay: same key, the same single event


# -- F16: the human that makes the thread is its owner --


async def test_create_thread_owner_is_the_user(world):
    repo, fake = world.maestro, world.fake
    with_user = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    without_user = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID)
    bodies = {r.body["external_key"]: r.body for r in fake.requests if r.method == "POST" and r.path.endswith("/agent/threads")}
    assert bodies[with_user.id]["owner"] == {"kind": "human", "principal_id": ANA.user_id}
    assert "owner" not in bodies[without_user.id]
    got, anonymous = await repo.get_thread(with_user.id), await repo.get_thread(without_user.id)
    assert got.owner == ANA and anonymous.owner == User(user_id="")
    # production: the same owners; the same users except the agent
    prod_with = await world.prod.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    prod_without = await world.prod.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID)
    assert (await world.prod.get_thread(prod_with.id)).owner == got.owner
    assert (await world.prod.get_thread(prod_without.id)).owner == anonymous.owner
    assert got.users == [ANA, User(user_id=AGENT_ID, user_name=ASSISTANT)]
    assert anonymous.users == [User(user_id=AGENT_ID, user_name=ASSISTANT)]


# -- F18: workspace and organization --


async def test_workspace_empty_is_personal_and_another_scope_is_a_configuration_error():
    personal = await FakeAgentApi().start()
    try:
        repo = Repository(persistence_for(personal))
        thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, "", user=ANA, title="Pessoal")
        created = next(r for r in personal.requests if r.method == "POST")
        assert created.body["scope_kind"] == "personal" and "workspace_id" not in created.body
        assert (await repo.get_thread(thread.id)).workspace_id == ""
        personal.requests.clear()
        with pytest.raises(MaestroAgentScopeError):
            await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID)
        with pytest.raises(MaestroAgentScopeError):
            await repo.list_threads({"organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID})
        with pytest.raises(MaestroAgentScopeError):
            await repo.create_thread(AGENT_ID, str(uuid4()), "")
        assert personal.requests == []
        assert [t.id for t in await repo.list_threads({"organization_id": ORGANIZATION_ID, "workspace_id": ""})] == [thread.id]
    finally:
        await personal.stop()


async def test_workspace_token_refuses_the_personal_default(world):
    # The default ``workspace_id=""`` is personal: a workspace agent says so (it never reads or writes another scope).
    with pytest.raises(MaestroAgentScopeError):
        await world.maestro.create_thread(AGENT_ID)
    assert world.fake.requests == []
    with pytest.raises(MaestroAgentScopeError):
        await world.maestro.list_threads()
    # ``admin`` lists what the token sees (no organization or workspace filter, as production's admin scope).
    assert await world.maestro.list_threads({"scope": "admin"}) == []


# -- F21: list filters, paging and count --


async def seed_threads(world: World) -> dict[str, Thread]:
    """The same threads on both sides: owners, queues, titles; a thread per second so the order is the order of activity."""
    made: dict[str, Thread] = {}
    plan = [
        ("a", ANA, "Boleto atrasado", "suporte"),
        ("b", BIA, "Troca de produto", "vendas"),
        ("c", ANA, "Segunda via 100% do boleto", None),
        ("d", CARLA, "boleto_novo", "suporte"),
        ("e", None, "Sem dono", None),
    ]
    for label, user, title, queue in plan:
        for repo in (world.prod, world.maestro):
            thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=user, title=title, queue=queue)
            if repo is world.prod:
                made[label] = thread
            else:
                made[label + "-maestro"] = thread
    # Production orders by ``last_event_at``: give both sides the same order of activity (creation order).
    for index, label in enumerate(made):
        if label.endswith("-maestro"):
            continue
        row = world.prod_db._threads[made[label].id]
        row["last_event_at"] = T0 + timedelta(seconds=index)
    return made


FILTERS: list[dict[str, Any]] = [
    {},
    {"user_id": ANA.user_id},
    {"user_id": BIA.user_id},
    {"user_email": CARLA.user_email},
    {"identity_ids": ["idp-bia"]},
    {"identity_ids": "idp-ana"},
    {"user_id": ANA.user_id, "user_email": CARLA.user_email},
    {"queue": "suporte"},
    {"queue": "__none__"},
    {"queue": ""},
    {"queue": "vendas", "user_id": ANA.user_id},
    {"title_query": "boleto"},
    {"title_query": "BOLETO"},
    {"title_query": "100%"},
    {"title_query": "_"},
    {"title_query": "inexistente"},
    {"scope": "workspace"},
    {"scope": "organization"},
    {"scope": "admin"},
    {"scope": "member", "user_id": "user-99"},
]


@pytest.mark.parametrize("extra", FILTERS, ids=lambda f: json.dumps(f, sort_keys=True))
async def test_list_and_count_threads_match_production(world, extra):
    made = await seed_threads(world)
    names = {made[label].id: label for label in "abcde"} | {made[label + "-maestro"].id: label for label in "abcde"}
    identity = extra.get("identity_ids")
    kwargs = {
        "scope": extra.get("scope", "member"),
        "user_email": extra.get("user_email"),
        "identity_ids": [identity] if isinstance(identity, str) else identity,
        "queue": extra.get("queue"),
        "title_query": extra.get("title_query"),
    }
    args = (AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, extra.get("user_id"))
    expected = await world.prod_db.list_threads(*args, None, 1000, **kwargs)
    got = await world.maestro_db.list_threads(*args, None, 1000, **kwargs)
    assert [names[t.id] for t in got] == [names[t.id] for t in expected]
    # count_threads is consistent with list_threads (and with production's count).
    assert await world.prod_db.count_threads(*args, **kwargs) == len(expected)
    assert await world.maestro_db.count_threads(*args, **kwargs) == len(expected)
    # The Repository passes the filters it has (user, scope, e-mail, identity IDs).
    repo_filter = {"agent_id": AGENT_ID, "organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID}
    repo_filter.update({k: v for k, v in extra.items() if k in ("user_id", "user_email", "identity_ids", "scope")})
    assert [names[t.id] for t in await world.maestro.list_threads(repo_filter)] == [
        names[t.id] for t in await world.prod.list_threads(repo_filter)
    ]


async def test_list_threads_limit_pages_until_the_limit_and_cursor(fake):
    # Page of the server: 2 threads. The filter keeps one in two: the SDK reads page after page until the limit.
    db = persistence_for(fake, page_size=2)
    repo = Repository(db)
    prod_db = ProductionThreads()
    prod = Repository(prod_db)
    plan = [(ANA if index % 2 == 0 else BIA) for index in range(9)]
    labels: list[str] = []
    for index, user in enumerate(plan):
        thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=user, title=f"T{index}")
        mirror = await prod.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=user, title=f"T{index}")
        prod_db._threads[mirror.id]["last_event_at"] = T0 + timedelta(seconds=index)
        labels.append(thread.id)
    expected_titles = ["T8", "T6", "T4", "T2", "T0"]
    base = {"agent_id": AGENT_ID, "organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID, "user_id": ANA.user_id}
    fake.requests.clear()
    got = await repo.list_threads(base, limit=3)
    assert [t.title for t in got] == expected_titles[:3]
    assert [t.title for t in await prod.list_threads(base, limit=3)] == expected_titles[:3]
    # Every page the server answered was asked with a cursor of the last one, and the sweep stopped when the limit was met.
    sent = [r for r in fake.requests if r.method == "GET" and r.path.endswith("/agent/threads")]
    assert all(r.query["limit"] == "2" and r.query["order"] == "desc" for r in sent)
    assert 2 <= len(sent) < 5 and "cursor" not in sent[0].query and "cursor" in sent[1].query
    assert [t.title for t in await repo.list_threads(base, limit=50)] == expected_titles
    assert await repo.list_threads(base, limit=0) == []
    assert await db.count_threads(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, ANA.user_id) == 5
    # The persistence cursor of production (``last_event_at|id``): older threads only.
    cursor_thread = got[1]
    cursor = f"{cursor_thread.last_event_at.isoformat()}|{cursor_thread.id}"
    page = await db.list_threads(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, ANA.user_id, cursor, 50)
    assert [t.title for t in page] == expected_titles[2:]


async def test_list_threads_server_filters(world):
    await seed_threads(world)
    world.fake.requests.clear()
    args = (AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, None, None, 50)
    await world.maestro_db.list_threads(*args, queue="suporte")
    await world.maestro_db.list_threads(*args, queue="__none__")
    await world.maestro_db.list_threads(*args, title_query="boleto")
    await world.maestro_db.list_threads(*args, user_email=CARLA.user_email, identity_ids=["idp-bia"], scope="member")
    sent = [r.query for r in world.fake.requests if r.method == "GET"]
    assert [q.get("queue") for q in sent] == ["suporte", "", None, None]
    # The other filters (user, title, scope) are the SDK's: the route has no such filter.
    assert all(set(q) <= {"limit", "order", "queue", "cursor"} for q in sent)


# -- F31: not found, deletion, empty reads --


async def test_missing_and_deleting_threads_read_as_none_and_empty(world):
    repo, fake = world.maestro, world.fake
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    event = await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Oi")], user=ANA)
    fake.add_run(world.maestro_id(thread.id), run_id=RUN)
    fake.delete_thread_state(world.maestro_id(thread.id))  # THREAD_DELETING
    for ref in (thread.id, "thread-inexistente"):
        assert await repo.get_thread(ref) is None
        assert await repo.list_events({"thread_id": ref}) == []
        assert await repo.list_runs({"thread_id": ref}) == []
        assert await AgentMemory(ref, world.maestro_db).get_history() == []
        assert await repo.update_thread(ref, title="x") is None
        await repo.remove_thread(ref)  # nothing to delete: no error, as production
    assert await repo.get_event(event.id) is None
    assert await repo.get_run(RUN) is None
    await repo.remove_event(event.id)  # no error
    # A deleted thread's key is free: the same key makes a new thread (production: new thread after remove_thread).
    live = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID)
    await repo.remove_thread(live.id)
    assert await repo.get_thread(live.id) is None


async def test_remove_event_removes_any_agent_visible_content_event(world):
    repo, fake = world.maestro, world.fake
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    maestro_id = world.maestro_id(thread.id)
    agent = fake._actor(fake.threads[maestro_id], {"kind": "self"})
    reasoning = fake.inject_event(maestro_id, "reasoning.created", {"text": "Pensando"}, actor=agent)
    run = fake.inject_event(
        maestro_id, "run.updated", {"run_id": RUN, "agent_id": AGENT_ID, "status": "running", "trigger_event_id": reasoning["event_id"]}
    )
    assert [e.type for e in await repo.list_events({"thread_id": thread.id})] == ["reasoning"]
    # A handler output (no SDK id) is removed by its Maestro id.
    await repo.remove_event(reasoning["event_id"])
    assert [e for e in fake.threads[maestro_id].events if e["type"] == "message.removed"][0]["payload"] == {
        "event_id": reasoning["event_id"]
    }
    assert await repo.list_events({"thread_id": thread.id}) == []
    assert await repo.get_event(reasoning["event_id"]) is None
    # A control event is not content: production has no such event, so the removal does nothing (no error, no event).
    before = len(fake.threads[maestro_id].events)
    await repo.remove_event(run["event_id"])
    assert len(fake.threads[maestro_id].events) == before
    assert await repo.get_event(run["event_id"]) is None


# -- brief item 10: events a handler produced --


async def test_get_event_finds_the_events_a_handler_produced(world):
    """A handler output has no SDK-chosen id: Maestro keeps its own ``event_id`` (and no ``client_event_id``)."""
    repo, fake = world.maestro, world.fake
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    maestro_id = world.maestro_id(thread.id)
    agent = fake._actor(fake.threads[maestro_id], {"kind": "self"})
    output = fake.inject_event(
        maestro_id,
        "message.created",
        {"client_event_id": "x", "content": [{"type": "text", "text": "Resposta"}], "mentions": []},
        actor=agent,
    )
    output["payload"].pop("client_event_id")
    # The id a handler can read (list_events, get_history_raw) is the Maestro event id; get_event finds it by that id.
    listed = await repo.list_events({"thread_id": thread.id})
    assert [e.id for e in listed] == [output["event_id"]]
    found = await repo.get_event(output["event_id"])
    assert found is not None and found.id == output["event_id"] and found.thread_id == thread.id
    assert found.model_dump() == listed[0].model_dump()
    assert found.actor == "assistant" and found.author == ASSISTANT and found.content[0].text == "Resposta"
    # An id that does not exist is None (as production), and an agent publication is found by the SDK id too.
    assert await repo.get_event(str(uuid4())) is None and await repo.get_event("evento-inexistente") is None
    mine = await repo.create_event(thread.id, "message", "assistant", content=[WireS2C_TextPart(text="Minha")])
    assert (await repo.get_event(mine.id)).id == mine.id
    published = next(e for e in fake.threads[maestro_id].events if e["payload"].get("client_event_id") == mine.id)
    assert (await repo.get_event(published["event_id"])).id == published["event_id"]  # by the Maestro id: the id given back


# -- things the agent API cannot do: loud, never silent --


async def test_what_the_agent_api_cannot_write_is_refused_loudly(world):
    repo, fake = world.maestro, world.fake
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    fake.requests.clear()
    for kind, actor, content in (
        ("reasoning", "assistant", None),
        ("message", "system", [WireS2C_TextPart(text="x")]),
        ("tool.call", "assistant", None),
    ):
        with pytest.raises(MaestroAgentUnsupported):
            await repo.create_event(thread.id, kind, actor, content=content, data={"id": "t"})
    with pytest.raises(MaestroAgentUnsupported):  # a user message needs a person
        await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="x")])
    with pytest.raises(MaestroAgentUnsupported):  # a file part without a URL (a WebSocket upload) has nothing to publish
        await repo.create_event(thread.id, "message", "assistant", content=[WireS2C_ImagePart(ref_id="up-1")])
    assert fake.requests == []
    for call in (world.maestro_db.update_event, world.maestro_db.create_run, world.maestro_db.update_run):
        with pytest.raises(MaestroAgentUnsupported):
            await call(None)


async def test_a_link_part_round_trips_as_a_media_link(world):
    repo = world.maestro
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    part = WireS2C_ImagePart(url="https://cdn.example.com/foto.png", mime_type="image/png", alt="Foto", filename="foto.png")
    await repo.create_event(thread.id, "message", "assistant", content=[WireS2C_TextPart(text="Veja"), part])
    (event,) = await repo.list_events({"thread_id": thread.id})
    assert event.content == [WireS2C_TextPart(text="Veja"), part]


async def test_history_beyond_the_page_limit_is_a_loud_error(fake):
    db = persistence_for(fake, page_size=2, max_history_pages=2)
    repo = Repository(db)
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    for index in range(7):
        await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text=f"m{index}")], user=ANA)
    # desc: the newest ones are in the first pages
    assert [e.content[0].text for e in await repo.list_events({"thread_id": thread.id}, limit=3)] == ["m6", "m5", "m4"]
    with pytest.raises(MaestroHistoryLimitError):
        await repo.list_events({"thread_id": thread.id}, limit=7)


# -- the rest of the Persistence surface --


async def test_settings_secrets_analytics_and_worker_seed_stay_local(fake):
    local = MemoryPersistence()
    db = persistence_for(fake, local)
    assert await db.get_settings("a1") == {}
    await db.set_settings("a1", {"k": "v"})
    await db.update_setting("a1", "n", 2)
    assert await local.get_settings("a1") == {"k": "v", "n": 2} == await db.get_settings("a1")
    await db.merge_service_secrets("a1", {"s": "1"})
    assert await db.get_service_secrets("a1") == {"s": "1"}
    with pytest.raises(NotImplementedError):  # the local persistence of the test: not implemented there either
        await db.get_worker_seed_hash_bytes("w1")
    assert fake.requests == []  # none of it is thread data


async def test_guest_cleanup_is_the_maestro_job_and_connect_calls_do_nothing(fake):
    db = persistence_for(fake)
    assert await db.delete_idle_guest_threads(60) == 0
    assert await db.connect() is None and await db.migrate() is None and await db.disconnect() is None
    assert fake.requests == []


async def test_the_local_persistence_is_asked_only_when_settings_are_used(fake):
    asked = []

    async def local() -> MemoryPersistence:
        asked.append(1)
        return MemoryPersistence()

    db = MaestroAgentPersistence(MaestroAgentApi(fake.config()), local=local)
    repo = Repository(db)
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
    await repo.get_thread(thread.id)
    assert asked == []
    assert await repo.get_settings("a1") == {}
    assert asked == [1]


async def test_transport_errors_are_not_hidden(fake):
    repo = Repository(persistence_for(fake))
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID)
    fake.fail_next[f"/api/v1/agent/threads/{thread.id}"] = "DEPENDENCY_UNAVAILABLE"
    with pytest.raises(MaestroAgentApiError) as error:
        await repo.get_thread(thread.id)
    assert (error.value.code, error.value.retryable) == ("DEPENDENCY_UNAVAILABLE", True)


# -- AgentServer.repository() and AgentServer.memory(thread_id) --


async def test_server_repository_and_memory_run_on_the_agent_api(fake):
    server = AgentServer(
        AgentConfig(database_type="postgres", thread_management="maestro", maestro=fake.config(), assistant_name=ASSISTANT)
    )
    server._ensure_initialized = AsyncMock(side_effect=AssertionError("the database is not used for threads"))
    repo = await server.repository()
    assert isinstance(repo, Repository) and isinstance(repo._persistence, MaestroAgentPersistence)
    thread = await repo.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA, title="Pelo servidor")
    await repo.create_event(thread.id, "message", "user", content=[WireS2C_TextPart(text="Oi")], user=ANA)
    memory = await server.memory(thread.id)
    assert isinstance(memory, AgentMemory) and isinstance(memory._persistence, MaestroAgentPersistence)
    assert await memory.get_history() == [{"role": "user", "content": "Oi"}]
    assert (await memory.get_thread()).title == "Pelo servidor"
    assert await (await server.memory("thread-inexistente")).get_history() == []
    # Settings are the only call that needs the database (the local persistence).
    with pytest.raises(AssertionError, match="database is not used"):
        await repo.get_settings("a1")


async def test_legacy_mode_repository_is_the_same_object_as_before():
    server = AgentServer(AgentConfig(database_type="postgres"))
    persistence = object()
    server._ensure_initialized = AsyncMock(return_value=persistence)
    assert (await server.repository())._persistence is persistence
    assert (await server.memory("t1"))._persistence is persistence


def test_filterable_fields_are_the_columns_the_memory_reads():
    # ``Repository.list_events`` passes these keys; the Maestro read applies the same set (production column filters).
    assert FILTERABLE_FIELDS == {"type", "actor", "user_id", "user_name", "user_email", "run_id"}
