from dataclasses import dataclass, field

from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.protocol.models import ContentPart


@dataclass
class AgentIncoming:
    """Represents an incoming message with its complete context.

    Attributes:
      message: Extracted text from content parts
      content: Full content parts from the message
      context: AgentContext with metadata (thread, org, user info)
      form_data: Submitted form values (None if not a form response)
      form_cancelled: Whether the form was cancelled
      form_event_id: ID of the original form event being responded to
    """

    message: str
    content: list[ContentPart]
    context: AgentContext
    form_data: dict | None = field(default=None)
    form_cancelled: bool = field(default=False)
    form_event_id: str | None = field(default=None)

    def __post_init__(self) -> None:
        # Bind platform-owned context outside creator/LLM arguments. Import stays lazy so
        # handlers that never use managed RAG do not pay a module dependency at startup.
        from dooers.tools.rag.runtime import bind_execution_context

        bind_execution_context(
            agent_id=self.context.agent_id,
            organization_id=self.context.organization_id,
            workspace_id=self.context.workspace_id,
            user_id=self.context.user.user_id,
            channel=self.context.channel,
        )
