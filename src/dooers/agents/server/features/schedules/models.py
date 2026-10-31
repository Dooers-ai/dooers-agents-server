"""Scheduled work: reminders and recurring tasks an agent runs on a user's behalf."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ScheduleKind = Literal["reminder", "task"]
ScheduleStatus = Literal["active", "paused", "completed", "canceled", "failed"]
Frequency = Literal["once", "minutely", "hourly", "daily", "weekly", "monthly"]

SCHEDULE_MAX_TITLE_CHARS = 200
SCHEDULE_MAX_INSTRUCTION_CHARS = 4000
#: Shortest repeat interval we accept — anything faster is a busy loop, not a reminder.
SCHEDULE_MIN_INTERVAL_SECONDS = 5 * 60


class Recurrence(BaseModel):
    """Small, explicit recurrence rule (no cron strings from the model)."""

    freq: Frequency = "once"
    #: Every ``interval`` units of ``freq`` (≥ 1).
    interval: int = Field(default=1, ge=1, le=1000)
    #: Weekly: weekdays 0=Monday … 6=Sunday. Empty → weekday of the first run.
    by_weekday: list[int] | None = None
    #: Monthly: day of month 1..31 (clamped to the month length). Empty → day of the first run.
    by_monthday: int | None = Field(default=None, ge=1, le=31)
    #: Local wall-clock time ``HH:MM`` for daily/weekly/monthly. Empty → time of the first run.
    at_time: str | None = None
    #: Stop after this instant (inclusive).
    until: datetime | None = None
    #: Stop after this many runs.
    count: int | None = Field(default=None, ge=1, le=10000)

    @field_validator("by_weekday")
    @classmethod
    def _weekdays(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        days = sorted({int(d) for d in value})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("by_weekday values must be 0 (Monday) … 6 (Sunday)")
        return days or None

    @field_validator("at_time")
    @classmethod
    def _at_time(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        raw = str(value).strip()
        parts = raw.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("at_time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("at_time must be a valid 24h time")
        return f"{hour:02d}:{minute:02d}"


class ScheduleRecord(BaseModel):
    id: str
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    #: Owner — the person the reminder is for. Scheduled runs act as this user.
    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    #: Thread the run posts into; ``None`` → a new thread per run.
    thread_id: str | None = None
    title: str
    #: What the agent must do (or say) when the schedule fires.
    instruction: str
    kind: ScheduleKind = "reminder"
    recurrence: Recurrence = Field(default_factory=Recurrence)
    timezone: str = "UTC"
    next_run_at: datetime | None = None
    status: ScheduleStatus = "active"
    run_count: int = 0
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ScheduleRunResult(BaseModel):
    schedule_id: str
    thread_id: str | None = None
    status: Literal["succeeded", "failed", "skipped"]
    error: str | None = None
    next_run_at: datetime | None = None
