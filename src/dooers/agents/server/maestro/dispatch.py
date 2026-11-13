"""``AgentServer.dispatch`` in ``thread_management="maestro"``: publish the trigger and follow the delivery (plan 5, task 6b).

Production ``dispatch(handler, ...)`` runs the handler while the caller reads the stream. In Maestro mode the handler runs in the
Maestro executor when Maestro delivers the trigger message to the agent, so ``dispatch`` only

1. publishes the message over the agent API (``MaestroIngress.publish`` with ``trigger=True``: thread by ``external_key``, the
   declared human or the external contact, files as artifacts, as in ``ingest``), after registering the handler (F5, see
   ``dispatch_registry``), and
2. returns a :class:`MaestroDispatchStream` that follows the thread (``GET /agent/threads/{id}/events``, newest page first, every
   250 ms) and yields the events of the delivery this call created.

The stream ends when the execution of the delivery ends (``execution.completed``, ``execution.canceled``) or raises ``HandlerError``
(``execution.failed``), or when the delivery reaches a terminal state without an execution (``delivery.updated``: nothing ran; a
``failed`` or ``expired`` delivery raises ``HandlerError``). It has no deadline of its own: Maestro bounds a delivery (the
execution deadline, the delivery expiry), and the caller can cancel the task that reads it. A delivery that waits for a person
(``waiting_input``) keeps the stream open.

What it yields (F14):

- the execution ran in this process (the handler of this call, registered): the handler's own ``AgentEvent``, the ones the Maestro
  sink accepted, in order; a failure of the handler raises ``HandlerError(str(error), original=error)``, as production does;
- otherwise the Maestro events of the delivery converted back with ``agent_event_from`` and the corrections of C32: the data keys
  production's ``AgentSend`` always sets (``None`` or empty), the title and queue ``update_thread`` changed (not the ones the
  Maestro event repeats), a participant with the full user shape, the run of the executing agent as ``agent_id=None``; a file is an
  ``image``, ``audio`` or ``document`` event with the temporary link of Maestro as the URL (the URL the handler sent is not kept).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from dooers.agents.server.dispatch import DispatchStream
from dooers.agents.server.exceptions import HandlerError
from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.protocol.models import User, WireC2S_ContentPart, WorkspaceQueueCatalog

from .agent_api import MaestroAgentApi, MaestroAgentApiError, MaestroAgentMissing
from .dispatch_registry import DISPATCHES, MARKER_KEY, DispatchEntry
from .ingress import MaestroIngress, MaestroPublished
from .outputs import agent_event_from

if TYPE_CHECKING:
    from dooers.agents.server.observability.otel import _NoOpTracker, _OtelTracker

logger = logging.getLogger(__name__)

#: Seconds between two reads of the thread.
POLL_SECONDS = 0.25
#: Events per page of a read.
PAGE_LIMIT = 100
#: Consecutive failed reads (retryable errors of the API) before the stream gives up.
MAX_READ_FAILURES = 8
_TERMINAL_DELIVERY = frozenset({"responded", "silent", "failed", "canceled", "expired"})
#: A delivery that ended in one of these never ran the handler to its end: production would have raised.
_DELIVERY_ERRORS = frozenset({"failed", "expired"})
_EXECUTION_ENDS = frozenset({"execution.completed", "execution.failed", "execution.canceled"})
#: The keys production's ``AgentSend`` always sets for a send type, with the value it gives when the caller left it out.
_SEND_DEFAULTS: dict[str, dict[str, Any]] = {
    "text": {"author": None},
    "reasoning": {"author": None},
    "contact": {"vcard": None, "phones": [], "author": None},
    "template": {"template_components": [], "author": None},
    "tool_call": {"display_name": None},
    "tool_result": {"display_name": None, "args": None},
    "tool_transaction": {"display_name": None},
    "run_start": {"agent_id": None},
    "run_end": {"error": None},
    "chart": {"title": "", "message": "", "series": [], "x_label": None, "y_label": None},
}
_USER_DEFAULTS: dict[str, Any] = {"user_name": None, "user_email": None, "identity_ids": []}

#: The sleep between reads (a test replaces it with a virtual clock).
_sleep = asyncio.sleep


class MaestroDispatchStream(DispatchStream):
    """The stream of a ``dispatch`` in Maestro mode. See the module."""

    def __init__(
        self,
        api: MaestroAgentApi,
        published: MaestroPublished,
        entry: DispatchEntry,
        *,
        tracker: _OtelTracker | _NoOpTracker | None = None,
    ) -> None:
        # No ``HandlerPipeline``: there is no local pipeline, and the three properties come from the publication.
        self._api, self._published, self._entry, self._tracker = api, published, entry, tracker
        self._delivery_id: str = published.response["delivery_id"]

    @property
    def thread_id(self) -> str:
        return self._published.thread_id

    @property
    def event_id(self) -> str:
        return self._published.event.id

    @property
    def is_new_thread(self) -> bool:
        return self._published.is_new_thread

    async def __aiter__(self) -> AsyncGenerator[AgentEvent, None]:  # type: ignore[override]
        tracker = self._tracker
        try:
            async for event in self._follow():
                yield event
        except Exception as error:
            if tracker:
                tracker.fail()
                tracker.record_error(error)
            raise
        finally:
            if tracker:
                tracker.end()

    async def collect(self) -> list[AgentEvent]:
        return [event async for event in self]

    # -- following the delivery --

    async def _follow(self) -> AsyncGenerator[AgentEvent, None]:
        published, entry = self._published, self._entry
        last = int(published.response["event"]["sequence"])
        converter = _Converter(self._api, published)
        execution: str | None = None
        drained = 0
        failure: str | None = None  # the error text of the executor's failure report (out of process)
        last_text: str | None = None  # the last message of the execution: the failure report when the handler had no open run
        while True:
            for event in await self._read_after(last):
                last = int(event["sequence"])
                if event.get("delivery_id") != self._delivery_id:
                    continue
                kind, payload = event["type"], event["payload"]
                if kind == "execution.started":
                    execution = payload["execution_id"]
                elif kind in _EXECUTION_ENDS:
                    for original in self._originals(execution, drained):
                        yield original
                    if kind == "execution.failed":
                        raise self._failed(execution, payload["reason_code"], failure, last_text)
                    self._finish()
                    return
                elif kind == "delivery.updated":
                    state = payload["delivery"]["state"]
                    if state in _TERMINAL_DELIVERY:
                        for original in self._originals(execution, drained):
                            yield original
                        self._finish()
                        if state in _DELIVERY_ERRORS:
                            reason = payload["delivery"]["reason_code"] or state
                            raise HandlerError(
                                f"The delivery of this message ended {state} ({reason}): the handler did not run to its end."
                            )
                        return
                elif not entry.claimed(event.get("execution_id") or ""):
                    if kind == "run.updated" and payload.get("status") == "failed" and payload.get("error"):
                        failure = payload["error"]
                    out = await converter.convert(event)
                    if out is not None:
                        if out.send_type == "text":
                            last_text = out.data["text"]
                        yield out
            # The handler's own events of an execution that ran in this process, as they are recorded.
            originals = self._originals(execution, drained)
            drained += len(originals)
            for original in originals:
                yield original
            await _sleep(POLL_SECONDS)

    def _originals(self, execution: str | None, drained: int) -> list[AgentEvent]:
        """The events the handler yielded in ``execution`` here, after the ``drained`` first ones."""
        if execution is None or not self._entry.claimed(execution):
            return []
        return list(self._entry.runs[execution][drained:])

    def _failed(self, execution: str | None, reason: str | None, failure: str | None, last_text: str | None) -> HandlerError:
        """``HandlerError`` of a failed execution: the handler's exception here; elsewhere the text the executor reported.

        The executor reports a handler failure as a message and ends the open run with the same text as its error: that text is the
        message. Without an open run only the message exists, so the last message of the execution is added to the reason.
        """
        error = self._entry.failures.get(execution or "")
        if error is not None:
            return HandlerError(str(error), original=error)
        if failure:
            return HandlerError(failure)
        message = f"The handler failed to answer this message ({reason or 'execution failed'})."
        return HandlerError(f"{message} Last message of the execution: {last_text}" if last_text else message)

    def _finish(self) -> None:
        DISPATCHES.forget(self._entry.event_id)

    async def _read_after(self, last: int) -> list[dict[str, Any]]:
        """The events of the thread after sequence ``last``, oldest first: the newest pages, until one reaches ``last``."""
        failures = 0
        while True:
            try:
                return await self._newest_after(last)
            except MaestroAgentApiError as error:
                if isinstance(error, MaestroAgentMissing) or not error.retryable or failures >= MAX_READ_FAILURES:
                    raise
                failures += 1
                await _sleep(error.retry_after_seconds or POLL_SECONDS)

    async def _newest_after(self, last: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self._api.list_events(self._published.maestro_thread_id, cursor=cursor, limit=PAGE_LIMIT, order="desc")
            reached = False
            for event in page["items"]:
                if int(event["sequence"]) <= last:
                    reached = True
                    break
                found.append(event)
            if reached or not page["has_more"] or not page["next_cursor"]:
                break
            cursor = page["next_cursor"]
        found.reverse()
        return found


class _Converter:
    """A Maestro event of the handler's outputs as the ``AgentEvent`` production's handler yielded (see the module)."""

    def __init__(self, api: MaestroAgentApi, published: MaestroPublished) -> None:
        self._api = api
        self._links = api.link_resolver(published.maestro_thread_id)
        self._title: str | None = published.thread.get("title")
        self._queue: str | None = published.thread.get("queue")

    async def convert(self, event: dict[str, Any]) -> AgentEvent | None:
        kind, payload = event["type"], event["payload"]
        if kind == "message.created":
            content = payload.get("content")
            part = content[0] if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) else {}
            if part.get("type") in {"attachment", "media_link"}:
                return await self._media(part, payload)
        if kind == "thread.updated":
            return self._thread(payload)
        converted = agent_event_from(event)
        if converted is None:
            return None
        data = converted.data
        if converted.send_type == "run_start" and data.get("agent_id") == self._api.config.instance_agent_id:
            data = {k: v for k, v in data.items() if k != "agent_id"}  # Maestro records the executing agent (the sink does the same)
        if converted.send_type == "participant_add":
            data = {"user": {**_USER_DEFAULTS, **data["user"]}}
        return AgentEvent(converted.send_type, {**deepcopy(_SEND_DEFAULTS.get(converted.send_type, {})), **deepcopy(data)})

    def _thread(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        """``update_thread``: only what changed. The Maestro event carries the title and the queue the thread has."""
        data: dict[str, Any] = {}
        title, queue = payload.get("title"), payload.get("queue")
        if title is not None and title != self._title:
            data["title"] = title
        if queue is not None and queue != self._queue:
            data["queue"] = queue
        self._title, self._queue = title if title is not None else self._title, queue if queue is not None else self._queue
        return AgentEvent("thread_update", data) if data else None

    async def _media(self, part: Mapping[str, Any], payload: Mapping[str, Any]) -> AgentEvent:
        author = payload.get("author")
        if part["type"] == "attachment":
            mime = part.get("mime_type")
            kind = "image" if (mime or "").startswith("image/") else "audio" if (mime or "").startswith("audio/") else "document"
            try:
                url = await self._links.url_for(part["artifact_id"])
            except MaestroAgentApiError:
                logger.warning("maestro dispatch: no temporary link for a file of the handler; the event goes without URL")
                url = None
        else:
            kind, url, mime = part["kind"], part["url"], part.get("mime_type")
        if kind == "image":
            data: dict[str, Any] = {"url": url, "mime_type": mime, "alt": part.get("alt"), "author": author}
        elif kind == "audio":
            data = {"url": url, "mime_type": mime, "duration": part.get("duration"), "author": author}
        else:
            data = {
                "url": url,
                "filename": part.get("filename") or "file",
                "mime_type": mime or "application/octet-stream",
                "author": author,
            }
        if payload.get("whatsapp") is not None:
            data["whatsapp"] = deepcopy(payload["whatsapp"])
        return AgentEvent(kind, data)


async def dispatch_in_maestro(
    ingress: MaestroIngress,
    handler: Any,
    agent_id: str,
    message: str,
    *,
    user: User | None,
    organization_id: str,
    workspace_id: str,
    thread_id: str | None,
    thread_title: str | None,
    content: list[WireC2S_ContentPart | dict[str, Any]] | None,
    channel: str,
    channel_meta: dict[str, Any] | None,
    queues: list[WorkspaceQueueCatalog] | None,
) -> MaestroDispatchStream:
    """``AgentServer.dispatch`` in Maestro mode: register the handler, publish the trigger, return the stream."""
    event_id = str(uuid.uuid4())
    marker = DISPATCHES.marker_for(ingress.agent_id, handler)
    entry = DISPATCHES.register(event_id, handler)
    try:
        published = await ingress.publish(
            agent_id=agent_id,
            message=message,
            user=user,
            organization_id=organization_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            thread_title=thread_title,
            content=content,
            channel=channel,
            channel_meta=channel_meta,
            queues=queues,
            actor="user",
            author=None,
            data={MARKER_KEY: marker} if marker else None,
            trigger=True,
            event_id=event_id,
        )
        if not published.response.get("delivery_id"):
            raise MaestroAgentApiError("INVALID_RESPONSE")
    except BaseException:
        DISPATCHES.forget(event_id)
        raise

    from dooers.agents.server.observability.otel import start_tracker

    resolved = user or User(user_id="")
    tracker = start_tracker(
        thread_id=published.thread_id,
        event_id=published.event.id,
        agent_id=agent_id,
        thread_title=published.thread.get("title"),
        organization_id=organization_id,
        workspace_id=workspace_id,
        channel=channel or "dooers-platform",
        user_id=resolved.user_id or None,
        user_name=resolved.user_name or None,
        user_email=resolved.user_email or None,
    )
    return MaestroDispatchStream(ingress.api, published, entry, tracker=tracker)


__all__ = ["POLL_SECONDS", "MaestroDispatchStream", "dispatch_in_maestro"]
