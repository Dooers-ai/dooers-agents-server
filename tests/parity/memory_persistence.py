"""In-memory ``Persistence`` for the parity suite.

The SDK has no SQLite backend: production uses ``PostgresPersistence`` (``postgres`` and
``dooers``) or Cosmos. This class keeps rows the way the Postgres backend writes and
reads them, so the legacy results match production:

- rows hold JSON text, so stored data never follows later mutation of the models;
- events keep only the Postgres columns (``user`` as id/name/email; no
  ``client_event_id``, ``streaming`` or ``finalized``);
- empty ``data`` is stored as NULL on insert, ``update_event`` keeps the old data
  when the new one is ``None`` (``COALESCE``) and clears empty content;
- ``update_thread`` does not touch ``metadata``; ``upsert_thread_participant``
  replaces the matching user or appends it;
- ordering is ``created_at`` / ``last_event_at`` / ``started_at`` like the SQL, with
  insertion order breaking ties.

Only the methods the scenarios use are implemented. The others raise
``NotImplementedError``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from dooers.agents.server.persistence.base import FILTERABLE_FIELDS
from dooers.agents.server.persistence.sanitize import without_nul
from dooers.agents.server.protocol.models import (
    Run,
    Thread,
    ThreadEvent,
    User,
    deserialize_s2c_part,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _json_or_none(value: Any) -> str | None:
    return json.dumps(value) if value is not None else None


def _loads(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


class MemoryPersistence:
    def __init__(self) -> None:
        self._threads: dict[str, dict[str, Any]] = {}
        self._events: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._settings: dict[str, dict[str, Any]] = {}
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    # -- Parity helpers (not part of the Persistence protocol) --

    def thread_ids(self) -> list[str]:
        """Thread ids in creation order."""
        return sorted(self._threads, key=lambda tid: self._threads[tid]["seq"])

    # -- Lifecycle --

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def migrate(self) -> None:
        return None

    # -- Threads --

    async def create_thread(self, thread: Thread) -> None:
        if thread.id in self._threads:
            raise ValueError(f"duplicate key: thread {thread.id}")
        self._threads[thread.id] = {
            "seq": self._next_sequence(),
            "id": thread.id,
            "agent_id": thread.agent_id,
            "organization_id": thread.organization_id,
            "workspace_id": thread.workspace_id,
            "owner": json.dumps(thread.owner.model_dump()),
            "users": json.dumps([u.model_dump() for u in thread.users]),
            "title": thread.title,
            "metadata": _json_or_none(thread.metadata),
            "queue": thread.queue,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            "last_event_at": thread.last_event_at,
        }

    def _row_to_thread(self, row: dict[str, Any]) -> Thread:
        owner = json.loads(row["owner"]) if row["owner"] else {"user_id": ""}
        users = json.loads(row["users"]) if row["users"] else []
        return Thread(
            id=row["id"],
            agent_id=row["agent_id"],
            organization_id=row["organization_id"] or "",
            workspace_id=row["workspace_id"] or "",
            owner=User(**owner),
            users=[User(**u) for u in users],
            title=row["title"],
            metadata=_loads(row["metadata"]),
            queue=row["queue"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_event_at=row["last_event_at"],
        )

    async def get_thread(self, thread_id: str) -> Thread | None:
        row = self._threads.get(thread_id)
        return self._row_to_thread(row) if row else None

    async def update_thread(self, thread: Thread) -> None:
        row = self._threads.get(thread.id)
        if row is None:
            return
        row.update(
            organization_id=thread.organization_id,
            workspace_id=thread.workspace_id,
            owner=json.dumps(thread.owner.model_dump()),
            users=json.dumps([u.model_dump() for u in thread.users]),
            title=thread.title,
            queue=thread.queue,
            updated_at=thread.updated_at,
            last_event_at=thread.last_event_at,
        )

    async def list_threads(
        self,
        agent_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        cursor: str | None,
        limit: int,
        scope: str = "member",
        user_email: str | None = None,
        identity_ids: list[str] | None = None,
        queue: str | None = None,
        title_query: str | None = None,
    ) -> list[Thread]:
        if scope != "member" or cursor or queue or title_query:
            raise NotImplementedError("list_threads: only the member scope without cursor/queue/title")
        rows = []
        for row in self._threads.values():
            if row["agent_id"] != agent_id:
                continue
            if row["organization_id"] != organization_id or row["workspace_id"] != workspace_id:
                continue
            users = json.loads(row["users"]) if row["users"] else []
            matches: list[bool] = []
            if user_id:
                matches.append(any(u.get("user_id") == user_id for u in users))
            if user_email:
                matches.append(any(u.get("user_email") == user_email for u in users))
            if identity_ids:
                matches.append(
                    any(u.get("user_id") in identity_ids for u in users)
                    or any(iid in identity_ids for u in users for iid in (u.get("identity_ids") or []))
                )
            if matches and not any(matches):
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["last_event_at"], r["id"]), reverse=True)
        return [self._row_to_thread(r) for r in rows[:limit]]

    async def count_threads(self, *args: Any, **kwargs: Any) -> int:
        raise NotImplementedError("count_threads")

    async def delete_thread(self, thread_id: str) -> None:
        self._events = {k: v for k, v in self._events.items() if v["thread_id"] != thread_id}
        self._runs = {k: v for k, v in self._runs.items() if v["thread_id"] != thread_id}
        self._threads.pop(thread_id, None)

    async def delete_idle_guest_threads(self, max_idle_seconds: int) -> int:
        raise NotImplementedError("delete_idle_guest_threads")

    async def upsert_thread_participant(self, thread_id: str, user: User) -> None:
        if user.user_id:
            field, value = "user_id", user.user_id
        elif user.user_email:
            field, value = "user_email", user.user_email
        else:
            return
        row = self._threads.get(thread_id)
        if row is None:
            return
        users = json.loads(row["users"]) if row["users"] else []
        dumped = user.model_dump()
        if any(u.get(field) == value for u in users):
            users = [dumped if u.get(field) == value else u for u in users]
        else:
            users.append(dumped)
        row["users"] = json.dumps(users)
        row["updated_at"] = _now()

    # -- Events --

    async def create_event(self, event: ThreadEvent) -> None:
        if event.id in self._events:
            raise ValueError(f"duplicate key: event {event.id}")
        content = None
        if event.content:
            content = json.dumps(without_nul([part.model_dump() for part in event.content]))
        self._events[event.id] = {
            "seq": self._next_sequence(),
            "id": event.id,
            "thread_id": event.thread_id,
            "run_id": event.run_id,
            "type": event.type,
            "actor": event.actor,
            "author": event.author,
            "user_id": event.user.user_id or None,
            "user_name": event.user.user_name,
            "user_email": event.user.user_email,
            "content": content,
            "data": json.dumps(without_nul(event.data)) if event.data else None,
            "created_at": event.created_at,
        }

    def _row_to_event(self, row: dict[str, Any]) -> ThreadEvent:
        content = None
        if row["content"]:
            content = [deserialize_s2c_part(p) for p in json.loads(row["content"])]
        return ThreadEvent(
            id=row["id"],
            thread_id=row["thread_id"],
            run_id=row["run_id"],
            type=row["type"],
            actor=row["actor"],
            author=row["author"],
            user=User(
                user_id=row["user_id"] or "",
                user_name=row["user_name"],
                user_email=row["user_email"],
            ),
            content=content,
            data=_loads(row["data"]),
            created_at=row["created_at"],
        )

    async def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: str = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        rows = [r for r in self._events.values() if r["thread_id"] == thread_id]
        if after_event_id and after_event_id in self._events:
            ref = self._events[after_event_id]["created_at"]
            if order == "desc":
                rows = [r for r in rows if r["created_at"] < ref]
            else:
                rows = [r for r in rows if r["created_at"] > ref]
        if before_event_id and before_event_id in self._events:
            ref = self._events[before_event_id]["created_at"]
            rows = [r for r in rows if r["created_at"] < ref]
        for key, value in (filters or {}).items():
            if key in FILTERABLE_FIELDS:
                rows = [r for r in rows if r[key] == value]
        rows.sort(key=lambda r: (r["created_at"], r["seq"]), reverse=order == "desc")
        return [self._row_to_event(r) for r in rows[:limit]]

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        row = self._events.get(event_id)
        return self._row_to_event(row) if row else None

    async def find_event_by_whatsapp_message_id(self, wa_message_id: str) -> ThreadEvent | None:
        wanted = (wa_message_id or "").strip()
        if not wanted:
            return None
        found = []
        for row in self._events.values():
            delivery = (_loads(row["data"]) or {}).get("delivery")
            if isinstance(delivery, dict) and delivery.get("wa_message_id") == wanted:
                found.append(row)
        if not found:
            return None
        found.sort(key=lambda r: (r["created_at"], r["seq"]), reverse=True)
        return self._row_to_event(found[0])

    async def update_event(self, event: ThreadEvent) -> None:
        row = self._events.get(event.id)
        if row is None:
            return
        content = None
        if event.content:
            content = json.dumps([part.model_dump() for part in event.content])
        row["content"] = content
        row["run_id"] = event.run_id
        row["author"] = event.author
        if event.data is not None:
            row["data"] = json.dumps(without_nul(event.data))

    async def delete_event(self, event_id: str) -> None:
        self._events.pop(event_id, None)

    # -- Runs --

    async def create_run(self, run: Run) -> None:
        if run.id in self._runs:
            raise ValueError(f"duplicate key: run {run.id}")
        self._runs[run.id] = {"seq": self._next_sequence(), **run.model_dump()}

    async def update_run(self, run: Run) -> None:
        row = self._runs.get(run.id)
        if row is None:
            return
        row.update(agent_id=run.agent_id, status=run.status, ended_at=run.ended_at, error=run.error)

    @staticmethod
    def _row_to_run(row: dict[str, Any]) -> Run:
        return Run(**{k: v for k, v in row.items() if k != "seq"})

    async def get_run(self, run_id: str) -> Run | None:
        row = self._runs.get(run_id)
        return self._row_to_run(row) if row else None

    async def list_runs(
        self,
        thread_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Run]:
        rows = list(self._runs.values())
        if thread_id:
            rows = [r for r in rows if r["thread_id"] == thread_id]
        if agent_id:
            rows = [r for r in rows if r["thread_id"] in self._threads and self._threads[r["thread_id"]]["agent_id"] == agent_id]
        if status:
            rows = [r for r in rows if r["status"] == status]
        rows.sort(key=lambda r: (r["started_at"], r["seq"]), reverse=True)
        return [self._row_to_run(r) for r in rows[:limit]]

    # -- Settings and secrets --

    def _settings_row(self, agent_id: str) -> dict[str, Any]:
        return self._settings.setdefault(agent_id, {"values": "{}", "services_secrets": None})

    async def get_settings(self, agent_id: str) -> dict[str, Any]:
        row = self._settings.get(agent_id)
        return json.loads(row["values"]) if row else {}

    async def update_setting(self, agent_id: str, field_id: str, value: Any) -> datetime:
        values = await self.get_settings(agent_id)
        values[field_id] = value
        self._settings_row(agent_id)["values"] = json.dumps(values)
        return _now()

    async def set_settings(self, agent_id: str, values: dict[str, Any]) -> datetime:
        self._settings_row(agent_id)["values"] = json.dumps(values)
        return _now()

    async def get_service_secrets(self, agent_id: str) -> dict[str, Any]:
        row = self._settings.get(agent_id)
        if not row or row["services_secrets"] is None:
            return {}
        return json.loads(row["services_secrets"])

    async def merge_service_secrets(self, agent_id: str, patch: dict[str, str]) -> datetime:
        current = await self.get_service_secrets(agent_id)
        current.update(patch)
        self._settings_row(agent_id)["services_secrets"] = json.dumps(current)
        return _now()

    # -- Not used by the scenarios --

    async def get_worker_seed_hash_bytes(self, worker_id: str) -> bytes | None:
        raise NotImplementedError("get_worker_seed_hash_bytes")

    async def set_worker_seed_hash_bytes(self, worker_id: str, secret_hash: bytes) -> None:
        raise NotImplementedError("set_worker_seed_hash_bytes")

    async def insert_analytics_events(self, events: list[Any]) -> None:
        raise NotImplementedError("insert_analytics_events")
