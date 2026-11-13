"""A loopback Maestro agent API (``/api/v1/agent/*``) for the SDK's ``MaestroAgentApi`` tests.

A real HTTP server (aiohttp, ``127.0.0.1``, ephemeral port) that plays the routes of
``dooers-service-maestro/src/modules/agent-api/routes.ts``: same paths, methods, headers, query
filters, status codes and ``Public.Error`` bodies. It keeps one organization/scope and one agent
in memory, and checks like Maestro does:

- every request (query, JSON body, multipart metadata, ``Idempotency-Key``) against the vendored
  contract, before it changes anything; a request the contract refuses gets ``INVALID_REQUEST``;
- every response against the vendored contract (``MaestroContracts.validate``), so a bug of the fake
  fails loudly (a ``500`` that ``failures`` records);
- the bearer: a Core agent token with the ``threads:manage`` scope (``agent-api/auth.ts``).

State is only what the SDK tests need: threads (found by ``external_key`` or id), events with
sequence and ``client_event_id``, participants, runs, uploaded files and channel deliveries.
``idempotent`` commands keep a receipt per ``Idempotency-Key``: the same key with the same request
replays the stored answer (200); another request with the key is ``IDEMPOTENCY_CONFLICT``.

Like Maestro, ``GET /agent/events/:id`` finds any event type by ``event_id`` (or an agent publication by its
``client_event_id``) and ``DELETE .../events/:eventId`` removes only content events (``removableTypes`` of
``events.ts``); a query key a route does not know is ``INVALID_REQUEST``; an error with a wait carries
``Retry-After``; the thread list defaults to ``order=desc`` (last activity, then id).

Knobs for tests: ``core_people`` (what the core knows about a human: name, e-mail, identity IDs, roles),
``inject_event`` (an event another writer made: a handler output, ``run.updated``, ``message.updated``),
``delete_thread_state`` (thread in deletion: reads answer ``THREAD_DELETING``),
``corrupt_next`` (the next answer of a path is not contract-valid), ``fail_next`` (the next request of
a path gets a given error code) and ``requests`` (everything the
server saw: method, path, query, headers and the JSON body, or the multipart parts).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dooers.agents.server.maestro import MaestroConfig, MaestroInstanceCredential
from dooers.agents.server.protocol.maestro import default_contracts, verify_contract_bundle

PREFIX = "/api/v1"
ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
AGENT_ID = "00000000-0000-4000-8000-000000000010"
RUNTIME_ID = "00000000-0000-4000-8000-000000000011"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
#: The fake Maestro files policy: just enough for the tests (the real one is ``maestro_file_policy``).
KNOWN_MIME_TYPES = {"text/plain", "image/png", "application/pdf"}
_FIXTURES = {case["id"]: case["data"] for case in json.loads(verify_contract_bundle()["fixtures.json"])["cases"] if case["valid"]}
_ERRORS = {
    "INVALID_REQUEST": (400, False),
    "UNAUTHENTICATED": (401, False),
    "FORBIDDEN": (403, False),
    "NOT_FOUND": (404, False),
    "PAYLOAD_TOO_LARGE": (413, False),
    "VALIDATION_FAILED": (422, False),
    "RATE_LIMITED": (429, True),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "THREAD_DELETING": (409, False),
    "UNSUPPORTED_CAPABILITY": (422, False),
    "DEPENDENCY_UNAVAILABLE": (503, True),
}
_CORE_PERSON_FIELDS = ("display_name", "email", "identity_ids", "system_role", "organization_role", "workspace_role")
_STATUS_RANK = {"accepted": 1, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
#: Content events the agent removes (``removableTypes`` in ``agent-api/events.ts``); control events stay in the history.
REMOVABLE_TYPES = frozenset(
    {
        "message.created",
        "reasoning.created",
        "tool_call.created",
        "tool_result.created",
        "tool_transaction.created",
        "chart.created",
        "form.created",
        "form.responded",
    }
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _Failure(Exception):
    def __init__(self, code: str, *, reason_code: str | None = None, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code, self.reason_code, self.retry_after = code, reason_code, retry_after


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any = None
    status: int = 0


@dataclass
class _Thread:
    id: str
    external_key: str | None
    title: str
    queue: str | None
    channel: str
    channel_meta: dict[str, Any] | None
    created_at: str
    participants: list[dict[str, Any]]
    deleting: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = ""
    version: int = 1


class FakeAgentApi:
    """One agent of one scope against an in-memory Maestro."""

    def __init__(self, *, workspace_id: str | None = None) -> None:
        self.contracts = default_contracts()
        self.workspace_id = workspace_id
        self.organization_id = ORGANIZATION_ID
        #: Test knob: the organization the answers claim (the token keeps ``organization_id``).
        self.view_organization_id: str | None = None
        self.agent_id = AGENT_ID
        self.requests: list[Recorded] = []
        self.failures: list[str] = []
        self.threads: dict[str, _Thread] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, tuple[str, int, Any]] = {}
        self.corrupt_next: set[str] = set()
        #: path -> error code the next request of that path gets (``DEPENDENCY_UNAVAILABLE``, ...).
        self.fail_next: dict[str, str] = {}
        self.token_scopes = ["threads:manage"]
        self.link_route_enabled = True
        #: principal id -> what the core knows about that human (``display_name``, ``email``, ``identity_ids`` and the roles).
        self.core_people: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._last_stamp = 0
        self._runner: web.AppRunner | None = None
        self.port = 0
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # -- lifecycle --

    async def start(self) -> FakeAgentApi:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{PREFIX}"

    def config(self, **overrides: Any) -> MaestroConfig:
        """The SDK configuration bound to this server (the callback base URL is the API prefix)."""
        public = self._key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        values: dict[str, Any] = {
            "runtime_endpoint": "http://127.0.0.1:9000/api/dev/agent/maestro/deliveries",
            "callback_base_url": self.base_url,
            "core_base_url": "http://127.0.0.1:8788/api/v1",
            "organization_id": self.organization_id,
            "workspace_id": self.workspace_id,
            "instance_agent_id": self.agent_id,
            "runtime_id": RUNTIME_ID,
            "trusted_public_keys": {"fixture": public},
            "credential_resolver": self.credential,
            "allow_local_http": True,
        }
        values.update(overrides)
        return MaestroConfig(**values)

    async def credential(self, request: Any) -> MaestroInstanceCredential:
        """The Core agent token exchange: a JWT with the claims of ``Identity.AgentTokenClaims``."""
        now = int(time.time())
        claims = deepcopy(_FIXTURES["identity.claims.agent-instance"])
        claims.update(
            sub=self.agent_id,
            agent_id=self.agent_id,
            runtime_id=RUNTIME_ID,
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
            iat=now - 1,
            exp=now + 240,
            jti=str(uuid4()),
            scopes=list(self.token_scopes),
        )
        header = _b64(json.dumps({"typ": "maestro-access+jwt", "alg": "RS256", "kid": "fixture"}).encode())
        token = f"{header}.{_b64(json.dumps(claims).encode())}.{_b64(b'signature')}"
        return MaestroInstanceCredential(token, claims["exp"])

    # -- helpers --

    def _tick(self) -> str:
        self._sequence += 1
        return str(self._sequence)

    def _stamp(self) -> str:
        """The time of a write: strictly increasing (milliseconds), so the order of activity is the order of writes."""
        wall = int(time.time() * 1000)
        self._last_stamp = max(wall, self._last_stamp + 1)
        return datetime.fromtimestamp(self._last_stamp / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _thread_by_ref(self, ref: str) -> _Thread:
        found = next((t for t in self.threads.values() if t.external_key == ref), None) or self.threads.get(ref)
        if found is None:
            raise _Failure("NOT_FOUND")
        if found.deleting:
            raise _Failure("THREAD_DELETING")
        return found

    def _agent_participant(self) -> dict[str, Any]:
        return {
            "availability": "unknown",
            "capabilities": [],
            "display_name": "Agente",
            "instance_agent_id": self.agent_id,
            "joined_sequence": "1",
            "kind": "agent",
            "left_sequence": None,
            "participant_id": str(uuid4()),
            "principal_id": self.agent_id,
            "role": "member",
            "state": "active",
        }

    def _participant(self, spec: dict[str, Any], role: str = "member") -> dict[str, Any]:
        base = {"joined_sequence": "1", "left_sequence": None, "participant_id": str(uuid4()), "role": role, "state": "active"}
        if spec["kind"] == "human":
            return {
                **base,
                "kind": "human",
                "principal_id": spec["principal_id"],
                **self._core_person(spec["principal_id"]),
            }
        if spec["kind"] == "agent":
            return {
                **base,
                "kind": "agent",
                "principal_id": spec["principal_id"],
                "instance_agent_id": spec["principal_id"],
                "display_name": "Agente",
                "availability": "unknown",
                "capabilities": [],
            }
        return {
            **base,
            "kind": "external",
            "channel": spec["channel"],
            "external_id": spec["external_id"],
            **({"display_name": spec["display_name"]} if "display_name" in spec else {}),
        }

    def _core_person(self, principal_id: str) -> dict[str, Any]:
        """The core's view of a human; one the core does not know has the placeholder name Maestro shows (and no e-mail)."""
        known = self.core_people.get(principal_id)
        if known is None:
            return {"display_name": "Humano", "email": None, "identity_ids": []}
        return {key: value for key, value in known.items() if key in _CORE_PERSON_FIELDS}

    def _view(self, thread: _Thread) -> dict[str, Any]:
        view = deepcopy(_FIXTURES["public.AgentThread.valid"])
        view.update(
            thread_id=thread.id,
            organization_id=self.view_organization_id or self.organization_id,
            workspace_id=self.workspace_id,
            scope_kind="personal" if self.workspace_id is None else "workspace",
            external_key=thread.external_key,
            owner_instance_agent_id=self.agent_id,
            title=thread.title,
            queue=thread.queue,
            channel=thread.channel,
            channel_meta=thread.channel_meta,
            created_at=thread.created_at,
            updated_at=thread.updated_at or thread.created_at,
            last_sequence=str(self._sequence),
            version=thread.version,
            participants=deepcopy(thread.participants),
        )
        return view

    def _actor(self, thread: _Thread, spec: dict[str, Any]) -> dict[str, Any]:
        if spec["kind"] == "self":
            agent = next(p for p in thread.participants if p["kind"] == "agent" and p["principal_id"] == self.agent_id)
            return {
                "display_name": "Agente",
                "instance_agent_id": self.agent_id,
                "kind": "agent",
                "participant_id": agent["participant_id"],
                "principal_id": self.agent_id,
            }
        if spec["kind"] == "human":
            core = self._core_person(spec["principal_id"])
            actor = {"kind": "human", "participant_id": None, "principal_id": spec["principal_id"], **core}
            # What the publisher declared wins (it comes back only to the agent that published it).
            actor["display_name"] = spec.get("display_name") or core["display_name"]
            for name in ("email", "identity_ids"):
                if name in spec:
                    actor[name] = spec[name]
            if actor.get("email") is None:
                del actor["email"]
            return actor
        match = next((p for p in thread.participants if p["kind"] == "external" and p["external_id"] == spec["external_id"]), None)
        if match is None:
            match = self._participant(spec)
            thread.participants.append(match)
        return {
            "channel": spec["channel"],
            "display_name": spec.get("display_name", spec["external_id"]),
            "external_id": spec["external_id"],
            "kind": "external",
            "participant_id": match["participant_id"],
        }

    def _append(self, thread: _Thread, type_: str, actor: dict[str, Any], payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
        event_id = str(uuid4())
        event = {
            "actor": actor,
            "causation_event_id": None,
            "created_at": self._stamp(),
            "event_id": event_id,
            "payload": payload,
            "root_event_id": event_id,
            "schema_version": 1,
            "sequence": self._tick(),
            "thread_id": thread.id,
            "type": type_,
            **extra,
        }
        thread.events.append(event)
        thread.updated_at = event["created_at"]
        return event

    def _visible_events(self, thread: _Thread) -> list[dict[str, Any]]:
        removed = {e["payload"]["event_id"] for e in thread.events if e["type"] == "message.removed"}
        return [e for e in thread.events if e["event_id"] not in removed]

    def _event_by_ref(self, ref: str) -> tuple[_Thread, dict[str, Any]]:
        for thread in self.threads.values():
            for event in self._visible_events(thread):
                if event["event_id"] == ref.lower() or (
                    event.get("published_by_instance_agent_id") and event["payload"].get("client_event_id") == ref
                ):
                    if thread.deleting:
                        raise _Failure("THREAD_DELETING")
                    return thread, event
        raise _Failure("NOT_FOUND")

    # -- contract checks --

    def _request_valid(self, schema: str, value: Any) -> None:
        if not self.contracts.is_valid(schema, value):
            raise _Failure("INVALID_REQUEST")

    def _reply(self, schema: str, value: Any, status: int, path: str) -> web.Response:
        if path in self.corrupt_next:
            self.corrupt_next.discard(path)
            return web.json_response({**value, "unexpected": True}, status=status)
        if not self.contracts.is_valid(schema, value):
            self.failures.append(f"{path}: response is not {schema}")
            return web.json_response({"fake_error": "response is not contract-valid"}, status=500)
        return web.json_response(value, status=status)

    @staticmethod
    def _error(failure: _Failure) -> web.Response:
        status, retryable = _ERRORS[failure.code]
        error: dict[str, Any] = {"code": failure.code, "message": "Falha.", "retryable": retryable, "request_id": str(uuid4())}
        details = {
            **({"reason_code": failure.reason_code} if failure.reason_code else {}),
            **({"retry_after_seconds": failure.retry_after} if failure.retry_after is not None else {}),
        }
        if details:
            error["details"] = details
        headers = {"Retry-After": str(failure.retry_after)} if failure.retry_after is not None else None
        return web.json_response({"error": error}, status=status, headers=headers)

    def _receipt(self, key: str | None, request: Any, build: Any) -> tuple[Any, int, bool]:
        """Idempotent command: ``build() -> (value, created_status)``. Replays answer 200 with the stored value."""
        if key is None or not re.fullmatch(r"[\x21-\x7e]{1,128}", key):
            raise _Failure("INVALID_REQUEST")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
        stored = self.receipts.get(key)
        if stored is not None:
            if stored[0] != digest:
                raise _Failure("IDEMPOTENCY_CONFLICT")
            return stored[2], 200, False
        value, status = build()
        self.receipts[key] = (digest, status, value)
        return value, status, True

    # -- routing --

    async def _handle(self, request: web.Request) -> web.Response:
        path = request.path
        record = Recorded(
            request.method, request.raw_path.split("?")[0], dict(request.query), {k.lower(): v for k, v in request.headers.items()}
        )
        self.requests.append(record)
        try:
            if path in self.fail_next:
                code = self.fail_next.pop(path)
                raise _Failure(code, retry_after=3 if code == "RATE_LIMITED" else None)
            response = await self._route(request, record)
        except _Failure as failure:
            response = self._error(failure)
        record.status = response.status
        return response

    async def _route(self, request: web.Request, record: Recorded) -> web.Response:
        path = request.path
        if not path.startswith(PREFIX + "/agent/"):
            raise _Failure("NOT_FOUND")
        auth = request.headers.get("Authorization", "")
        try:
            claims = json.loads(base64.urlsafe_b64decode(auth.split(" ", 1)[1].split(".")[1] + "=="))
        except Exception:
            raise _Failure("UNAUTHENTICATED") from None
        if "threads:manage" not in claims.get("scopes", []) or claims.get("agent_id") != self.agent_id:
            raise _Failure("FORBIDDEN")
        if request.headers.get("Cookie") is not None:
            raise _Failure("UNAUTHENTICATED")
        parts = [unquote(p) for p in request.raw_path.split("?")[0][len(PREFIX + "/agent/") :].split("/")]
        method = request.method
        key = request.headers.get("Idempotency-Key")
        query = dict(request.query)
        per_thread_list = len(parts) == 3 and parts[0] == "threads" and parts[2] in ("events", "runs")
        lists = parts in (["threads"], ["channel-deliveries"]) or per_thread_list
        if query and not (method == "GET" and lists):
            # Only the list routes read a query; any other key is refused (``z.strictObject`` in the routes).
            raise _Failure("INVALID_REQUEST")

        if parts == ["threads"]:
            if method == "POST":
                return await self._create_thread(request, record, key)
            if method == "GET":
                return self._list_threads(query, path)
        if len(parts) == 2 and parts[0] == "threads":
            if method == "GET":
                self._request_valid("Public.AgentThreadPath", {"id": parts[1]})
                return self._reply("Public.AgentThread", self._view(self._thread_by_ref(parts[1])), 200, path)
            if method == "PATCH":
                return await self._patch_thread(request, record, parts[1], key, path)
            if method == "DELETE":
                return await self._delete_thread(request, record, parts[1], key, path)
        if len(parts) == 3 and parts[0] == "threads" and parts[2] == "events":
            if method == "GET":
                return self._list_events(parts[1], query, path)
            if method == "POST":
                return await self._publish(request, record, parts[1], path)
        if len(parts) == 4 and parts[0] == "threads" and parts[2] == "events" and method == "DELETE":
            return await self._remove_event(request, record, parts[1], parts[3], key, path)
        if len(parts) == 3 and parts[0] == "threads" and parts[2] == "participants" and method == "POST":
            return await self._add_participant(request, record, parts[1], key, path)
        if len(parts) == 2 and parts[0] == "events" and method == "GET":
            self._request_valid("Public.AgentEventPath", {"id": parts[1]})
            return self._reply("Public.AgentEvent", self._event_by_ref(parts[1])[1], 200, path)
        if len(parts) == 3 and parts[0] == "threads" and parts[2] == "artifacts" and method == "POST":
            return await self._upload(request, record, parts[1], key, path)
        if len(parts) == 5 and parts[0] == "threads" and parts[2] == "artifacts" and parts[4] == "links" and method == "POST":
            return await self._link(request, record, parts[1], parts[3], path)
        if len(parts) == 3 and parts[0] == "threads" and parts[2] == "runs" and method == "GET":
            return self._list_runs(parts[1], query, path)
        if len(parts) == 2 and parts[0] == "runs" and method == "GET":
            return self._get_run(parts[1], path)
        if parts == ["channel-deliveries"]:
            if method == "POST":
                return await self._record_delivery(request, record, key, path)
            if method == "GET":
                return self._find_delivery(query, path)
        raise _Failure("NOT_FOUND")

    @staticmethod
    async def _json(request: web.Request, record: Recorded) -> Any:
        if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise _Failure("INVALID_REQUEST")
        try:
            record.body = json.loads(await request.read())
        except ValueError:
            raise _Failure("INVALID_REQUEST") from None
        return record.body

    # -- threads --

    async def _create_thread(self, request: web.Request, record: Recorded, key: str | None) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.AgentThreadCreateRequest", body)
        expected = ("personal", None) if self.workspace_id is None else ("workspace", self.workspace_id)
        if (body["scope_kind"], body.get("workspace_id")) != expected:
            raise _Failure("FORBIDDEN")

        def build() -> tuple[Any, int]:
            existing = next(
                (
                    t
                    for t in self.threads.values()
                    if body.get("external_key") and t.external_key == body["external_key"] and not t.deleting
                ),
                None,
            )
            if existing is not None:
                return self._view(existing), 200
            participants = [self._agent_participant()]
            for spec in body["participants"]:
                if spec["kind"] == "agent" and spec["principal_id"] == self.agent_id:
                    continue
                participants.append(self._participant(spec))
            if "owner" in body and not any(
                p["kind"] == "human" and p["principal_id"] == body["owner"]["principal_id"] for p in participants
            ):
                participants.append(self._participant(body["owner"], "owner"))
            thread = _Thread(
                id=str(uuid4()),
                external_key=body.get("external_key"),
                title=body.get("title", ""),
                queue=body.get("queue"),
                channel=body.get("channel", "dooers-platform"),
                channel_meta=body.get("channel_meta"),
                created_at=self._stamp(),
                participants=participants,
            )
            self.threads[thread.id] = thread
            self._tick()
            return self._view(thread), 201

        value, status, _ = self._receipt(key, body, build)
        return self._reply("Public.AgentThread", value, status, request.path)

    def _list_threads(self, query: dict[str, str], path: str) -> web.Response:
        parsed = {**query, **({"limit": int(query["limit"])} if "limit" in query else {})}
        self._request_valid("Public.AgentThreadListQuery", parsed)
        items = [t for t in self.threads.values() if not t.deleting]
        if "external_key" in query:
            items = [t for t in items if t.external_key == query["external_key"]]
        if "queue" in query:
            items = [t for t in items if (t.queue or "") == query["queue"]]
        items.sort(key=lambda t: (t.updated_at or t.created_at, t.id), reverse=query.get("order", "desc") == "desc")
        offset = int(query["cursor"].split(".")[1]) if "cursor" in query else 0
        limit = int(query.get("limit", "50"))
        page = items[offset : offset + limit]
        more = offset + limit < len(items)
        value = {
            "items": [self._view(t) for t in page],
            "has_more": more,
            "next_cursor": f"mql1.{offset + limit}" if more else None,
            "watermark": str(self._sequence),
        }
        return self._reply("Public.AgentThreadPage", value, 200, path)

    async def _patch_thread(self, request: web.Request, record: Recorded, ref: str, key: str | None, path: str) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.AgentThreadPatchRequest", body)
        thread = self._thread_by_ref(ref)

        def build() -> tuple[Any, int]:
            if "title" in body:
                thread.title = body["title"]
            if "queue" in body:
                thread.queue = body["queue"] or None
            thread.version += 1
            thread.updated_at = self._stamp()
            return self._view(thread), 200

        value, _status, _ = self._receipt(key, {"ref": ref, **body}, build)
        return self._reply("Public.AgentThread", value, 200, path)

    async def _delete_thread(self, request: web.Request, record: Recorded, ref: str, key: str | None, path: str) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.ReasonRequest", body)
        thread = self._thread_by_ref(ref)

        def build() -> tuple[Any, int]:
            thread.deleting = True
            return {"deletion_job_id": str(uuid4()), "state": "deleting"}, 202

        value, status, _ = self._receipt(key, {"ref": ref, **body}, build)
        return self._reply("Public.DeleteResponse", value, 202 if status == 200 else status, path)

    # -- events --

    def _list_events(self, ref: str, query: dict[str, str], path: str) -> web.Response:
        parsed = {**query, **({"limit": int(query["limit"])} if "limit" in query else {})}
        self._request_valid("Public.AgentEventListQuery", parsed)
        thread = self._thread_by_ref(ref)
        items = self._visible_events(thread)
        if "type" in query:
            items = [e for e in items if e["type"] in query["type"].split(",")]
        if "actor_kind" in query:
            items = [e for e in items if e["actor"]["kind"] == query["actor_kind"]]
        if "actor_id" in query:
            items = [
                e
                for e in items
                if e["actor"].get("principal_id") == query["actor_id"] or e["actor"].get("external_id") == query["actor_id"]
            ]
        if "run_id" in query:
            items = [e for e in items if e["payload"].get("run_id") == query["run_id"]]
        descending = query.get("order") == "desc"
        items.sort(key=lambda e: int(e["sequence"]), reverse=descending)
        offset = int(query["cursor"].split(".")[1]) if "cursor" in query else 0
        limit = int(query.get("limit", "50"))
        page = items[offset : offset + limit]
        more = offset + limit < len(items)
        value = {
            "items": page,
            "has_more": more,
            "next_cursor": f"mev1.{offset + limit}" if more else None,
            "watermark": str(self._sequence),
        }
        return self._reply("Public.AgentEventPage", value, 200, path)

    async def _publish(self, request: web.Request, record: Recorded, ref: str, path: str) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.AgentEventCreateRequest", body)
        thread = self._thread_by_ref(ref)

        def build() -> tuple[Any, int]:
            payload = {"client_event_id": body["idempotency_key"], "content": body["payload"]["content"], "mentions": []}
            for name in ("author", "data", "run_id", "template", "whatsapp"):
                if name in body["payload"]:
                    payload[name] = body["payload"][name]
            event = self._append(
                thread, "message.created", self._actor(thread, body["actor"]), payload, published_by_instance_agent_id=self.agent_id
            )
            response: dict[str, Any] = {"event": event, "cursor": f"mev1.{len(thread.events)}"}
            if body["trigger"]:
                response["delivery_id"] = str(uuid4())
            return response, 201

        value, status, _ = self._receipt(body["idempotency_key"], {"ref": thread.id, **body}, build)
        return self._reply("Public.AgentEventCreateResponse", value, status, path)

    async def _remove_event(
        self, request: web.Request, record: Recorded, ref: str, event_id: str, key: str | None, path: str
    ) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.Empty", body)
        thread = self._thread_by_ref(ref)

        def build() -> tuple[Any, int]:
            target = next((e for e in self._visible_events(thread) if e["event_id"] == event_id), None)
            if target is None:
                raise _Failure("NOT_FOUND")
            if target["type"] not in REMOVABLE_TYPES:
                raise _Failure("VALIDATION_FAILED", reason_code="event_not_removable")
            return self._append(thread, "message.removed", self._actor(thread, {"kind": "self"}), {"event_id": event_id}), 200

        value, _status, _ = self._receipt(key, {"ref": thread.id, "event": event_id}, build)
        return self._reply("Public.AgentEvent", value, 200, path)

    async def _add_participant(self, request: web.Request, record: Recorded, ref: str, key: str | None, path: str) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.AgentParticipantAddRequest", body)
        thread = self._thread_by_ref(ref)

        def build() -> tuple[Any, int]:
            participant = self._participant(body)
            thread.participants.append(participant)
            event = self._append(
                thread, "participant.added", self._actor(thread, {"kind": "self"}), {"history_policy": "full", "participant": participant}
            )
            return event, 201

        value, status, _ = self._receipt(key, {"ref": thread.id, **body}, build)
        return self._reply("Public.AgentEvent", value, status, path)

    # -- files --

    async def _upload(self, request: web.Request, record: Recorded, ref: str, key: str | None, path: str) -> web.Response:
        thread = self._thread_by_ref(ref)
        reader = await request.multipart()
        first = await reader.next()
        if first is None or first.name != "metadata" or first.headers.get("Content-Type") is not None:
            raise _Failure("INVALID_REQUEST")
        metadata = json.loads(await first.read())
        second = await reader.next()
        if second is None or second.name != "file":
            raise _Failure("INVALID_REQUEST")
        data = bytes(await second.read())
        record.body = {
            "metadata": metadata,
            "file": {"filename": second.filename, "content_type": second.headers.get("Content-Type"), "size": len(data)},
        }
        self._request_valid("Public.AgentArtifactCreateRequest", metadata)
        if (
            metadata["size_bytes"] != len(data)
            or metadata["sha256"] != hashlib.sha256(data).hexdigest()
            or metadata["filename"] != second.filename
            or metadata["mime_type"] not in KNOWN_MIME_TYPES
        ):
            raise _Failure("VALIDATION_FAILED")

        def build() -> tuple[Any, int]:
            artifact = {
                "artifact_id": str(uuid4()),
                "filename": metadata["filename"],
                "mime_type": metadata["mime_type"],
                "size_bytes": len(data),
            }
            self.artifacts[artifact["artifact_id"]] = {**artifact, "thread_id": thread.id, "data": data}
            return artifact, 201

        value, status, _ = self._receipt(key, {"ref": thread.id, **metadata}, build)
        return self._reply("Public.AgentArtifact", value, 201 if status == 200 else status, path)

    async def _link(self, request: web.Request, record: Recorded, ref: str, artifact_id: str, path: str) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.Empty", body)
        if not self.link_route_enabled:
            raise _Failure("UNSUPPORTED_CAPABILITY")
        thread = self._thread_by_ref(ref)
        artifact = self.artifacts.get(artifact_id)
        if artifact is None or artifact["thread_id"] != thread.id:
            raise _Failure("NOT_FOUND")
        value = {"url": f"https://files.example.test/{artifact_id}?sig=fixture", "expires_at": "2099-01-01T00:00:00Z"}
        return self._reply("Public.ArtifactLinkResponse", value, 200, path)

    # -- runs --

    def add_run(self, thread_id: str, *, status: str = "succeeded", run_id: str | None = None) -> dict[str, Any]:
        """Test setup: a run of ``thread_id`` (the fake does not execute handlers)."""
        run = {
            "agent_id": self.agent_id,
            "ended_at": None if status == "running" else self._stamp(),
            "error": "content_policy" if status == "failed" else None,
            "run_id": run_id or str(uuid4()),
            "started_at": self._stamp(),
            "status": status,
            "thread_id": thread_id,
            "trigger_event_id": str(uuid4()),
        }
        self.runs.append(run)
        return run

    def _list_runs(self, ref: str, query: dict[str, str], path: str) -> web.Response:
        parsed = {**query, **({"limit": int(query["limit"])} if "limit" in query else {})}
        self._request_valid("Public.AgentRunListQuery", parsed)
        thread = self._thread_by_ref(ref)
        items = sorted((r for r in self.runs if r["thread_id"] == thread.id), key=lambda r: r["started_at"], reverse=True)
        value = {"items": items, "has_more": False, "next_cursor": None, "watermark": str(self._sequence)}
        return self._reply("Public.AgentRunPage", value, 200, path)

    def _get_run(self, run_id: str, path: str) -> web.Response:
        self._request_valid("Public.AgentRunPath", {"id": run_id})
        run = next((r for r in self.runs if r["run_id"] == run_id.lower()), None)
        # ``agentVisibleThreads``: a thread in deletion is not one of the agent's threads, so its run is not found.
        if run is None or self.threads[run["thread_id"]].deleting:
            raise _Failure("NOT_FOUND")
        return self._reply("Public.AgentRun", run, 200, path)

    # -- channel deliveries --

    async def _record_delivery(self, request: web.Request, record: Recorded, key: str | None, path: str) -> web.Response:
        body = await self._json(request, record)
        self._request_valid("Public.AgentChannelDeliveryRequest", body)
        thread = self._thread_by_ref(body["thread_id"])
        target = next(
            (e for e in self._visible_events(thread) if e["event_id"] == body["event_id"] and e["type"] == "message.created"), None
        )
        if target is None:
            raise _Failure("NOT_FOUND")

        def build() -> tuple[Any, int]:
            current = self.deliveries.get(target["event_id"], {}).get("status")
            rank = _STATUS_RANK.get(body["status"])
            if rank is None or (current is not None and (current == "failed" or rank < _STATUS_RANK[current])):
                return {"applied": False}, 200
            self.deliveries[target["event_id"]] = {
                "thread_id": thread.id,
                "event_id": target["event_id"],
                "status": body["status"],
                "provider_message_id": body.get("provider_message_id"),
            }
            payload = {"channel": "whatsapp", "event_id": target["event_id"], "status": body["status"]}
            for name in ("error", "error_code", "provider_message_id"):
                if name in body:
                    payload[name] = body[name]
            event = self._append(thread, "channel_delivery.updated", self._actor(thread, {"kind": "self"}), payload)
            return {"applied": True, "event": event}, 201

        value, status, _ = self._receipt(key, body, build)
        return self._reply("Public.AgentChannelDeliveryResponse", value, status, path)

    def _find_delivery(self, query: dict[str, str], path: str) -> web.Response:
        self._request_valid("Public.AgentChannelDeliveryQuery", query)
        found = next((d for d in self.deliveries.values() if d["provider_message_id"] == query["provider_message_id"]), None)
        if found is None:
            raise _Failure("NOT_FOUND")
        thread = self.threads[found["thread_id"]]
        if thread.deleting:
            raise _Failure("THREAD_DELETING")
        value = {"thread_id": found["thread_id"], "event_id": found["event_id"], "status": found["status"]}
        return self._reply("Public.AgentChannelDelivery", value, 200, path)

    # -- test setup --

    def inject_event(self, thread_id: str, type_: str, payload: dict[str, Any], *, actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """Test setup: an event that another writer made (a handler output, ``run.updated``, ``message.updated``).

        It has no ``client_event_id`` and no ``published_by_instance_agent_id``: the agent API did not publish it.
        """
        thread = self.threads[thread_id]
        return self._append(thread, type_, actor or self._actor(thread, {"kind": "self"}), payload)

    def delete_thread_state(self, thread_id: str) -> None:
        """The thread is in deletion: every read of it answers ``THREAD_DELETING``."""
        self.threads[thread_id].deleting = True
