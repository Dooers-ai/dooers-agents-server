from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.protocol.models import ContentPart

if TYPE_CHECKING:
    from dooers.tools.rag.runtime import RAGContextToken


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
        # Legacy mode binds here and drops the token, as production does: the instance keeps
        # only its fields, so asdict/deepcopy/pickle/repr/eq stay the production ones.
        # Maestro mode binds in ``maestro_handler_context``, which keeps the token and resets it.
        # getattr: production read only the fields, so a duck-typed context without ``maestro`` binds too.
        if getattr(self.context, "maestro", None) is None:
            self.bind_runtime_context()

    def bind_runtime_context(self, *, inherit_children: bool = True) -> "RAGContextToken | None":
        """Bind the platform RAG context to the current task and return the reset token.

        ``inherit_children=False`` (Maestro private scopes) keeps child tasks out of the context.

        The token is never stored on the instance; the caller that binds also resets.
        """
        # Bind platform-owned context outside creator/LLM arguments. Import stays lazy so
        # handlers that never use managed RAG do not pay a module dependency at startup.
        from dooers.tools.rag.runtime import bind_execution_context

        return bind_execution_context(
            agent_id=self.context.agent_id,
            organization_id=self.context.organization_id,
            workspace_id=self.context.workspace_id,
            user_id=self.context.user.user_id,
            channel=self.context.channel,
            thread_id=getattr(self.context, "thread_id", ""),
            inherit_children=inherit_children,
        )

    @staticmethod
    def reset_runtime_context(token: "RAGContextToken | None") -> None:
        """Restore the RAG context that ``bind_runtime_context`` replaced (``None``: no-op)."""
        from dooers.tools.rag.runtime import reset_execution_context

        reset_execution_context(token)
