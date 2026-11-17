"""``AgentServer.ingest`` in ``thread_management="maestro"``: the publication of a caller's message over the agent API.

``MaestroIngress.publish`` is the Maestro side of ``HandlerPipeline.setup``: it finds or creates the thread, records the person
as a participant, turns the content into Maestro parts and publishes the message (``POST /agent/threads/{id}/events``). It is
the part ``dispatch`` shares (plan 5, task 6b: the same call with ``trigger=True``).

What it takes from production, unchanged:

- the content parts are resolved by ``HandlerPipeline._resolve_content_parts_async`` (same upload store, same durable fallback,
  same errors: ``UploadReferenceError``, ``UnsupportedContentTypeError``, inline media), so a ``ref_id`` means what it means there;
- the event is the production ``ThreadEvent`` of ``HandlerPipeline.setup``, built before anything is written: an actor other than
  ``user`` raises the same ``ValidationError`` (F13), and ``data`` is ``HandlerPipeline._with_inbound_source_data`` (the
  caller's data plus ``source``, F7);
- the thread id is the caller's: it is the ``external_key`` of the thread (F8) and the value ``publish`` hands back. Without one
  the SDK makes a UUID and uses it as the key, as ``Repository.create_thread`` does.

What Maestro changes (declared in the README and in ``tests/test_maestro_ingest.py``):

- a person is a declared human (``channel == "dooers-platform"``) or an external contact (``whatsapp``, ``dooers-public-chat``);
  no other channel and no person without an id can be published (``MaestroAgentUnsupported``, before any write);
- a file is an artifact: ``ref_id`` and bytes are uploaded and the part carries the MIME type Maestro returns; a URL is fetched
  and uploaded, and stays a ``media_link`` when it cannot be (F11);
- a person is added to the thread the way ``upsert_thread_participant`` does: only when the thread does not have them.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dooers.agents.server.config import AgentConfig
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline, _user_author_display
from dooers.agents.server.persistence.base import Persistence
from dooers.agents.server.protocol.models import (
    TextPart,
    ThreadEvent,
    User,
    WireC2S_ContentPart,
    WireS2C_ContactPart,
    WireS2C_TextPart,
    WorkspaceQueueCatalog,
)
from dooers.agents.server.upload_store import UploadEntry, UploadStore

from .agent_api import MaestroAgentApi, MaestroAgentApiError, MaestroAgentMissing
from .agent_persistence import MaestroAgentUnsupported
from .artifacts import artifact_type_allowed, media_url_upload_allowed
from .media_fetch import MediaFetchError, decode_data_url, fetch_media, is_data_url
from .outputs import media_link_of
from .sink import _artifact_filename

logger = logging.getLogger(__name__)

#: Production channel -> the channel of a Maestro external contact (``Public`` ``external.channel``).
EXTERNAL_CHANNELS = {"whatsapp": "whatsapp", "dooers-public-chat": "public_chat"}
#: Failures of an upload that leave the message with a ``media_link``: production never fails on the file of a URL.
_TOLERATED_UPLOAD_ERRORS = frozenset(
    {
        "REQUEST_INVALID",
        "VALIDATION_FAILED",
        "PAYLOAD_TOO_LARGE",
        "UNSUPPORTED_CAPABILITY",
        "TRANSPORT_UNAVAILABLE",
        "DEPENDENCY_UNAVAILABLE",
        "RATE_LIMITED",
    }
)
_MEDIA_TIMEOUT_SECONDS = 30.0


async def _noop_handler(*_args: Any, **_kwargs: Any):
    if False:  # pragma: no cover
        yield None


@dataclass(frozen=True)
class MaestroPublished:
    """A message published to a thread of the agent."""

    #: The caller's thread id: what ``ingest`` returns and what ``DispatchStream.thread_id`` is.
    thread_id: str
    #: The Maestro id of the thread (every later call of the SDK uses it).
    maestro_thread_id: str
    is_new_thread: bool
    #: The production event (its id is the Maestro ``client_event_id``).
    event: ThreadEvent
    #: ``Public.AgentEventCreateResponse``: ``event``, ``cursor`` and, with ``trigger``, ``delivery_id``.
    response: dict[str, Any]
    #: The thread as it was when the message was published (``Public.AgentThread``): title and queue before the handler changes them.
    thread: dict[str, Any] = field(default_factory=dict)


class MaestroIngress:
    """Publishes the messages of ``AgentServer.ingest`` (and ``dispatch``) to the Maestro thread of a caller's id."""

    def __init__(
        self,
        config: AgentConfig,
        api: MaestroAgentApi,
        *,
        persistence: Persistence,
        upload_store: Callable[[], Awaitable[UploadStore]],
    ) -> None:
        self._config, self._api, self._persistence, self._upload_store = config, api, persistence, upload_store

    @property
    def agent_id(self) -> str:
        """The id of this agent in every production call that names it (``""`` and any case of it are the same agent)."""
        return self._api.config.instance_agent_id

    @property
    def api(self) -> MaestroAgentApi:
        """The agent API this ingress publishes with (``dispatch`` follows the thread with it)."""
        return self._api

    async def publish(
        self,
        *,
        agent_id: str,
        message: str,
        user: User | None,
        organization_id: str,
        workspace_id: str,
        thread_id: str | None,
        thread_title: str | None,
        content: list[WireC2S_ContentPart | dict[str, Any]] | None,
        channel: str,
        channel_meta: dict[str, Any] | None,
        queues: list[WorkspaceQueueCatalog] | None,
        actor: str,
        author: str | None,
        data: dict[str, Any] | None,
        trigger: bool,
        event_id: str | None = None,
    ) -> MaestroPublished:
        """Find or create the thread, record the person and publish the message. See the module.

        ``event_id`` is the SDK event id (the ``client_event_id`` and the idempotency key); ``dispatch`` chooses it before the call, to
        register its handler under it. Without one the call makes a UUID.
        """
        self._api.check_scope(organization_id=organization_id, workspace_id=workspace_id, agent_id=agent_id)
        caller_thread = thread_id or str(uuid.uuid4())
        context = HandlerContext(
            handler=_noop_handler,
            agent_id=self.agent_id,
            message=message,
            organization_id=organization_id,
            workspace_id=workspace_id,
            channel=(channel or "dooers-platform"),
            channel_meta=channel_meta,
            user=user or User(user_id=""),
            thread_id=caller_thread,
            thread_title=thread_title,
            content=content,
            data=data,
            persist_actor=actor,
            persist_author=author,
            queues=list(queues or []),
        )
        # Nothing is consumed or written until the Maestro limits of the call are checked (the person, the parts); the F13
        # ``ValidationError`` of a non-user actor stays the first error of the event, as in production.
        store = _RecordingStore(await self._upload_store()) if content else None
        try:
            if actor == "user":
                who = self._person(context)
            event, storage_parts, handler_parts = await self._event(context, store, event_id)
            who = self._person(context)
            prepared = [await self._prepare(s, h) for s, h in zip(storage_parts, handler_parts, strict=True)]
            view = await self._find(caller_thread)
            is_new = view is None
            if view is None:
                view = await self._api.create_thread(
                    external_key=caller_thread,
                    title=context.thread_title,
                    channel=context.channel,
                    channel_meta=context.channel_meta,
                    participants=[who["participant"]] if who["kind"] == "external" else [],
                    owner=who["participant"] if who["kind"] == "human" else None,
                )
            elif not _has_member(view, who["participant"]):
                await self._add(view["thread_id"], who["participant"])
            maestro_thread = view["thread_id"]
            parts = [await self._materialize(maestro_thread, item) for item in prepared]
            payload: dict[str, Any] = {"content": parts}
            if event.data:
                payload["data"] = deepcopy(event.data)
            response = await self._api.publish_event(
                maestro_thread, actor=who["actor"], payload=payload, trigger=trigger, idempotency_key=event.id
            )
        except BaseException:
            # A staged file is single use in production, but its next step there is a local write. Here a failure can come from
            # any remote call: the files the call consumed go back, so the caller can repeat it with the same ``ref_id``.
            if store is not None:
                store.restore()
            raise
        return MaestroPublished(caller_thread, maestro_thread, is_new, event, response, deepcopy(view))

    # -- the production event --

    async def _event(
        self, context: HandlerContext, store: _RecordingStore | None, event_id: str | None = None
    ) -> tuple[ThreadEvent, list[Any], list[Any]]:
        """``HandlerPipeline.setup`` up to the ``ThreadEvent``: its content parts and the event, nothing written."""
        if context.content:
            pipeline = HandlerPipeline(persistence=self._persistence, upload_store=store, agent_config=self._config)  # type: ignore[arg-type]
            handler_parts, storage_parts = await pipeline._resolve_content_parts_async(
                context.content, agent_id=context.agent_id, thread_id=context.thread_id
            )
        else:
            text = context.message or ""
            handler_parts, storage_parts = [TextPart(text=text)], [WireS2C_TextPart(text=text)]
        author = context.persist_author
        if author is None and context.persist_actor == "user":
            author = _user_author_display(context.user)
        elif author is None and context.persist_actor == "assistant":
            author = self._config.assistant_name
        event = ThreadEvent(
            id=event_id or str(uuid.uuid4()),
            thread_id=context.thread_id,  # type: ignore[arg-type]
            run_id=None,
            type=context.event_type,  # type: ignore[arg-type]
            actor=context.persist_actor,  # type: ignore[arg-type]
            author=author,
            user=context.user if context.persist_actor == "user" else None,  # type: ignore[arg-type]
            content=storage_parts,
            data=HandlerPipeline._with_inbound_source_data(context.data, context),
            created_at=datetime.now(UTC),
            client_event_id=context.client_event_id,
        )
        return event, storage_parts, handler_parts

    # -- the person --

    @staticmethod
    def _person(context: HandlerContext) -> dict[str, Any]:
        """The actor of the publication and the participant of the thread for the person of the call."""
        user, channel = context.user, context.channel
        if channel == "dooers-platform":
            if not user.user_id:
                raise MaestroAgentUnsupported("ingest: a message of a person without user_id cannot be published through the agent API")
            actor: dict[str, Any] = {"kind": "human", "principal_id": user.user_id}
            if user.user_name:
                actor["display_name"] = user.user_name
            if user.user_email:
                actor["email"] = user.user_email
            if user.identity_ids:
                actor["identity_ids"] = list(user.identity_ids)
            return {"kind": "human", "actor": actor, "participant": {"kind": "human", "principal_id": user.user_id}}
        maestro_channel = EXTERNAL_CHANNELS.get(channel)
        if maestro_channel is None:
            raise MaestroAgentUnsupported(f"ingest: channel {channel!r} has no external contact in the agent API")
        meta = context.channel_meta if isinstance(context.channel_meta, Mapping) else {}
        whatsapp = meta.get("whatsapp") if isinstance(meta.get("whatsapp"), Mapping) else {}
        external_id = user.user_id or (str(whatsapp.get("from_e164") or "").strip() if channel == "whatsapp" else "")
        if not external_id:
            raise MaestroAgentUnsupported("ingest: a channel message of a contact without an id cannot be published through the agent API")
        contact: dict[str, Any] = {"kind": "external", "channel": maestro_channel, "external_id": external_id}
        if user.user_name:
            contact["display_name"] = user.user_name[:200]
        return {"kind": "external", "actor": contact, "participant": contact}

    async def _find(self, caller_thread: str) -> dict[str, Any] | None:
        """The thread of the caller's id: its external key, else (for a UUID) a Maestro id of a thread without key."""
        view = await self._api.find_thread(caller_thread)
        if view is not None or not _is_uuid(caller_thread):
            return view
        try:
            return await self._api.get_thread(caller_thread)
        except MaestroAgentMissing:
            return None

    async def _add(self, maestro_thread: str, participant: Mapping[str, Any]) -> None:
        try:
            await self._api.add_participant(maestro_thread, participant)
        except MaestroAgentApiError as error:
            if error.reason_code != "already_participant":  # another publication of the same person won the race
                raise

    # -- the content --

    async def _prepare(self, storage: Any, handler: Any) -> _Prepared:
        """One part as Maestro takes it, before anything is written: the limits of the file and the fetch of a URL."""
        kind = storage.type
        if kind == "text":
            return _Prepared(part={"type": "text", "text": storage.text})
        if kind == "contact":
            assert isinstance(storage, WireS2C_ContactPart)
            contact: dict[str, Any] = {"type": "contact", "display_name": storage.display_name}
            if storage.vcard is not None:
                contact["vcard"] = storage.vcard
            if storage.phones:
                contact["phones"] = deepcopy(storage.phones)
            return _Prepared(part=contact)
        config = self._api.config
        data = getattr(handler, "data", b"")
        if data:
            filename = _artifact_filename(storage.filename or getattr(handler, "filename", None) or "file")
            mime = getattr(handler, "mime_type", None) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            # A file the caller staged (``ref_id``) or gave as bytes has no URL to fall back to: a refusal is the caller's error
            # (``upload_artifact`` would refuse it too, after the thread is made).
            if len(data) > config.max_artifact_bytes or not artifact_type_allowed(config, mime, filename):
                raise MaestroAgentApiError("REQUEST_INVALID")
            return _Prepared(upload=_Upload(kind, storage, data, filename, mime))
        url = storage.url
        if not url:
            raise MaestroAgentUnsupported(f"ingest: a {kind!r} part without a file and without a URL cannot be published")
        return await self._prepare_url(kind, storage, url)

    async def _prepare_url(self, kind: str, storage: Any, url: str) -> _Prepared:
        """A media URL: fetched now, uploaded later; a ``media_link`` when it cannot be (production keeps the URL)."""
        config = self._api.config
        limits = {"max_bytes": config.max_artifact_bytes, "hint_mime": storage.mime_type, "hint_filename": storage.filename}
        # Production keeps the URL text and the part's fields as they came, whatever the scheme.
        link = media_link_of(kind, storage.model_dump())
        reason: str | None = None
        try:
            fetched = (
                decode_data_url(url, **limits) if is_data_url(url) else await fetch_media(url, timeout_s=_MEDIA_TIMEOUT_SECONDS, **limits)
            )
        except MediaFetchError as error:
            reason = error.reason
        else:
            filename = _artifact_filename(fetched.filename)
            if media_url_upload_allowed(config, fetched.mime_type, filename):
                return _Prepared(upload=_Upload(kind, storage, fetched.data, filename, fetched.mime_type, from_url=True), fallback=link)
            reason = "policy"
        # Only the reason: the URL may carry a token (the log rule of the sink).
        logger.warning("maestro ingest: media URL not uploaded (reason=%s); publishing media_link", reason)
        return _Prepared(part=link)

    async def _materialize(self, maestro_thread: str, item: _Prepared) -> dict[str, Any]:
        """The part of one prepared item: the upload of its file (the first writes of a call that has a thread)."""
        if item.upload is None:
            assert item.part is not None
            return item.part
        up = item.upload
        try:
            artifact = await self._api.upload_artifact(maestro_thread, up.data, up.filename, up.mime)
        except MaestroAgentApiError as error:
            if not up.from_url or error.code not in _TOLERATED_UPLOAD_ERRORS:
                raise
            logger.warning("maestro ingest: media URL not uploaded (reason=upload_%s); publishing media_link", error.code.lower())
            return item.fallback
        part: dict[str, Any] = {
            "type": "attachment",
            "artifact_id": artifact["artifact_id"],
            # The MIME type Maestro stores is the one the part carries (and the family the thread shows).
            "mime_type": artifact["mime_type"],
            "filename": artifact["filename"],
            "size_bytes": artifact["size_bytes"],
        }
        if up.kind == "audio" and up.storage.duration is not None:
            part["duration"] = up.storage.duration
        return part


@dataclass
class _Upload:
    kind: str
    storage: Any
    data: bytes
    filename: str
    mime: str
    #: The file came from a URL (it may fall back to a ``media_link``); a staged file or bytes cannot.
    from_url: bool = False


@dataclass
class _Prepared:
    part: dict[str, Any] | None = None
    upload: _Upload | None = None
    fallback: dict[str, Any] | None = None


class _RecordingStore:
    """The ``UploadStore`` of a call, remembering which staged files the call took, so a failure can give them back."""

    def __init__(self, store: UploadStore) -> None:
        self._store = store
        self._taken: dict[str, UploadEntry] = {}
        self._made: set[str] = set()

    def store(self, data: bytes, filename: str, mime_type: str) -> str:
        ref_id = self._store.store(data, filename, mime_type)
        self._made.add(ref_id)  # an inline file of this call: the caller does not know its id
        return ref_id

    def consume(self, ref_id: str) -> UploadEntry | None:
        entry = self._store.consume(ref_id)
        if entry is not None and ref_id not in self._made:
            self._taken[ref_id] = entry
        return entry

    def restore(self) -> None:
        for ref_id, entry in self._taken.items():
            self._store.restore(ref_id, entry)
        self._taken.clear()


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _has_member(view: Mapping[str, Any], participant: Mapping[str, Any]) -> bool:
    """The thread already has the person (Maestro refuses a second membership: ``already_participant``)."""
    for member in view["participants"]:
        if member.get("state") == "removed" or member["kind"] != participant["kind"]:
            continue
        if participant["kind"] == "external":
            if member["external_id"] == participant["external_id"]:
                return True
        elif member["principal_id"] == participant["principal_id"]:
            return True
    return False


__all__ = ["EXTERNAL_CHANNELS", "MaestroIngress", "MaestroPublished"]
