"""Personal (empty workspace) threads use participant-only list scope."""

from datetime import UTC, datetime

from dooers.agents.server.handlers.router import (
    _can_access_thread,
    _user_is_thread_participant,
    resolve_scope,
)
from dooers.agents.server.protocol.models import Thread, User


def test_resolve_scope_empty_workspace_forces_member_for_org_manager():
    user = User(
        user_id="u1",
        organization_role="manager",
        workspace_role="manager",
        system_role="user",
    )
    assert resolve_scope(user, workspace_id="") == "member"
    assert resolve_scope(user, workspace_id="   ") == "member"


def test_resolve_scope_real_workspace_keeps_org_elevated():
    user = User(
        user_id="u1",
        organization_role="manager",
        workspace_role="member",
        system_role="user",
    )
    assert resolve_scope(user, workspace_id="ws-1") == "organization"


def test_resolve_scope_real_workspace_workspace_manager():
    user = User(
        user_id="u1",
        organization_role="member",
        workspace_role="manager",
        system_role="user",
    )
    assert resolve_scope(user, workspace_id="ws-1") == "workspace"


def _thread(*, workspace_id: str, owner_id: str, participant_ids: list[str]) -> Thread:
    now = datetime.now(UTC)
    return Thread(
        id="t1",
        agent_id="a1",
        organization_id="o1",
        workspace_id=workspace_id,
        owner=User(user_id=owner_id),
        users=[User(user_id=pid) for pid in participant_ids],
        created_at=now,
        updated_at=now,
        last_event_at=now,
    )


def test_personal_thread_requires_participant():
    thread = _thread(workspace_id="", owner_id="owner", participant_ids=["owner", "u1"])
    participant = User(user_id="u1", organization_role="manager", workspace_role="manager")
    stranger = User(user_id="u2", organization_role="manager", workspace_role="manager")
    assert _user_is_thread_participant(participant, thread) is True
    assert _can_access_thread(participant, thread, connection_workspace_id="") is True
    assert _can_access_thread(stranger, thread, connection_workspace_id="") is False


def test_workspace_thread_manager_can_access():
    thread = _thread(workspace_id="ws-1", owner_id="owner", participant_ids=["owner"])
    manager = User(user_id="mgr", organization_role="member", workspace_role="manager")
    assert _can_access_thread(manager, thread, connection_workspace_id="ws-1") is True
