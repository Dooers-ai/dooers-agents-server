from dataclasses import dataclass, field
from datetime import datetime

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

    def members_for_queue(self, slug: str | None = None) -> list[WorkspaceQueueMember]:
        """Members of ``slug``, or of the current thread queue when omitted."""
        target = self.queue if slug is None else slug
        if not target:
            return []
        for queue in self.queues:
            if queue.slug == target:
                return list(queue.members)
        return []
