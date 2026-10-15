"""thread.upsert must carry each connection's own access flags."""

import json
from datetime import UTC, datetime

import pytest

from dooers.agents.server.broadcast import broadcast_thread_upsert
from dooers.agents.server.protocol.models import Thread, ThreadAccess, User
from dooers.agents.server.registry import ConnectionRegistry


class _Socket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, data: str) -> None:
        self.messages.append(data)


def _thread() -> Thread:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    owner = User(user_id="owner")
    return Thread(
        id="t1",
        agent_id="agent-1",
        workspace_id="ws-1",
        owner=owner,
        users=[owner],
        queue="human",
        created_at=now,
        updated_at=now,
        last_event_at=now,
        access=ThreadAccess(read=True, write=True, manage=True),
    )


@pytest.mark.asyncio
async def test_thread_upsert_access_is_per_connection() -> None:
    registry = ConnectionRegistry()
    participant = _Socket()
    supervisor = _Socket()
    await registry.register(
        "agent-1",
        participant,
        user=User(user_id="owner"),
        workspace_id="ws-1",
    )
    await registry.register(
        "agent-1",
        supervisor,
        user=User(user_id="manager", organization_role="manager"),
        workspace_id="ws-1",
    )

    sent = await broadcast_thread_upsert(registry, "agent-1", _thread())
    assert sent == 2

    participant_access = json.loads(participant.messages[0])["payload"]["thread"]["access"]
    supervisor_access = json.loads(supervisor.messages[0])["payload"]["thread"]["access"]

    assert participant_access == {"read": True, "write": True, "manage": True}
    assert supervisor_access == {"read": True, "write": False, "manage": True}


@pytest.mark.asyncio
async def test_thread_upsert_omits_access_without_a_session() -> None:
    registry = ConnectionRegistry()
    anonymous = _Socket()
    await registry.register("agent-1", anonymous)

    await broadcast_thread_upsert(registry, "agent-1", _thread())

    thread = json.loads(anonymous.messages[0])["payload"]["thread"]
    assert "access" not in thread
