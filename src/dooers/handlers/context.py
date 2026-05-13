from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dooers.protocol.models import User


@dataclass(frozen=True)
class ContextOrganization:
    id: str = ""
    role: str = "member"
    plan: str = "free"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextWorkspace:
    id: str = ""
    role: str = "member"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextAgent:
    id: str = ""
    owner_user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    """Contextual metadata for the incoming message."""

    thread_id: str
    agent_id: str
    event_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user: User = field(default_factory=lambda: User(user_id=""))
    organization: ContextOrganization = field(default_factory=ContextOrganization)
    workspace: ContextWorkspace = field(default_factory=ContextWorkspace)
    agent: ContextAgent = field(default_factory=ContextAgent)
    thread_title: str | None = field(default=None)
    thread_created_at: datetime | None = field(default=None)
