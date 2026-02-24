from dataclasses import dataclass, field
from datetime import datetime

from dooers.protocol.models import User


@dataclass
class WorkerContext:
    """Contextual metadata for the incoming message."""

    thread_id: str
    event_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user: User = field(default_factory=lambda: User(user_id=""))
    thread_title: str | None = field(default=None)
    thread_created_at: datetime | None = field(default=None)
