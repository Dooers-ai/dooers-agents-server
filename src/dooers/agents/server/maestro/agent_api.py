"""HTTP client of the Maestro agent API (``{callback_base_url}/agent/*``), used outside a delivery.

``Repository`` and ``AgentMemory`` of a ``thread_management="maestro"`` agent run over this client
(``MaestroAgentPersistence``), not over the local database. It follows ``MaestroContextClient``:

- one Core agent credential per call (scope ``threads:manage``, resolved by ``MaestroConfig.credential_resolver``,
  never cached here), plus the workload identity headers of the remote profile;
- no redirect, no proxy from the environment, no implicit retry: a call that fails says if it is
  worth repeating (``retryable``) and the caller repeats it with the same ``Idempotency-Key``;
- every request is validated against the vendored contract before it is sent, and every response
  after it arrives (``Public.Agent*``): a response outside the contract is ``INVALID_RESPONSE``;
- errors are closed codes (``MaestroAgentApiError.code``). They never carry URL, token, payload or
  thread key. ``NOT_FOUND`` and ``THREAD_DELETING`` have their own classes (both are
  ``MaestroAgentMissing``) so the persistence maps them to ``None`` / empty lists (F31).

Every method returns the validated ``dict`` of the response (a deep copy, as ``MaestroContracts.validate``).

Idempotency keys. ``thread.create`` is already idempotent by ``external_key`` in Maestro, and Maestro keeps
the key column after a purge, so the key never carries the ``external_key`` or a phone number: it is
``thread.create:{uuid}``, where ``uuid`` is the SDK thread id when the caller has one (a stable key for a
retry), or a random UUID. Every other mutation sends ``{operation}:{random uuid}`` unless the caller gives a
key. ``publish_event`` sends the SDK event id as ``idempotency_key`` (``client_event_id``, F8).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

from dooers.agents.server.protocol.maestro import MaestroContractError, MaestroContracts, default_contracts

from .artifacts import artifact_type_allowed
from .client import MAX_CONTEXT_PAGE_BYTES, MAX_RESPONSE_BYTES, resolve_instance_credential
from .config import MaestroConfig, MaestroConfigurationError
from .links import ArtifactLinkMissing, LinkResolver
from .security import strict_json

logger = logging.getLogger(__name__)

SCOPES = ("threads:manage",)
#: Statuses worth repeating when the body is not a ``Public.Error`` (a proxy, a crash).
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_UPLOAD_BYTES_PER_SECOND = 1048576
_ERROR_BODY_BYTES = 65536


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


#: "Not given" for a scope argument whose ``None`` is a value (``workspace_id=None`` is the personal scope).
UNSET: Any = _Unset()


class MaestroAgentApiError(RuntimeError):
    """A failed agent API call. ``code`` is closed: a ``Public.Error`` code or one of the SDK codes below.

    SDK codes: ``REQUEST_INVALID`` (the request is outside the contract; nothing was sent),
    ``INVALID_RESPONSE`` (the answer is outside the contract), ``CREDENTIAL_UNAVAILABLE`` (no usable
    Core agent token), ``TRANSPORT_UNAVAILABLE`` (connection, timeout; retryable).

    ``status`` is the HTTP status (``None`` before an answer), ``retryable`` says if repeating the same call
    can work, ``reason_code`` and ``retry_after_seconds`` come from ``Public.Error.details``.
    The message is the code: it never holds URL, token, payload or key.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        reason_code: str | None = None,
        retry_after_seconds: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id


class MaestroAgentMissing(MaestroAgentApiError):
    """The thread or the record is not there for this agent: ``NOT_FOUND`` or ``THREAD_DELETING``."""


class MaestroAgentNotFound(MaestroAgentMissing):
    """``NOT_FOUND``: it does not exist, is not visible to the agent or was removed."""


class MaestroAgentThreadDeleting(MaestroAgentMissing):
    """``THREAD_DELETING``: the thread is being deleted (production: it is already gone)."""


class MaestroAgentScopeError(MaestroConfigurationError):
    """The call names another organization or workspace than the one of the agent token (F18)."""

    def __init__(self) -> None:
        super().__init__("MAESTRO_AGENT_SCOPE_MISMATCH")


def _missing(code: str, **kwargs: Any) -> MaestroAgentApiError:
    if code == "NOT_FOUND":
        return MaestroAgentNotFound(code, **kwargs)
    if code == "THREAD_DELETING":
        return MaestroAgentThreadDeleting(code, **kwargs)
    return MaestroAgentApiError(code, **kwargs)


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """An injected transport that the per-call client must not close."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class MaestroAgentApi:
    """The agent API of Maestro for one agent instance of one organization and workspace."""

    def __init__(
        self,
        config: MaestroConfig,
        *,
        contracts: MaestroContracts | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._contracts = contracts or default_contracts()
        self._transport = transport

    # -- scope (F18) --

    def check_scope(self, *, organization_id: str | None = UNSET, workspace_id: str | None = UNSET) -> None:
        """The agent token covers one organization and one workspace; anything else is a configuration error.

        ``workspace_id`` ``""`` or ``None`` is the personal scope (``MaestroConfig.workspace_id is None``).
        An argument that is not given is not checked. ``organization_id`` ``""`` or ``None`` is not checked.
        Raises ``MaestroAgentScopeError`` (a ``MaestroConfigurationError``).
        """
        if organization_id is not UNSET and organization_id and organization_id.lower() != self.config.organization_id.lower():
            raise MaestroAgentScopeError()
        if workspace_id is not UNSET:
            wanted = self.config.workspace_id.lower() if self.config.workspace_id else None
            given = workspace_id.lower() if workspace_id else None
            if given != wanted:
                raise MaestroAgentScopeError()

    def _scope_body(self) -> dict[str, Any]:
        if self.config.workspace_id is None:
            return {"scope_kind": "personal"}
        return {"scope_kind": "workspace", "workspace_id": self.config.workspace_id}

    def _check_thread(self, thread: Mapping[str, Any]) -> None:
        """A thread the contract accepts but of another scope is a wrong answer, never handed to the caller."""
        workspace = thread.get("workspace_id")
        if str(thread["organization_id"]).lower() != self.config.organization_id.lower() or (
            (workspace.lower() if workspace else None) != (self.config.workspace_id.lower() if self.config.workspace_id else None)
        ):
            raise MaestroAgentApiError("INVALID_RESPONSE")

    # -- idempotency --

    @staticmethod
    def thread_idempotency_key(thread_id: str | None = None) -> str:
        """``thread.create:{uuid}`` for ``create_thread``: the SDK thread id when it is a UUID, else a random UUID.

        The key never holds an ``external_key`` or a phone number (Maestro keeps the key column after a purge):
        a ``thread_id`` that is not a UUID is ignored.
        """
        if thread_id and _is_uuid(thread_id):
            return f"thread.create:{thread_id.lower()}"
        return f"thread.create:{uuid4()}"

    @staticmethod
    def _key(operation: str, given: str | None) -> str:
        key = given if given is not None else f"{operation}:{uuid4()}"
        if not 1 <= len(key) <= 128 or not all("!" <= char <= "~" for char in key):
            raise ValueError("invalid idempotency key")
        return key

    # -- threads --

    async def create_thread(
        self,
        *,
        external_key: str | None = None,
        title: str | None = None,
        channel: str | None = None,
        channel_meta: Mapping[str, Any] | None = None,
        queue: str | None = None,
        participants: Sequence[Mapping[str, Any]] = (),
        owner: Mapping[str, Any] | None = None,
        organization_id: str | None = UNSET,
        workspace_id: str | None = UNSET,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """``POST /agent/threads``: the thread (``Public.AgentThread``). The same ``external_key`` returns the thread.

        ``organization_id`` / ``workspace_id`` are only checked against the token (``check_scope``; ``""`` is
        personal). The thread is created in the scope of the token. ``participants`` are
        ``{"kind": "human"|"agent", "principal_id"}`` or ``{"kind": "external", "channel", "external_id"[, "display_name"]}``.
        ``idempotency_key`` (default ``thread_idempotency_key()``) must not hold ``external_key`` or a contact id:
        ``ValueError`` otherwise.
        """
        self.check_scope(organization_id=organization_id, workspace_id=workspace_id)
        key = self._key("thread.create", idempotency_key if idempotency_key is not None else self.thread_idempotency_key())
        # A UUID is not identifying: the SDK thread id is the external key of a thread made by ``Repository.create_thread`` (F8).
        identifying = {
            value
            for value in (external_key, *(p.get("external_id") for p in participants if p.get("kind") == "external"))
            if value and not _is_uuid(value)
        }
        identifying |= {value.lstrip("+") for value in identifying if value.lstrip("+")}
        if any(value in key for value in identifying):
            raise ValueError("the idempotency key must not hold an external key or a contact id")
        body: dict[str, Any] = {**self._scope_body(), "participants": [dict(p) for p in participants]}
        for name, value in (("external_key", external_key), ("title", title), ("channel", channel), ("queue", queue)):
            if value is not None:
                body[name] = value
        if channel_meta is not None:
            body["channel_meta"] = dict(channel_meta)
        if owner is not None:
            body["owner"] = dict(owner)
        return await self._call(
            "POST",
            "/agent/threads",
            body=body,
            body_schema="Public.AgentThreadCreateRequest",
            response_schema="Public.AgentThread",
            idempotency_key=key,
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
            check=self._check_thread,
        )

    async def find_thread(self, external_key: str) -> dict[str, Any] | None:
        """The agent's thread with this ``external_key``, or ``None`` (``GET /agent/threads?external_key=``)."""
        page = await self.list_threads(external_key=external_key, limit=1)
        return page["items"][0] if page["items"] else None

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        order: str | None = None,
        queue: str | None = None,
        external_key: str | None = None,
    ) -> dict[str, Any]:
        """``GET /agent/threads``: ``Public.AgentThreadPage`` of the threads of the agent in the token scope.

        Filters of the server: ``external_key``, ``queue`` (``""`` = no queue), ``order`` (``asc``/``desc`` of the
        last activity), ``cursor``, ``limit`` (1 to 999).
        """
        query = _query(cursor=cursor, limit=limit, order=order, queue=queue, external_key=external_key)
        return await self._call(
            "GET",
            "/agent/threads",
            query=query,
            query_schema="Public.AgentThreadListQuery",
            response_schema="Public.AgentThreadPage",
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
            check=lambda page: [self._check_thread(item) for item in page["items"]],
        )

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        """``GET /agent/threads/{id}``: ``thread_id`` is the caller's ``external_key`` or the Maestro id."""
        return await self._call(
            "GET",
            "/agent/threads/{id}",
            segments=("threads", thread_id),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            response_schema="Public.AgentThread",
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
            check=self._check_thread,
        )

    async def update_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        queue: str | None = None,
        clear_queue: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """``PATCH /agent/threads/{id}``: title and/or queue (``None`` = unchanged; ``clear_queue=True`` sends ``null``)."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if clear_queue:
            body["queue"] = None
        elif queue is not None:
            body["queue"] = queue
        return await self._call(
            "PATCH",
            "/agent/threads/{id}",
            segments=("threads", thread_id),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            body=body,
            body_schema="Public.AgentThreadPatchRequest",
            response_schema="Public.AgentThread",
            idempotency_key=self._key("thread.update", idempotency_key),
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
            check=self._check_thread,
        )

    async def delete_thread(self, thread_id: str, *, reason_code: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """``DELETE /agent/threads/{id}`` (202): ``{"deletion_job_id", "state": "deleting"}``. The key is free at once."""
        return await self._call(
            "DELETE",
            "/agent/threads/{id}",
            segments=("threads", thread_id),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            body={"reason_code": reason_code} if reason_code is not None else {},
            body_schema="Public.ReasonRequest",
            response_schema="Public.DeleteResponse",
            idempotency_key=self._key("thread.delete", idempotency_key),
        )

    # -- events --

    async def list_events(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        order: str = "asc",
        types: Sequence[str] | None = None,
        actor_kind: str | None = None,
        actor_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """``GET /agent/threads/{id}/events``: ``Public.AgentEventPage``. Filters run in the server (F21).

        ``types`` (event types), ``actor_kind`` (``human|agent|system|external``), ``actor_id`` and ``run_id``.
        An event removed by ``message.removed`` is not listed.
        """
        query = _query(
            cursor=cursor,
            limit=limit,
            order=order,
            type=",".join(types) if types else None,
            actor_kind=actor_kind,
            actor_id=actor_id,
            run_id=run_id,
        )
        return await self._call(
            "GET",
            "/agent/threads/{id}/events",
            segments=("threads", thread_id, "events"),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            query=query,
            query_schema="Public.AgentEventListQuery",
            response_schema="Public.AgentEventPage",
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
        )

    async def get_event(self, event_id: str) -> dict[str, Any]:
        """``GET /agent/events/{id}``: ``Public.AgentEvent`` by ``event_id`` or by the ``client_event_id`` the agent published.

        Only publications of this API have a ``client_event_id`` (an event of a handler output has none).
        """
        return await self._call(
            "GET",
            "/agent/events/{id}",
            segments=("events", event_id),
            path_schema=("Public.AgentEventPath", {"id": event_id}),
            response_schema="Public.AgentEvent",
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
        )

    async def publish_event(
        self,
        thread_id: str,
        *,
        actor: Mapping[str, Any],
        payload: Mapping[str, Any],
        trigger: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """``POST /agent/threads/{id}/events``: a ``message.created``; ``Public.AgentEventCreateResponse``.

        ``actor`` is ``{"kind": "self"}``, ``{"kind": "human", "principal_id"[, "display_name", "email", "identity_ids"]}``
        or ``{"kind": "external", "channel", "external_id"[, "display_name"]}``. ``idempotency_key`` is the SDK event id
        (it becomes ``client_event_id``, F8): the same key and request replay the same answer.
        """
        body = {
            "type": "message.created",
            "actor": dict(actor),
            "payload": dict(payload),
            "trigger": trigger,
            "idempotency_key": idempotency_key,
        }
        return await self._call(
            "POST",
            "/agent/threads/{id}/events",
            segments=("threads", thread_id, "events"),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            body=body,
            body_schema="Public.AgentEventCreateRequest",
            response_schema="Public.AgentEventCreateResponse",
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
        )

    async def remove_event(self, thread_id: str, event_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """``DELETE /agent/threads/{id}/events/{eventId}``: the ``message.removed`` event (``Public.AgentEvent``)."""
        return await self._call(
            "DELETE",
            "/agent/threads/{id}/events/{eventId}",
            segments=("threads", thread_id, "events", event_id),
            path_schema=("Public.AgentThreadEventPath", {"id": thread_id, "eventId": event_id}),
            body={},
            body_schema="Public.Empty",
            response_schema="Public.AgentEvent",
            idempotency_key=self._key("event.remove", idempotency_key),
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
        )

    async def add_participant(
        self, thread_id: str, participant: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """``POST /agent/threads/{id}/participants``: the ``participant.added`` event (``Public.AgentEvent``).

        ``participant`` as in ``create_thread``; ``role`` defaults to ``member``.
        """
        return await self._call(
            "POST",
            "/agent/threads/{id}/participants",
            segments=("threads", thread_id, "participants"),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            body={"role": "member", **participant},
            body_schema="Public.AgentParticipantAddRequest",
            response_schema="Public.AgentEvent",
            idempotency_key=self._key("participant.add", idempotency_key),
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
        )

    # -- files --

    async def upload_artifact(
        self, thread_id: str, data: bytes, filename: str, mime_type: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """``POST /agent/threads/{id}/artifacts`` (multipart): ``Public.AgentArtifact`` ``{artifact_id, filename, mime_type, size_bytes}``.

        The file policy is the production one (``MaestroConfig.artifact_mime_types`` may restrict it) up to
        ``max_artifact_bytes``; a file outside it is ``REQUEST_INVALID`` and nothing is sent. Use the returned
        ``mime_type`` (Maestro stores the canonical one) in the attachment part of ``publish_event``.
        """
        if (
            type(data) is not bytes
            or not 0 < len(data) <= self.config.max_artifact_bytes
            or not artifact_type_allowed(self.config, mime_type, filename)
        ):
            raise MaestroAgentApiError("REQUEST_INVALID")
        metadata = {"filename": filename, "mime_type": mime_type, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

        def check(artifact: Mapping[str, Any]) -> None:
            if artifact["filename"] != filename or artifact["size_bytes"] != len(data):
                raise MaestroAgentApiError("INVALID_RESPONSE")

        seconds = max(self.config.artifact_timeout_seconds, math.ceil(len(data) / _UPLOAD_BYTES_PER_SECOND))
        return await self._call(
            "POST",
            "/agent/threads/{id}/artifacts",
            segments=("threads", thread_id, "artifacts"),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            body=metadata,
            body_schema="Public.AgentArtifactCreateRequest",
            # The local parser of Maestro requires the metadata first and without a Content-Type.
            files=[("metadata", (None, _json_bytes(metadata).decode())), ("file", (filename, data, mime_type))],
            response_schema="Public.AgentArtifact",
            idempotency_key=self._key("artifact.upload", idempotency_key),
            timeout=seconds,
            check=check,
        )

    async def issue_artifact_link(self, thread_id: str, artifact_id: str) -> dict[str, Any]:
        """``POST /agent/threads/{id}/artifacts/{artifactId}/links``: ``Public.ArtifactLinkResponse`` ``{url, expires_at}``.

        ``url`` is a bearer link (no login): never log it. A route that is off (``ARTIFACT_LINKS_ENABLED``) is
        ``UNSUPPORTED_CAPABILITY``; a file that is not of the thread is ``NOT_FOUND``.
        """
        return await self._call(
            "POST",
            "/agent/threads/{id}/artifacts/{artifactId}/links",
            segments=("threads", thread_id, "artifacts", artifact_id, "links"),
            path_schema=("Public.AgentThreadArtifactPath", {"id": thread_id, "artifactId": artifact_id}),
            body={},
            body_schema="Public.Empty",
            response_schema="Public.ArtifactLinkResponse",
        )

    def link_resolver(self, thread_id: str) -> LinkResolver:
        """A ``LinkResolver`` of the temporary links of the files of ``thread_id``.

        A file Maestro has no link for (``NOT_FOUND``, ``FORBIDDEN``) gives ``None`` (the part goes without URL).
        """

        async def issue(artifact_id: str) -> dict[str, Any]:
            try:
                return await self.issue_artifact_link(thread_id, artifact_id)
            except MaestroAgentMissing:
                raise ArtifactLinkMissing() from None
            except MaestroAgentApiError as error:
                if error.code == "FORBIDDEN":
                    raise ArtifactLinkMissing() from None
                raise

        return LinkResolver(issue)

    # -- runs --

    async def list_runs(self, thread_id: str, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """``GET /agent/threads/{id}/runs``: ``Public.AgentRunPage``, newest ``started_at`` first."""
        return await self._call(
            "GET",
            "/agent/threads/{id}/runs",
            segments=("threads", thread_id, "runs"),
            path_schema=("Public.AgentThreadPath", {"id": thread_id}),
            query=_query(cursor=cursor, limit=limit),
            query_schema="Public.AgentRunListQuery",
            response_schema="Public.AgentRunPage",
        )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """``GET /agent/runs/{id}``: ``Public.AgentRun`` of the agent's threads."""
        return await self._call(
            "GET",
            "/agent/runs/{id}",
            segments=("runs", run_id),
            path_schema=("Public.AgentRunPath", {"id": run_id}),
            response_schema="Public.AgentRun",
        )

    # -- channel deliveries --

    async def record_channel_delivery(
        self,
        *,
        thread_id: str,
        event_id: str,
        status: str,
        provider_message_id: str | None = None,
        error: str | None = None,
        error_code: str | int | None = None,
        channel: str = "whatsapp",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """``POST /agent/channel-deliveries``: ``{"applied": false}`` or ``{"applied": true, "event"}``.

        The answer is a ``Public.AgentChannelDeliveryResponse``.

        ``thread_id`` is the caller's ``external_key`` or the Maestro id. A status that does not move forward is
        ``{"applied": false}`` (no error), as the production receipt rule.
        """
        body: dict[str, Any] = {"channel": channel, "thread_id": thread_id, "event_id": event_id, "status": status}
        for name, value in (("provider_message_id", provider_message_id), ("error", error), ("error_code", error_code)):
            if value is not None:
                body[name] = value
        return await self._call(
            "POST",
            "/agent/channel-deliveries",
            body=body,
            body_schema="Public.AgentChannelDeliveryRequest",
            response_schema="Public.AgentChannelDeliveryResponse",
            idempotency_key=self._key("channel-delivery", idempotency_key),
            max_bytes=MAX_CONTEXT_PAGE_BYTES,
        )

    async def find_channel_delivery(self, provider_message_id: str) -> dict[str, Any]:
        """``GET /agent/channel-deliveries``: ``{thread_id, event_id, status}`` (``Public.AgentChannelDelivery``).

        An unknown id or a removed message is ``MaestroAgentNotFound``.
        """
        return await self._call(
            "GET",
            "/agent/channel-deliveries",
            query={"provider_message_id": provider_message_id},
            query_schema="Public.AgentChannelDeliveryQuery",
            response_schema="Public.AgentChannelDelivery",
        )

    # -- transport --

    def _validate(self, schema: str, value: Any, *, response: bool) -> Any:
        try:
            return self._contracts.validate(schema, value)
        except MaestroContractError:
            raise MaestroAgentApiError("INVALID_RESPONSE" if response else "REQUEST_INVALID") from None

    async def _headers(self, url: str) -> dict[str, str]:
        try:
            credential = await resolve_instance_credential(self.config, None, self._contracts, SCOPES)
            headers = {
                "Authorization": f"Bearer {credential.token}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-store",
            }
            headers.update(await self.config.transport_headers(url))
            return headers
        except Exception:
            raise MaestroAgentApiError("CREDENTIAL_UNAVAILABLE") from None

    async def _call(
        self,
        method: str,
        route: str,
        *,
        segments: Sequence[str] = (),
        path_schema: tuple[str, dict[str, str]] | None = None,
        query: dict[str, Any] | None = None,
        query_schema: str | None = None,
        body: Any = None,
        body_schema: str | None = None,
        files: list[Any] | None = None,
        response_schema: str,
        idempotency_key: str | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
        timeout: float | None = None,
        check: Callable[[Any], Any] | None = None,
    ) -> Any:
        """One request: validate the request, call, validate the answer. ``route`` is the log name (no ids)."""
        if path_schema is not None:
            self._validate(*path_schema, response=False)
        if query_schema is not None:
            self._validate(query_schema, query or {}, response=False)
        if body_schema is not None:
            self._validate(body_schema, body, response=False)
        path = "/".join(quote(segment, safe="") for segment in segments) if segments else route.removeprefix("/agent/")
        url = f"{self.config.callback_base_url}/agent/{path}"
        params = {name: str(value) for name, value in (query or {}).items()}
        try:
            async with asyncio.timeout(timeout or self.config.network_timeout_seconds):
                headers = await self._headers(self.config.callback_base_url)
                if idempotency_key is not None:
                    headers["Idempotency-Key"] = idempotency_key
                content = None
                if files is None and body is not None:
                    headers["Content-Type"] = "application/json"
                    content = _json_bytes(body)
                transport = _BorrowedTransport(self._transport) if self._transport else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False, timeout=self.config.network_timeout_seconds
                ) as http:
                    async with http.stream(method, url, params=params or None, headers=headers, content=content, files=files) as response:
                        raw = await self._read(response, max_bytes if response.status_code < 400 else _ERROR_BODY_BYTES)
                        status = response.status_code
                        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        except MaestroAgentApiError:
            raise
        except (TimeoutError, httpx.HTTPError, OSError):
            logger.warning("maestro agent api %s: transport failure", route)
            raise MaestroAgentApiError("TRANSPORT_UNAVAILABLE", retryable=True) from None
        if status >= 400:
            raise self._failure(method, route, status, raw)
        if status not in {200, 201, 202} or content_type != "application/json":
            raise MaestroAgentApiError("INVALID_RESPONSE", status=status)
        try:
            value = self._validate(response_schema, strict_json(raw), response=True)
        except (ValueError, TypeError):
            raise MaestroAgentApiError("INVALID_RESPONSE", status=status) from None
        if check is not None:
            try:
                check(value)
            except MaestroAgentApiError as error:
                error.status = status
                raise
            except (KeyError, TypeError, ValueError, AttributeError):
                raise MaestroAgentApiError("INVALID_RESPONSE", status=status) from None
        return value

    @staticmethod
    async def _read(response: httpx.Response, limit: int) -> bytes:
        raw = bytearray()
        if response.headers.get("content-encoding", "identity") != "identity":
            raise MaestroAgentApiError("INVALID_RESPONSE", status=response.status_code)
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > limit:
                raise MaestroAgentApiError("INVALID_RESPONSE", status=response.status_code)
        return bytes(raw)

    def _failure(self, method: str, route: str, status: int, raw: bytes) -> MaestroAgentApiError:
        try:
            error = self._contracts.validate("Public.Error", strict_json(raw))["error"]
        except (MaestroContractError, ValueError, TypeError):
            logger.warning("maestro agent api %s %s: HTTP %s without a contract error", method, route, status)
            return MaestroAgentApiError("INVALID_RESPONSE", status=status, retryable=status in _RETRYABLE_STATUSES)
        details = error.get("details", {})
        logger.warning("maestro agent api %s %s: %s (HTTP %s)", method, route, error["code"], status)
        return _missing(
            error["code"],
            status=status,
            retryable=error["retryable"],
            reason_code=details.get("reason_code"),
            retry_after_seconds=details.get("retry_after_seconds"),
            request_id=error["request_id"],
        )


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _query(**values: Any) -> dict[str, Any]:
    """The query the contract validates: only what is given (``limit`` stays an integer)."""
    return {name: value for name, value in values.items() if value is not None}
