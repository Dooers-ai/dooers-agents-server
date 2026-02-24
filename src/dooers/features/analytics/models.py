from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel


class AnalyticsEvent(StrEnum):
    """Built-in analytics event types."""

    # Automatic events
    THREAD_CREATED = "thread.created"
    MESSAGE_C2S = "message.c2s"
    MESSAGE_S2C = "message.s2c"
    TOOL_CALLED = "tool.called"
    ERROR_OCCURRED = "error.occurred"

    # Feedback events
    FEEDBACK_LIKE = "feedback.like"
    FEEDBACK_DISLIKE = "feedback.dislike"


class AnalyticsEventPayload(BaseModel):
    """Payload for an analytics event."""

    event: str  # AnalyticsEvent value or custom string
    timestamp: datetime
    worker_id: str
    thread_id: str | None = None
    user_id: str | None = None
    run_id: str | None = None
    event_id: str | None = None
    data: dict[str, Any] | None = None


class FeedbackData(BaseModel):
    """Feedback targeting information."""

    target_type: Literal["event", "run", "thread"]
    target_id: str
    reason: str | None = None


class AnalyticsBatch(BaseModel):
    """Batch of analytics events for webhook delivery."""

    batch_id: str
    worker_id: str
    events: list[AnalyticsEventPayload]
    sent_at: datetime
