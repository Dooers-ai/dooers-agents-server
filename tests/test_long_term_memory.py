"""Long-term memory isolation and delegated identity propagation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dooers.agents.server.handlers.memory_store import LongTermMemoryStore, MemoryUnavailableError
from dooers.tools.rag.runtime import bind_execution_context, current_execution_context

AGENT = "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_core_verified_identity_is_inherited_by_child_tool_task():
    bind_execution_context(
        agent_id=AGENT, organization_id="org", workspace_id="ws",
        user_id="user-a", channel="dooers-platform", auth_assertion="signed-handshake",
    )

    async def child():
        ctx = current_execution_context()
        assert ctx.user_id == "user-a"
        assert ctx.auth_assertion == "signed-handshake"
        bind_execution_context(agent_id=AGENT, channel="dooers-platform")
        assert current_execution_context().auth_assertion == "signed-handshake"

    await asyncio.create_task(child())
    assert current_execution_context().user_id == "user-a"


@pytest.mark.asyncio
async def test_different_user_or_agent_does_not_inherit_delegation():
    bind_execution_context(agent_id=AGENT, user_id="user-a", channel="dooers-platform",
                           auth_assertion="signed-handshake")
    bind_execution_context(agent_id=AGENT, user_id="user-b", channel="dooers-platform")
    assert current_execution_context().auth_assertion is None
    bind_execution_context(agent_id="other-agent", channel="dooers-platform")
    assert current_execution_context().user_id == ""


def test_memory_rejects_user_without_core_assertion():
    bind_execution_context(agent_id=AGENT, user_id="user-a", channel="dooers-platform")
    persistence = Mock()
    persistence._prefix = "agent_"
    context = SimpleNamespace(
        agent_id=AGENT, organization_id="org", thread_id="thread", event_id="event",
        user=SimpleNamespace(user_id="user-a"), channel="dooers-platform",
    )
    store = LongTermMemoryStore(persistence, context)
    with pytest.raises(MemoryUnavailableError):
        store._identity()
