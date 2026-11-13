"""The SDK reads the core person, workspace and thread data of a Maestro delivery context (plan 5, task 5a).

Production reference (origin/main): ``auth_validation`` builds ``User`` from Core's ``ConnectionContext``
(an empty name or e-mail is ``None``; the three roles are Core's) and the workspace catalog
(``queues`` and ``allowed_queues``); ``HandlerPipeline.setup`` builds ``AgentContext`` from the thread
row (title, creation time, queue, channel, channel metadata). A Maestro delivery carries the same
data in ``Public.ContextResponse``: ``workspace`` on every page, the roles and profile on each human
participant and actor, and ``thread`` (the thread view at the context cutoff).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from dooers.agents.server.maestro.context import (
    MaestroPrivateResources,
    current_maestro_context,
    maestro_handler_context,
    thread_at_cutoff,
    thread_from_view,
)
from dooers.agents.server.maestro.legacy_model import production_user, to_thread
from dooers.agents.server.maestro.persistence import MaestroExecutionPersistence
from dooers.agents.server.protocol.maestro import default_contracts
from dooers.agents.server.protocol.models import User, WorkspaceQueueCatalog, WorkspaceQueueMember
from tests.parity.context_pages import ThreadPages
from tests.parity.model import ASSISTANT_NAME
from tests.test_maestro_context import fixture_server, prepare, private
from tests.test_maestro_legacy_model import AGENT, PERSON, MaestroThread, _thread_view
from tests.test_maestro_memory_parity import PARTICIPANTS, ContextMemory

#: What Core's connection context says about a person (production ``User`` of the web chat).
CORE_PROFILE = {
    "display_name": "Ana Souza",
    "email": "ana@example.com",
    "identity_ids": ["idp-ana"],
    "system_role": "user",
    "organization_role": "owner",
    "workspace_role": "manager",
}
ANA = User(
    user_id="auth0|human-fixture",
    user_name="Ana Souza",
    user_email="ana@example.com",
    identity_ids=["idp-ana"],
    system_role="user",
    organization_role="owner",
    workspace_role="manager",
)
WORKSPACE = {
    "allowed_queues": ["suporte", "vendas"],
    "queues": [
        {
            "slug": "suporte",
            "name": "Suporte",
            "members": [
                {
                    "user_id": "user-2",
                    "user_name": "Bia Lima",
                    "user_email": "bia@example.com",
                    "identity_ids": ["idp-bia"],
                    "claims": {"nivel": 2},
                }
            ],
        },
        {"slug": "vendas", "name": "Vendas", "members": []},
    ],
}
QUEUES = [
    WorkspaceQueueCatalog(
        slug="suporte",
        name="Suporte",
        members=[
            WorkspaceQueueMember(
                user_id="user-2", user_name="Bia Lima", user_email="bia@example.com", identity_ids=["idp-bia"], claims={"nivel": 2}
            )
        ],
    ),
    WorkspaceQueueCatalog(slug="vendas", name="Vendas"),
]
THREAD = {
    "thread_id": str(uuid4()),
    "title": "Pedido 1042",
    "queue": "vendas",
    "channel": "whatsapp",
    "channel_meta": {"whatsapp": {"instance_id": "inst-1"}},
    "created_at": "2026-10-13T15:00:00Z",
    "owner_instance_agent_id": None,
}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def bundle():
    return default_contracts()


def _core_page(page: dict, *, actor: bool = True, **changes) -> dict:
    """The page as a Maestro with the core connection context serves it."""
    profile = {**CORE_PROFILE, **changes}
    page["participants"][0].update(profile)
    if actor:
        page["trigger_event"]["actor"].update(profile)
        page["history"]["items"][0]["actor"].update(profile)
    page["workspace"] = deepcopy(WORKSPACE)
    return page


# -- on.context.user --


async def test_user_comes_from_the_core_fields_of_the_participant_and_the_actor(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _core_page(page)
        assert bundle.is_valid("Public.ContextResponse", page)
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user == ANA


async def test_user_comes_from_the_participant_when_the_actor_has_no_core_fields(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _core_page(page, actor=False)
        async with maestro_handler_context(memory, private) as prepared:
            # The actor keeps its own name; e-mail, identity IDs and roles come from the participant.
            assert prepared.on.context.user == ANA.model_copy(update={"user_name": "Pessoa"})


async def test_declared_profile_of_the_actor_wins_and_the_roles_come_from_the_participant(key, bundle):
    """The agent's own publication of a person (``AgentServer.dispatch``): the actor has the declared e-mail and IDs."""
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _core_page(page, actor=False)
        declared = {"display_name": "Ana Declarada", "email": "declarada@example.com", "identity_ids": ["idp-declarada"]}
        page["trigger_event"]["actor"].update(declared)
        page["history"]["items"][0]["actor"].update(declared)
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.user == User(
                user_id=ANA.user_id,
                user_name="Ana Declarada",
                user_email="declarada@example.com",
                identity_ids=["idp-declarada"],
                system_role="user",
                organization_role="owner",
                workspace_role="manager",
            )


async def test_core_person_without_a_name_has_no_user_name_and_never_the_placeholder(key, bundle):
    """Core described the person with an empty name: Maestro keeps its placeholder; production has ``user_name=None``."""
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _core_page(page, display_name="Humano", email=None)
        async with maestro_handler_context(memory, private) as prepared:
            user = prepared.on.context.user
            assert (user.user_name, user.user_email) == (None, None)
            assert (user.organization_role, user.workspace_role) == ("owner", "manager")


async def test_person_the_core_does_not_describe_keeps_the_user_defaults(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        async with maestro_handler_context(memory, private) as prepared:
            user = prepared.on.context.user
            assert (user.system_role, user.organization_role, user.workspace_role) == ("user", "member", "member")
            assert user.user_name == "Pessoa"


# -- on.context.queues and the allowed queues --


async def test_queues_and_allowed_queues_come_from_the_workspace(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _core_page(page)
        async with maestro_handler_context(memory, private) as prepared:
            context = prepared.on.context
            assert context.queues == QUEUES
            assert [member.user_id for member in context.members_for_queue("suporte")] == ["user-2"]
            assert current_maestro_context().allowed_queues == ("suporte", "vendas")
            # A copy per execution: the handler may change it.
            context.queues[0].name = "changed"
            assert page["workspace"]["queues"][0]["name"] == "Suporte"


async def test_workspace_replaces_the_queues_of_the_operator(key, bundle):
    async def resolve(scope):
        return MaestroPrivateResources(scope, {}, {}, {}, queues=(WorkspaceQueueCatalog(slug="operador", name="Operador"),))

    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        _core_page(page)
        async with maestro_handler_context(memory, resolve) as prepared:
            assert [queue.slug for queue in prepared.on.context.queues] == ["suporte", "vendas"]


async def test_empty_workspace_is_a_personal_scope_and_the_operator_queues_do_not_apply(key, bundle):
    async def resolve(scope):
        return MaestroPrivateResources(scope, {}, {}, {}, queues=(WorkspaceQueueCatalog(slug="operador", name="Operador"),))

    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["workspace"] = {"allowed_queues": [], "queues": []}
        async with maestro_handler_context(memory, resolve) as prepared:
            assert prepared.on.context.queues == []
            assert current_maestro_context().allowed_queues == ()


async def test_without_workspace_the_deprecated_operator_queues_are_the_fallback(key, bundle):
    """A Maestro that does not send ``workspace`` yet: ``MaestroPrivateResources.queues`` still fills the catalog."""

    async def resolve(scope):
        return MaestroPrivateResources(scope, {}, {}, {}, queues=QUEUES)

    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        assert "workspace" not in page
        async with maestro_handler_context(memory, resolve) as prepared:
            assert prepared.on.context.queues == QUEUES
            assert prepared.on.context.queues[0] is not QUEUES[0]
            assert current_maestro_context().allowed_queues == ("suporte", "vendas")


async def test_without_workspace_and_without_operator_queues_the_catalog_is_empty(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.queues == []
            assert current_maestro_context().allowed_queues == ()


# -- thread fields --


async def test_thread_fields_come_from_the_thread_view(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["thread"] = {**THREAD, "thread_id": memory._envelope["thread_id"]}
        assert bundle.is_valid("Public.ContextResponse", page)
        async with maestro_handler_context(memory, private) as prepared:
            context = prepared.on.context
            assert (context.thread_title, context.queue, context.channel) == ("Pedido 1042", "vendas", "whatsapp")
            assert context.channel_meta == THREAD["channel_meta"]
            assert context.thread_created_at == datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
            # The handler's copy: changing it leaves the page alone.
            context.channel_meta["whatsapp"]["instance_id"] = "other"
            assert page["thread"]["channel_meta"]["whatsapp"]["instance_id"] == "inst-1"


async def test_thread_view_with_empty_values_gives_the_production_defaults(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["thread"] = {
            **THREAD,
            "thread_id": memory._envelope["thread_id"],
            "title": "",
            "queue": None,
            "channel": "dooers-platform",
            "channel_meta": None,
        }
        async with maestro_handler_context(memory, private) as prepared:
            context = prepared.on.context
            assert (context.thread_title, context.queue, context.channel, context.channel_meta) == (None, None, "dooers-platform", None)


async def test_a_channel_contact_in_a_platform_thread_takes_the_channel_of_the_contact(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        actor = {"kind": "external", "external_id": "+5511999990000", "channel": "whatsapp", "display_name": "Cliente"}
        page["trigger_event"]["actor"] = page["history"]["items"][0]["actor"] = actor
        page["thread"] = {**THREAD, "thread_id": memory._envelope["thread_id"], "channel": "dooers-platform", "channel_meta": None}
        async with maestro_handler_context(memory, private) as prepared:
            assert prepared.on.context.channel == "whatsapp"


def test_thread_at_cutoff_reads_the_events_when_there_is_no_thread_view():
    """A Maestro that does not send ``thread`` yet: ``thread.created`` and later changes, as before this change."""
    created = {"type": "thread.created", "payload": {"thread": {**THREAD, "queue": "suporte", "title": "Primeiro"}}}
    changed = {"type": "thread.updated", "payload": {"title": "Depois", "queue": None}}
    state = thread_at_cutoff([created, changed])
    assert (state["title"], state["queue"], state["channel"]) == ("Depois", None, "whatsapp")
    assert state["created_at"] == datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


def test_thread_at_cutoff_adds_the_later_changes_to_the_thread_view():
    state = thread_at_cutoff(
        [{"type": "thread.updated", "payload": {"title": "Depois"}}], base=thread_from_view({**THREAD, "queue": "vendas"})
    )
    assert (state["title"], state["queue"], state["channel"]) == ("Depois", "vendas", "whatsapp")


# -- the window head --


async def test_window_head_carries_the_workspace_and_the_thread_view_when_the_page_has_them():
    thread = MaestroThread(default_contracts())
    thread.message(AGENT_ACTOR, [{"type": "text", "text": "oi"}])
    pages = ThreadPages(thread.events, list(PARTICIPANTS), workspace=deepcopy(WORKSPACE), thread=deepcopy(THREAD))
    head = await pages.window().load()
    assert head["workspace"] == WORKSPACE and head["thread"] == THREAD


async def test_window_head_has_no_workspace_or_thread_view_from_an_older_maestro():
    thread = MaestroThread(default_contracts())
    thread.message(AGENT_ACTOR, [{"type": "text", "text": "oi"}])
    head = await ThreadPages(thread.events, list(PARTICIPANTS)).window().load()
    assert "workspace" not in head and "thread" not in head


AGENT_ACTOR = AGENT


# -- the production User of a participant or an actor (legacy_model.production_user) --


def test_production_user_has_the_core_roles():
    person = {"kind": "human", "principal_id": ANA.user_id, **CORE_PROFILE}
    assert production_user(person) == ANA


def test_production_user_of_a_core_person_without_a_name_has_no_name():
    person = {"kind": "human", "principal_id": "user-1", **CORE_PROFILE, "display_name": "Humano", "email": None}
    user = production_user(person)
    assert (user.user_name, user.user_email) == (None, None) and user.workspace_role == "manager"


def test_production_user_without_core_fields_keeps_the_defaults_and_the_name():
    user = production_user({"kind": "human", "principal_id": "user-1", "display_name": "Humano"})
    assert (user.system_role, user.organization_role, user.workspace_role) == ("user", "member", "member")
    assert user.user_name == "Humano"


def test_production_user_reads_the_participant_when_the_actor_has_no_core_fields():
    actor = {"kind": "human", "principal_id": "user-1", "display_name": "Ana Declarada", "email": "declarada@example.com"}
    participant = {"kind": "human", "principal_id": "user-1", **CORE_PROFILE}
    user = production_user(actor, participant)
    assert (user.user_name, user.user_email, user.identity_ids) == ("Ana Declarada", "declarada@example.com", ["idp-ana"])
    assert (user.system_role, user.organization_role, user.workspace_role) == ("user", "owner", "manager")


# -- Thread.users and Thread.owner of get_thread() --


def _view_with_core_person(**changes):
    thread = MaestroThread(default_contracts())
    view = _thread_view(thread.thread_id)
    view["participants"][0].update(CORE_PROFILE)
    view.update(changes)
    return view


def test_thread_owner_and_users_have_the_core_roles():
    thread = to_thread(_view_with_core_person(), recipient_instance_id=AGENT["instance_agent_id"], assistant_name=ASSISTANT_NAME)
    person = ANA.model_copy(update={"user_id": PERSON["principal_id"]})
    assert thread.owner == person and thread.users[0] == person


async def test_get_thread_reads_the_core_roles_and_the_thread_view():
    thread = MaestroThread(default_contracts())
    thread.message(AGENT_ACTOR, [{"type": "text", "text": "oi"}])
    participants = [{**PARTICIPANTS[0], **CORE_PROFILE}, PARTICIPANTS[1]]
    view = {**THREAD, "thread_id": thread.thread_id, "title": "Atual", "queue": "suporte"}
    memory = ContextMemory(thread, participants, workspace=deepcopy(WORKSPACE), thread_view=view)
    read = await MaestroExecutionPersistence(memory, assistant_name=ASSISTANT_NAME).get_thread(thread.thread_id)
    person = ANA.model_copy(update={"user_id": PERSON["principal_id"]})
    assert read.owner == person and read.users[0] == person
    assert (read.title, read.queue) == ("Atual", "suporte")
    assert read.created_at == datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


async def test_get_thread_does_not_read_the_first_event_when_the_page_has_the_thread_view():
    thread = MaestroThread(default_contracts())
    for number in range(6):
        thread.message(AGENT_ACTOR, [{"type": "text", "text": f"m{number}"}])
    view = {**THREAD, "thread_id": thread.thread_id}
    memory = ContextMemory(thread, list(PARTICIPANTS), page_size=2, thread_view=view)
    await MaestroExecutionPersistence(memory, assistant_name=ASSISTANT_NAME).get_thread(thread.thread_id)
    assert [direction for _, _, direction in memory.pages.requests] == ["backward"]


async def test_get_thread_without_the_thread_view_reads_the_first_event():
    thread = MaestroThread(default_contracts())
    for number in range(6):
        thread.message(AGENT_ACTOR, [{"type": "text", "text": f"m{number}"}])
    memory = ContextMemory(thread, list(PARTICIPANTS), page_size=2)
    await MaestroExecutionPersistence(memory, assistant_name=ASSISTANT_NAME).get_thread(thread.thread_id)
    assert [direction for _, _, direction in memory.pages.requests] == ["backward", "forward"]
