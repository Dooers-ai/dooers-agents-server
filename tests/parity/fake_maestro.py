"""In-memory Maestro for the parity suite: the SDK's Maestro mode runs against it.

``FakeMaestro`` plays the two sides the SDK does not own:

- the client (a person in the web chat): ``create_thread``, ``publish``, ``upload``
  and ``respond_form``;
- the Maestro service: it signs one dispatch per delivery (``deliveries_for`` and
  ``deliver``) and answers the SDK callbacks (context, heartbeat, outputs, stream,
  complete, artifacts), with the same semantics as ``CallbackFixture`` in
  ``tests/test_maestro_executor.py``.

Every request, from either side, is checked against the vendored contract with
``MaestroContracts`` (``is_valid``, the validator behind ``validate``) before it changes
any state. A request the contract does not accept changes nothing: a client request
raises ``FakeMaestroRejected``, and a callback gets the contract's ``Public.Error`` (also
kept in ``rejections``). Every response and every stored event goes through
``MaestroContracts.validate`` too, so a bug in the fake fails loudly.

Each accepted output becomes the event of its type (index 4.4: ``message.create`` ->
``message.created``, ``run.update`` -> ``run.updated``, ...), with the payload as sent
plus the fields the contract says Maestro adds. The fake does not apply ``thread.update``
or ``participant.add`` to the thread state, and it creates no ``form.responded`` delivery
(plan 3, task 7). It issues temporary artifact links (``ARTIFACT_LINKS_ENABLED``) for the
files linked to the delivery's thread up to its context cutoff, without serving their bytes. Scope: one person and one agent per thread,
no retries or lease renewal, no suspension/interactions, no archive or delete. A real
Maestro also writes execution events, and none of them has a legacy ``ThreadEvent``.

``legacy_view`` and ``legacy_thread_view`` (test only) map Maestro data to the
production shapes that ``normalize`` compares. They only rename and reshape what
Maestro has. A production field that Maestro does not carry keeps the production
model default (``None`` or the ``User`` defaults). They never fill it from the scenario.
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import dooers.agents.server.maestro.adapter as adapter_module
import dooers.agents.server.maestro.executor as executor_module
from dooers.agents.server.maestro import MaestroConfig, MaestroDeliveryAdapter, MaestroInstanceCredential
from dooers.agents.server.maestro.callbacks import STORED_EVENT_TYPES
from dooers.agents.server.maestro.context import MaestroPrivateResources, MaestroPrivateScope
from dooers.agents.server.protocol.maestro import MaestroContracts
from dooers.agents.server.protocol.models import Thread, ThreadEvent, User
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import AGENT_ID, ASSISTANT_NAME, ORG_ID, WORKSPACE_ID
from tests.test_maestro_admission import BASE, json_bytes, request, sign, signed_request, timestamp
from tests.test_maestro_context import AGENT

# The scenario identities (UUIDs, as Maestro requires), so both modes see the same ids.
ORGANIZATION_ID = ORG_ID
INSTANCE_AGENT_ID = AGENT_ID
RUNTIME_ID = BASE["authorized_runtime_id"]
CALLBACK_BASE_URL = BASE["configured_callback_base_url"]
RUNTIME_ENDPOINT = "http://127.0.0.1:9000/api/dev/agent/maestro/deliveries"
CORE_BASE_URL = "http://127.0.0.1:8788/api/v1"
#: Outputs that make a phase `responded` in Maestro (spec M07-005/006); the SDK has its own copy.
VISIBLE_REPLY_OUTPUTS = frozenset({"message.create", "form.create", "chart.create"})
#: Maestro accepts only these file types today (``LOCAL_SYNTHETIC_MIME_TYPES``, design 5.5).
MAESTRO_MIME_TYPES = frozenset({"text/plain", "application/pdf", "image/png", "image/jpeg", "audio/wav"})
MAX_ARTIFACT_BYTES = 8388608
_ACTORS = {"human": "user", "agent": "assistant", "system": "system"}


@functools.cache
def shared_contracts() -> MaestroContracts:
    """The vendored contract, compiled once per process (the validator is immutable)."""
    return MaestroContracts()


@functools.cache
def signing_key() -> rsa.RSAPrivateKey:
    """The fake Maestro signing key (dispatch and capability JWTs, core credential)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@contextmanager
def reuse_compiled_contracts() -> Iterator[None]:
    """Speed only: the adapter and the executor reuse ``shared_contracts()``.

    Each ``MaestroContracts()`` compiles every schema of the same verified bundle again
    (about 1.3 s). Validation behavior does not change.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(adapter_module, "MaestroContracts", shared_contracts))
        stack.enter_context(patch.object(executor_module, "MaestroContracts", shared_contracts))
        yield


async def private_resources(scope: MaestroPrivateScope) -> MaestroPrivateResources:
    """The creator's private resolver: an empty private database, no settings or analytics."""
    return MaestroPrivateResources(scope, MemoryPersistence(), {}, {})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sequence_key(sequence: str) -> tuple[int, str]:
    return len(sequence), sequence


class FakeMaestroRejected(Exception):
    """A client request (thread, message, upload, form response) the contract does not accept."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"{action}: {reason}")
        self.action = action
        self.reason = reason


class _CallbackError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass
class Delivery:
    """One delivery to the agent: its signed dispatch and what its execution did."""

    delivery_id: str
    execution_id: str
    thread_id: str
    trigger_event: dict[str, Any]
    envelope: dict[str, Any]
    headers: dict[str, str]
    state: str = "running"
    reason_code: str | None = None
    status_code: int | None = None
    progress: list[str] = field(default_factory=list)
    output_event_ids: list[str] = field(default_factory=list)
    output_kinds: list[str] = field(default_factory=list)
    stream_chunks: list[dict[str, Any]] = field(default_factory=list)
    context_pages: int = 0
    replies: dict[str, tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=dict)

    @property
    def capability(self) -> str:
        return self.headers["Maestro-Execution-Capability"]

    @property
    def context_cursor(self) -> str:
        return self.envelope["context_cursor"]


class FakeMaestro:
    def __init__(
        self,
        *,
        assistant_name: str = ASSISTANT_NAME,
        artifact_mime_types: tuple[str, ...] = (),
        product_capabilities: tuple[str, ...] = (),
    ) -> None:
        self.contracts = shared_contracts()
        self.assistant_name = assistant_name
        key = signing_key()
        self.core_token, core_expires_at = self._core_credential(key)

        async def credential(_request: Any) -> MaestroInstanceCredential:
            return MaestroInstanceCredential(self.core_token, core_expires_at)

        public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.config = MaestroConfig(
            runtime_endpoint=RUNTIME_ENDPOINT,
            callback_base_url=CALLBACK_BASE_URL,
            core_base_url=CORE_BASE_URL,
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            instance_agent_id=INSTANCE_AGENT_ID,
            runtime_id=RUNTIME_ID,
            trusted_public_keys={BASE["dispatch_header"]["kid"]: public_pem},
            credential_resolver=credential,
            allow_local_http=True,
            artifact_mime_types=artifact_mime_types,
            product_capabilities=product_capabilities,
        )
        self._key = key
        self._base_path = urlsplit(CALLBACK_BASE_URL).path
        self.threads: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.deliveries: dict[str, Delivery] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.rejections: list[tuple[str, str]] = []

    @staticmethod
    def _core_credential(key: rsa.RSAPrivateKey) -> tuple[str, int]:
        now = int(time.time())
        claims = deepcopy(AGENT)
        claims.update(
            sub=INSTANCE_AGENT_ID,
            agent_id=INSTANCE_AGENT_ID,
            runtime_id=RUNTIME_ID,
            organization_id=ORGANIZATION_ID,
            workspace_id=WORKSPACE_ID,
            iat=now - 1,
            exp=now + 240,  # the contract caps a core credential at 300 s
            jti=str(uuid4()),
            scopes=["processing:write", "responses:write", "deliveries:read", "context:read"],
        )
        token = sign(key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "core-fixture"}, claims)
        return token, claims["exp"]

    # -- client side (a person in the web chat) --

    def _client_request(self, action: str, schema: str, value: dict[str, Any], detail: str = "") -> dict[str, Any]:
        if not self.contracts.is_valid(schema, value):
            raise FakeMaestroRejected(action, f"{schema} does not accept the request {detail}".strip())
        return deepcopy(value)

    def create_thread(self, human: User, *, title: str | None) -> str:
        request = self._client_request(
            "thread.create",
            "Public.ThreadCreateRequest",
            {
                "scope_kind": "workspace",
                "workspace_id": WORKSPACE_ID,
                "title": title or "",
                "participants": [{"kind": "agent", "principal_id": INSTANCE_AGENT_ID}],
            },
        )
        thread_id, now = str(uuid4()), _now()
        person = {
            "participant_id": str(uuid4()),
            "display_name": human.user_name or human.user_id,
            "state": "active",
            "joined_sequence": "0",
            "left_sequence": None,
            "kind": "human",
            "principal_id": human.user_id,
            "role": "owner",
        }
        agent = {
            "participant_id": str(uuid4()),
            "display_name": self.assistant_name,
            "state": "active",
            "joined_sequence": "0",
            "left_sequence": None,
            "kind": "agent",
            "principal_id": INSTANCE_AGENT_ID,
            "instance_agent_id": INSTANCE_AGENT_ID,
            "role": "member",
            "capabilities": [],
            "availability": "available",
        }
        thread = {
            "organization_id": ORGANIZATION_ID,
            "scope_kind": request["scope_kind"],
            "workspace_id": request["workspace_id"],
            "thread_id": thread_id,
            "mode": "maestro",
            "title": request["title"],
            "state": "active",
            "version": 1,
            "participants": [person, agent],
            "permissions": {
                "read": True,
                "write": True,
                "manage": True,
                "supervise": False,
                "export": True,
                "cancel": True,
                "feedback": True,
                "archive": True,
                "remove_participants": True,
            },
            "capabilities": [],
            "last_sequence": "0",
            "cursor": f"thread.{thread_id}.0",
            "supervision_policy": "strict",
            "policy_version": 1,
            "created_at": now,
            "updated_at": now,
        }
        self.threads[thread_id] = self.contracts.validate("Public.Thread", thread)
        self.events[thread_id] = []
        return thread_id

    def publish(self, thread_id: str, human: User, content: list[dict[str, Any]], *, client_event_id: str) -> dict[str, Any]:
        request = self._client_request(
            "message.create",
            "Public.MessageCreateRequest",
            {"type": "message.create", "client_event_id": client_event_id, "content": deepcopy(content), "mentions": []},
            f"(part types: {[part.get('type') for part in content]})",
        )
        person = self._participant(thread_id, kind="human", principal_id=human.user_id)
        for part in request["content"]:
            if part["type"] == "attachment":
                artifact = self.artifacts.get(part["artifact_id"])
                if artifact is None or artifact["state"] != "ready_unbound" or artifact["uploaded_by"] != human.user_id:
                    raise FakeMaestroRejected("message.create", "attachment is not an unbound upload of this person")
        actor = {
            "kind": "human",
            "principal_id": person["principal_id"],
            "participant_id": person["participant_id"],
            "display_name": person["display_name"],
        }
        payload = {"client_event_id": request["client_event_id"], "content": request["content"], "mentions": request["mentions"]}
        event = self._append(thread_id, actor, payload)
        self._link_attachments(event)
        return event

    def respond_form(self, thread_id: str, human: User, data: Any) -> dict[str, Any]:
        """The legacy ``form.response``: Maestro answers only interactions, and v1 has no legacy form."""
        self._participant(thread_id, kind="human", principal_id=human.user_id)
        raise FakeMaestroRejected(
            "form.response",
            "the thread has no open form: the Maestro contract has no event for a legacy form (send.form without suspension)",
        )

    def upload(self, human: User, data: bytes, *, filename: str, mime_type: str) -> dict[str, Any]:
        """A person uploads a file for the next message; returns the attachment part."""
        metadata = self._client_request(
            "artifact.upload",
            "Public.ArtifactCreateRequest",
            {
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "workspace_id": WORKSPACE_ID,
            },
        )
        if mime_type not in MAESTRO_MIME_TYPES or not 0 < len(data) <= MAX_ARTIFACT_BYTES:
            raise FakeMaestroRejected("artifact.upload", f"Maestro does not accept {mime_type} ({len(data)} bytes)")
        artifact = self._store_artifact(metadata, data, uploaded_by=human.user_id, execution_id=None)
        return {
            "type": "attachment",
            "artifact_id": artifact["artifact_id"],
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(data),
        }

    # -- delivery side --

    def deliveries_for(self, event: dict[str, Any]) -> list[Delivery]:
        """Routing with one person and one agent: a person's message invokes the agent."""
        if event["actor"]["kind"] != "human":
            return []
        thread_id = event["thread_id"]
        agent = self._participant(thread_id, kind="agent", principal_id=INSTANCE_AGENT_ID)
        delivery_id, execution_id = str(uuid4()), str(uuid4())
        bound = {
            "organization_id": ORGANIZATION_ID,
            "workspace_id": WORKSPACE_ID,
            "thread_id": thread_id,
            "delivery_id": delivery_id,
            "execution_id": execution_id,
            "phase_id": "initial",
            "recipient_participant_id": agent["participant_id"],
            "instance_agent_id": INSTANCE_AGENT_ID,
            "runtime_id": RUNTIME_ID,
            "lease_fence": "1",
        }

        def bind(data: dict[str, Any]) -> None:
            for section in ("envelope", "dispatch_claims", "capability_claims"):
                data[section].update(bound)
            data["envelope"].update(
                callback_base_url=CALLBACK_BASE_URL,
                context_cursor=f"ctx.{delivery_id}",
                context_cutoff_sequence=event["sequence"],
                trigger_event_id=event["event_id"],
                trigger_sequence=event["sequence"],
                root_event_id=event["root_event_id"],
                attempt_number=1,
                phase_retry_number=0,
                causal_depth=0,
            )
            data["capability_claims"].update(sub=INSTANCE_AGENT_ID, jti=str(uuid4()))
            # ARTIFACT_LINKS_ENABLED: the capability carries artifact.link.issue, as Maestro signs it.
            data["capability_claims"]["operations"] = [*data["capability_claims"]["operations"], "artifact.link.issue"]
            data["dispatch_claims"]["jti"] = str(uuid4())

        envelope, headers = signed_request(self._key, self.config, change=bind)
        delivery = Delivery(delivery_id, execution_id, thread_id, deepcopy(event), envelope, headers)
        self.deliveries[delivery_id] = delivery
        return [delivery]

    async def deliver(self, adapter: MaestroDeliveryAdapter, delivery: Delivery) -> httpx.Response:
        """POST the signed dispatch to the SDK adapter, as the Maestro dispatcher does."""
        response = await request(adapter, json_bytes(delivery.envelope), delivery.headers)
        delivery.status_code = response.status_code
        return response

    @contextmanager
    def serving(self, mock: respx.MockRouter | None = None) -> Iterator[FakeMaestro]:
        """Route the SDK callbacks (``callback_base_url``) to ``handle``; nothing reaches the network."""
        with ExitStack() as stack:
            if mock is None:
                mock = stack.enter_context(respx.mock(assert_all_called=False))
            mock.route(url__startswith=CALLBACK_BASE_URL + "/").mock(side_effect=self.handle)
            stack.enter_context(reuse_compiled_contracts())
            yield self

    def all_events(self) -> list[dict[str, Any]]:
        """Every thread (creation order) with its events (sequence order)."""
        return [deepcopy(event) for thread_id in self.threads for event in self.events[thread_id]]

    # -- Maestro side: SDK callbacks --

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path[len(self._base_path) :] if request.url.path.startswith(self._base_path + "/") else ""
        segments = path.strip("/").split("/")
        action = f"{request.method} {path}"
        try:
            if request.method == "GET" and len(segments) == 3 and segments[0] == "deliveries" and segments[2] == "context":
                action = "context"
                return self._context(request, segments[1])
            if request.method == "POST" and len(segments) == 3 and segments[0] == "executions":
                action = segments[2]
                return self._execution(request, segments[1], action)
            if request.method == "POST" and segments == ["artifacts"]:
                action = "artifact.upload"
                return self._artifact_upload(request)
            if request.method == "GET" and len(segments) == 3 and segments[0] == "artifacts" and segments[2] == "download":
                action = "artifact.download"
                return self._artifact_download(request, segments[1])
            if (
                request.method == "POST"
                and len(segments) == 5
                and (segments[0], segments[2], segments[4]) == ("executions", "artifacts", "links")
            ):
                action = "artifact.link.issue"
                return self._artifact_link(request, segments[1], segments[3])
            raise _CallbackError(404, "NOT_FOUND")
        except _CallbackError as error:
            self.rejections.append((action, error.code))
            body = {
                "error": {
                    "code": error.code,
                    "message": f"FakeMaestro rejected {action}.",
                    "retryable": False,
                    "request_id": str(uuid4()),
                }
            }
            return httpx.Response(error.status, json=self.contracts.validate("Public.Error", body))

    def _authorized_delivery(self, request: httpx.Request) -> Delivery:
        if request.headers.get("authorization") != f"Bearer {self.core_token}":
            raise _CallbackError(401, "UNAUTHENTICATED")
        capability = request.headers.get("maestro-execution-capability")
        for delivery in self.deliveries.values():
            if delivery.capability == capability:
                return delivery
        raise _CallbackError(401, "UNAUTHENTICATED")

    def _json(self, request: httpx.Request, schema: str) -> dict[str, Any]:
        try:
            value = json.loads(request.content)
        except ValueError:
            raise _CallbackError(400, "INVALID_REQUEST") from None
        if not self.contracts.is_valid(schema, value):
            raise _CallbackError(400, "INVALID_REQUEST")
        return value

    @staticmethod
    def _running(delivery: Delivery, body: dict[str, Any]) -> None:
        if body["lease_fence"] != delivery.envelope["lease_fence"]:
            raise _CallbackError(409, "LEASE_STALE")
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")

    def _reply(self, schema: str, value: dict[str, Any], status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=self.contracts.validate(schema, value))

    def _context(self, request: httpx.Request, delivery_id: str) -> httpx.Response:
        delivery = self._authorized_delivery(request)
        if delivery.delivery_id != delivery_id:
            raise _CallbackError(403, "FORBIDDEN")
        params = dict(request.url.params)
        try:
            query = {"cursor": params["cursor"], "limit": int(params.get("limit", "100"))}
        except (KeyError, ValueError):
            raise _CallbackError(400, "INVALID_REQUEST") from None
        if not self.contracts.is_valid("Public.PageQuery", query):
            raise _CallbackError(400, "INVALID_REQUEST")
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")
        cursor = query["cursor"]
        if cursor == delivery.context_cursor:
            offset = 0
        elif cursor.startswith(delivery.context_cursor + ".") and cursor.rsplit(".", 1)[1].isdecimal():
            offset = int(cursor.rsplit(".", 1)[1])
        else:
            raise _CallbackError(400, "CURSOR_INVALID")
        cutoff = _sequence_key(delivery.envelope["context_cutoff_sequence"])
        history = [event for event in self.events[delivery.thread_id] if _sequence_key(event["sequence"]) <= cutoff]
        end = offset + query["limit"]
        has_more = end < len(history)
        delivery.context_pages += 1
        page = {
            "delivery_id": delivery.delivery_id,
            "trigger_event": deepcopy(delivery.trigger_event),
            "participants": deepcopy(self.threads[delivery.thread_id]["participants"]),
            "context_cursor": delivery.context_cursor,
            "history": {
                "items": deepcopy(history[offset:end]),
                "next_cursor": f"{delivery.context_cursor}.{end}" if has_more else None,
                "has_more": has_more,
                "watermark": delivery.envelope["context_cutoff_sequence"],
            },
        }
        return self._reply("Public.ContextResponse", page)

    def _execution(self, request: httpx.Request, execution_id: str, action: str) -> httpx.Response:
        handlers = {
            "heartbeat": self._heartbeat,
            "outputs": self._outputs,
            "stream": self._stream,
            "complete": self._complete,
        }
        if action in {"suspend", "tool-approval-suspend"}:
            self._authorized_delivery(request)
            raise _CallbackError(422, "UNSUPPORTED_CAPABILITY")
        if action not in handlers:
            raise _CallbackError(404, "NOT_FOUND")
        delivery = self._authorized_delivery(request)
        if delivery.execution_id != execution_id:
            raise _CallbackError(403, "FORBIDDEN")
        return handlers[action](request, delivery)

    def _heartbeat(self, request: httpx.Request, delivery: Delivery) -> httpx.Response:
        body = self._json(request, "Public.HeartbeatRequest")
        self._running(delivery, body)
        delivery.progress.append(body.get("progress_code", "running"))
        lease_until = min(time.time() + 59, _parse(delivery.envelope["deadline_at"]).timestamp())
        return self._reply("Public.HeartbeatResponse", {"lease_until": timestamp(lease_until)})

    def _stream(self, request: httpx.Request, delivery: Delivery) -> httpx.Response:
        body = self._json(request, "Public.StreamRequest")
        self._running(delivery, body)
        delivery.stream_chunks.append({key: body[key] for key in ("output_id", "chunk_index", "delta")})
        return self._reply(
            "Public.StreamResponse",
            {"status": "accepted", "ephemeral": True, "output_id": body["output_id"], "chunk_index": body["chunk_index"]},
        )

    def _idempotent(self, request: httpx.Request, delivery: Delivery, body: dict[str, Any]) -> dict[str, Any] | None:
        key = request.headers.get("idempotency-key")
        if not key:
            raise _CallbackError(400, "INVALID_REQUEST")
        semantic = {name: value for name, value in body.items() if name != "lease_fence"}
        previous = delivery.replies.get(key)
        if previous is None:
            return None
        if previous[0] != semantic:
            raise _CallbackError(409, "IDEMPOTENCY_CONFLICT")
        return previous[1]

    def _remember(self, request: httpx.Request, delivery: Delivery, body: dict[str, Any], result: dict[str, Any]) -> None:
        semantic = {name: value for name, value in body.items() if name != "lease_fence"}
        delivery.replies[request.headers["idempotency-key"]] = (deepcopy(semantic), deepcopy(result))

    def _outputs(self, request: httpx.Request, delivery: Delivery) -> httpx.Response:
        body = self._json(request, "Public.OutputRequest")
        previous = self._idempotent(request, delivery, body)
        if previous is not None:
            return self._reply("Public.PersistedResponse", {**previous, "duplicate": True})
        self._running(delivery, body)
        kind = body["type"]
        for part in body["payload"]["content"] if kind == "message.create" else ():
            if part["type"] == "attachment":
                artifact = self.artifacts.get(part["artifact_id"])
                if artifact is None or artifact["state"] != "ready_unbound" or artifact["execution_id"] != delivery.execution_id:
                    raise _CallbackError(409, "ARTIFACT_NOT_READY")
        agent = self._participant(delivery.thread_id, kind="agent", principal_id=INSTANCE_AGENT_ID)
        actor = {
            "kind": "agent",
            "principal_id": INSTANCE_AGENT_ID,
            "instance_agent_id": INSTANCE_AGENT_ID,
            "participant_id": agent["participant_id"],
            "display_name": agent["display_name"],
        }
        payload = self._stored_payload(delivery, body)
        trigger = delivery.trigger_event
        event = self._append(
            delivery.thread_id,
            actor,
            payload,
            event_type=STORED_EVENT_TYPES[kind],
            causation_event_id=trigger["event_id"],
            root_event_id=trigger["root_event_id"],
            delivery_id=delivery.delivery_id,
            execution_id=delivery.execution_id,
            output_id=body["output_id"],
        )
        if kind == "message.create":
            self._link_attachments(event)
        delivery.output_event_ids.append(event["event_id"])
        delivery.output_kinds.append(kind)
        result = {"status": "persisted", "duplicate": False, "event": event, "cursor": f"thread.{delivery.thread_id}.{event['sequence']}"}
        self._remember(request, delivery, body, result)
        return self._reply("Public.PersistedResponse", result)

    def _stored_payload(self, delivery: Delivery, body: dict[str, Any]) -> dict[str, Any]:
        """The output payload as sent, plus the fields the contract says Maestro adds."""
        kind, payload = body["type"], deepcopy(body["payload"])
        thread = self.threads[delivery.thread_id]
        if kind == "message.create":
            handoff = payload.pop("handoff", None)
            if handoff is not None:
                payload["routing_decision"] = {
                    "version": 1,
                    "mode": "direct",
                    "recipients": list(handoff["recipients"]),
                    "execution": "sequential",
                }
            return {**payload, "execution_id": delivery.execution_id, "output_id": body["output_id"]}
        if kind == "run.update":
            return {"agent_id": INSTANCE_AGENT_ID, **payload, "trigger_event_id": delivery.trigger_event["event_id"]}
        if kind == "thread.update":
            if payload.get("queue") == "":
                payload["queue"] = None  # Maestro stores `queue || null`
            # Recorded only: the thread state keeps its title (plan 3, task 7).
            current = {"thread_id": thread["thread_id"], "state": thread["state"], "version": thread["version"]}
            return {"title": thread["title"], **payload, **current}
        if kind == "participant.add":
            # Recorded only: the person does not join the thread (plan 3, task 7).
            user = payload.pop("user")
            participant = {
                "participant_id": str(uuid4()),
                "display_name": user.get("user_name") or user["user_id"],
                "state": "active",
                "joined_sequence": str(int(thread["last_sequence"]) + 1),
                "left_sequence": None,
                "kind": "human",
                "principal_id": user["user_id"],
                "role": "member",
            }
            return {**payload, "participant": participant, "history_policy": "full"}
        return payload

    def _complete(self, request: httpx.Request, delivery: Delivery) -> httpx.Response:
        body = self._json(request, "Public.CompleteRequest")
        previous = self._idempotent(request, delivery, body)
        if previous is not None:
            return self._reply("Public.CallbackResponse", previous)
        self._running(delivery, body)
        # Maestro (M07-005/006): `responded` needs a visible reply in the phase; `silent` refuses one.
        visible = any(kind in VISIBLE_REPLY_OUTPUTS for kind in delivery.output_kinds)
        if (body["outcome"] == "responded" and not visible) or (body["outcome"] == "silent" and visible):
            raise _CallbackError(422, "VALIDATION_FAILED")
        delivery.state, delivery.reason_code = body["outcome"], body.get("reason_code")
        result = {
            "delivery_id": delivery.delivery_id,
            "execution_id": delivery.execution_id,
            "state": body["outcome"],
            "result_event_ids": list(delivery.output_event_ids),
        }
        self._remember(request, delivery, body, result)
        return self._reply("Public.CallbackResponse", result)

    def _artifact_upload(self, request: httpx.Request) -> httpx.Response:
        delivery = self._authorized_delivery(request)
        if not request.headers.get("idempotency-key"):
            raise _CallbackError(400, "INVALID_REQUEST")
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            raise _CallbackError(400, "INVALID_REQUEST")
        message = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.read())
        parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        if set(parts) != {"metadata", "file"}:
            raise _CallbackError(400, "INVALID_REQUEST")
        try:
            metadata = json.loads(parts["metadata"].get_payload(decode=True))
        except ValueError:
            raise _CallbackError(400, "INVALID_REQUEST") from None
        if not self.contracts.is_valid("Public.ArtifactCreateRequest", metadata):
            raise _CallbackError(400, "INVALID_REQUEST")
        data = parts["file"].get_payload(decode=True) or b""
        if (
            metadata.get("execution_id") != delivery.execution_id
            or metadata.get("thread_id") != delivery.thread_id
            or metadata.get("lease_fence") != delivery.envelope["lease_fence"]
            or metadata["workspace_id"] != WORKSPACE_ID
        ):
            raise _CallbackError(403, "FORBIDDEN")
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")
        if (
            metadata["mime_type"] not in MAESTRO_MIME_TYPES
            or parts["file"].get_content_type() != metadata["mime_type"]
            or metadata["size_bytes"] != len(data)
            or not 0 < len(data) <= MAX_ARTIFACT_BYTES
            or metadata["sha256"] != hashlib.sha256(data).hexdigest()
        ):
            raise _CallbackError(422, "VALIDATION_FAILED")
        artifact = self._store_artifact(metadata, data, uploaded_by=INSTANCE_AGENT_ID, execution_id=delivery.execution_id)
        public = {key: artifact[key] for key in ("artifact_id", "filename", "mime_type", "size_bytes", "sha256", "state", "created_at")}
        result = {
            "artifact": {**public, "expires_at": None},
            "upload_session_id": str(uuid4()),
            "upload_mode": "multipart_proxy",
            "max_size_bytes": MAX_ARTIFACT_BYTES,
            "expires_at": timestamp(time.time() + 3600),
        }
        return self._reply("Public.UploadSessionResponse", result, status=201)

    def _artifact_download(self, request: httpx.Request, artifact_id: str) -> httpx.Response:
        delivery = self._authorized_delivery(request)
        artifact = self.artifacts.get(artifact_id)
        if artifact is None or artifact["state"] != "linked" or artifact["thread_id"] != delivery.thread_id:
            raise _CallbackError(404, "NOT_FOUND")
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")
        return httpx.Response(200, content=artifact["data"], headers={"Content-Type": artifact["mime_type"]})

    def _artifact_link(self, request: httpx.Request, execution_id: str, artifact_id: str) -> httpx.Response:
        """A temporary link to one file of the thread, linked up to the context cutoff (``{url, expires_at}``)."""
        delivery = self._authorized_delivery(request)
        if delivery.execution_id != execution_id:
            raise _CallbackError(404, "NOT_FOUND")
        try:
            body = json.loads(request.content)
        except ValueError:
            raise _CallbackError(400, "INVALID_REQUEST") from None
        if not self.contracts.is_valid("Public.Empty", body):
            raise _CallbackError(400, "INVALID_REQUEST")
        artifact = self.artifacts.get(artifact_id)
        cutoff = _sequence_key(delivery.envelope["context_cutoff_sequence"])
        if (
            artifact is None
            or artifact["state"] != "linked"
            or artifact["thread_id"] != delivery.thread_id
            or _sequence_key(artifact["linked_sequence"]) > cutoff
        ):
            raise _CallbackError(404, "NOT_FOUND")
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")
        token = f"{artifact_id.replace('-', '')}.{uuid4().hex}"
        link = {"url": f"{CALLBACK_BASE_URL}/artifact-links/{token}", "expires_at": timestamp(time.time() + 3600)}
        return self._reply("Public.ArtifactLinkResponse", link)

    # -- state --

    def _participant(self, thread_id: str, *, kind: str, principal_id: str) -> dict[str, Any]:
        thread = self.threads.get(thread_id)
        matches = [
            participant
            for participant in (thread["participants"] if thread else [])
            if participant["kind"] == kind and participant["principal_id"] == principal_id and participant["state"] == "active"
        ]
        if len(matches) != 1:
            raise FakeMaestroRejected("thread.access", f"{kind} {principal_id} is not an active participant of the thread")
        return matches[0]

    def _append(
        self, thread_id: str, actor: dict[str, Any], payload: dict[str, Any], *, event_type: str = "message.created", **extra: Any
    ) -> dict[str, Any]:
        thread = self.threads[thread_id]
        sequence = str(int(thread["last_sequence"]) + 1)
        event_id = str(uuid4())
        event = {
            "event_id": event_id,
            "thread_id": thread_id,
            "sequence": sequence,
            "schema_version": 1,
            "actor": deepcopy(actor),
            "created_at": _now(),
            "causation_event_id": extra.pop("causation_event_id", None),
            "root_event_id": extra.pop("root_event_id", event_id),
            "type": event_type,
            "payload": payload,
            **extra,
        }
        event = self.contracts.validate(f"Public.Event.{event_type}", event)
        thread.update(last_sequence=sequence, cursor=f"thread.{thread_id}.{sequence}", updated_at=event["created_at"])
        self.events[thread_id].append(event)
        return deepcopy(event)

    def _store_artifact(self, metadata: dict[str, Any], data: bytes, *, uploaded_by: str, execution_id: str | None) -> dict[str, Any]:
        artifact = {
            "artifact_id": str(uuid4()),
            "filename": metadata["filename"],
            "mime_type": metadata["mime_type"],
            "size_bytes": metadata["size_bytes"],
            "sha256": metadata["sha256"],
            "state": "ready_unbound",
            "created_at": _now(),
            "data": bytes(data),
            "uploaded_by": uploaded_by,
            "execution_id": execution_id,
            "thread_id": None,
            "linked_sequence": None,
        }
        self.artifacts[artifact["artifact_id"]] = artifact
        return artifact

    def _link_attachments(self, event: dict[str, Any]) -> None:
        for part in event["payload"]["content"]:
            if part["type"] == "attachment":
                artifact = self.artifacts[part["artifact_id"]]
                if artifact["state"] != "linked":
                    artifact.update(state="linked", thread_id=event["thread_id"], linked_sequence=event["sequence"])


# -- Production views (test only) --


def _legacy_part(part: dict[str, Any]) -> dict[str, Any]:
    if part["type"] == "text":
        return {"type": "text", "text": part["text"]}
    if part["type"] == "contact":
        return {"type": "contact", **{key: deepcopy(part[key]) for key in ("display_name", "vcard", "phones") if key in part}}
    if part["type"] == "media_link":
        # A URL the SDK could not upload: the production part keeps the agent's URL (index 4.3).
        view = {"type": part["kind"], "url": part["url"]}
        return view | {key: part[key] for key in ("mime_type", "filename", "alt", "duration") if key in part}
    mime_type = part.get("mime_type")
    kind = "image" if (mime_type or "").startswith("image/") else "audio" if (mime_type or "").startswith("audio/") else "document"
    view = {"type": kind, "ref_id": part["artifact_id"], "filename": part.get("filename"), "mime_type": mime_type}
    if kind == "document":
        view["size_bytes"] = part.get("size_bytes")
    return view


def legacy_view(event: dict[str, Any]) -> dict[str, Any]:
    """A Maestro ``message.created`` event as the production ``ThreadEvent`` dump.

    Renames only: ``event_id`` is the id, the actor kind is the legacy actor, the actor
    display name is the author and, for a person, the ``User``. An attachment is the media
    part of its MIME family with the artifact id as ``ref_id`` (no URL: Maestro has none).
    Maestro has no run, e-mail, ``data`` or stored client event id, so those keep the
    production defaults.
    """
    if event.get("type") != "message.created":
        raise ValueError(f"no legacy ThreadEvent for the Maestro event {event.get('type')!r}")
    actor = event["actor"]
    user = User(user_id=actor["principal_id"], user_name=actor["display_name"]) if actor["kind"] == "human" else User(user_id="")
    return ThreadEvent(
        id=event["event_id"],
        thread_id=event["thread_id"],
        type="message",
        actor=_ACTORS[actor["kind"]],
        author=actor["display_name"],
        user=user,
        content=[_legacy_part(part) for part in event["payload"]["content"]],
        created_at=_parse(event["created_at"]),
    ).model_dump()


def legacy_thread_view(thread: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """A Maestro thread as the production ``Thread`` dump (no queue or metadata in Maestro v1).

    Production lists the agent in ``users`` too, so ``users`` is every active participant
    in participant order. The owner is the person with the ``owner`` role.
    """
    active = [p for p in thread["participants"] if p["state"] == "active"]
    (agent,) = [p for p in active if p["kind"] == "agent"]
    (owner,) = [p for p in active if p["kind"] == "human" and p["role"] == "owner"]
    return Thread(
        id=thread["thread_id"],
        agent_id=agent["instance_agent_id"],
        organization_id=thread["organization_id"],
        workspace_id=thread["workspace_id"],
        owner=User(user_id=owner["principal_id"], user_name=owner["display_name"]),
        users=[User(user_id=p["principal_id"], user_name=p["display_name"]) for p in active],
        title=thread["title"] or None,
        created_at=_parse(thread["created_at"]),
        updated_at=_parse(thread["updated_at"]),
        last_event_at=_parse(events[-1]["created_at"] if events else thread["created_at"]),
    ).model_dump()
