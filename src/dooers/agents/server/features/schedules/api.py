"""Handler-facing schedules: ``memory.schedules`` bound to the turn's user and thread."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from dooers.agents.server.features.schedules.models import (
    SCHEDULE_MAX_INSTRUCTION_CHARS,
    SCHEDULE_MAX_TITLE_CHARS,
    Recurrence,
    ScheduleRecord,
)
from dooers.agents.server.features.schedules.recurrence import first_run_at, resolve_timezone
from dooers.agents.server.features.schedules.store import ScheduleStore, new_schedule_id

logger = logging.getLogger(__name__)

#: ``(action, record)`` → push ``schedule.upsert`` / ``schedule.deleted`` to the owner's sockets.
ScheduleNotifier = Callable[[str, ScheduleRecord], Awaitable[None]]

#: Per user and agent. Keeps a runaway model from filling the table.
MAX_OPEN_SCHEDULES_PER_USER = 50


class SchedulesUnavailableError(RuntimeError):
    pass


class AgentSchedules:
    def __init__(
        self,
        store: ScheduleStore | None,
        *,
        agent_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str,
        user_name: str | None = None,
        user_email: str | None = None,
        thread_id: str | None = None,
        default_timezone: str | None = None,
        notifier: ScheduleNotifier | None = None,
    ) -> None:
        self._store = store
        self._agent_id = agent_id
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._user_id = (user_id or "").strip()
        self._user_name = user_name
        self._user_email = user_email
        self._thread_id = thread_id
        self._default_tz = resolve_timezone(default_timezone).key
        self._notify = notifier

    @property
    def available(self) -> bool:
        return self._store is not None and bool(self._user_id) and not self._user_id.startswith("guest:")

    @property
    def default_timezone(self) -> str:
        return self._default_tz

    def _require(self) -> ScheduleStore:
        if self._store is None:
            raise SchedulesUnavailableError("scheduling is not configured for this agent")
        if not self.available:
            raise SchedulesUnavailableError("scheduling requires a signed-in user")
        return self._store

    async def create(
        self,
        *,
        title: str,
        instruction: str,
        start_at: datetime | None = None,
        recurrence: Recurrence | dict[str, Any] | None = None,
        timezone: str | None = None,
        kind: str = "reminder",
        thread_id: str | None = None,
        new_thread: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ScheduleRecord:
        """Create a reminder/task. ``start_at`` naive → interpreted in ``timezone``."""
        store = self._require()
        if await store.count_active(agent_id=self._agent_id, organization_id=self._organization_id, user_id=self._user_id) >= (
            MAX_OPEN_SCHEDULES_PER_USER
        ):
            raise ValueError(f"limit of {MAX_OPEN_SCHEDULES_PER_USER} active schedules reached; cancel some first")
        title_text = " ".join(str(title or "").split())[:SCHEDULE_MAX_TITLE_CHARS]
        instruction_text = str(instruction or "").strip()[:SCHEDULE_MAX_INSTRUCTION_CHARS]
        if not title_text:
            raise ValueError("title is required")
        if not instruction_text:
            raise ValueError("instruction is required")
        rule = recurrence if isinstance(recurrence, Recurrence) else Recurrence(**(recurrence or {}))
        tz_name = resolve_timezone(timezone or self._default_tz).key
        now = datetime.now(UTC)
        if rule.freq == "once" and start_at is None:
            raise ValueError("start_at is required for a one-time schedule")
        next_run = first_run_at(rule, tz_name=tz_name, start_at=start_at, now=now)
        if rule.freq == "once" and next_run < now:
            raise ValueError("start_at is in the past")
        record = ScheduleRecord(
            id=new_schedule_id(),
            agent_id=self._agent_id,
            organization_id=self._organization_id,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            user_name=self._user_name,
            user_email=self._user_email,
            thread_id=None if new_thread else (thread_id or self._thread_id),
            title=title_text,
            instruction=instruction_text,
            kind="task" if kind == "task" else "reminder",
            recurrence=rule,
            timezone=tz_name,
            next_run_at=next_run,
            metadata={**(metadata or {}), "anchor": next_run.isoformat()},
            created_at=now,
            updated_at=now,
        )
        await store.create(record)
        await self._emit("upsert", record)
        return record

    async def list(self, *, thread_only: bool = False, include_closed: bool = False) -> list[ScheduleRecord]:
        if not self.available:
            return []
        store = self._require()
        return await store.list(
            agent_id=self._agent_id,
            organization_id=self._organization_id,
            user_id=self._user_id,
            thread_id=self._thread_id if thread_only else None,
            include_closed=include_closed,
        )

    async def get(self, schedule_id: str) -> ScheduleRecord | None:
        store = self._require()
        record = await store.get(schedule_id)
        if record is None or not self.owns(record):
            return None
        return record

    def owns(self, record: ScheduleRecord) -> bool:
        return (
            record.agent_id == self._agent_id
            and record.organization_id == self._organization_id
            and record.user_id == self._user_id
        )

    async def _set_status(self, schedule_id: str, status: str) -> ScheduleRecord | None:
        store = self._require()
        record = await self.get(schedule_id)
        if record is None:
            return None
        if record.status in ("completed", "canceled") and status != "canceled":
            raise ValueError(f"schedule is already {record.status}")
        fields: dict[str, Any] = {"status": status}
        if status == "active" and record.next_run_at is not None and record.next_run_at < datetime.now(UTC):
            # Resuming after a long pause fires once now instead of replaying missed runs.
            fields["next_run_at"] = datetime.now(UTC)
        updated = await store.update(schedule_id, fields)
        if updated is not None:
            await self._emit("deleted" if status == "canceled" else "upsert", updated)
        return updated

    async def cancel(self, schedule_id: str) -> ScheduleRecord | None:
        return await self._set_status(schedule_id, "canceled")

    async def pause(self, schedule_id: str) -> ScheduleRecord | None:
        return await self._set_status(schedule_id, "paused")

    async def resume(self, schedule_id: str) -> ScheduleRecord | None:
        return await self._set_status(schedule_id, "active")

    async def _emit(self, action: str, record: ScheduleRecord) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(action, record)
        except Exception:  # noqa: BLE001
            logger.exception("schedule notification failed")
