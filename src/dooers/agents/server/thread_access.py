"""Thread read / write / manage capabilities and same-day view grants.

Personal 1:1 threads use empty ``workspace_id``. Elevated org roles may
supervise (read + manage) without write until they add themselves as a
participant. System admins observe read-only when not a participant.
Workspace managers only elevate inside a real workspace (never on personal 1:1).

View grants for elevated (non-participant) reads are stored **in-process**
until end of UTC day — no core round-trip.

``threadSupervision`` mode (seeded as ``__dooers_thread_supervision_mode``) controls
when elevated reads require ``access_reason``:
- ``strict``: always
- ``operational``: only personal 1:1 (empty workspace_id)
- ``open``: never
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from dooers.agents.server.protocol.models import Thread, ThreadAccess, User

ThreadSupervisionMode = Literal["strict", "operational", "open"]

# Must match dooers-service-core THREAD_SUPERVISION_MODE_SEED_KEY
THREAD_SUPERVISION_MODE_SEED_KEY = "__dooers_thread_supervision_mode"
_VALID_MODES = frozenset({"strict", "operational", "open"})


def normalize_thread_supervision_mode(raw: Any) -> ThreadSupervisionMode:
    if isinstance(raw, str) and raw.strip() in _VALID_MODES:
        return raw.strip()  # type: ignore[return-value]
    return "strict"


def thread_supervision_mode_from_settings(settings: dict[str, Any] | None) -> ThreadSupervisionMode:
    """Resolve mode from seeded settings, then optional process env, else strict."""
    if isinstance(settings, dict):
        seeded = settings.get(THREAD_SUPERVISION_MODE_SEED_KEY)
        if isinstance(seeded, str) and seeded.strip() in _VALID_MODES:
            return seeded.strip()  # type: ignore[return-value]
    env = (os.environ.get("AGENT_THREAD_SUPERVISION_MODE") or "").strip()
    if env in _VALID_MODES:
        return env  # type: ignore[return-value]
    return "strict"


def thread_supervision_requires_reason(
    mode: ThreadSupervisionMode,
    *,
    workspace_id: str = "",
) -> bool:
    if mode == "open":
        return False
    if mode == "operational":
        return not (workspace_id or "").strip()
    return True


def user_is_thread_participant(user: User, thread: Thread) -> bool:
    """True if the connected user owns or participates in the thread."""
    identity_ids = {uid for uid in [user.user_id, *(user.identity_ids or [])] if uid}
    if not identity_ids:
        return False
    if thread.owner and thread.owner.user_id and thread.owner.user_id in identity_ids:
        return True
    for participant in thread.users or []:
        if participant.user_id and participant.user_id in identity_ids:
            return True
        for iid in participant.identity_ids or []:
            if iid in identity_ids:
                return True
    return False


def actor_id_is_participant(actor_id: str, thread: Thread) -> bool:
    """True if ``actor_id`` (user_id or agent_id) appears in thread users/owner."""
    aid = (actor_id or "").strip()
    if not aid:
        return False
    if thread.owner and thread.owner.user_id == aid:
        return True
    for participant in thread.users or []:
        if participant.user_id == aid:
            return True
        if aid in (participant.identity_ids or []):
            return True
    return False


def _is_org_elevated(user: User) -> bool:
    return user.organization_role in ("owner", "manager")


def _is_workspace_elevated(user: User) -> bool:
    return user.workspace_role in ("manager", "owner")


def _is_system_admin(user: User) -> bool:
    return (user.system_role or "") == "admin"


def resolve_list_scope(user: User, *, workspace_id: str = "") -> str:
    """Scope for ``list_threads`` / ``count_threads``.

    Empty connection workspace + org elevation uses ``organization`` scope;
    persistence then restricts to personal threads (``workspace_id=''``) so
    workspace chats are not mixed into the personal agent inbox.
    """
    ws = (workspace_id or "").strip()
    if _is_system_admin(user):
        return "admin"
    if _is_org_elevated(user):
        return "organization"
    if ws and _is_workspace_elevated(user):
        return "workspace"
    return "member"


def resolve_thread_access(
    user: User,
    thread: Thread,
    *,
    connection_workspace_id: str = "",
) -> ThreadAccess:
    """Capabilities for one thread given the connected session."""
    if user_is_thread_participant(user, thread):
        return ThreadAccess(read=True, write=True, manage=True)

    if _is_system_admin(user):
        return ThreadAccess(read=True, write=False, manage=False)

    thread_ws = (thread.workspace_id or "").strip()
    conn_ws = (connection_workspace_id or "").strip()

    if not thread_ws:
        # Personal 1:1 — org owner/manager may supervise; workspace managers cannot.
        if _is_org_elevated(user):
            return ThreadAccess(read=True, write=False, manage=True)
        return ThreadAccess()

    # Workspace-scoped thread
    if conn_ws and thread_ws != conn_ws and not _is_org_elevated(user):
        return ThreadAccess()

    if _is_org_elevated(user):
        return ThreadAccess(read=True, write=False, manage=True)
    if _is_workspace_elevated(user) and (not conn_ws or conn_ws == thread_ws):
        return ThreadAccess(read=True, write=False, manage=True)
    return ThreadAccess()


def attach_thread_access(
    thread: Thread,
    user: User,
    *,
    connection_workspace_id: str = "",
) -> Thread:
    access = resolve_thread_access(user, thread, connection_workspace_id=connection_workspace_id)
    return thread.model_copy(update={"access": access})


def agent_participant(agent_id: str, *, assistant_name: str = "Assistant") -> User:
    """Synthetic participant entry for the serving agent."""
    return User(user_id=agent_id, user_name=assistant_name)


def ensure_agent_in_users(
    thread: Thread,
    *,
    agent_id: str,
    assistant_name: str = "Assistant",
) -> Thread:
    """Return thread with agent_id present in ``users`` (lazy backfill for old rows)."""
    aid = (agent_id or thread.agent_id or "").strip()
    if not aid:
        return thread
    if actor_id_is_participant(aid, thread):
        return thread
    users = list(thread.users or [])
    users.append(agent_participant(aid, assistant_name=assistant_name))
    return thread.model_copy(update={"users": users})


def end_of_utc_day(now: datetime | None = None) -> datetime:
    """Grant expiry: end of the current UTC calendar day."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=1)


class ThreadViewGrantStore:
    """In-process same-day view grants for elevated (non-participant) reads.

    Shared across WebSocket connections on one AgentServer process. Multi-replica
    or restart may re-prompt for a reason — acceptable.
    """

    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], datetime] = {}

    @staticmethod
    def _key(user_id: str, thread_id: str) -> tuple[str, str]:
        return (user_id, thread_id)

    def has_valid_grant(self, user_id: str, thread_id: str, *, now: datetime | None = None) -> bool:
        if not user_id or not thread_id:
            return False
        expires = self._grants.get(self._key(user_id, thread_id))
        if expires is None:
            return False
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if current >= expires:
            self._grants.pop(self._key(user_id, thread_id), None)
            return False
        return True

    def put_grant(self, user_id: str, thread_id: str, *, now: datetime | None = None) -> datetime:
        expires = end_of_utc_day(now)
        self._grants[self._key(user_id, thread_id)] = expires
        return expires


# Back-compat names used by router / tests
def resolve_scope(user: User, *, workspace_id: str = "") -> str:
    return resolve_list_scope(user, workspace_id=workspace_id)


def _user_is_thread_participant(user: User, thread: Thread) -> bool:
    return user_is_thread_participant(user, thread)


def _can_access_thread(user: User, thread: Thread, *, connection_workspace_id: str) -> bool:
    return resolve_thread_access(user, thread, connection_workspace_id=connection_workspace_id).read
