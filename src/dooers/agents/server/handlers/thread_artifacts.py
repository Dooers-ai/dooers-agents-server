"""Extract and list media artifacts from thread message events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from dooers.agents.server.protocol.models import (
    ArtifactDirection,
    ArtifactKind,
    Thread,
    ThreadArtifact,
    ThreadEvent,
)

if TYPE_CHECKING:
    from dooers.agents.server.persistence.base import Persistence

ArtifactListDirection = Literal["in", "out", "all"]
_MEDIA_KINDS: frozenset[str] = frozenset({"audio", "image", "document"})
_EVENT_SCAN_BATCH = 50


def _direction_for_actor(actor: str) -> ArtifactDirection:
    return "in" if actor == "user" else "out"


def extract_artifacts_from_event(event: ThreadEvent) -> list[ThreadArtifact]:
    """Return zero or more artifacts from a single thread event."""
    if event.type != "message" or not event.content:
        return []

    direction = _direction_for_actor(event.actor)
    user_id = event.user.user_id if event.user and event.user.user_id else None
    user_name = event.user.user_name if event.user and event.user.user_name else None
    out: list[ThreadArtifact] = []

    for part in event.content:
        part_type = getattr(part, "type", None)
        if part_type not in _MEDIA_KINDS:
            continue
        out.append(
            ThreadArtifact(
                event_id=event.id,
                direction=direction,
                kind=part_type,  # type: ignore[arg-type]
                filename=getattr(part, "filename", None),
                mime_type=getattr(part, "mime_type", None),
                url=getattr(part, "url", None),
                ref_id=getattr(part, "ref_id", None),
                created_at=event.created_at,
                user_id=user_id,
                user_name=user_name,
            )
        )
    return out


async def list_thread_artifacts(
    persistence: Persistence,
    thread_id: str,
    thread: Thread,
    *,
    cursor: str | None = None,
    limit: int = 30,
    direction: ArtifactListDirection = "all",
    hydrate_events: Callable[[list[ThreadEvent], Thread], Awaitable[list[ThreadEvent]]]
    | None = None,
) -> tuple[list[ThreadArtifact], str | None, bool]:
    """List media artifacts newest-first with offset cursor pagination (v1)."""
    safe_limit = max(1, min(limit, 100))
    skip = 0
    if cursor:
        try:
            skip = max(0, int(cursor))
        except ValueError:
            skip = 0

    collected: list[ThreadArtifact] = []
    before_event_id: str | None = None
    target = skip + safe_limit + 1

    while len(collected) < target:
        events = await persistence.get_events(
            thread_id,
            before_event_id=before_event_id,
            limit=_EVENT_SCAN_BATCH,
            order="desc",
        )
        if not events:
            break
        before_event_id = events[-1].id
        if hydrate_events is not None:
            events = await hydrate_events(events, thread)
        for event in events:
            for artifact in extract_artifacts_from_event(event):
                if direction != "all" and artifact.direction != direction:
                    continue
                collected.append(artifact)
                if len(collected) >= target:
                    break
            if len(collected) >= target:
                break

    has_more = len(collected) > skip + safe_limit
    page = collected[skip : skip + safe_limit]
    next_cursor = str(skip + safe_limit) if has_more else None
    return page, next_cursor, has_more
