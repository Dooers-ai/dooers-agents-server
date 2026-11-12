"""The production ``AgentMemory`` over the delivery context of one Maestro execution.

``MaestroAgentMemory`` is the production ``AgentMemory`` over ``MaestroExecutionPersistence``:
a handler written for production reads the same history, in every format, in a Maestro
delivery (design 7.5). No production method changes.

``MaestroExecutionPersistence`` reads the execution snapshot (``MaestroMemory.execution_snapshot``:
the context is read once per execution, because below the cutoff it does not change; every read
checks the lease again). It adds the events Maestro stored for the outputs this execution already
published (the sink's echoes), as production stores each yielded event before the next read. A
message that arrives after the cutoff stays out (production reads the table live). It folds the
whole list (``legacy_model.fold_events``) and converts it to the production models; then
``limit``, ``order``, ``filters``, ``after_event_id`` and ``before_event_id`` select from the
converted events, as the production query selects from the stored rows. Only the selected
events get temporary links (in parallel); a file this execution uploaded keeps the URL it came
from (it is past the cutoff, so Maestro has no link for it; production stores that URL).

``get_thread`` and ``get_events`` are the reads ``AgentMemory`` uses (``list_artifacts``
included). Every other method raises ``ThreadManagementModeError``: in an execution the agent
writes through ``send`` (plan 5 adds ``MaestroAgentPersistence`` for ``Repository`` and
``dispatch``).

This module imports the production handlers, so ``maestro/__init__`` and ``memory`` load it
only on use (``AgentConfig`` imports ``maestro`` at startup).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol

from dooers.agents.server.exceptions import ThreadManagementModeError
from dooers.agents.server.handlers.memory import AgentMemory
from dooers.agents.server.persistence.base import FILTERABLE_FIELDS, EventOrder, Persistence
from dooers.agents.server.protocol.models import Thread, ThreadEvent

from .context import thread_at_cutoff
from .legacy_model import DEFAULT_ASSISTANT_NAME, fold_events, to_thread, to_thread_event

if TYPE_CHECKING:
    from .artifacts import MaestroHydratedArtifact
    from .links import LinkResolver
    from .memory import MaestroArtifactReference, MaestroMemory


class PublishedOutputs(Protocol):
    """What this execution already published (``MaestroEventSink``)."""

    @property
    def published_events(self) -> list[dict[str, Any]]: ...

    @property
    def published_media_urls(self) -> Mapping[str, str]: ...


class _NoLinks:
    """Converts without links: the selection needs no URL, and only selected events get one."""

    async def url_for(self, artifact_id: str) -> str:
        return ""


class _ExecutionLinks:
    """The execution's links, except a file this execution uploaded: the URL it came from."""

    def __init__(self, links: LinkResolver, uploaded: Mapping[str, str]) -> None:
        self._links, self._uploaded = links, uploaded

    async def url_for(self, artifact_id: str) -> str | None:
        uploaded = self._uploaded.get(artifact_id)
        return uploaded if uploaded is not None else await self._links.url_for(artifact_id)


async def _all(coroutines: Iterable[Awaitable[Any]]) -> list[Any]:
    """``asyncio.gather``, but the first failure cancels the others before it is raised."""
    tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


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

    ``memory`` is the execution's ``MaestroMemory`` (``envelope``, ``execution_snapshot()`` and
    ``links``). ``assistant_name`` is ``AgentConfig.assistant_name``. ``published`` is what this
    execution already published (the executor's sink); without it, only the context is read.
    """

    def __init__(
        self, memory: MaestroMemory, *, assistant_name: str = DEFAULT_ASSISTANT_NAME, published: PublishedOutputs | None = None
    ) -> None:
        self._memory = memory
        self._assistant_name = assistant_name
        self._published = published

    def _reads(self, thread_id: str) -> bool:
        """Only the delivery's thread exists here; another thread is unknown, as in production."""
        return thread_id == self._memory.envelope["thread_id"]

    async def _thread_state(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], Mapping[str, str]]:
        """The snapshot, its events plus the echoes (in order), its participants plus the ones added, the uploaded URLs."""
        snapshot = await self._memory.execution_snapshot()
        events, participants, uploaded = list(snapshot["events"]), list(snapshot["participants"]), {}
        if self._published is not None:
            # An echo is after the cutoff; an event already read (same event id) is not added again.
            known_events = {event["event_id"] for event in events}
            echoes = []
            for echo in self._published.published_events:
                if echo["event_id"] not in known_events:
                    known_events.add(echo["event_id"])
                    echoes.append(echo)
            events.extend(echoes)
            known = {participant["participant_id"] for participant in participants}
            for echo in echoes:
                added = echo["payload"]["participant"] if echo["type"] == "participant.added" else None
                if added is not None and added["participant_id"] not in known:
                    known.add(added["participant_id"])
                    participants.append(added)
            uploaded = self._published.published_media_urls
        return snapshot, events, participants, uploaded

    def _conversion(self, participants: list[dict[str, Any]]) -> dict[str, Any]:
        agents = [participant for participant in participants if participant["kind"] == "agent"]
        return {
            "recipient_instance_id": self._memory.envelope["instance_agent_id"],
            "agent_names": {agent["instance_agent_id"]: agent["display_name"] for agent in agents},
            "multi_agent": sum(agent["state"] == "active" for agent in agents) > 1,
            "assistant_name": self._assistant_name,
        }

    async def get_thread(self, thread_id: str) -> Thread | None:
        if not self._reads(thread_id):
            return None
        snapshot, events, participants, _ = await self._thread_state()
        envelope, events = self._memory.envelope, events or [snapshot["trigger_event"]]
        # Title, queue and creation time by the rule on.context uses; the owner agent from thread.created.
        state = thread_at_cutoff(events)
        created = next((event["payload"]["thread"] for event in events if event["type"] == "thread.created"), {})
        last = events[-1]["created_at"]
        view = {
            "thread_id": envelope["thread_id"],
            "organization_id": envelope["organization_id"],
            "workspace_id": envelope["workspace_id"],
            "owner_instance_agent_id": created.get("owner_instance_agent_id"),
            "participants": participants,
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
        _, events, participants, uploaded = await self._thread_state()
        conversion = self._conversion(participants)
        # The whole list is folded first: an edit or removal can target an event of any page.
        rows = []
        for event in fold_events(events):
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
        links = _ExecutionLinks(self._memory.links, uploaded)
        return await _all(to_thread_event(event, links=links, **conversion) for _, (event, _) in selected[: max(limit, 0)])

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

    def __init__(
        self, memory: MaestroMemory, *, assistant_name: str = DEFAULT_ASSISTANT_NAME, published: PublishedOutputs | None = None
    ) -> None:
        persistence = MaestroExecutionPersistence(memory, assistant_name=assistant_name, published=published)
        super().__init__(memory.envelope["thread_id"], persistence)
        self._maestro = memory

    async def hydrate_artifact(self, reference: MaestroArtifactReference) -> MaestroHydratedArtifact:
        return await self._maestro.hydrate_artifact(reference)

    async def list_maestro_artifacts(
        self, *, cursor: str | None = None, limit: int = 30, direction: Literal["in", "out", "all"] = "all"
    ) -> tuple[list[MaestroArtifactReference], str | None, bool]:
        return await self._maestro.list_artifacts(cursor=cursor, limit=limit, direction=direction)
