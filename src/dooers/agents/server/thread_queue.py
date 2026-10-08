"""Workspace thread queue slugs — routing state, not delivery."""

from __future__ import annotations

import re

QUEUE_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
#: ``thread.list`` sentinel — not a valid slug; means ``queue IS NULL``.
UNQUEUED_LIST_FILTER = "__none__"


class QueueValidationError(ValueError):
    """Invalid queue on create or ``thread.update``."""


def is_unqueued_list_filter(value: str | None) -> bool:
    return value == UNQUEUED_LIST_FILTER


def queue_for_create(
    requested: object | None,
    *,
    workspace_id: str | None,
    allowed: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    """Queue to persist on a newly created thread.

    ``requested is None`` leaves the thread unqueued. An explicit value is
    validated like ``thread.update``.
    """
    if requested is None:
        return None
    return resolve_thread_queue(requested, workspace_id=workspace_id, allowed=allowed)


def normalize_queue_slug(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QueueValidationError("invalid queue")
    slug = value.strip().lower()
    if not slug:
        return None
    if not QUEUE_SLUG_RE.match(slug):
        raise QueueValidationError("invalid queue")
    return slug


def resolve_thread_queue(
    value: object,
    *,
    workspace_id: str | None,
    allowed: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    """Validate a queue write.

    Personal threads (empty workspace) cannot have a queue.
    Workspace threads may be unqueued (``None``). When Core sent a catalog,
    a non-null slug must be in that list; otherwise any well-formed slug is
    accepted.
    """
    slug = normalize_queue_slug(value)
    workspace = (workspace_id or "").strip()
    if not workspace:
        if slug:
            raise QueueValidationError("queue not allowed on personal threads")
        return None
    if slug is None:
        return None
    if allowed:
        if slug not in set(allowed):
            raise QueueValidationError("unknown queue")
    return slug
