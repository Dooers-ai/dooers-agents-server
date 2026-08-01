"""Unit tests for thread artifact extraction and listing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dooers.agents.server.handlers.thread_artifacts import (
    extract_artifacts_from_event,
    list_thread_artifacts,
)
from dooers.agents.server.protocol.models import (
    Thread,
    ThreadEvent,
    User,
    WireS2C_DocumentPart,
    WireS2C_ImagePart,
    WireS2C_TextPart,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_extract_artifacts_from_user_document():
    event = ThreadEvent(
        id="ev-1",
        thread_id="t-1",
        type="message",
        actor="user",
        user=User(user_id="u1", user_name="Alice"),
        content=[
            WireS2C_TextPart(text="see attached"),
            WireS2C_DocumentPart(
                url="https://example.com/a.pdf",
                filename="a.pdf",
                mime_type="application/pdf",
            ),
        ],
        created_at=_now(),
    )
    arts = extract_artifacts_from_event(event)
    assert len(arts) == 1
    assert arts[0].direction == "in"
    assert arts[0].kind == "document"
    assert arts[0].filename == "a.pdf"
    assert arts[0].user_name == "Alice"


def test_extract_artifacts_from_assistant_image():
    event = ThreadEvent(
        id="ev-2",
        thread_id="t-1",
        type="message",
        actor="assistant",
        content=[WireS2C_ImagePart(url="https://example.com/x.png", mime_type="image/png")],
        created_at=_now(),
    )
    arts = extract_artifacts_from_event(event)
    assert len(arts) == 1
    assert arts[0].direction == "out"
    assert arts[0].kind == "image"


def test_extract_artifacts_skips_non_message():
    event = ThreadEvent(
        id="ev-3",
        thread_id="t-1",
        type="reasoning",
        actor="assistant",
        data={"text": "thinking"},
        created_at=_now(),
    )
    assert extract_artifacts_from_event(event) == []


class _FakePersistence:
    def __init__(self, events: list[ThreadEvent]):
        self._events = events

    async def get_thread(self, thread_id: str) -> Thread | None:
        return Thread(
            id=thread_id,
            agent_id="agent-1",
            organization_id="org-1",
            workspace_id="",
            owner=User(user_id="u1"),
            users=[],
            created_at=_now(),
            updated_at=_now(),
            last_event_at=_now(),
        )

    async def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: str = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        events = sorted(self._events, key=lambda e: e.created_at, reverse=True)
        if before_event_id:
            idx = next((i for i, e in enumerate(events) if e.id == before_event_id), None)
            if idx is not None:
                events = events[idx + 1 :]
        return events[:limit]


@pytest.mark.asyncio
async def test_list_thread_artifacts_pagination_and_direction():
    t0 = _now()
    t1 = datetime.fromtimestamp(t0.timestamp() + 1, tz=UTC)
    events = [
        ThreadEvent(
            id="ev-old",
            thread_id="t-1",
            type="message",
            actor="user",
            content=[WireS2C_DocumentPart(url="https://x/old.pdf", filename="old.pdf", mime_type="application/pdf")],
            created_at=t0,
        ),
        ThreadEvent(
            id="ev-new",
            thread_id="t-1",
            type="message",
            actor="assistant",
            content=[WireS2C_ImagePart(url="https://x/new.png", mime_type="image/png")],
            created_at=t1,
        ),
    ]
    persistence = _FakePersistence(events)
    thread = await persistence.get_thread("t-1")
    assert thread is not None

    page1, cursor, has_more = await list_thread_artifacts(
        persistence, "t-1", thread, limit=1, direction="all"
    )
    assert len(page1) == 1
    assert page1[0].event_id == "ev-new"
    assert has_more is True
    assert cursor == "1"

    page2, cursor2, has_more2 = await list_thread_artifacts(
        persistence, "t-1", thread, cursor=cursor, limit=1, direction="all"
    )
    assert len(page2) == 1
    assert page2[0].event_id == "ev-old"
    assert has_more2 is False
    assert cursor2 is None

    in_only, _, _ = await list_thread_artifacts(
        persistence, "t-1", thread, limit=10, direction="in"
    )
    assert len(in_only) == 1
    assert in_only[0].direction == "in"
