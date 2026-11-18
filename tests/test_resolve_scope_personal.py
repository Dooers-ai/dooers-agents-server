"""Thread access capabilities and same-day view grants."""

from datetime import UTC, datetime

from dooers.agents.server.handlers.router import (
    _can_access_thread,
    resolve_scope,
)
from dooers.agents.server.protocol.models import Thread, User
from dooers.agents.server.thread_access import (
    ThreadViewGrantStore,
    actor_id_is_participant,
    end_of_utc_day,
    resolve_list_scope,
    resolve_thread_access,
    user_is_thread_participant,
)


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


def test_resolve_list_scope_empty_workspace_org_manager_is_organization():
    user = User(
        user_id="u1",
        organization_role="manager",
        workspace_role="manager",
        system_role="user",
    )
    assert resolve_list_scope(user, workspace_id="") == "organization"
    assert resolve_scope(user, workspace_id="") == "organization"


def test_resolve_list_scope_real_workspace_keeps_org_elevated():
    user = User(
        user_id="u1",
        organization_role="manager",
        workspace_role="member",
        system_role="user",
    )
    assert resolve_list_scope(user, workspace_id="ws-1") == "organization"


def test_resolve_list_scope_workspace_manager():
    user = User(
        user_id="u1",
        organization_role="member",
        workspace_role="manager",
        system_role="user",
    )
    assert resolve_list_scope(user, workspace_id="ws-1") == "workspace"
    assert resolve_list_scope(user, workspace_id="") == "member"


def test_personal_thread_org_manager_read_manage_no_write():
    thread = _thread(workspace_id="", owner_id="owner", participant_ids=["owner", "a1"])
    manager = User(user_id="mgr", organization_role="manager", workspace_role="manager")
    access = resolve_thread_access(manager, thread, connection_workspace_id="")
    assert access.read is True
    assert access.write is False
    assert access.manage is True
    assert _can_access_thread(manager, thread, connection_workspace_id="") is True


def test_personal_thread_workspace_manager_none():
    thread = _thread(workspace_id="", owner_id="owner", participant_ids=["owner"])
    ws_mgr = User(user_id="wm", organization_role="member", workspace_role="manager")
    access = resolve_thread_access(ws_mgr, thread, connection_workspace_id="")
    assert access.read is False
    assert access.write is False
    assert access.manage is False


def test_personal_thread_system_admin_read_only():
    thread = _thread(workspace_id="", owner_id="owner", participant_ids=["owner"])
    admin = User(user_id="sa", system_role="admin", organization_role="member")
    access = resolve_thread_access(admin, thread, connection_workspace_id="")
    assert access.read is True
    assert access.write is False
    assert access.manage is False


def test_participant_full_access():
    thread = _thread(workspace_id="", owner_id="owner", participant_ids=["owner", "u1", "a1"])
    participant = User(user_id="u1", organization_role="member")
    access = resolve_thread_access(participant, thread, connection_workspace_id="")
    assert access.read and access.write and access.manage
    assert user_is_thread_participant(participant, thread) is True


def test_workspace_thread_manager_read_manage():
    thread = _thread(workspace_id="ws-1", owner_id="owner", participant_ids=["owner"])
    manager = User(user_id="mgr", organization_role="member", workspace_role="manager")
    access = resolve_thread_access(manager, thread, connection_workspace_id="ws-1")
    assert access.read is True
    assert access.write is False
    assert access.manage is True


def test_actor_id_is_participant_for_agent():
    thread = _thread(workspace_id="", owner_id="u1", participant_ids=["u1", "agent-xyz"])
    assert actor_id_is_participant("agent-xyz", thread) is True
    assert actor_id_is_participant("other", thread) is False


def test_view_grant_store_same_utc_day():
    store = ThreadViewGrantStore()
    now = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    assert store.has_valid_grant("u1", "t1", now=now) is False
    expires = store.put_grant("u1", "t1", now=now)
    assert expires == end_of_utc_day(now)
    assert store.has_valid_grant("u1", "t1", now=now) is True
    assert store.has_valid_grant("u1", "t1", now=datetime(2026, 9, 10, 23, 59, tzinfo=UTC)) is True
    assert store.has_valid_grant("u1", "t1", now=datetime(2026, 9, 11, 0, 0, tzinfo=UTC)) is False


def test_thread_supervision_requires_reason():
    from dooers.agents.server.thread_access import thread_supervision_requires_reason

    assert thread_supervision_requires_reason("strict", workspace_id="ws") is True
    assert thread_supervision_requires_reason("operational", workspace_id="") is True
    assert thread_supervision_requires_reason("operational", workspace_id="ws") is False
    assert thread_supervision_requires_reason("open", workspace_id="") is False


def test_thread_supervision_mode_from_settings(monkeypatch):
    from dooers.agents.server.thread_access import (
        THREAD_SUPERVISION_MODE_SEED_KEY,
        thread_supervision_mode_from_settings,
    )

    assert thread_supervision_mode_from_settings(None) == "strict"
    assert (
        thread_supervision_mode_from_settings({THREAD_SUPERVISION_MODE_SEED_KEY: "open"}) == "open"
    )
    monkeypatch.setenv("AGENT_THREAD_SUPERVISION_MODE", "operational")
    assert thread_supervision_mode_from_settings({}) == "operational"
