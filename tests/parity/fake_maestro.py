"""In-memory Maestro for the parity suite: the SDK's Maestro mode runs against it.

``FakeMaestro`` plays the sides the SDK does not own:

- the web chat (a person): ``create_thread``, ``publish``, ``upload`` and ``respond_form``.
  The person's actor and participant are the placeholder ``"Humano"`` in what browsers read
  (``events/publication.ts``, ``participantView``). The agent's context adds what Core's
  connection context says about the person (``core_people``; a person who creates a thread or
  publishes is one): name, e-mail, identity IDs and the three roles, and the ``workspace`` catalog
  (``queues``) and the ``thread`` view on every page, as Maestro's ``agent-profiles.ts`` and
  ``callbacks.ts`` do. ``connection_context=False`` is a Maestro that sends none of them;
- the agent's own publication of a person's message, ``agent_publish``: the Maestro side of
  production ``AgentServer.dispatch`` (``POST /agent/threads/:id/events`` with a declared
  person and ``trigger: true``, Maestro 99c2902). The declared name goes with the message;
  the declared e-mail and identity IDs go back only to this agent, in its delivery context.
  The agent content accepts a URL part (``media_link``), which a web chat person cannot send.
  The SDK side of this call is plan 5: the parity harness plays it, as it plays the web chat;
- the Maestro service: it signs one dispatch per delivery (``deliveries_for`` and
  ``deliver``) and answers the SDK callbacks (context, heartbeat, outputs, stream,
  complete, artifacts, temporary links), with the same semantics as ``CallbackFixture`` in
  ``tests/test_maestro_executor.py``.

Every request, from either side, is checked against the vendored contract with
``MaestroContracts`` (``is_valid``, the validator behind ``validate``) before it changes
any state. A request the contract does not accept changes nothing: a client request
raises ``FakeMaestroRejected``, and a callback gets the contract's ``Public.Error`` (also
kept in ``rejections``). Every response and every stored event goes through
``MaestroContracts.validate`` too, so a bug in the fake fails loudly. One exception: the
request schema of ``agent_publish`` (``Public.AgentEventCreateRequest``) is not vendored yet
(plan 5 syncs it), so each part is checked against ``Public.ContentPart`` and the stored
event against ``Public.Event.message.created``.

As Maestro does:

- a new thread starts with ``thread.created`` (sequence 1) and carries the SDK fields of
  index 4.3 (``queue``, ``channel``, ...);
- each accepted output becomes the event of its type (index 4.4: ``message.create`` ->
  ``message.created``, ``run.update`` -> ``run.updated``, ...), with the payload as sent plus
  the fields Maestro adds. ``thread.update`` changes the title and queue of the thread and
  ``participant.add`` adds the person (the stored participant has only the principal; the agent's
  context adds what the core knows of it; one already in the thread is refused ``already_participant``);
- ``respond_form`` answers a production form (``form.created``): it records
  ``form.responded``, its own causal root, and ``deliveries_for`` gives it one delivery to
  the form's author;
- files follow Maestro's file policy (``maestro_file_policy``: type matrix, canonical MIME and content inspection) up to 25 MiB, and an
  execution gets a temporary link (``ARTIFACT_LINKS_ENABLED``) for each file linked to its
  thread up to the context cutoff, without the fake serving the bytes.

Scope: one agent per thread, no retries or lease renewal, no suspension/interactions, no
archive or delete. A real Maestro also writes execution events, and none of them has a
legacy ``ThreadEvent``.

``agent_view`` gives the thread as the agent's delivery context has it (the whole thread,
no cutoff): the parity harness reads it back through the SDK's own conversion
(``MaestroExecutionPersistence``), never through a view of its own.
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dooers.agents.server.maestro import MaestroConfig, MaestroDeliveryAdapter, MaestroInstanceCredential
from dooers.agents.server.maestro.callbacks import STORED_EVENT_TYPES
from dooers.agents.server.maestro.context import MaestroPrivateResources, MaestroPrivateScope
from dooers.agents.server.protocol.maestro import MaestroContracts, default_contracts
from dooers.agents.server.protocol.models import User, WorkspaceQueueCatalog
from tests.parity.context_pages import ContextGap, CursorInvalid, serve_context
from tests.parity.maestro_file_policy import MAX_ARTIFACT_BYTES, canonical_mime_type, maestro_accepts_file
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
#: Parts of an agent message (``messageOf``, ``events.ts``): Maestro refuses more with ``PAYLOAD_TOO_LARGE``.
MAX_AGENT_MESSAGE_PARTS = 20
#: Name Maestro gives a web chat person in actors and participants (no Core profile in Maestro).
WEB_CHAT_PERSON_NAME = "Humano"
#: ``Public.Thread.channel`` of a public chat visitor's own thread (plan 6, ``public-chat.ts``'s
#: ``PUBLIC_CHAT_CHANNEL``). The visitor's own actor/participant ``channel`` field is a different thing
#: (the messaging channel, ``externalChannelSchema``: ``"public_chat"``), never this value.
PUBLIC_CHAT_THREAD_CHANNEL = "dooers-public-chat"
#: externalActorFields.channel of the public chat visitor's own actor/participant (``externalChannelSchema``).
PUBLIC_CHAT_MESSAGE_CHANNEL = "public_chat"
#: The thread SDK fields of index 4.3 that every ``Public.Thread`` projection of Maestro carries.
THREAD_SDK_FIELDS = {
    "queue": None,
    "channel": "dooers-platform",
    "channel_meta": None,
    "external_key": None,
    "owner_instance_agent_id": None,
}
_PERMISSIONS = ("read", "write", "manage", "supervise", "export", "cancel", "feedback", "archive", "remove_participants")
_NO_PERMISSIONS = dict.fromkeys(_PERMISSIONS, False)


def shared_contracts() -> MaestroContracts:
    """The vendored contract, compiled once per process (the SDK shares the same instance)."""
    return default_contracts()


@functools.cache
def signing_key() -> rsa.RSAPrivateKey:
    """The fake Maestro signing key (dispatch and capability JWTs, core credential)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


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
    """A client request (thread, message, upload, form response) the contract or a Maestro rule refuses."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"{action}: {reason}")
        self.action = action
        self.reason = reason


class _CallbackError(Exception):
    def __init__(self, status: int, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.reason = reason


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
    #: ``(cursor, limit, direction)`` of every context request, in order.
    context_requests: list[tuple[str | None, int | None, str]] = field(default_factory=list)
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
        core_people: Sequence[User] = (),
        queues: Sequence[WorkspaceQueueCatalog] = (),
        connection_context: bool = True,
    ) -> None:
        self.contracts = shared_contracts()
        self.assistant_name = assistant_name
        #: What Core's connection context says about each person (by user id); the one who creates a thread
        #: or publishes is added.
        self.core_people: dict[str, User] = {person.user_id: person for person in core_people}
        #: The workspace queue catalog Core gives the agent's context.
        self.queues = tuple(queues)
        #: False: a Maestro without the core connection context (no ``workspace``, ``thread`` or person fields).
        self.connection_context = connection_context
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
        #: event_id -> e-mail and identity IDs declared by the publishing agent (agent context only).
        self.declared_profiles: dict[str, dict[str, Any]] = {}
        #: external_key -> thread_id (``ensurePublicChatThread``'s own idempotency: the same key always
        #: gives the same thread).
        self._external_threads: dict[str, str] = {}
        #: thread_id -> the visitor's declared pre-chat (``guest_name``/``guest_email``/``guest_phone`` of the
        #: first message's metadata), agent-only (``visitorAgentFields.guest_profile``), never a browser.
        self._guest_profiles: dict[str, dict[str, Any]] = {}
        #: Test hook (plan 5, task 8): called with ``(thread_id, event)`` right after ``_append`` stores it, so the
        #: harness can mirror a delivery's own writes into ``FakeAgentApi`` as they happen (a handler output a later
        #: call of the SAME delivery already needs, e.g. ``record_channel_delivery`` after a WhatsApp send).
        self.on_append: Callable[[str, dict[str, Any]], None] | None = None

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
            # "threads:manage" lets this same credential also answer MaestroAgentApi calls (plan 5, task 8):
            # AgentServer ingress (dispatch/ingest/repository/chat_upload/whatsapp) outside a delivery.
            scopes=["processing:write", "responses:write", "deliveries:read", "context:read", "threads:manage"],
        )
        token = sign(key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "core-fixture"}, claims)
        return token, claims["exp"]

    # -- client side: the web chat (a person) and the agent's own publication --

    def _client_request(self, action: str, schema: str, value: dict[str, Any], detail: str = "") -> dict[str, Any]:
        if not self.contracts.is_valid(schema, value):
            raise FakeMaestroRejected(action, f"{schema} does not accept the request {detail}".strip())
        return deepcopy(value)

    def create_thread(self, human: User, *, title: str | None) -> str:
        """A person creates a workspace thread with the agent in the web chat; ``thread.created`` is sequence 1."""
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
        self.core_people.setdefault(human.user_id, human)
        thread_id, now = str(uuid4()), _now()
        person = {
            "participant_id": str(uuid4()),
            "display_name": WEB_CHAT_PERSON_NAME,
            "state": "active",
            "joined_sequence": "1",
            "left_sequence": None,
            "kind": "human",
            "principal_id": human.user_id,
            "role": "owner",
        }
        agent = {
            "participant_id": str(uuid4()),
            "display_name": self.assistant_name,
            "state": "active",
            "joined_sequence": "1",
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
            "permissions": dict.fromkeys(_NO_PERMISSIONS, True) | {"supervise": False},
            "capabilities": [],
            "last_sequence": "0",
            "cursor": f"thread.{thread_id}.0",
            "supervision_policy": "strict",
            "policy_version": 1,
            "created_at": now,
            "updated_at": now,
            **THREAD_SDK_FIELDS,
        }
        self.threads[thread_id] = self.contracts.validate("Public.Thread", thread)
        self.events[thread_id] = []
        # The shared event never carries the creator's permissions (threads/persistence.md).
        view = {**deepcopy(thread), "permissions": dict(_NO_PERMISSIONS), "last_sequence": "1", "cursor": f"thread.{thread_id}.1"}
        self._append(thread_id, self._person_actor(person), {"thread": view}, event_type="thread.created")
        return thread_id

    def admit_agent_thread(
        self,
        thread_id: str,
        *,
        title: str,
        queue: str | None,
        channel: str,
        channel_meta: dict[str, Any] | None,
        external_key: str | None,
        owner_instance_agent_id: str | None,
        participants: list[dict[str, Any]],
        created_at: str,
        updated_at: str,
        version: int,
    ) -> None:
        """A thread the agent API created (plan 5, task 8), admitted here so a real delivery can run for it.

        The harness builds ``participants`` from ``FakeAgentApi``'s own thread (same field shapes: an agent-api
        thread's participants already carry ``participant_id``/``kind``/``principal_id`` or ``external_id``/
        ``display_name``/``role``/``state``, the same union ``Public.Thread`` accepts). Does nothing if this thread
        is already admitted (the harness calls this once per delivery, before ``admit_agent_delivery``).
        """
        if thread_id in self.threads:
            return
        thread = {
            "organization_id": ORGANIZATION_ID,
            "scope_kind": "workspace" if WORKSPACE_ID else "personal",
            "workspace_id": WORKSPACE_ID,
            "thread_id": thread_id,
            "mode": "maestro",
            "title": title or "",
            "state": "active",
            "version": version,
            "participants": deepcopy(participants),
            "permissions": dict.fromkeys(_NO_PERMISSIONS, True) | {"supervise": False},
            "capabilities": [],
            "last_sequence": "0",
            "cursor": f"thread.{thread_id}.0",
            "supervision_policy": "strict",
            "policy_version": 1,
            "created_at": created_at,
            "updated_at": updated_at,
            "queue": queue,
            "channel": channel,
            "channel_meta": deepcopy(channel_meta),
            "external_key": external_key,
            "owner_instance_agent_id": owner_instance_agent_id,
        }
        self.threads[thread_id] = self.contracts.validate("Public.Thread", thread)
        self.events[thread_id] = []

    def ensure_external_thread(
        self,
        visitor: User,
        *,
        external_key: str,
        connection_type: str,
        channel_meta: dict[str, Any] | None = None,
        guest_profile: dict[str, Any] | None = None,
    ) -> str:
        """``ensurePublicChatThread`` (plan 6, ``public-chat.ts``): the visitor talks to Maestro directly,
        never through the Maestro-mode Router. The same ``external_key`` always gives the same thread
        (idempotent, as in production: two tabs of the same visitor share it). The visitor and the link's
        agent are the thread's only two participants, both ``role: "member"`` (an external participant's
        role is always ``member``, per the contract; the agent never owns a visitor's thread either).

        ``guest_profile`` (the visitor's own declared pre-chat, never verified by the core) is kept apart
        from the stored participant (agent-only, ``visitorAgentFields.guest_profile``): the plain
        ``Public.Thread`` participant a browser could read carries none of it.
        """
        thread_id = self._external_threads.get(external_key)
        if thread_id is not None:
            return thread_id
        thread_id, now = str(uuid4()), _now()
        visitor_participant = {
            "participant_id": str(uuid4()),
            "state": "active",
            "joined_sequence": "1",
            "left_sequence": None,
            "kind": "external",
            "external_id": visitor.user_id,
            "channel": PUBLIC_CHAT_MESSAGE_CHANNEL,
            "connection_type": connection_type,
            "role": "member",
        }
        agent = {
            "participant_id": str(uuid4()),
            "display_name": self.assistant_name,
            "state": "active",
            "joined_sequence": "1",
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
            "scope_kind": "workspace" if WORKSPACE_ID else "personal",
            "workspace_id": WORKSPACE_ID,
            "thread_id": thread_id,
            "mode": "maestro",
            "title": "",
            "state": "active",
            "version": 1,
            # ``rows`` come ``orderBy(kind, principal_id)`` (``public-chat.ts``): the agent, then the visitor.
            "participants": [agent, visitor_participant],
            "permissions": dict.fromkeys(_NO_PERMISSIONS, True) | {"supervise": False},
            "capabilities": [],
            "last_sequence": "0",
            "cursor": f"thread.{thread_id}.0",
            "supervision_policy": "strict",
            "policy_version": 1,
            "created_at": now,
            "updated_at": now,
            "queue": None,
            "channel": PUBLIC_CHAT_THREAD_CHANNEL,
            "channel_meta": deepcopy(channel_meta) if channel_meta else None,
            "external_key": external_key,
            "owner_instance_agent_id": INSTANCE_AGENT_ID,
        }
        self.threads[thread_id] = self.contracts.validate("Public.Thread", thread)
        self.events[thread_id] = []
        view = {**deepcopy(thread), "permissions": dict(_NO_PERMISSIONS), "last_sequence": "1", "cursor": f"thread.{thread_id}.1"}
        self._append(thread_id, self._external_actor(visitor_participant), {"thread": view}, event_type="thread.created")
        self._external_threads[external_key] = thread_id
        if guest_profile:
            self._guest_profiles[thread_id] = dict(guest_profile)
        return thread_id

    def publish_external(self, thread_id: str, visitor: User, content: list[dict[str, Any]], *, client_event_id: str) -> dict[str, Any]:
        """The visitor's own message (``message.publish``), mirroring ``publish`` but with an external actor."""
        request = self._client_request(
            "message.create",
            "Public.MessageCreateRequest",
            {"type": "message.create", "client_event_id": client_event_id, "content": deepcopy(content), "mentions": []},
            f"(part types: {[part.get('type') for part in content]})",
        )
        participant = self._external_participant(thread_id, external_id=visitor.user_id)
        self._check_attachments(request["content"], visitor.user_id)
        payload = {"client_event_id": request["client_event_id"], "content": request["content"], "mentions": request["mentions"]}
        event = self._append(thread_id, self._external_actor(participant), payload)
        self._link_attachments(event)
        return event

    def admit_agent_event(self, thread_id: str, event_type: str, actor: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """One event of an already-admitted agent-api thread, mirrored here verbatim (same actor/payload)."""
        return self._append(thread_id, actor, payload, event_type=event_type)

    def admit_agent_artifact(self, artifact_id: str, data: bytes, *, filename: str, mime_type: str, thread_id: str) -> None:
        """A file the agent API already holds for ``artifact_id`` (plan 5, task 8: a staged ``chat_upload``).

        Admitted ``linked`` (not ``ready_unbound``): the agent-api publish that referenced it already
        happened; this only lets the delivery's own ``artifact.link.issue``/download find it, same as
        ``_link_attachments`` does for a web chat upload. Idempotent: an artifact already admitted here
        (by a previous delivery of the same thread) is left as it is.
        """
        if artifact_id in self.artifacts:
            return
        self.artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "filename": filename,
            "mime_type": canonical_mime_type(mime_type, filename),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "state": "linked",
            "created_at": _now(),
            "data": bytes(data),
            "uploaded_by": INSTANCE_AGENT_ID,
            "execution_id": None,
            "thread_id": thread_id,
            "linked_sequence": "0",
        }

    def publish(self, thread_id: str, human: User, content: list[dict[str, Any]], *, client_event_id: str) -> dict[str, Any]:
        """A person's message in the web chat (``Public.MessageCreateRequest``: text, attachment, contact)."""
        request = self._client_request(
            "message.create",
            "Public.MessageCreateRequest",
            {"type": "message.create", "client_event_id": client_event_id, "content": deepcopy(content), "mentions": []},
            f"(part types: {[part.get('type') for part in content]})",
        )
        person = self._participant(thread_id, kind="human", principal_id=human.user_id)
        self.core_people.setdefault(human.user_id, human)
        self._check_attachments(request["content"], human.user_id)
        payload = {"client_event_id": request["client_event_id"], "content": request["content"], "mentions": request["mentions"]}
        event = self._append(thread_id, self._person_actor(person), payload)
        self._link_attachments(event)
        return event

    def agent_publish(
        self, thread_id: str, human: User, content: list[dict[str, Any]], *, client_event_id: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The agent publishes a person's message for itself (production ``AgentServer.dispatch``).

        ``POST /agent/threads/:id/events`` with ``actor: {kind: "human", principal_id,
        display_name, email, identity_ids}``, the agent content and ``trigger: true``. Only in a
        thread with one agent, and the person must be in the thread. ``client_event_id`` is the
        idempotency key. The e-mail and identity IDs with a value are kept apart from the
        actor and go back only to this agent (``agent_view``).
        """
        action = "agent.event.create"
        if len(content) > MAX_AGENT_MESSAGE_PARTS:
            raise FakeMaestroRejected(action, f"PAYLOAD_TOO_LARGE: {len(content)} parts, Maestro accepts {MAX_AGENT_MESSAGE_PARTS}")
        if not content or not all(self.contracts.is_valid("Public.ContentPart", part) for part in content):
            raise FakeMaestroRejected(action, f"Public.ContentPart does not accept the parts {[part.get('type') for part in content]}")
        thread = self.threads.get(thread_id)
        if thread is None or sum(p["kind"] == "agent" and p["state"] != "removed" for p in thread["participants"]) != 1:
            raise FakeMaestroRejected(action, "multi_agent_thread: only a thread with one agent")
        person = self._participant(thread_id, kind="human", principal_id=human.user_id)
        self.core_people.setdefault(human.user_id, human)
        self._check_attachments(content, INSTANCE_AGENT_ID)
        actor = {**self._person_actor(person), **({"display_name": human.user_name} if human.user_name else {})}
        payload: dict[str, Any] = {"client_event_id": client_event_id, "content": deepcopy(content), "mentions": []}
        if data:
            payload["data"] = deepcopy(data)  # the caller's data of the publication (``dispatch`` marks its handler here)
        event = self._append(thread_id, actor, payload)
        profile = {
            **({"email": human.user_email} if human.user_email else {}),
            **({"identity_ids": list(human.identity_ids)} if human.identity_ids else {}),
        }
        if profile:
            self.declared_profiles[event["event_id"]] = profile
        self._link_attachments(event)
        return event

    def respond_form(self, thread_id: str, human: User, form_event_id: str, *, values: dict[str, Any], cancelled: bool) -> dict[str, Any]:
        """``POST /threads/:id/forms/:formEventId/responses``: the answer to a production form.

        As ``respondLegacyFormCommand``: the form must be a ``form.created`` of an agent that is
        still an active participant (else ``agent_not_participant``); ``form.responded`` opens its
        own causal root, also when the form is cancelled.
        """
        self._client_request("form.response", "Public.FormEventPath", {"id": thread_id, "formEventId": form_event_id})
        request = self._client_request("form.response", "Public.FormResponseRequest", {"values": values, "cancelled": cancelled})
        person = self._participant(thread_id, kind="human", principal_id=human.user_id)
        form = self._form_author(thread_id, form_event_id)[0]
        payload = {"form_event_id": form["event_id"], "values": request["values"], "cancelled": request["cancelled"]}
        return self._append(thread_id, self._person_actor(person), payload, event_type="form.responded")

    def respond_form_external(
        self, thread_id: str, visitor: User, form_event_id: str, *, values: dict[str, Any], cancelled: bool
    ) -> dict[str, Any]:
        """``respond_form`` for a public chat visitor: the same route, its actor is the visitor's (``respondLegacyFormCommand``
        accepts a ``human`` or an ``external`` principal, as ``legacy-forms.ts`` does).
        """
        self._client_request("form.response", "Public.FormEventPath", {"id": thread_id, "formEventId": form_event_id})
        request = self._client_request("form.response", "Public.FormResponseRequest", {"values": values, "cancelled": cancelled})
        participant = self._external_participant(thread_id, external_id=visitor.user_id)
        form = self._form_author(thread_id, form_event_id)[0]
        payload = {"form_event_id": form["event_id"], "values": request["values"], "cancelled": request["cancelled"]}
        return self._append(thread_id, self._external_actor(participant), payload, event_type="form.responded")

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
        if not maestro_accepts_file(mime_type, filename, data):
            raise FakeMaestroRejected("artifact.upload", f"Maestro does not accept {mime_type} ({len(data)} bytes)")
        artifact = self._store_artifact(metadata, data, uploaded_by=human.user_id, execution_id=None)
        return {
            "type": "attachment",
            "artifact_id": artifact["artifact_id"],
            "filename": filename,
            "mime_type": artifact["mime_type"],
            "size_bytes": len(data),
        }

    # -- delivery side --

    def deliveries_for(self, event: dict[str, Any]) -> list[Delivery]:
        """Routing with one agent: a person's or a public chat visitor's message invokes it; a form
        response goes to the form's author.
        """
        if event["actor"]["kind"] not in ("human", "external"):
            return []
        thread_id = event["thread_id"]
        if event["type"] == "form.responded":
            agent = self._form_author(thread_id, event["payload"]["form_event_id"])[1]
        elif event["type"] == "message.created":
            agent = self._participant(thread_id, kind="agent", principal_id=INSTANCE_AGENT_ID)
        else:
            return []
        delivery_id, execution_id = str(uuid4()), str(uuid4())
        bound = {
            "organization_id": ORGANIZATION_ID,
            "workspace_id": WORKSPACE_ID,
            "thread_id": thread_id,
            "delivery_id": delivery_id,
            "execution_id": execution_id,
            "phase_id": "initial",
            "recipient_participant_id": agent["participant_id"],
            "instance_agent_id": agent["instance_agent_id"],
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
            data["capability_claims"].update(sub=agent["instance_agent_id"], jti=str(uuid4()))
            # ARTIFACT_LINKS_ENABLED: the capability carries artifact.link.issue, as Maestro signs it.
            data["capability_claims"]["operations"] = [*data["capability_claims"]["operations"], "artifact.link.issue"]
            data["dispatch_claims"]["jti"] = str(uuid4())

        envelope, headers = signed_request(self._key, self.config, change=bind)
        delivery = Delivery(delivery_id, execution_id, thread_id, deepcopy(event), envelope, headers)
        self.deliveries[delivery_id] = delivery
        return [delivery]

    def admit_agent_delivery(self, thread_id: str, delivery_id: str) -> Delivery:
        """A delivery for the trigger the agent API published on this thread (plan 5, task 8: ``AgentServer`` ingress).

        Unlike ``deliveries_for`` (the web chat, where the actor is always a Core human), the trigger of an agent-api
        publication may be an ``external`` contact (WhatsApp) too. The trigger is the thread's last event (the harness
        mirrors it here right before calling this), and the recipient is the thread's one active agent participant.
        """
        event = self.events[thread_id][-1]
        agent = self._participant(thread_id, kind="agent", principal_id=INSTANCE_AGENT_ID)
        execution_id = str(uuid4())
        bound = {
            "organization_id": ORGANIZATION_ID,
            "workspace_id": WORKSPACE_ID,
            "thread_id": thread_id,
            "delivery_id": delivery_id,
            "execution_id": execution_id,
            "phase_id": "initial",
            "recipient_participant_id": agent["participant_id"],
            "instance_agent_id": agent["instance_agent_id"],
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
            data["capability_claims"].update(sub=agent["instance_agent_id"], jti=str(uuid4()))
            data["capability_claims"]["operations"] = [*data["capability_claims"]["operations"], "artifact.link.issue"]
            data["dispatch_claims"]["jti"] = str(uuid4())

        envelope, headers = signed_request(self._key, self.config, change=bind)
        delivery = Delivery(delivery_id, execution_id, thread_id, deepcopy(event), envelope, headers)
        self.deliveries[delivery_id] = delivery
        return delivery

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
            yield self

    def all_events(self) -> list[dict[str, Any]]:
        """Every thread (creation order) with its events (sequence order), as stored."""
        return [deepcopy(event) for thread_id in self.threads for event in self.events[thread_id]]

    def agent_view(self, thread_id: str) -> dict[str, Any]:
        """The whole thread as the agent's delivery context has it: events and participants.

        Same shape as ``MaestroMemory.snapshot()`` (the trigger is the last event).
        """
        events = [self._agent_event(event) for event in self.events[thread_id]]
        view = {"trigger_event": deepcopy(events[-1]), "participants": self._context_participants(thread_id), "events": events}
        if self.connection_context:
            view.update(workspace=self._workspace(), thread=self._thread_view(thread_id, len(events)))
        return view

    def _context_participants(self, thread_id: str) -> list[dict[str, Any]]:
        """The participants of the delivery context, in Maestro's order: by participant id (a random UUID).

        ``callbacks.ts`` lists them ``ORDER BY participants.id``: the order says nothing about who
        joined first. The SDK orders them (``legacy_model.to_thread``).
        """
        participants = sorted(deepcopy(self.threads[thread_id]["participants"]), key=lambda participant: participant["participant_id"])
        return [self._with_profile(participant, thread_id) for participant in participants]

    def artifact_link(self, thread_id: str, artifact_id: str, *, cutoff: str | None = None) -> dict[str, Any]:
        """A temporary link (``{url, expires_at}``) to a file linked to the thread (up to ``cutoff``).

        ``LookupError`` when the file is not linked to the thread up to the cutoff.
        """
        artifact = self.artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact["state"] != "linked"
            or artifact["thread_id"] != thread_id
            or (cutoff is not None and _sequence_key(artifact["linked_sequence"]) > _sequence_key(cutoff))
        ):
            raise LookupError(artifact_id)
        token = f"{artifact_id.replace('-', '')}.{uuid4().hex}"
        link = {"url": f"{CALLBACK_BASE_URL}/artifact-links/{token}", "expires_at": timestamp(time.time() + 3600)}
        return self.contracts.validate("Public.ArtifactLinkResponse", link)

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
                    **({"details": {"reason_code": error.reason}} if error.reason else {}),
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
        """``GET /deliveries/:id/context``: forward or backward pages (``context_pages.serve_context``)."""
        delivery = self._authorized_delivery(request)
        if delivery.delivery_id != delivery_id:
            raise _CallbackError(403, "FORBIDDEN")
        params = dict(request.url.params)
        try:
            query = {key: (int(value) if key == "limit" else value) for key, value in params.items()}
        except ValueError:
            raise _CallbackError(400, "INVALID_REQUEST") from None
        if not self.contracts.is_valid("Public.ContextQuery", query):
            raise _CallbackError(400, "INVALID_REQUEST")
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")
        delivery.context_pages += 1
        delivery.context_requests.append((query.get("cursor"), query.get("limit"), query.get("direction", "forward")))

        cutoff = int(delivery.envelope["context_cutoff_sequence"])

        def page(items: list[dict[str, Any]], next_cursor: str | None, has_more: bool) -> dict[str, Any]:
            return {
                "delivery_id": delivery.delivery_id,
                "trigger_event": self._agent_event(delivery.trigger_event),
                "participants": self._context_participants(delivery.thread_id),
                "context_cursor": delivery.context_cursor,
                "history": {
                    "items": [self._agent_event(event) for event in items],
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "watermark": delivery.envelope["context_cutoff_sequence"],
                },
                **(
                    {"workspace": self._workspace(), "thread": self._thread_view(delivery.thread_id, cutoff)}
                    if self.connection_context
                    else {}
                ),
            }

        try:
            value = serve_context(
                self.events[delivery.thread_id],
                cutoff=int(delivery.envelope["context_cutoff_sequence"]),
                base=delivery.context_cursor,
                cursor=query.get("cursor"),
                limit=query.get("limit"),
                direction=query.get("direction", "forward"),
                page=page,
            )
        except CursorInvalid:
            raise _CallbackError(400, "CURSOR_INVALID") from None
        except ContextGap:
            raise _CallbackError(503, "DEPENDENCY_UNAVAILABLE") from None
        return self._reply("Public.ContextResponse", value)

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
        """The output payload as sent, plus the fields Maestro adds; thread changes are applied.

        Called before the event is appended: a refusal (``_CallbackError``) changes nothing.
        """
        kind, payload = body["type"], deepcopy(body["payload"])
        thread = self.threads[delivery.thread_id]
        run = {"run_id": payload.pop("run_id")} if "run_id" in payload else {}
        if kind == "message.create":
            handoff = payload.pop("handoff", None)
            if handoff is not None:
                payload["routing_decision"] = {
                    "version": 1,
                    "mode": "direct",
                    "recipients": list(handoff["recipients"]),
                    "execution": "sequential",
                }
            return {**payload, **run, "execution_id": delivery.execution_id, "output_id": body["output_id"]}
        if kind == "run.update":
            return {"agent_id": INSTANCE_AGENT_ID, **payload, **run, "trigger_event_id": delivery.trigger_event["event_id"]}
        if kind == "thread.update":
            # updateThreadAsAgent: the title "" is valid; the queue "" is null; the version always grows.
            changes = {key: payload[key] for key in ("title", "queue") if key in payload}
            if changes.get("queue") == "":
                changes["queue"] = None
            thread.update(changes, version=thread["version"] + 1)
            current = {key: thread[key] for key in ("thread_id", "title", "version", "queue")}
            return {**current, "state": "active", **run}
        if kind == "participant.add":
            # addHumanAsAgent: only the principal id is kept (no name, e-mail or identity IDs).
            principal_id = payload["user"]["user_id"]
            if any(p["kind"] == "human" and p["principal_id"] == principal_id and p["state"] != "removed" for p in thread["participants"]):
                raise _CallbackError(422, "VALIDATION_FAILED", "already_participant")
            participant = {
                "participant_id": str(uuid4()),
                "display_name": WEB_CHAT_PERSON_NAME,
                "state": "active",
                "joined_sequence": str(int(thread["last_sequence"]) + 1),
                "left_sequence": None,
                "kind": "human",
                "principal_id": principal_id,
                "role": "member",
            }
            thread["participants"].append(participant)
            thread["version"] += 1
            return {"participant": deepcopy(participant), "history_policy": "full", **run}
        return {**payload, **run}

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
            not maestro_accepts_file(metadata["mime_type"], metadata["filename"], data)
            or parts["file"].get_content_type() != metadata["mime_type"]
            or metadata["size_bytes"] != len(data)
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
        try:
            link = self.artifact_link(delivery.thread_id, artifact_id, cutoff=delivery.envelope["context_cutoff_sequence"])
        except LookupError:
            raise _CallbackError(404, "NOT_FOUND") from None
        if delivery.state != "running":
            raise _CallbackError(409, "EXECUTION_TERMINAL")
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

    @staticmethod
    def _person_actor(person: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "human",
            "principal_id": person["principal_id"],
            "participant_id": person["participant_id"],
            "display_name": person["display_name"],
        }

    def _external_participant(self, thread_id: str, *, external_id: str) -> dict[str, Any]:
        thread = self.threads.get(thread_id)
        matches = [
            participant
            for participant in (thread["participants"] if thread else [])
            if participant["kind"] == "external" and participant["external_id"] == external_id and participant["state"] == "active"
        ]
        if len(matches) != 1:
            raise FakeMaestroRejected("thread.access", f"external {external_id} is not an active participant of the thread")
        return matches[0]

    @staticmethod
    def _external_actor(participant: dict[str, Any]) -> dict[str, Any]:
        """``visitorActor`` (``public-chat.ts``): no ``display_name`` (unlike the web chat's own placeholder)."""
        return {
            "kind": "external",
            "external_id": participant["external_id"],
            "channel": participant["channel"],
            "connection_type": participant["connection_type"],
            "participant_id": participant["participant_id"],
        }

    def _form_author(self, thread_id: str, form_event_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The production form (``form.created`` of an agent) and its author, an active agent participant."""
        form = next((event for event in self.events.get(thread_id, []) if event["event_id"] == form_event_id), None)
        if form is None or form["type"] != "form.created" or form["actor"]["kind"] != "agent":
            raise FakeMaestroRejected("form.response", "NOT_FOUND: no production form with this id in the thread")
        participants = self.threads[thread_id]["participants"]
        author = next((p for p in participants if p["participant_id"] == form["actor"]["participant_id"]), None)
        if author is None or author["kind"] != "agent" or author["state"] != "active":
            raise FakeMaestroRejected("form.response", "FORBIDDEN agent_not_participant")
        return form, author

    def _agent_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """The event as the agent reads it (``enrichEvent``, ``agent-profiles.ts``).

        A declared person carries the declared e-mail and identity IDs. Then each person the core knows
        (an active human of the thread) gets the core profile, unless the actor already has its own:
        the human actor, and the people in ``thread.created`` and ``participant.added``.
        """
        view = deepcopy(event)
        view["actor"].update(self.declared_profiles.get(event["event_id"], {}))
        thread_id = event["thread_id"]
        view["actor"] = self._with_profile(view["actor"], thread_id)
        if view["type"] == "thread.created":
            thread = view["payload"]["thread"]
            thread["participants"] = [self._with_profile(person, thread_id) for person in thread["participants"]]
        elif view["type"] == "participant.added":
            view["payload"]["participant"] = self._with_profile(view["payload"]["participant"], thread_id)
        return view

    def _with_profile(self, view: dict[str, Any], thread_id: str) -> dict[str, Any]:
        """``withProfile``: agent-only fields of an actor or participant (never a browser).

        A human gets them from the core's connection context (``_with_human_profile``), actor or
        participant alike (the contract allows ``email``/``identity_ids``/the roles on both). A public
        chat visitor's PARTICIPANT gets its own Core profile or the ``guest`` role, plus its own
        declared pre-chat (``_with_external_profile``); its ACTOR never does (``Public.AgentEvent``'s
        ``external`` actor branch has no such fields — only ``channel``/``connection_type``/
        ``display_name``/``external_id``/``participant_id``, unlike the human branch): ``"role"`` is
        on a participant only, so that is how the two are told apart here.
        """
        if not self.connection_context:
            return view
        if view.get("kind") == "human" and "principal_id" in view:
            return self._with_human_profile(view, thread_id)
        if view.get("kind") == "external" and "external_id" in view and "role" in view:
            return self._with_external_profile(view, thread_id)
        return view

    def _with_human_profile(self, view: dict[str, Any], thread_id: str) -> dict[str, Any]:
        """An active human the core knows gets name (when it has one), e-mail, identity IDs and roles.

        The e-mail is ``null`` when the core has none; a name the core lacks leaves the placeholder.
        An actor or participant that already has an e-mail or identity IDs (the agent declared them) keeps them.
        """
        person = self.core_people.get(view["principal_id"])
        active = any(
            p["kind"] == "human" and p["principal_id"] == view["principal_id"] and p["state"] == "active"
            for p in self.threads[thread_id]["participants"]
        )
        if person is None or not active or "email" in view or "identity_ids" in view:
            return view
        return {
            **view,
            **({"display_name": person.user_name} if person.user_name else {}),
            "email": person.user_email or None,
            "identity_ids": list(person.identity_ids),
            # Core's handshake always gives "user" (task 4b): never the person's platform role.
            "system_role": "user",
            "organization_role": person.organization_role,
            "workspace_role": person.workspace_role,
        }

    def _with_external_profile(self, view: dict[str, Any], thread_id: str) -> dict[str, Any]:
        """A public chat visitor's own Core profile, or the ``guest`` role, plus its declared pre-chat.

        Authenticated (``connection_type != "guest"``): name, e-mail, identity IDs and the roles of the
        signed-in user, from ``core_people`` (keyed by ``external_id``, the production user id) — the
        same merge a human gets, system_role always ``"user"`` (``_with_human_profile``). Guest: no name,
        no e-mail (``null``), no identity IDs (``[]``), and ``"guest"`` in the three roles (``visitorContextSchema``'s own
        docstring); ``guest_profile`` (the visitor's own declared name/e-mail/phone, never verified by the
        core) is added either way, agent-only (``visitorAgentFields.guest_profile``, index 4.3.1).
        """
        active = any(
            p["kind"] == "external" and p.get("external_id") == view["external_id"] and p["state"] == "active"
            for p in self.threads[thread_id]["participants"]
        )
        if not active or "email" in view or "identity_ids" in view:
            return view
        extra: dict[str, Any] = {}
        if view.get("connection_type") == "guest":
            # ``agentParticipantView``: the person the core described has no e-mail ("" is null) and no identity IDs.
            extra.update(email=None, identity_ids=[], system_role="guest", organization_role="guest", workspace_role="guest")
        else:
            person = self.core_people.get(view["external_id"])
            extra.update(
                **({"display_name": person.user_name} if person is not None and person.user_name else {}),
                email=(person.user_email or None) if person is not None else None,
                identity_ids=list(person.identity_ids) if person is not None else [],
                system_role="user",
                organization_role=person.organization_role if person is not None else "member",
                workspace_role=person.workspace_role if person is not None else "member",
            )
        guest_profile = self._guest_profiles.get(thread_id)
        if guest_profile:
            extra["guest_profile"] = deepcopy(guest_profile)
        return {**view, **extra}

    def _workspace(self) -> dict[str, Any]:
        """``workspace`` of the context: the core's queue catalog and its slugs."""
        return {
            "allowed_queues": [queue.slug for queue in self.queues if queue.slug],
            "queues": [queue.model_dump() for queue in self.queues],
        }

    def _thread_view(self, thread_id: str, cutoff: int) -> dict[str, Any]:
        """``thread`` of the context (``Public.ContextThread``): the thread as it was at the ``cutoff``.

        Title and queue are the last ``thread.created`` or ``thread.updated`` up to the cutoff; the other
        fields are never written after the creation.
        """
        thread = self.threads[thread_id]
        state = {"title": thread["title"], "queue": thread["queue"]}
        for event in self.events[thread_id]:
            if int(event["sequence"]) > cutoff:
                break
            if event["type"] == "thread.created":
                state = {key: event["payload"]["thread"][key] for key in ("title", "queue")}
            elif event["type"] == "thread.updated":
                state = {key: event["payload"][key] for key in ("title", "queue")}
        return {
            "thread_id": thread_id,
            **state,
            "channel": thread["channel"],
            "channel_meta": deepcopy(thread["channel_meta"]),
            "created_at": thread["created_at"],
            "owner_instance_agent_id": thread["owner_instance_agent_id"],
        }

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
        self.contracts.validate("Public.Thread", thread)
        self.events[thread_id].append(event)
        if self.on_append is not None:
            self.on_append(thread_id, deepcopy(event))
        return deepcopy(event)

    def _check_attachments(self, content: list[dict[str, Any]], uploader: str) -> None:
        for part in content:
            if part["type"] == "attachment":
                artifact = self.artifacts.get(part["artifact_id"])
                if artifact is None or artifact["state"] != "ready_unbound" or artifact["uploaded_by"] != uploader:
                    raise FakeMaestroRejected("message.create", "attachment is not an unbound upload of this sender")

    def _store_artifact(self, metadata: dict[str, Any], data: bytes, *, uploaded_by: str, execution_id: str | None) -> dict[str, Any]:
        artifact = {
            "artifact_id": str(uuid4()),
            "filename": metadata["filename"],
            # Maestro stores the canonical MIME (``canonicalMimeType``), never the client's raw value.
            "mime_type": canonical_mime_type(metadata["mime_type"], metadata["filename"]),
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
