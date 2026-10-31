from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dooers.agents.server.protocol.models import (
    ChatContext,
    User,
    WorkspaceQueueCatalog,
    WorkspaceQueueMember,
)


@dataclass
class AgentContext:
    """Contextual metadata for the incoming message."""

    thread_id: str
    agent_id: str
    event_id: str
    organization_id: str = ""
    workspace_id: str = ""
    channel: str = "dooers-platform"
    channel_meta: dict | None = None
    user: User = field(default_factory=lambda: User(user_id=""))
    thread_title: str | None = field(default=None)
    thread_created_at: datetime | None = field(default=None)
    queue: str | None = None
    #: Workspace queue catalog from the connect handshake (snapshot).
    queues: list[WorkspaceQueueCatalog] = field(default_factory=list)
    chat_context: ChatContext | None = None
    #: Why this turn runs when no person typed it, e.g. ``{"type": "schedule", "schedule_id": ...}``.
    trigger: dict[str, Any] | None = None
    #: Set by the pipeline; ``cancelled`` reads it. Not part of the public constructor contract.
    _active_run: Any = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        """True once someone pressed Stop (``run.cancel``). Long loops should check it."""
        return bool(getattr(self._active_run, "cancel_requested", False))

    @property
    def timezone(self) -> str | None:
        """IANA time zone reported by the client for this turn (unvalidated hint)."""
        return self.chat_context.timezone if self.chat_context else None

    def members_for_queue(self, slug: str | None = None) -> list[WorkspaceQueueMember]:
        """Members of ``slug``, or of the current thread queue when omitted."""
        target = self.queue if slug is None else slug
        if not target:
            return []
        for queue in self.queues:
            if queue.slug == target:
                return list(queue.members)
        return []
