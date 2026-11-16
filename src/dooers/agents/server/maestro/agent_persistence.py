"""The production ``Repository`` and ``AgentMemory`` over the Maestro agent API (outside a delivery).

``MaestroAgentPersistence`` is the ``Persistence`` that ``AgentServer.repository()`` and
``AgentServer.memory(thread_id)`` use in ``thread_management="maestro"``. No production method changes: the
``Repository`` and the ``AgentMemory`` of production call this persistence, and it answers with the production
models (``Thread``, ``ThreadEvent``, ``Run``), converted by ``legacy_model`` (the conversion of a handler's memory).

Thread ids (F8). A thread made by ``Repository.create_thread`` has the SDK id as its ``external_key``. A
``thread_id`` the caller gives is the agent's ``external_key`` first, then the Maestro id (the route does it); one
that is not a UUID can only be an ``external_key``. Every answer uses the caller's ``thread_id``; where there is none
(a list, ``get_event``, ``get_run``) it is the ``external_key`` the thread has, else the Maestro id. An event a
``Repository.create_event`` published has the SDK event id as ``client_event_id`` and keeps it as ``ThreadEvent.id``;
any other event (a handler output) has no SDK id: its id is the Maestro ``event_id``, which is what every read gives
and what ``get_event`` and ``remove_event`` find.

Reads. ``get_events`` is the production query: the events are folded (edits, removals, the run of each input event,
the queue before each change) and converted by ``MaestroExecutionPersistence``, over a window of the thread read
backward through ``GET /agent/threads/{id}/events`` (``_AgentWindow``): pages of newest events until the selection is
the one the whole thread would give. Filters (``type``, ``actor``, ``user_id``, ``run_id``) are applied after the
fold, never in the server: an edit or the run of an input event is a later event of another actor, so a server filter
would show stale content. The window holds at most ``max_history_pages`` pages (``MaestroHistoryLimitError``).
``list_threads`` asks the server for ``queue`` and applies the other filters itself, page by page, until the limit
(``count_threads`` reads the same sweep). ``NOT_FOUND`` and ``THREAD_DELETING`` are ``None`` or an empty list (F31).

Writes. ``create_thread`` (owner: the human of the call, F16; scope: ``""`` is personal and another scope is
``MaestroAgentScopeError``, F18), ``update_thread`` (title and queue), ``delete_thread``, ``create_event`` (only
``message`` events: ``assistant`` as the agent, ``user`` as the human of ``event.user``) and ``delete_event`` (any
content event). What the agent API has no route for raises ``MaestroAgentUnsupported``. One input is not stored and does not
raise: the ``author`` of a user event (the author is derived from the person, as ``HandlerPipeline`` writes it).
``agent_id`` of a call names this agent (``""`` or the instance agent id): another agent is ``MaestroAgentScopeError``.

Settings, service secrets, analytics and the worker seed are not thread data: they go to the local persistence of the
``AgentServer`` (F21), asked only when used. Guest cleanup is Maestro's (plan 6): ``delete_idle_guest_threads`` is 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from dooers.agents.server.persistence.base import EventOrder, Persistence
from dooers.agents.server.protocol.models import Run, Thread, ThreadEvent, User
from dooers.agents.server.thread_queue import is_unqueued_list_filter

from .agent_api import MaestroAgentApi, MaestroAgentApiError, MaestroAgentMissing
from .legacy_model import DEFAULT_ASSISTANT_NAME, fold_events, to_thread, to_thread_event
from .links import LinkResolver
from .persistence import MaestroExecutionPersistence
from .window import MaestroHistoryLimitError

if TYPE_CHECKING:
    from dooers.agents.server.features.analytics.models import AnalyticsEventPayload

#: Threads a ``list_runs`` without a thread reads (the route is per thread): more is ``MaestroAgentLimitError``.
MAX_RUN_SWEEP_THREADS = 1000
_MEDIA_FIELDS = {
    "image": ("mime_type", "filename", "alt"),
    "audio": ("mime_type", "filename", "duration"),
    "document": ("mime_type", "filename"),
}


class MaestroAgentUnsupported(NotImplementedError):
    """The Maestro agent API has no route for this production call (the message names it); nothing was sent."""


class MaestroAgentLimitError(RuntimeError):
    """A read would need more calls than the SDK allows for one production call."""


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


class _AgentWindow:
    """The part of ``MaestroContextWindow`` that ``MaestroExecutionPersistence`` reads, over ``GET .../events``.

    ``events`` is the window, oldest first: the newest page first, then one older page per ``read_back``.
    """

    def __init__(
        self,
        api: MaestroAgentApi,
        ref: str,
        participants: list[dict[str, Any]],
        *,
        page_size: int,
        max_pages: int,
        thread_view: dict[str, Any] | None = None,
    ) -> None:
        self._api, self._ref, self._page_size = api, ref, page_size
        self.max_pages = max_pages
        self._head = {"participants": participants, **({"thread": thread_view} if thread_view is not None else {})}
        self._events: list[dict[str, Any]] = []
        self._next: str | None = None
        self._loaded = False
        self._pages = 0
        self._origin: dict[str, Any] | None = None
        self._origin_read = False
        self._lock = asyncio.Lock()

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._events

    @property
    def exhausted(self) -> bool:
        return self._loaded and self._next is None

    def _take(self, page: dict[str, Any]) -> None:
        newest_first = page["items"]
        known = {event["event_id"] for event in self._events}
        if any(event["event_id"] in known for event in newest_first):
            raise MaestroAgentApiError("INVALID_RESPONSE")
        self._events[:0] = reversed(newest_first)
        self._next = page["next_cursor"] if page["has_more"] else None
        self._pages += 1

    async def load(self) -> dict[str, Any]:
        if not self._loaded:
            async with self._lock:
                if not self._loaded:
                    self._take(await self._api.list_events(self._ref, limit=self._page_size, order="desc"))
                    self._loaded = True
        return self._head

    async def read_back(self) -> bool:
        await self.load()
        wanted = self._next
        async with self._lock:
            if self._next != wanted:
                return True
            if self._next is None:
                return False
            if self._pages >= self.max_pages:
                raise MaestroHistoryLimitError(self.max_pages)
            self._take(await self._api.list_events(self._ref, cursor=self._next, limit=self._page_size, order="desc"))
            return True

    async def read_until(self, done: Callable[[], bool]) -> None:
        await self.load()
        while not done() and await self.read_back():
            pass

    async def origin(self) -> dict[str, Any] | None:
        """The first event of the thread (an ascending page of one event), read once."""
        await self.load()
        if not self._origin_read:
            async with self._lock:
                if not self._origin_read:
                    if self.exhausted:
                        self._origin = self._events[0] if self._events else None
                    else:
                        page = await self._api.list_events(self._ref, limit=1, order="asc")
                        self._origin = deepcopy(page["items"][0]) if page["items"] else None
                    self._origin_read = True
        return self._origin


class _Reader:
    """What ``MaestroExecutionPersistence`` needs of ``MaestroMemory``: the envelope, the window and the links."""

    def __init__(self, api: MaestroAgentApi, ref: str, window: _AgentWindow, links: LinkResolver) -> None:
        config = api.config
        self.envelope = {
            "thread_id": ref,
            "organization_id": config.organization_id,
            "workspace_id": config.workspace_id,
            "instance_agent_id": config.instance_agent_id,
        }
        self._window, self.links = window, links

    def window(self) -> _AgentWindow:
        return self._window


class _ThreadReads(MaestroExecutionPersistence):
    """The reads of a handler's memory over one thread of the agent API, with the ids the caller knows."""

    def _published_by_me(self, raw: Mapping[str, Any]) -> str | None:
        """The SDK event id of an event this agent published through the API (its ``client_event_id``)."""
        if raw.get("published_by_instance_agent_id") == self._memory.envelope["instance_agent_id"]:
            return raw["payload"].get("client_event_id")
        return None

    async def _select(self, window, events, participants, after_event_id, before_event_id, limit, order, filters):  # type: ignore[override]
        # A reference may be an SDK id (``client_event_id``); the fold works with Maestro ids.
        alias = {client: raw["event_id"] for raw in events if (client := self._published_by_me(raw))}
        return await super()._select(
            window,
            events,
            participants,
            alias.get(after_event_id, after_event_id),
            alias.get(before_event_id, before_event_id),
            limit,
            order,
            filters,
        )

    def _expose(self, event: ThreadEvent, raw: Mapping[str, Any] | None, *, as_id: str | None = None) -> ThreadEvent:
        update: dict[str, Any] = {"thread_id": self._memory.envelope["thread_id"]}
        if raw is not None:
            # ``data`` is not touched: ``to_thread_event`` already folds the message's ``payload.data`` into it.
            update["id"] = as_id or self._published_by_me(raw) or event.id
        return event.model_copy(update=update)

    async def get_events(  # type: ignore[override]
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
        before_event_id: str | None = None,
        limit: int = 50,
        order: EventOrder = "asc",
        filters: dict[str, str] | None = None,
    ) -> list[ThreadEvent]:
        events = await super().get_events(
            thread_id, after_event_id=after_event_id, before_event_id=before_event_id, limit=limit, order=order, filters=filters
        )
        raw_by_id = {raw["event_id"]: raw for raw in self._memory.window().events}
        return [self._expose(event, raw_by_id.get(event.id)) for event in events]

    async def get_event_of(self, raw_id: str, *, as_id: str) -> ThreadEvent | None:
        """The production event of the Maestro event ``raw_id``: folded with the events after it, as ``get_events`` does."""
        window = self._memory.window()
        head = await window.load()
        await window.read_until(lambda: any(raw["event_id"] == raw_id for raw in window.events))
        events = list(window.events)
        folded = fold_events(events, queue=await self._queue_before(window, events))
        target = next((raw for raw in folded if raw["event_id"] == raw_id), None)
        if target is None:
            return None  # a removed event or a control event (an edit, a removal)
        converted = await to_thread_event(target, links=self._memory.links, **self._conversion(list(head["participants"])))
        return None if converted is None else self._expose(converted, target, as_id=as_id)


def _content(parts: Iterable[Any] | None) -> list[dict[str, Any]]:
    """The parts of a production message as the agent API takes them: text, a contact or a file by URL."""
    content: list[dict[str, Any]] = []
    for part in parts or []:
        if part.type == "text":
            content.append({"type": "text", "text": part.text})
        elif part.type == "contact" and part.display_name:
            contact: dict[str, Any] = {"type": "contact", "display_name": part.display_name}
            if part.vcard is not None:
                contact["vcard"] = part.vcard
            if part.phones:
                contact["phones"] = deepcopy(part.phones)
            content.append(contact)
        elif part.type in _MEDIA_FIELDS and isinstance(part.url, str) and part.url.lower().startswith(("https://", "http://")):
            link: dict[str, Any] = {"type": "media_link", "kind": part.type, "url": part.url}
            link.update({name: getattr(part, name) for name in _MEDIA_FIELDS[part.type] if getattr(part, name) is not None})
            content.append(link)
        else:
            raise MaestroAgentUnsupported(
                f"create_event: a {part.type!r} part without an http(s) URL cannot be published through the agent API"
            )
    if not content:
        raise MaestroAgentUnsupported("create_event: a message needs at least one part")
    return content


def _publication(event: ThreadEvent) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(actor, payload)`` of the ``message.created`` that stands for a production ``message`` event."""
    if event.type != "message":
        raise MaestroAgentUnsupported(f"create_event: type {event.type!r} has no route in the agent API (only 'message')")
    payload: dict[str, Any] = {"content": _content(event.content)}
    if event.actor == "assistant":
        actor: dict[str, Any] = {"kind": "self"}
        if event.author is not None:
            payload["author"] = event.author
    elif event.actor == "user" and event.user.user_id:
        actor = {"kind": "human", "principal_id": event.user.user_id}
        if event.user.user_name:
            actor["display_name"] = event.user.user_name
        if event.user.user_email:
            actor["email"] = event.user.user_email
        if event.user.identity_ids:
            actor["identity_ids"] = list(event.user.identity_ids)
    else:
        raise MaestroAgentUnsupported(f"create_event: actor {event.actor!r} without a person cannot be published through the agent API")
    if event.data:
        payload["data"] = deepcopy(event.data)
    if event.run_id is not None:
        payload["run_id"] = event.run_id
    return actor, payload


class MaestroAgentPersistence(Persistence):
    """``Persistence`` of ``Repository`` and ``AgentMemory`` over the Maestro agent API (see the module).

    ``api`` is the agent's ``MaestroAgentApi``; ``assistant_name`` is ``AgentConfig.assistant_name``; ``local`` is
    the persistence of the ``AgentServer`` (or a coroutine function that gives it) for settings, service secrets,
    analytics and the worker seed. ``page_size`` (1 to 100) is the size of each page asked of the server;
    ``max_history_pages`` (default ``MaestroConfig.max_history_pages``) is how far back one event read may go.
    """

    def __init__(
        self,
        api: MaestroAgentApi,
        *,
        assistant_name: str = DEFAULT_ASSISTANT_NAME,
        local: Persistence | Callable[[], Awaitable[Persistence]] | None = None,
        page_size: int = 100,
        max_history_pages: int | None = None,
    ) -> None:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("page_size must be 1 to 100")
        self._api = api
        self._config = api.config
        self._assistant_name = assistant_name
        self._local = local
        self._page_size = page_size
        self._max_pages = max_history_pages if max_history_pages is not None else api.config.max_history_pages
        self._keys: dict[str, str] = {}
        self._links: dict[str, LinkResolver] = {}

    # -- helpers --

    def _thread(self, view: dict[str, Any], caller_id: str | None = None) -> Thread:
        key = view.get("external_key") or view["thread_id"]
        self._keys[view["thread_id"]] = key
        thread = to_thread(view, recipient_instance_id=self._config.instance_agent_id, assistant_name=self._assistant_name)
        return thread.model_copy(update={"id": caller_id or key})

    async def _caller_key(self, maestro_thread_id: str) -> str:
        """The id the SDK shows for a Maestro thread: its ``external_key``, else the Maestro id."""
        if maestro_thread_id not in self._keys:
            try:
                self._thread(await self._api.get_thread(maestro_thread_id))
            except MaestroAgentMissing:
                return maestro_thread_id
        return self._keys.get(maestro_thread_id, maestro_thread_id)

    async def _view(self, ref: str) -> dict[str, Any] | None:
        """``GET /agent/threads/{ref}``, or ``None`` when the agent has no such thread (F31).

        A ref the contract refuses (an empty id) is a thread nobody has: production gives ``None``, never an error.
        """
        try:
            return await self._api.get_thread(ref)
        except MaestroAgentMissing:
            return None
        except MaestroAgentApiError as error:
            if error.code == "REQUEST_INVALID":
                return None
            raise

    async def _reads(self, ref: str) -> _ThreadReads | None:
        view = await self._view(ref)
        if view is None:
            return None
        self._thread(view)
        window = _AgentWindow(self._api, ref, view["participants"], page_size=self._page_size, max_pages=self._max_pages, thread_view=view)
        links = self._links.setdefault(view["thread_id"], self._api.link_resolver(view["thread_id"]))
        return _ThreadReads(_Reader(self._api, ref, window, links), assistant_name=self._assistant_name)  # type: ignore[arg-type]

    async def _local_persistence(self) -> Persistence:
        local = self._local
        if local is None:
            raise MaestroAgentUnsupported("no local persistence: settings, secrets, analytics and the worker seed are not Maestro data")
        return await local() if callable(local) else local

    def _run(self, item: dict[str, Any], thread_id: str) -> Run:
        agent_id = item["agent_id"]
        return Run(
            id=item["run_id"],
            thread_id=thread_id,
            # ``send.run_start()`` without an agent is stored with no agent id in production (``legacy_model.to_run``).
            agent_id=None if agent_id == self._config.instance_agent_id else agent_id,
            status=item["status"],
            started_at=_time(item["started_at"]),
            ended_at=_time(item["ended_at"]),
            error=item["error"],
        )

    # -- lifecycle: the local persistence is the AgentServer's; there is nothing to open here --

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def migrate(self) -> None:
        return None

    # -- threads --

    async def create_thread(self, thread: Thread) -> None:
        """``POST /agent/threads`` with ``external_key = thread.id`` (F8) and the human of the call as owner (F16).

        The same key returns the thread that exists (Maestro dedups by key): production would fail on a duplicate id.
        ``thread.agent_id`` must name this agent (``""`` or the instance agent id): ``MaestroAgentScopeError`` otherwise.
        ``thread.metadata`` is not sent.
        """
        self._api.check_scope(agent_id=thread.agent_id)
        owner = {"kind": "human", "principal_id": thread.owner.user_id} if thread.owner.user_id else None
        seen = {"", thread.owner.user_id, self._config.instance_agent_id}
        participants = []
        for user in thread.users:
            if user.user_id not in seen:
                seen.add(user.user_id)
                participants.append({"kind": "human", "principal_id": user.user_id})
        await self._api.create_thread(
            external_key=thread.id,
            title=thread.title,
            queue=thread.queue,
            participants=participants,
            owner=owner,
            organization_id=thread.organization_id,
            workspace_id=thread.workspace_id,
            # A key made from the SDK thread id (stable for a retry); never from the external key's text (a phone).
            idempotency_key=self._api.thread_idempotency_key(thread.id),
        )

    async def get_thread(self, thread_id: str) -> Thread | None:
        view = await self._view(thread_id)
        return None if view is None else self._thread(view, thread_id)

    async def update_thread(self, thread: Thread) -> None:
        """Title and queue (what ``Repository.update_thread`` changes); only what differs is sent."""
        current = await self._view(thread.id)
        if current is None:
            return None
        try:
            title = thread.title or ""
            queue = thread.queue or None
            changes: dict[str, Any] = {}
            if (current["title"] or "") != title:
                changes["title"] = title
            if (current.get("queue") or None) != queue:
                changes.update(clear_queue=True) if queue is None else changes.update(queue=queue)
            if changes:
                await self._api.update_thread(thread.id, **changes)
        except MaestroAgentMissing:
            return None

    def _scope(self, scope: str, agent_id: str, organization_id: str, workspace_id: str) -> None:
        """The token covers one agent, one organization and one workspace (F18).

        The agent is checked in every scope (it selects data); ``admin`` has no organization or workspace filter in production.
        """
        self._api.check_scope(agent_id=agent_id)
        if scope != "admin":
            self._api.check_scope(organization_id=organization_id, workspace_id=workspace_id)

    async def _sweep(self, queue: str | None) -> AsyncIterator[Thread]:
        """Every thread of the agent, newest activity first, ``page_size`` per call."""
        cursor = None
        while True:
            page = await self._api.list_threads(cursor=cursor, limit=self._page_size, order="desc", queue=queue)
            for view in page["items"]:
                yield self._thread(view)
            cursor = page["next_cursor"]
            if not page["has_more"] or cursor is None:
                return

    @staticmethod
    def _matches(
        thread: Thread,
        *,
        scope: str,
        user_id: str | None,
        user_email: str | None,
        identity_ids: list[str] | None,
        queue: str | None,
        title_query: str | None,
    ) -> bool:
        """The filters production's ``list_threads`` has and the route does not (``_build_scope_conditions``)."""
        if scope == "member":
            found: list[bool] = []
            if user_id:
                found.append(any(u.user_id == user_id for u in thread.users))
            if user_email:
                found.append(any(u.user_email == user_email for u in thread.users))
            if identity_ids:
                found.append(
                    any(u.user_id in identity_ids for u in thread.users)
                    or any(iid in identity_ids for u in thread.users for iid in u.identity_ids)
                )
            if found and not any(found):
                return False
        if is_unqueued_list_filter(queue):
            if thread.queue:
                return False
        elif queue and thread.queue != queue:
            return False
        return not title_query or title_query.lower() in (thread.title or "").lower()

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
        """The agent's threads, newest activity first. ``agent_id`` names this agent or it is a ``MaestroAgentScopeError``.

        The route filters ``queue`` (``__none__`` is ``queue=""``); the SDK applies the rest and pages until ``limit``.
        ``cursor`` is production's ``last_event_at|id``: only older threads.
        """
        self._scope(scope, agent_id, organization_id, workspace_id)
        after = None
        if cursor:
            stamp, _, thread_id = cursor.rpartition("|")
            after = (datetime.fromisoformat(stamp), thread_id) if stamp else (datetime.fromisoformat(cursor), None)
        server_queue = "" if is_unqueued_list_filter(queue) else (queue or None)
        found: list[Thread] = []
        if limit <= 0:
            return found
        async for thread in self._sweep(server_queue):
            if after is not None and not (
                thread.last_event_at < after[0] or (thread.last_event_at == after[0] and after[1] is not None and thread.id < after[1])
            ):
                continue
            if self._matches(
                thread, scope=scope, user_id=user_id, user_email=user_email, identity_ids=identity_ids, queue=queue, title_query=title_query
            ):
                found.append(thread)
                if len(found) >= limit:
                    break
        return found

    async def count_threads(
        self,
        agent_id: str,
        organization_id: str,
        workspace_id: str,
        user_id: str | None,
        scope: str = "member",
        user_email: str | None = None,
        identity_ids: list[str] | None = None,
        queue: str | None = None,
        title_query: str | None = None,
    ) -> int:
        """How many threads ``list_threads`` would give with no limit (the same sweep and filters)."""
        self._scope(scope, agent_id, organization_id, workspace_id)
        server_queue = "" if is_unqueued_list_filter(queue) else (queue or None)
        return sum(
            [
                1
                async for thread in self._sweep(server_queue)
                if self._matches(
                    thread,
                    scope=scope,
                    user_id=user_id,
                    user_email=user_email,
                    identity_ids=identity_ids,
                    queue=queue,
                    title_query=title_query,
                )
            ]
        )

    async def delete_thread(self, thread_id: str) -> None:
        """``DELETE /agent/threads/{id}``: Maestro deletes in the background (a read says gone at once); a missing thread is a no-op."""
        try:
            await self._api.delete_thread(thread_id)
        except MaestroAgentMissing:
            return None
        except MaestroAgentApiError as error:
            if error.code == "REQUEST_INVALID":  # an id nobody has (empty): nothing to delete, as production
                return None
            raise

    async def delete_idle_guest_threads(self, max_idle_seconds: int) -> int:
        """Maestro owns the cleanup of guest threads (plan 6): nothing to do here."""
        return 0

    async def upsert_thread_participant(self, thread_id: str, user: User) -> None:
        raise MaestroAgentUnsupported("upsert_thread_participant: dispatch adds participants through the agent API (plan 5, task 6)")

    # -- events --

    async def create_event(self, event: ThreadEvent) -> None:
        """``POST /agent/threads/{id}/events`` (``message.created``, ``trigger: false``); the SDK event id is the idempotency key (F8)."""
        actor, payload = _publication(event)
        await self._api.publish_event(event.thread_id, actor=actor, payload=payload, trigger=False, idempotency_key=event.id)

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
        reads = await self._reads(thread_id)
        if reads is None:
            return []
        try:
            return await reads.get_events(
                thread_id, after_event_id=after_event_id, before_event_id=before_event_id, limit=limit, order=order, filters=filters
            )
        except MaestroAgentMissing:
            return []

    async def get_event(self, event_id: str) -> ThreadEvent | None:
        """By the SDK event id of a publication, or by the Maestro ``event_id`` (any event the agent reads)."""
        try:
            raw = await self._api.get_event(event_id)
            reads = await self._reads(await self._caller_key(raw["thread_id"]))
            return None if reads is None else await reads.get_event_of(raw["event_id"], as_id=event_id)
        except MaestroAgentMissing:
            return None
        except MaestroAgentApiError as error:
            if error.code == "REQUEST_INVALID":  # an id nobody has: the contract refuses it before it is sent
                return None
            raise

    async def update_event(self, event: ThreadEvent) -> None:
        raise MaestroAgentUnsupported("update_event: the agent API has no route to change an event")

    async def delete_event(self, event_id: str) -> None:
        """``DELETE .../events/{eventId}``: any content event the agent sees; a control event or a missing one is a no-op."""
        try:
            raw = await self._api.get_event(event_id)
            await self._api.remove_event(raw["thread_id"], raw["event_id"])
        except MaestroAgentMissing:
            return None
        except MaestroAgentApiError as error:
            if error.code == "REQUEST_INVALID" or error.reason_code == "event_not_removable":
                return None
            raise

    # -- runs --

    async def create_run(self, run: Run) -> None:
        raise MaestroAgentUnsupported("create_run: Maestro records the runs of an execution (the agent API has no route to write one)")

    async def update_run(self, run: Run) -> None:
        raise MaestroAgentUnsupported("update_run: Maestro records the runs of an execution (the agent API has no route to write one)")

    async def get_run(self, run_id: str) -> Run | None:
        if not _is_uuid(run_id):
            return None  # Maestro run ids are UUIDs
        try:
            item = await self._api.get_run(run_id)
            return self._run(item, await self._caller_key(item["thread_id"]))
        except MaestroAgentMissing:
            return None

    async def _thread_runs(self, ref: str, caller_id: str) -> list[Run]:
        runs: list[Run] = []
        cursor = None
        try:
            while True:
                page = await self._api.list_runs(ref, cursor=cursor)
                runs.extend(self._run(item, caller_id) for item in page["items"])
                cursor = page["next_cursor"]
                if not page["has_more"] or cursor is None:
                    return runs
        except MaestroAgentMissing:
            return []

    async def list_runs(
        self,
        thread_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Run]:
        """The runs, newest start first. Without ``thread_id`` the route (per thread) is read for each thread of the agent.

        ``agent_id`` names this agent (``""`` or the instance agent id) or it is a ``MaestroAgentScopeError``.
        """
        self._api.check_scope(agent_id=agent_id)
        if thread_id:
            runs = await self._thread_runs(thread_id, thread_id)
        else:
            runs = []
            seen = 0
            async for thread in self._sweep(None):
                seen += 1
                if seen > MAX_RUN_SWEEP_THREADS:
                    raise MaestroAgentLimitError(
                        f"list_runs without thread_id reads at most {MAX_RUN_SWEEP_THREADS} threads; pass thread_id"
                    )
                runs.extend(await self._thread_runs(thread.id, thread.id))
        if status:
            runs = [run for run in runs if run.status == status]
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[: max(limit, 0)]

    # -- not thread data: the AgentServer's local persistence (F21) --

    async def get_settings(self, agent_id: str) -> dict[str, Any]:
        return await (await self._local_persistence()).get_settings(agent_id)

    async def update_setting(self, agent_id: str, field_id: str, value: Any) -> datetime:
        return await (await self._local_persistence()).update_setting(agent_id, field_id, value)

    async def set_settings(self, agent_id: str, values: dict[str, Any]) -> datetime:
        return await (await self._local_persistence()).set_settings(agent_id, values)

    async def get_worker_seed_hash_bytes(self, worker_id: str) -> bytes | None:
        return await (await self._local_persistence()).get_worker_seed_hash_bytes(worker_id)

    async def set_worker_seed_hash_bytes(self, worker_id: str, secret_hash: bytes) -> None:
        await (await self._local_persistence()).set_worker_seed_hash_bytes(worker_id, secret_hash)

    async def get_service_secrets(self, agent_id: str) -> dict[str, Any]:
        return await (await self._local_persistence()).get_service_secrets(agent_id)

    async def merge_service_secrets(self, agent_id: str, patch: dict[str, str]) -> datetime:
        return await (await self._local_persistence()).merge_service_secrets(agent_id, patch)

    async def insert_analytics_events(self, events: Sequence[AnalyticsEventPayload]) -> None:
        await (await self._local_persistence()).insert_analytics_events(list(events))
