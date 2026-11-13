"""The handler's history read from the end of the thread (plan 3, task B).

Production reads the latest rows of a thread (``get_events`` with a ``limit``). In a Maestro
delivery the same reads go through ``MaestroContextWindow``: the context read backward from the
cutoff (``direction=backward``), page by page, until the selection is the one the whole thread
would give. These tests use ``ThreadPages`` (Maestro's pages, ``tests/parity/context_pages.py``)
under the real window, and FakeMaestro under the real adapter.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from dooers.agents.server.maestro import create_text_delivery_adapter
from dooers.agents.server.maestro.client import MaestroContextError
from dooers.agents.server.maestro.persistence import MaestroAgentMemory, MaestroExecutionPersistence
from dooers.agents.server.maestro.window import MaestroHistoryLimitError
from tests.parity.fake_maestro import FakeMaestro, private_resources, shared_contracts
from tests.parity.model import ASSISTANT_NAME, USER
from tests.test_maestro_legacy_model import PERSON, MaestroThread, _thread_view
from tests.test_maestro_memory_parity import (
    FILES_TURN,
    HISTORY_FORMATS,
    LAST_TURN,
    MAESTRO_AGENT,
    PHOTO_TURN,
    TURNS,
    ContextMemory,
    Published,
    Turn,
    _in_execution,
    _maestro,
    _normalized,
    _production,
    _text,
    _text_turn,
)


def _answer(number: int):
    return lambda send, _: [send.run_start(), send.text(f"Resposta {number}"), send.run_end()]


def _chat(turns: int, *, first: int = 0) -> tuple[Turn, ...]:
    """``turns`` person messages, each answered inside a run: 4 Maestro events per turn."""
    return tuple(_text_turn(f"Pergunta {n}", _answer(n)) for n in range(first, first + turns))


def _context(thread: MaestroThread, **options: Any) -> tuple[ContextMemory, MaestroAgentMemory]:
    context = ContextMemory(thread, **options)
    return context, MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME)


def _backward(context: ContextMemory) -> int:
    return sum(1 for _cursor, _limit, direction in context.pages.requests if direction == "backward")


def _without_ref_ids(value: Any) -> Any:
    """A person's upload has the artifact id as ``ref_id`` in Maestro; production's parts have none (README)."""
    if isinstance(value, dict):
        return {key: _without_ref_ids(item) for key, item in value.items() if key != "ref_id"}
    if isinstance(value, list):
        return [_without_ref_ids(item) for item in value]
    return value


def _dump(models) -> Any:
    return _normalized(_without_ref_ids([model.model_dump() for model in models]))


def _texts(events) -> list[str]:
    return [part.text for event in events for part in (event.content or []) if getattr(part, "text", None)]


@pytest.fixture(scope="module")
async def long_thread():
    """5000 Maestro events (1250 answered turns) and production's rows for the same turns."""
    turns = _chat(1250)
    thread = _maestro(turns)
    assert len(thread.events) == 5000
    return thread, await _production(turns)


# -- Long threads --


async def test_5000_events_get_history_reads_one_backward_page_and_equals_production(long_thread):
    thread, production = long_thread
    context, memory = _context(thread)

    for fmt in ("openai", "anthropic"):
        assert _normalized(await memory.get_history(limit=20, format=fmt)) == _normalized(
            await production.get_history(limit=20, format=fmt)
        )

    # get_history asks for 60 rows; half of the events are runs (no row): two pages of 100 events, nothing else.
    assert _backward(context) == 2 and len(context.pages.requests) == 2
    assert max(limit for _cursor, limit, _direction in context.pages.requests) <= 100
    assert len(await memory.get_history(limit=20)) == 20


async def test_5000_events_raw_events_equal_production(long_thread):
    thread, production = long_thread
    context, memory = _context(thread)

    raw = await memory.get_history_raw(limit=20, order="desc")
    expected = await production.get_history_raw(limit=20, order="desc")

    assert _dump(raw) == _dump(expected)
    assert _backward(context) == 1 and len(context.pages.requests) == 1


async def test_5000_events_after_event_id_reads_back_only_to_the_reference(long_thread):
    thread, production = long_thread
    context, memory = _context(thread)
    everything = await production.get_history_raw(limit=10_000)
    reference = len(everything) - 61  # 61 rows from the end: the second page of the window

    ours = await memory._persistence.get_events(thread.thread_id, limit=1000, after_event_id=_ids(await _maestro_raw(thread))[reference])
    theirs = await production._persistence.get_events(production._thread_id, limit=1000, after_event_id=everything[reference].id)

    assert _dump(ours) == _dump(theirs)
    assert len(ours) == 60 and _backward(context) <= 3


async def _maestro_raw(thread: MaestroThread):
    """The Maestro side's full list (a whole-thread window): ids in stored order."""
    return await MaestroAgentMemory(ContextMemory(thread, page_size=100, max_pages=100), assistant_name=ASSISTANT_NAME).get_history_raw(
        limit=10_000
    )


def _ids(events) -> list[str]:
    return [event.id for event in events]


async def test_a_long_thread_through_the_real_adapter_answers_with_bounded_pages():
    fake = FakeMaestro()
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        seen["history"] = await memory.get_history(limit=20)
        seen["thread"] = await memory.get_thread()
        seen["title"] = on.context.thread_title
        yield send.text("Li o fim da conversa.")

    thread_id = fake.create_thread(USER, title="Atendimento")
    for number in range(1, 2500):
        fake.publish(thread_id, USER, [{"type": "text", "text": f"Mensagem {number}"}], client_event_id=f"client-{number}")
    trigger = fake.publish(thread_id, USER, [{"type": "text", "text": "Mensagem final"}], client_event_id="client-final")
    (delivery,) = fake.deliveries_for(trigger)
    with fake.serving():
        response = await fake.deliver(create_text_delivery_adapter(fake.config, handler, private_resources), delivery)

    assert response.status_code == 200 and response.json()["state"] == "responded"
    assert [m["content"] for m in seen["history"]][-1] == "Mensagem final" and len(seen["history"]) == 20
    assert seen["title"] == "Atendimento" and seen["thread"].title == "Atendimento"
    backward = [request for request in delivery.context_requests if request[2] == "backward"]
    forward = [request for request in delivery.context_requests if request[2] == "forward"]
    # The newest page once; two one-event forward pages: the thread's first event (``thread.created``: channel and
    # creation time) and the authorization check of ``on`` (``MaestroMemory.get_thread``).
    assert (len(backward), len(forward)) == (1, 2) and all(request[1] == 1 for request in forward)
    assert delivery.context_pages == 3


async def test_a_history_read_past_the_window_cap_reaches_the_handler_as_its_own_error():
    fake = FakeMaestro()
    config = dataclasses.replace(fake.config, max_history_pages=1)
    seen: dict[str, Any] = {}

    async def handler(on, send, memory, analytics, settings):
        try:
            await memory.get_history_raw(limit=500, order="asc")
        except MaestroHistoryLimitError as error:
            seen["error"] = error
        yield send.text("Não consegui ler tudo.")

    thread_id = fake.create_thread(USER, title=None)
    for number in range(1, 130):
        trigger = fake.publish(thread_id, USER, [{"type": "text", "text": f"m{number}"}], client_event_id=f"c{number}")
    (delivery,) = fake.deliveries_for(trigger)
    with fake.serving():
        response = await fake.deliver(create_text_delivery_adapter(config, handler, private_resources), delivery)

    # Not a 413 and not a lost lease: the handler saw the error, answered, and the delivery completed.
    assert response.status_code == 200 and response.json()["state"] == "responded"
    assert seen["error"].max_pages == 1 and "MAESTRO_HISTORY_LIMIT" in str(seen["error"])


async def test_an_unhandled_history_limit_fails_the_handler_not_the_delivery_authority():
    fake = FakeMaestro()
    config = dataclasses.replace(fake.config, max_history_pages=1)

    async def handler(on, send, memory, analytics, settings):
        await memory.get_history_raw(limit=500, order="asc")
        yield send.text("never")

    thread_id = fake.create_thread(USER, title=None)
    for number in range(1, 130):
        trigger = fake.publish(thread_id, USER, [{"type": "text", "text": f"m{number}"}], client_event_id=f"c{number}")
    (delivery,) = fake.deliveries_for(trigger)
    with fake.serving():
        response = await fake.deliver(create_text_delivery_adapter(config, handler, private_resources), delivery)

    assert response.status_code == 200 and response.json()["state"] == "failed"
    assert delivery.reason_code == "handler_error"


# -- The window --


async def test_update_and_removal_inside_the_window_and_outside_it():
    thread = MaestroThread(shared_contracts())
    messages = [thread.message(PERSON, [_text(f"m{n}")]) for n in range(30)]  # sequences 1..30
    edit = {"event_id": messages[28]["event_id"], "content": [_text("m28 editada")]}
    thread.append("message.updated", MAESTRO_AGENT, edit)  # 31: its target is in the window
    thread.append("message.removed", MAESTRO_AGENT, {"event_id": messages[27]["event_id"]})  # 32
    thread.append("message.updated", MAESTRO_AGENT, {"event_id": messages[2]["event_id"], "content": [_text("m2 editada")]})  # 33: far back
    thread.append("message.removed", MAESTRO_AGENT, {"event_id": messages[3]["event_id"]})  # 34: far back
    context, memory = _context(thread, page_size=5)

    newest = await memory.get_history_raw(limit=3, order="desc")

    # The window: events 34..25 (two pages). The edit applies to its target; the removal takes its target out.
    assert _texts(newest) == ["m29", "m28 editada", "m26"]
    assert _backward(context) == 2
    # A change whose target is older than the window is not shown (and nothing fails).
    assert "m2 editada" not in _texts(newest)

    # Reading to the start brings the old targets into the window: the edit and the removal apply.
    whole = _texts(await memory.get_history_raw(limit=100, order="asc"))
    assert whole[2] == "m2 editada" and "m3" not in whole and "m27" not in whole and "m28 editada" in whole
    assert len(whole) == 28


async def test_a_removed_target_that_the_window_must_skip_is_replaced_by_older_events():
    thread = MaestroThread(shared_contracts())
    messages = [thread.message(PERSON, [_text(f"m{n}")]) for n in range(20)]
    for target in messages[10:20]:
        thread.append("message.removed", MAESTRO_AGENT, {"event_id": target["event_id"]})
    context, memory = _context(thread, page_size=5)

    newest = await memory.get_history_raw(limit=3, order="desc")

    # 10 removals fill the first two pages, the 10 removed messages the next two: the 3 newest visible are older.
    assert _texts(newest) == ["m9", "m8", "m7"]
    assert _backward(context) == 5


@pytest.mark.parametrize("page_size", [2, 3, 5])
async def test_filters_and_references_equal_production_whatever_the_page_size(page_size):
    production = await _production(TURNS)
    thread = _maestro(TURNS)
    persistence = production._persistence
    expected_all = await persistence.get_events(production._thread_id, limit=1000)

    for factory in (_persistence_over(thread, page_size),):
        ours_all = await factory.get_events(thread.thread_id, limit=1000)
        assert len(ours_all) == len(expected_all)
        for index in range(len(expected_all)):
            for order in ("asc", "desc"):
                for limit in (2, 100):
                    for kind in ("after", "before", "both", "none", "actor"):
                        options: dict[str, Any] = {"order": order, "limit": limit}
                        ours_options, theirs_options = dict(options), dict(options)
                        if kind in ("after", "both"):
                            ours_options["after_event_id"], theirs_options["after_event_id"] = ours_all[index].id, expected_all[index].id
                        if kind in ("before", "both"):
                            other = (index + 3) % len(expected_all)
                            ours_options["before_event_id"], theirs_options["before_event_id"] = ours_all[other].id, expected_all[other].id
                        if kind == "actor":
                            ours_options["filters"] = theirs_options["filters"] = {"actor": "user"}
                        ours = await factory.get_events(thread.thread_id, **ours_options)
                        theirs = await persistence.get_events(production._thread_id, **theirs_options)
                        assert _dump(ours) == _dump(theirs), (
                            index,
                            order,
                            limit,
                            kind,
                        )


def _persistence_over(thread: MaestroThread, page_size: int, **options: Any) -> MaestroExecutionPersistence:
    return MaestroExecutionPersistence(ContextMemory(thread, page_size=page_size, **options), assistant_name=ASSISTANT_NAME)


async def test_unknown_or_removed_references_are_ignored_like_production():
    thread = MaestroThread(shared_contracts())
    messages = [thread.message(PERSON, [_text(f"m{n}")]) for n in range(12)]
    thread.append("message.removed", MAESTRO_AGENT, {"event_id": messages[10]["event_id"]})
    persistence = _persistence_over(thread, 3)

    removed = await persistence.get_events(thread.thread_id, before_event_id=messages[10]["event_id"], order="desc", limit=2)
    unknown = await persistence.get_events(thread.thread_id, after_event_id="no-such-event", order="desc", limit=2)

    assert _texts(removed) == ["m11", "m9"]
    assert _texts(unknown) == ["m11", "m9"]


async def test_a_read_past_the_window_cap_fails_with_its_own_clear_error():
    thread = _maestro(_chat(75))  # 300 events
    context, memory = _context(thread, page_size=10, max_pages=2)

    with pytest.raises(MaestroHistoryLimitError, match="MAESTRO_HISTORY_LIMIT") as error:
        await memory.get_history_raw(limit=500, order="asc")

    assert not isinstance(error.value, MaestroContextError) and error.value.max_pages == 2
    # What was read stays: a bounded read still works, and the page count of one read is its own.
    assert _backward(context) == 2  # the window holds two pages: a third is refused
    assert len(await memory.get_history_raw(limit=8, order="desc")) == 8


async def test_the_window_is_one_per_execution_and_grows_only_when_needed():
    thread = _maestro(_chat(75))
    context, memory = _context(thread, page_size=10)

    await memory.get_history_raw(limit=5, order="desc")
    await memory.get_history_raw(limit=5, order="desc")
    await memory.get_thread()
    assert _backward(context) == 1
    await memory.get_history_raw(limit=14, order="desc")
    assert _backward(context) == 3
    # The lease is checked on every read, cached or not.
    checks = context.pages.checks
    await memory.get_history_raw(limit=5, order="desc")
    assert context.pages.checks > checks


@pytest.mark.parametrize("fmt", HISTORY_FORMATS)
async def test_read_your_writes_with_a_window_that_does_not_reach_the_start(fmt):
    from tests.test_maestro_memory_parity import WRITES_TURN

    turns = (PHOTO_TURN, FILES_TURN, WRITES_TURN)
    production = await _production(turns)
    memory = _in_execution(turns, page_size=2)

    for options in ({"limit": 2}, {"limit": 3}, {"filters": {"actor": "user"}}):
        expected = await production.get_history(format=fmt, **options)
        assert _normalized(await memory.get_history(format=fmt, **options)) == _normalized(expected), options


async def test_echoes_come_after_a_window_with_several_pages():
    thread = _maestro((*_chat(10), _text_turn("Última", _answer(99))))
    cutoff = max(index for index, event in enumerate(thread.events) if event["actor"]["kind"] == "human") + 1
    published = Published(published_events=thread.events[cutoff:])
    context = ContextMemory(thread, cutoff=cutoff, page_size=4)
    memory = MaestroAgentMemory(context, assistant_name=ASSISTANT_NAME, published=published)

    history = await memory.get_history(limit=3)

    assert [m["content"] for m in history] == ["Resposta 9", "Última", "Resposta 99"]


# -- get_thread and list_artifacts --


def _thread_with_created(*, messages: int) -> MaestroThread:
    thread = MaestroThread(shared_contracts())
    thread.append("thread.created", PERSON, {"thread": _thread_view(thread.thread_id, queue="vendas")})
    for number in range(messages):
        thread.message(PERSON, [_text(f"m{number}")])
    return thread


async def test_get_thread_reads_the_head_the_window_and_the_first_event():
    thread = _thread_with_created(messages=40)
    thread.append(
        "thread.updated",
        MAESTRO_AGENT,
        {"thread_id": thread.thread_id, "state": "active", "title": "Pedido 42", "version": 2, "queue": "suporte"},
    )
    context, memory = _context(thread, page_size=5)

    result = await memory.get_thread()

    # Participants from the head; creation time from ``thread.created`` (read on its own); the change is in the window.
    assert (result.title, result.queue) == ("Pedido 42", "suporte")
    assert result.created_at.isoformat().startswith("2026-11-12T10:00:00")
    assert [user.user_id for user in result.users][0] == PERSON["principal_id"]
    assert (_backward(context), len(context.pages.requests)) == (1, 2)
    assert context.pages.requests[1][1:] == (1, "forward")


async def test_get_thread_does_not_see_a_title_change_older_than_the_window():
    """Declared difference (README): the title and queue are the last change the window holds."""
    thread = _thread_with_created(messages=2)
    thread.append(
        "thread.updated",
        MAESTRO_AGENT,
        {"thread_id": thread.thread_id, "state": "active", "title": "Antigo", "version": 2, "queue": "vendas"},
    )
    for number in range(40):
        thread.message(PERSON, [_text(f"depois {number}")])
    _, memory = _context(thread, page_size=5)

    assert (await memory.get_thread()).title == "Nova conversa"


async def test_list_artifacts_scans_back_only_as_far_as_it_needs():
    turns = (*_chat(100), PHOTO_TURN, *_chat(2, first=500), PHOTO_TURN, LAST_TURN)
    production = await _production(turns)
    thread = _maestro(turns)
    context, memory = _context(thread, page_size=10)

    page, cursor, more = await memory.list_artifacts(limit=1)
    expected, expected_cursor, expected_more = await production.list_artifacts(limit=1)

    assert _dump(page) == _dump(expected)
    assert (cursor, more) == (expected_cursor, expected_more)
    # Production scans 50 events a batch until it has two artifacts: both images are in the last batch,
    # not in the 400 events of the thread before them (10 events a page here).
    assert 1 < _backward(context) <= 11


async def test_the_handler_memory_never_reads_the_whole_thread_forward():
    context, memory = _context(_maestro(TURNS), page_size=100)

    await memory.get_history()
    await memory.get_thread()
    await memory.list_artifacts()

    assert context.snapshots == 0
