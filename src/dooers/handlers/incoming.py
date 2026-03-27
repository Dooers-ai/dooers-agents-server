from dataclasses import dataclass, field

from dooers.handlers.context import WorkerContext
from dooers.protocol.models import ContentPart


@dataclass
class WorkerIncoming:
    """Represents an incoming message with its complete context.

    Attributes:
      message: Extracted text from content parts
      content: Full content parts from the message
      context: WorkerContext with metadata (thread, org, user info)
      form_data: Submitted form values (None if not a form response)
      form_cancelled: Whether the form was cancelled
      form_event_id: ID of the original form event being responded to
    """

    message: str
    content: list[ContentPart]
    context: WorkerContext
    form_data: dict | None = field(default=None)
    form_cancelled: bool = field(default=False)
    form_event_id: str | None = field(default=None)
