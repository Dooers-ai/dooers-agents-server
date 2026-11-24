"""The handler sees the caller's thread key inside a delivery too (the ``external_key`` of the thread view).

Production gives the handler the thread id the caller chose (``wa:inst:{instance}:{+E164}`` for WhatsApp,
the ``thread_id`` of ``dispatch``/``ingest``). In a Dooers Threads delivery the envelope and the events carry the
Dooers Threads thread UUID; ``Public.ContextThread.external_key`` carries the caller's key (``null`` when there is
none, and for a public chat visitor thread, whose key belongs to the Dooers Threads). Every value the handler reads
(context, ``ThreadEvent``, ``Thread``) shows that key; every call back to the Dooers Threads keeps the UUID.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from dooers.agents.server.dooers_threads.agent_persistence import callers_key
from dooers.agents.server.dooers_threads.context import dooers_threads_handler_context
from dooers.agents.server.dooers_threads.persistence import DooersThreadsAgentMemory
from dooers.agents.server.protocol.dooers_threads import default_contracts
from tests.test_dooers_threads_context import fixture_server, prepare, private
from tests.test_dooers_threads_core_context import AGENT_ACTOR, THREAD
from tests.test_dooers_threads_legacy_model import DooersThreadsThread
from tests.test_dooers_threads_memory_parity import PARTICIPANTS, ContextMemory

KEY = "wa:inst:inst-1:+5511999990000"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def bundle():
    return default_contracts()


def _memory(external_key: str | None | type(...) = KEY) -> tuple[DooersThreadsThread, ContextMemory]:
    thread = DooersThreadsThread(default_contracts())
    for number in range(3):
        thread.message(AGENT_ACTOR, [{"type": "text", "text": f"m{number}"}])
    view = {**THREAD, "thread_id": thread.thread_id}
    if external_key is not ...:
        view["external_key"] = external_key
    return thread, ContextMemory(thread, list(PARTICIPANTS), thread_view=view)


# -- on.context.thread_id --


async def test_the_context_thread_id_is_the_callers_key(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["thread"] = {**THREAD, "thread_id": memory._envelope["thread_id"], "external_key": KEY}
        assert bundle.is_valid("Public.ContextResponse", page)
        async with dooers_threads_handler_context(memory, private) as prepared:
            assert prepared.on.context.thread_id == KEY
            # The calls back to the Dooers Threads keep the UUID.
            assert memory._envelope["thread_id"] != KEY


@pytest.mark.parametrize("external_key", [None, ...])
async def test_without_a_key_the_context_thread_id_is_the_dooers_threads_id(key, bundle, external_key):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page["thread"] = {**THREAD, "thread_id": memory._envelope["thread_id"]}
        if external_key is not ...:
            page["thread"]["external_key"] = external_key
        async with dooers_threads_handler_context(memory, private) as prepared:
            assert prepared.on.context.thread_id == memory._envelope["thread_id"]


async def test_an_older_dooers_threads_without_the_thread_view_keeps_the_dooers_threads_id(key, bundle):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(key, bundle, server)
        page.pop("thread", None)
        async with dooers_threads_handler_context(memory, private) as prepared:
            assert prepared.on.context.thread_id == memory._envelope["thread_id"]


# -- memory --


async def test_the_handlers_memory_shows_the_key_in_events_and_thread():
    thread, context = _memory()
    memory = DooersThreadsAgentMemory(context, thread_id=KEY)
    assert memory._thread_id == KEY
    raw = await memory.get_history_raw(limit=10)
    assert raw and {event.thread_id for event in raw} == {KEY}
    assert (await memory.get_thread()).id == KEY
    # The Dooers Threads id still reads the same thread (an internal caller, as the SDK's own code).
    assert (await memory._persistence.get_thread(thread.thread_id)).id == KEY


async def test_the_memory_of_a_thread_without_a_key_shows_the_dooers_threads_id():
    thread, context = _memory(None)
    memory = DooersThreadsAgentMemory(context, thread_id=thread.thread_id)
    assert {event.thread_id for event in await memory.get_history_raw(limit=10)} == {thread.thread_id}
    assert (await memory.get_thread()).id == thread.thread_id


async def test_an_unknown_thread_id_is_still_unknown():
    _, context = _memory()
    memory = DooersThreadsAgentMemory(context, thread_id=KEY)
    assert await memory._persistence.get_thread(str(uuid4())) is None
    assert await memory._persistence.get_events(str(uuid4())) == []


# -- outside a delivery: the same rule (``DooersThreadsAgentPersistence``) --

VISITOR = {
    "kind": "external",
    "external_id": "guest:abc123",
    "channel": "public_chat",
    "connection_type": "guest",
    "participant_id": str(uuid4()),
    "state": "active",
    "joined_sequence": "1",
    "left_sequence": None,
}
PUBLIC_KEY = "public:11111111-1111-4111-8111-111111111111:guest:guest:abc123"


def _persistence():
    from types import SimpleNamespace

    from dooers.agents.server.dooers_threads.agent_persistence import DooersThreadsAgentPersistence

    api = SimpleNamespace(config=SimpleNamespace(instance_agent_id=str(uuid4()), max_history_pages=5))
    return DooersThreadsAgentPersistence(api)  # type: ignore[arg-type]


async def test_a_thread_read_outside_a_delivery_shows_the_callers_key():
    from tests.test_dooers_threads_legacy_model import _thread_view

    persistence, thread_id = _persistence(), str(uuid4())
    thread = persistence._thread(_thread_view(thread_id, external_key=KEY))
    assert thread.id == KEY
    assert persistence._keys[thread_id] == KEY


async def test_a_public_chat_thread_read_outside_a_delivery_shows_a_plain_id_never_the_dooers_threads_key():
    """Production shows the id of a public chat thread as a UUID: its key (``public:...``) is the Dooers Threads' own."""
    from tests.test_dooers_threads_legacy_model import _thread_view

    persistence, thread_id = _persistence(), str(uuid4())
    view = _thread_view(thread_id, external_key=PUBLIC_KEY)
    view["participants"].append(VISITOR)
    assert persistence._thread(view).id == thread_id
    assert persistence._keys[thread_id] == thread_id
    assert await persistence._caller_key(thread_id) == thread_id


async def test_an_agent_chosen_public_prefixed_key_is_still_the_callers_key():
    """The rule looks at who made the thread (a visitor's ``connection_type``), not at the ``public:`` prefix of the key."""
    from tests.test_dooers_threads_legacy_model import _thread_view

    persistence, thread_id = _persistence(), str(uuid4())
    view = _thread_view(thread_id, external_key="public:x")
    assert not any(p.get("connection_type") for p in view["participants"])
    assert callers_key(view) == "public:x"
    assert persistence._thread(view).id == "public:x"
    assert persistence._keys[thread_id] == "public:x"
    assert await persistence._caller_key(thread_id) == "public:x"


async def test_an_agent_chosen_public_prefixed_key_reaches_the_handler_inside_a_delivery():
    thread, context = _memory("public:x")
    memory = DooersThreadsAgentMemory(context, thread_id="public:x")
    assert {event.thread_id for event in await memory.get_history_raw(limit=10)} == {"public:x"}
    assert (await memory.get_thread()).id == "public:x"
