from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .collector import AnalyticsCollector


class WorkerAnalytics:
    """
    Handler API for analytics tracking.

    Provides methods for tracking custom events and recording feedback.
    Passed as the fourth parameter to handler functions.
    """

    def __init__(
        self,
        worker_id: str,
        thread_id: str,
        user_id: str | None,
        run_id: str | None,
        collector: AnalyticsCollector,
    ) -> None:
        self._worker_id = worker_id
        self._thread_id = thread_id
        self._user_id = user_id
        self._run_id = run_id
        self._collector = collector

    async def track(self, event: str, data: dict[str, Any] | None = None) -> None:
        """
        Track a custom analytics event.

        Args:
            event: Event name (e.g., "llm.called", "search.performed")
            data: Optional event-specific data
        """
        await self._collector.track(
            event=event,
            worker_id=self._worker_id,
            thread_id=self._thread_id,
            user_id=self._user_id,
            run_id=self._run_id,
            data=data,
        )

    async def like(
        self,
        target_type: str,
        target_id: str,
        reason: str | None = None,
    ) -> None:
        """
        Record a like (positive feedback).

        Useful for external sources like WhatsApp where users can't
        interact with the dashboard directly.

        Args:
            target_type: Type of target ("event", "run", or "thread")
            target_id: ID of the target
            reason: Optional reason for the feedback
        """
        await self._collector.feedback(
            feedback_type="like",
            target_type=target_type,
            target_id=target_id,
            worker_id=self._worker_id,
            thread_id=self._thread_id,
            user_id=self._user_id,
            reason=reason,
        )

    async def dislike(
        self,
        target_type: str,
        target_id: str,
        reason: str | None = None,
    ) -> None:
        """
        Record a dislike (negative feedback).

        Useful for external sources like WhatsApp where users can't
        interact with the dashboard directly.

        Args:
            target_type: Type of target ("event", "run", or "thread")
            target_id: ID of the target
            reason: Optional reason for the feedback
        """
        await self._collector.feedback(
            feedback_type="dislike",
            target_type=target_type,
            target_id=target_id,
            worker_id=self._worker_id,
            thread_id=self._thread_id,
            user_id=self._user_id,
            reason=reason,
        )
