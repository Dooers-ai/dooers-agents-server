"""Reading other people's threads needs the ``thread:view_others`` platform permission."""

from datetime import UTC, datetime

from dooers.agents.server.handlers.router import _can_access_creator_settings
from dooers.agents.server.protocol.models import Thread, User
from dooers.agents.server.thread_access import resolve_list_scope, resolve_thread_access


def _thread(workspace_id: str = "w1") -> Thread:
    now = datetime.now(UTC)
    return Thread(
        id="t1",
        agent_id="a1",
        organization_id="o1",
        workspace_id=workspace_id,
        owner=User(user_id="someone-else"),
        users=[],
        created_at=now,
        updated_at=now,
        last_event_at=now,
    )


def _manager(**overrides) -> User:
    return User(
        user_id="u1",
        organization_role="manager",
        workspace_role="manager",
        **overrides,
    )


def test_manager_with_permission_supervises():
    user = _manager(organization_member_role="manager", permissions=["thread:view_others"])
    assert resolve_thread_access(user, _thread(), connection_workspace_id="w1").read
    assert resolve_list_scope(user, workspace_id="") == "organization"


def test_manager_without_permission_on_new_core_does_not_read():
    # New core sends the explicit list; an empty one means no supervision even if a
    # stale/forged role still says manager.
    user = _manager(organization_member_role="manager", permissions=[])
    assert not resolve_thread_access(user, _thread(), connection_workspace_id="w1").read
    assert not resolve_thread_access(user, _thread(workspace_id=""), connection_workspace_id="").read
    assert resolve_list_scope(user, workspace_id="w1") == "member"


def test_older_core_without_permissions_keeps_role_rules():
    user = _manager()
    assert resolve_thread_access(user, _thread(), connection_workspace_id="w1").read


def test_creator_settings_follow_the_actual_org_role():
    # Core lowers organization_role for thread supervision; creator settings stay with admins.
    user = User(
        user_id="u1",
        organization_role="member",
        organization_member_role="manager",
        permissions=[],
    )
    assert _can_access_creator_settings(user, agent_owner_user_id=None)
    plain = User(user_id="u2", organization_role="member", organization_member_role="member")
    assert not _can_access_creator_settings(plain, agent_owner_user_id=None)
