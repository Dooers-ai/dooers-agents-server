"""In-memory Persistence double for pipeline/router tests (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dooers.agents.server.protocol.models import Run, Thread, ThreadEvent, User


class MemoryPersistence:
    def __init__(self) -> None:
        self.threads: dict[str, Thread] = {}
        self.events: dict[str, ThreadEvent] = {}
        self.runs: dict[str, Run] = {}
        self.settings: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def migrate(self) -> None: ...

    async def create_thread(self, thread: Thread) -> None:
        self.threads[thread.id] = thread.model_copy(deep=True)

    async def get_thread(self, thread_id: str) -> Thread | None:
        thread = self.threads.get(thread_id)
        return thread.model_copy(deep=True) if thread else None

    async def update_thread(self, thread: Thread) -> None:
        self.threads[thread.id] = thread.model_copy(deep=True)

    async def delete_thread(self, thread_id: str) -> None:
        self.threads.pop(thread_id, None)

    async def upsert_thread_participant(self, thread_id: str, user: User) -> None:
        thread = self.threads.get(thread_id)
        if thread and all(u.user_id != user.user_id for u in thread.users):
            thread.users.append(user)

    async def create_event(self, event: ThreadEvent) -> None:
        self.events[event.id] = event.model_copy(deep=True)

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        event = self.events.get(event_id)
        return event.model_copy(deep=True) if event else None

    async def update_event(self, event: ThreadEvent) -> None:
        self.events[event.id] = event.model_copy(deep=True)

    async def delete_event(self, event_id: str) -> None:
        self.events.pop(event_id, None)

    async def get_events(self, thread_id: str, **kwargs: Any) -> list[ThreadEvent]:
        rows = sorted((e for e in self.events.values() if e.thread_id == thread_id), key=lambda e: e.created_at)
        if kwargs.get("order") == "desc":
            rows.reverse()
        return rows[: kwargs.get("limit", 50)]

    async def create_run(self, run: Run) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    async def update_run(self, run: Run) -> None:
        self.runs[run.id] = run.model_copy(deep=True)

    async def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    async def list_runs(self, thread_id=None, agent_id=None, status=None, limit=50) -> list[Run]:
        rows = [r for r in self.runs.values() if (thread_id is None or r.thread_id == thread_id) and (status is None or r.status == status)]
        return rows[:limit]

    async def get_settings(self, agent_id: str) -> dict[str, Any]:
        return dict(self.settings.get(agent_id, {}))

    async def update_setting(self, agent_id: str, field_id: str, value: Any) -> datetime:
        self.settings.setdefault(agent_id, {})[field_id] = value
        return datetime.now(UTC)

    async def set_settings(self, agent_id: str, values: dict[str, Any]) -> datetime:
        self.settings[agent_id] = dict(values)
        return datetime.now(UTC)

    async def get_service_secrets(self, agent_id: str) -> dict[str, Any]:
        return {}

    async def insert_analytics_events(self, events) -> None: ...

    # helpers
    def messages(self, thread_id: str, actor: str = "assistant") -> list[ThreadEvent]:
        return sorted(
            (e for e in self.events.values() if e.thread_id == thread_id and e.actor == actor and e.type == "message"),
            key=lambda e: e.created_at,
        )


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:  # pragma: no cover - not used
        raise RuntimeError("not used")

    def frames(self, frame_type: str | None = None) -> list[dict[str, Any]]:
        import json

        out = [json.loads(raw) for raw in self.sent]
        return [f for f in out if frame_type is None or f.get("type") == frame_type]
