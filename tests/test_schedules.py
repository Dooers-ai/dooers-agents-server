from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dooers.agents.server import AgentConfig, AgentServer
from dooers.agents.server.features.schedules import (
    AgentSchedules,
    InMemoryScheduleStore,
    Recurrence,
    first_run_at,
    next_run_after,
)
from tests.fakes import MemoryPersistence

SP = "America/Sao_Paulo"


def test_daily_at_local_time():
    now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # 12:00 in São Paulo
    rule = Recurrence(freq="daily", at_time="09:00")
    first = first_run_at(rule, tz_name=SP, start_at=None, now=now)
    assert first == datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # tomorrow 09:00 local
    second = next_run_after(rule, tz_name=SP, previous=first, anchor=first, run_count=1, now=first)
    assert second == first + timedelta(days=1)


def test_weekly_on_selected_days():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # Monday 09:00 SP
    rule = Recurrence(freq="weekly", by_weekday=[0, 2], at_time="10:30")
    first = first_run_at(rule, tz_name=SP, start_at=None, now=now)
    assert first.astimezone(UTC) == datetime(2026, 11, 2, 13, 30, tzinfo=UTC)
    nxt = next_run_after(rule, tz_name=SP, previous=first, anchor=first, run_count=1, now=first)
    assert nxt == datetime(2026, 11, 4, 13, 30, tzinfo=UTC)  # Wednesday


def test_monthly_clamps_to_month_end():
    start = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
    rule = Recurrence(freq="monthly", by_monthday=31, at_time="12:00")
    nxt = next_run_after(rule, tz_name="UTC", previous=start, anchor=start, run_count=1, now=start)
    assert nxt == datetime(2027, 2, 28, 12, 0, tzinfo=UTC)


def test_count_and_once_finish():
    start = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert next_run_after(Recurrence(), tz_name="UTC", previous=start, anchor=start, run_count=1, now=start) is None
    rule = Recurrence(freq="daily", count=2)
    assert next_run_after(rule, tz_name="UTC", previous=start, anchor=start, run_count=2, now=start) is None


def test_missed_runs_are_not_replayed():
    anchor = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    rule = Recurrence(freq="hourly", interval=1)
    later = anchor + timedelta(hours=5, minutes=10)
    nxt = next_run_after(rule, tz_name="UTC", previous=anchor, anchor=anchor, run_count=1, now=later)
    assert nxt == anchor + timedelta(hours=6)


def test_too_frequent_is_rejected():
    with pytest.raises(ValueError):
        first_run_at(Recurrence(freq="minutely", interval=1), tz_name="UTC", start_at=None)


def _schedules(store, **kw) -> AgentSchedules:
    return AgentSchedules(
        store, agent_id="a1", organization_id="o1", workspace_id="w1", user_id="u1", thread_id="t1", default_timezone=SP, **kw
    )


async def test_create_validates_and_scopes_to_owner():
    store = InMemoryScheduleStore()
    api = _schedules(store)
    with pytest.raises(ValueError):
        await api.create(title="x", instruction="y")  # once without start
    with pytest.raises(ValueError):
        await api.create(title="x", instruction="y", start_at=datetime.now(UTC) - timedelta(hours=1))
    record = await api.create(title="Pagar boleto", instruction="Lembrar do boleto", start_at=datetime.now(UTC) + timedelta(hours=2))
    assert record.thread_id == "t1" and record.timezone == SP
    other = AgentSchedules(store, agent_id="a1", organization_id="o1", workspace_id="w1", user_id="u2")
    assert await other.get(record.id) is None
    assert [r.id for r in await api.list()] == [record.id]
    paused = await api.pause(record.id)
    assert paused and paused.status == "paused"
    canceled = await api.cancel(record.id)
    assert canceled and canceled.status == "canceled"
    assert await api.list() == []


async def test_guest_cannot_schedule():
    api = AgentSchedules(InMemoryScheduleStore(), agent_id="a1", organization_id="o1", workspace_id="", user_id="guest:1")
    assert api.available is False


async def test_run_due_schedules_dispatches_into_thread():
    server = AgentServer(AgentConfig(database_type="postgres", analytics_enabled=False))
    persistence = MemoryPersistence()
    server._persistence = persistence  # type: ignore[assignment]
    server._initialized = True
    store = InMemoryScheduleStore()
    server.use_stores(schedule_store=store)

    seen: list[dict] = []

    async def handler(incoming, send, memory, analytics, settings):
        seen.append(incoming.context.trigger or {})
        yield send.run_start()
        yield send.text(f"⏰ {incoming.context.trigger['title']}")
        yield send.run_end()

    api = AgentSchedules(store, agent_id="a1", organization_id="o1", workspace_id="w1", user_id="u1", default_timezone="UTC")
    record = await api.create(title="Daily brief", instruction="Resumo diário", recurrence={"freq": "daily"}, start_at=datetime.now(UTC))
    # make it due
    await store.update(record.id, {"next_run_at": datetime.now(UTC) - timedelta(seconds=1)})

    results = await server.run_due_schedules(handler)
    assert [r.status for r in results] == ["succeeded"]
    assert seen[0]["type"] == "schedule" and seen[0]["schedule_id"] == record.id
    thread_id = results[0].thread_id
    assert thread_id
    system_events = [e for e in persistence.events.values() if e.thread_id == thread_id and e.actor == "system"]
    assert system_events and system_events[0].data["trigger"]["schedule_id"] == record.id
    assert persistence.messages(thread_id)[0].content[0].text == "⏰ Daily brief"
    updated = await store.get(record.id)
    assert updated and updated.run_count == 1 and updated.status == "active"
    assert updated.next_run_at and updated.next_run_at > datetime.now(UTC)
    # Not due anymore → nothing runs.
    assert await server.run_due_schedules(handler) == []
