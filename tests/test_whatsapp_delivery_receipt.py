"""Async Meta failures land on the thread event that carries the wamid."""

from datetime import UTC, datetime

import pytest

from dooers.agents.server.features.channels.whatsapp.delivery_receipt import (
    apply_whatsapp_delivery_receipt,
    bind_agent_server,
)
from dooers.agents.server.protocol.models import Thread, ThreadEvent


def _now() -> datetime:
    return datetime.now(UTC)


class _Persistence:
    def __init__(self, event: ThreadEvent, thread: Thread) -> None:
        self.event = event
        self.thread = thread
        self.updated: ThreadEvent | None = None

    async def find_event_by_whatsapp_message_id(self, wa_message_id: str) -> ThreadEvent | None:
        delivery = (self.event.data or {}).get("delivery") or {}
        if delivery.get("wa_message_id") == wa_message_id:
            return self.event
        return None

    async def get_thread(self, thread_id: str) -> Thread | None:
        if thread_id == self.thread.id:
            return self.thread
        return None

    async def update_event(self, event: ThreadEvent) -> None:
        self.updated = event


@pytest.mark.asyncio
async def test_receipt_marks_event_failed_and_broadcasts():
    now = _now()
    event = ThreadEvent(
        id="ev-1",
        thread_id="th-1",
        type="message",
        actor="assistant",
        data={
            "delivery": {
                "channel": "whatsapp",
                "status": "accepted",
                "wa_message_id": "wamid.1",
            }
        },
        created_at=now,
    )
    thread = Thread(
        id="th-1",
        agent_id="agent-1",
        created_at=now,
        updated_at=now,
        last_event_at=now,
    )
    persistence = _Persistence(event, thread)
    broadcasts: list[tuple[str, dict]] = []

    class _Server:
        _persistence = persistence

        async def _broadcast_dict_to_agent(self, agent_id: str, payload: dict) -> None:
            broadcasts.append((agent_id, payload))

    bind_agent_server(_Server())
    body = (
        b'{"delivery_receipt":true,"wa_message_id":"wamid.1","status":"failed",'
        b'"error_code":130497,"error_message":"restricted"}'
    )

    applied = await apply_whatsapp_delivery_receipt(persistence, "agent-1", body)

    assert applied is True
    delivery = event.data["delivery"]
    assert delivery["status"] == "failed"
    assert delivery["error_code"] == 130497
    assert delivery["error_message"] == "restricted"
    assert persistence.updated is event
    assert broadcasts == [
        (
            "agent-1",
            {"type": "event.append", "thread_id": "th-1", "events": [event]},
        )
    ]


@pytest.mark.asyncio
async def test_receipt_missing_wamid_is_not_applied():
    now = _now()
    event = ThreadEvent(
        id="ev-1",
        thread_id="th-1",
        type="message",
        actor="assistant",
        data={"delivery": {"wa_message_id": "wamid.1", "status": "accepted"}},
        created_at=now,
    )
    thread = Thread(id="th-1", agent_id="agent-1", created_at=now, updated_at=now, last_event_at=now)
    persistence = _Persistence(event, thread)

    applied = await apply_whatsapp_delivery_receipt(
        persistence,
        "agent-1",
        b'{"delivery_receipt":true,"wa_message_id":"wamid.other","status":"failed"}',
    )

    assert applied is False
    assert event.data["delivery"]["status"] == "accepted"
