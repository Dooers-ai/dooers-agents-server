"""The production ``AgentMemory`` over the delivery context of one Maestro execution.

``MaestroAgentMemory`` is the production ``AgentMemory`` over ``MaestroExecutionPersistence``:
a handler written for production reads the same history, in every format, in a Maestro
delivery (design 7.5). No production method changes.

``MaestroExecutionPersistence`` reads a new snapshot of the delivery context on each call
(``MaestroMemory.snapshot``: every read is authorized again, nothing is cached). It folds the
whole snapshot (``legacy_model.fold_events``) and converts it to the production models; then
``limit``, ``order``, ``filters``, ``after_event_id`` and ``before_event_id`` select from the
converted events, as the production query selects from the stored rows. Only the selected
events get temporary links.

``get_thread`` and ``get_events`` are the reads ``AgentMemory`` uses (``list_artifacts``
included). Every other method raises ``ThreadManagementModeError``: in an execution the agent
writes through ``send`` (plan 5 adds ``MaestroAgentPersistence`` for ``Repository`` and
``dispatch``).

This module imports the production handlers, so ``maestro/__init__`` and ``memory`` load it
only on use (``AgentConfig`` imports ``maestro`` at startup).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from dooers.agents.server.exceptions import ThreadManagementModeError
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.persistence.base import FILTERABLE_FIELDS, EventOrder, Persistence
from dooers.agents.server.protocol.models import Thread, ThreadEvent

from .legacy_model import DEFAULT_ASSISTANT_NAME, fold_events, to_thread, to_thread_event

if TYPE_CHECKING:
    from .artifacts import MaestroHydratedArtifact
    from .memory import MaestroArtifactReference, MaestroMemory


class _NoLinks:
    """Converts without links: the selection needs no URL, and only selected events get one."""

    async def url_for(self, artifact_id: str) -> str:
        return ""


def _column(event: ThreadEvent, key: str) -> str | None:
    """The value of a production filter column (``PostgresPersistence.create_event`` stores ``user_id or None``)."""
    if key == "user_id":
        return event.user.user_id or None
    if key in ("user_name", "user_email"):
        return getattr(event.user, key)
    return getattr(event, key)


async def _unsupported(*_args: Any, **_kwargs: Any) -> Any:
    raise ThreadManagementModeError("THREAD_MANAGEMENT_MISMATCH")


class MaestroExecutionPersistence(Persistence):
    """``Persistence`` that reads the thread of one Maestro delivery; nothing else.

    ``memory`` is the execution's ``MaestroMemory`` (its envelope, ``snapshot()`` and
    ``LinkResolver``). ``assistant_name`` is ``AgentConfig.assistant_name``.
    """

    def __init__(self, memory: MaestroMemory, *, assistant_name: str = DEFAULT_ASSISTANT_NAME) -> None:
        self._memory = memory
        self._assistant_name = assistant_name

    def _reads(self, thread_id: str) -> bool:
        """Only the delivery's thread exists here; another thread is unknown, as in production."""
        return thread_id == self._memory._envelope["thread_id"]

    def _conversion(self, participants: list[dict[str, Any]]) -> dict[str, Any]:
        agents = [participant for participant in participants if participant["kind"] == "agent"]
        return {
            "recipient_instance_id": self._memory._envelope["instance_agent_id"],
            "agent_names": {agent["instance_agent_id"]: agent["display_name"] for agent in agents},
            "multi_agent": sum(agent["state"] == "active" for agent in agents) > 1,
            "assistant_name": self._assistant_name,
        }

    async def get_thread(self, thread_id: str) -> Thread | None:
        if not self._reads(thread_id):
            return None
        from .context import _thread_at_cutoff

        snapshot = await self._memory.snapshot()
        envelope, events = self._memory._envelope, snapshot["events"] or [snapshot["trigger_event"]]
        # Title, queue and creation time by the rule on.context uses; the owner agent from thread.created.
        state = _thread_at_cutoff(events)
        created = next((event["payload"]["thread"] for event in events if event["type"] == "thread.created"), {})
        last = events[-1]["created_at"]
        view = {
            "thread_id": envelope["thread_id"],
            "organization_id": envelope["organization_id"],
            "workspace_id": envelope["workspace_id"],
            "owner_instance_agent_id": created.get("owner_instance_agent_id"),
            "participants": snapshot["participants"],
            "title": state["title"] or "",
            "queue": state["queue"],
            "created_at": state["created_at"].isoformat() if state["created_at"] else events[0]["created_at"],
            "updated_at": last,
        }
        return to_thread(view, recipient_instance_id=envelope["instance_agent_id"], assistant_name=self._assistant_name)

    async def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: EventOrder = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        if not self._reads(thread_id):
            return []
        snapshot = await self._memory.snapshot()
        conversion = self._conversion(snapshot["participants"])
        # The whole snapshot is folded first: an edit or removal can target an event of any page.
        rows = []
        for event in fold_events(snapshot["events"]):
            converted = await to_thread_event(event, links=_NoLinks(), **conversion)
            if converted is not None:
                rows.append((event, converted))
        # Stored order is the Maestro sequence (production: created_at). An unknown reference is ignored.
        positions = {converted.id: index for index, (_, converted) in enumerate(rows)}
        selected = list(enumerate(rows))
        if after_event_id in positions:
            reference = positions[after_event_id]
            selected = [(i, row) for i, row in selected if (i < reference if order == "desc" else i > reference)]
        if before_event_id in positions:
            reference = positions[before_event_id]
            selected = [(i, row) for i, row in selected if i < reference]
        for key, value in (filters or {}).items():
            if key in FILTERABLE_FIELDS:
                # SQL ``column = value``: NULL never matches.
                selected = [(i, row) for i, row in selected if (column := _column(row[1], key)) is not None and column == value]
        if order == "desc":
            selected.reverse()
        links = self._memory._links
        return [await to_thread_event(event, links=links, **conversion) for _, (event, _) in selected[: max(limit, 0)]]

    # Inside an execution the agent writes through ``send``; nothing else is read here.
    connect = disconnect = migrate = _unsupported
    create_thread = update_thread = list_threads = count_threads = delete_thread = delete_idle_guest_threads = _unsupported
    create_event = get_event = update_event = delete_event = _unsupported
    create_run = update_run = get_run = list_runs = upsert_thread_participant = _unsupported
    get_settings = update_setting = set_settings = _unsupported
    get_worker_seed_hash_bytes = set_worker_seed_hash_bytes = get_service_secrets = merge_service_secrets = _unsupported
    insert_analytics_events = _unsupported


class MaestroAgentMemory(AgentMemory):
    """The production ``AgentMemory`` a handler gets in a Maestro execution, plus the Maestro extras.

    Every production method is inherited unchanged and reads through
    ``MaestroExecutionPersistence``. The extras read the Maestro context as it is:
    ``hydrate_artifact`` gives the bytes of one attachment, ``list_maestro_artifacts`` the
    ``MaestroArtifactReference`` of each attachment (``MaestroMemory.list_artifacts``).
    """

    def __init__(self, memory: MaestroMemory, *, assistant_name: str = DEFAULT_ASSISTANT_NAME) -> None:
        super().__init__(memory._envelope["thread_id"], MaestroExecutionPersistence(memory, assistant_name=assistant_name))
        self._maestro = memory

    async def hydrate_artifact(self, reference: MaestroArtifactReference) -> MaestroHydratedArtifact:
        return await self._maestro.hydrate_artifact(reference)

    async def list_maestro_artifacts(
        self, *, cursor: str | None = None, limit: int = 30, direction: Literal["in", "out", "all"] = "all"
    ) -> tuple[list[MaestroArtifactReference], str | None, bool]:
        return await self._maestro.list_artifacts(cursor=cursor, limit=limit, direction=direction)
