from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from dooers.protocol.models import Run, Thread, ThreadEvent, User

if TYPE_CHECKING:
    from dooers.features.analytics.models import AnalyticsEventPayload

EventOrder = Literal["asc", "desc"]

FILTERABLE_FIELDS = {"type", "actor", "user_id", "user_name", "user_email", "run_id"}


class Persistence(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def migrate(self) -> None: ...
    async def create_thread(self, thread: Thread) -> None: ...
    async def get_thread(self, thread_id: str) -> Thread | None: ...
    async def update_thread(self, thread: Thread) -> None: ...
    async def list_threads(
        self,
        worker_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        cursor: str | None,
        limit: int,
        scope: str = "member",
        user_email: str | None = None,
    ) -> list[Thread]: ...
    async def count_threads(
        self,
        worker_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        scope: str = "member",
        user_email: str | None = None,
    ) -> int: ...
    async def delete_thread(self, thread_id: str) -> None: ...
    async def create_event(self, event: ThreadEvent) -> None: ...
    async def update_event(self, event: ThreadEvent) -> None: ...  # Persist all stored fields from event (generic update)
    async def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: EventOrder = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]: ...
    async def create_run(self, run: Run) -> None: ...
    async def update_run(self, run: Run) -> None: ...
    async def get_event(self, event_id: str) -> ThreadEvent | None: ...
    async def delete_event(self, event_id: str) -> None: ...
    async def get_run(self, run_id: str) -> Run | None: ...
    async def list_runs(
        self,
        thread_id: str | None = None,
        worker_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Run]: ...
    async def upsert_thread_participant(self, thread_id: str, user: User) -> None: ...

    async def get_settings(self, worker_id: str) -> dict[str, Any]: ...
    async def update_setting(self, worker_id: str, field_id: str, value: Any) -> datetime: ...
    async def set_settings(self, worker_id: str, values: dict[str, Any]) -> datetime: ...

    async def insert_analytics_events(self, events: list[AnalyticsEventPayload]) -> None: ...
