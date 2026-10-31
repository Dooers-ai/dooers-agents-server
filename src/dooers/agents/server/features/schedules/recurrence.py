"""Next-occurrence math for :class:`Recurrence`, in the schedule's own time zone.

Rules are evaluated on local wall-clock time (so "every day at 09:00" stays at 09:00 across
daylight-saving changes) and returned as aware UTC datetimes. Missed occurrences are not
replayed: a schedule that wakes up late fires once and then moves to the next future slot.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dooers.agents.server.features.schedules.models import (
    SCHEDULE_MIN_INTERVAL_SECONDS,
    Recurrence,
)

_MAX_STEPS = 5000


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or "UTC").strip() or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _aware(value: datetime, tz: ZoneInfo) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


def _at(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at).replace(tzinfo=tz)


def _parse_at(rule: Recurrence, anchor_local: datetime) -> time:
    if rule.at_time:
        hour, minute = (int(x) for x in rule.at_time.split(":"))
        return time(hour, minute)
    return time(anchor_local.hour, anchor_local.minute)


def _add_months(day: date, months: int, monthday: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(monthday, last))


def validate_recurrence(rule: Recurrence) -> None:
    unit = {"minutely": 60, "hourly": 3600}.get(rule.freq)
    if unit is not None and unit * rule.interval < SCHEDULE_MIN_INTERVAL_SECONDS:
        raise ValueError(f"repeat interval must be at least {SCHEDULE_MIN_INTERVAL_SECONDS // 60} minutes")


def first_run_at(rule: Recurrence, *, tz_name: str, start_at: datetime | None, now: datetime | None = None) -> datetime:
    """First occurrence at or after ``start_at`` (default: now)."""
    validate_recurrence(rule)
    tz = resolve_timezone(tz_name)
    now = now or datetime.now(UTC)
    start = _aware(start_at, tz) if start_at else now
    if rule.freq == "once":
        return start.astimezone(UTC)
    if rule.freq in ("minutely", "hourly"):
        return max(start, now).astimezone(UTC)
    local = start.astimezone(tz)
    at = _parse_at(rule, local)
    candidate = _at(local.date(), at, tz)
    floor = max(start, now)
    for _ in range(400):
        if candidate >= floor - timedelta(seconds=1) and _matches(rule, candidate, anchor=local):
            return candidate.astimezone(UTC)
        candidate = _at(candidate.date() + timedelta(days=1), at, tz)
    return candidate.astimezone(UTC)


def _matches(rule: Recurrence, candidate: datetime, *, anchor: datetime) -> bool:
    if rule.freq == "weekly":
        days = rule.by_weekday or [anchor.weekday()]
        if candidate.weekday() not in days:
            return False
        weeks = (candidate.date() - (anchor.date() - timedelta(days=anchor.weekday()))).days // 7
        return weeks % rule.interval == 0
    if rule.freq == "monthly":
        monthday = rule.by_monthday or anchor.day
        last = calendar.monthrange(candidate.year, candidate.month)[1]
        if candidate.day != min(monthday, last):
            return False
        months = (candidate.year - anchor.year) * 12 + candidate.month - anchor.month
        return months % rule.interval == 0
    if rule.freq == "daily":
        return (candidate.date() - anchor.date()).days % rule.interval == 0
    return True


def next_run_after(
    rule: Recurrence,
    *,
    tz_name: str,
    previous: datetime,
    anchor: datetime,
    run_count: int,
    now: datetime | None = None,
) -> datetime | None:
    """Next occurrence strictly after both ``previous`` and ``now``; ``None`` when finished.

    ``anchor`` is the schedule's first run (defines weekday/monthday/interval phase);
    ``run_count`` is the number of completed runs (for ``count``).
    """
    if rule.freq == "once":
        return None
    if rule.count is not None and run_count >= rule.count:
        return None
    tz = resolve_timezone(tz_name)
    now = now or datetime.now(UTC)
    floor = max(previous, now)
    anchor_local = anchor.astimezone(tz)
    result: datetime | None = None
    if rule.freq in ("minutely", "hourly"):
        step = timedelta(minutes=rule.interval) if rule.freq == "minutely" else timedelta(hours=rule.interval)
        candidate = previous + step
        steps = 0
        while candidate <= floor and steps < _MAX_STEPS:
            candidate += step
            steps += 1
        result = candidate
    else:
        at = _parse_at(rule, anchor_local)
        day = floor.astimezone(tz).date()
        if rule.freq == "monthly":
            monthday = rule.by_monthday or anchor_local.day
            months = (day.year - anchor_local.year) * 12 + day.month - anchor_local.month
            months -= months % rule.interval
            for _ in range(_MAX_STEPS):
                target = _add_months(date(anchor_local.year, anchor_local.month, 1), months, monthday)
                candidate = _at(target, at, tz)
                if candidate > floor:
                    result = candidate
                    break
                months += rule.interval
        else:
            for _ in range(_MAX_STEPS):
                candidate = _at(day, at, tz)
                if candidate > floor and _matches(rule, candidate, anchor=anchor_local):
                    result = candidate
                    break
                day += timedelta(days=1)
    if result is None:
        return None
    result = result.astimezone(UTC)
    if rule.until is not None and result > _aware(rule.until, tz).astimezone(UTC):
        return None
    return result


def describe_recurrence(rule: Recurrence, tz_name: str) -> str:
    """Short English description (UIs translate their own copy; this is for the model)."""
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    every = "" if rule.interval == 1 else f" {rule.interval}"
    at = f" at {rule.at_time}" if rule.at_time else ""
    if rule.freq == "once":
        return "once"
    if rule.freq == "minutely":
        return f"every{every} minute(s)"
    if rule.freq == "hourly":
        return f"every{every} hour(s)"
    if rule.freq == "daily":
        return f"every{every} day(s){at} ({tz_name})"
    if rule.freq == "weekly":
        days = ", ".join(names[d] for d in (rule.by_weekday or []))
        return f"every{every} week(s){' on ' + days if days else ''}{at} ({tz_name})"
    return f"every{every} month(s){' on day ' + str(rule.by_monthday) if rule.by_monthday else ''}{at} ({tz_name})"
