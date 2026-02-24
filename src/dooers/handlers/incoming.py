from dataclasses import dataclass

from dooers.handlers.context import WorkerContext
from dooers.protocol.models import ContentPart


@dataclass
class WorkerIncoming:
    """Represents an incoming message with its complete context.

    Attributes:
      message: Extracted text from content parts
      content: Full content parts from the message
      context: WorkerContext with metadata (thread, org, user info)
    """

    message: str
    content: list[ContentPart]
    context: WorkerContext
