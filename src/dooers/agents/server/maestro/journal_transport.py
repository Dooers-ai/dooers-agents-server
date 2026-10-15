"""Transporte operacional local separado das três credenciais de execução.

HMAC por direção/operação/instância. Somente loopback explícito, sem redirects,
proxy ambiental ou audience derivada do request. Nenhum payload de ferramenta.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from dooers.agents.server.protocol.maestro.journal import EffectProjection

from .journal_store import JournalError, PostgresEffectJournal, canonical, decimal, decode, projection_data, scope_data, uuid

MAX_BODY = 16384
HEADER = "Maestro-Journal-Authorization"


def local_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.username
            or url.password
            or url.query
            or url.fragment
            or "%" in url.path
            or "//" in url.path
            or not url.path.startswith("/")
            or any(part in {".", ".."} for part in url.path.split("/"))
            or url.netloc != f"127.0.0.1:{url.port}"
        ):
            raise ValueError
        return value
    except Exception:
        raise JournalError("JOURNAL_CONFIGURATION_INVALID") from None


@dataclass(frozen=True)
class JournalServiceKey:
    key_id: str
    secret: bytes = field(repr=False)
    audience: str
    operation: str
    organization_id: str
    workspace_id: str | None
    instance_agent_id: str

    def __post_init__(self):
        local_url(self.audience)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.key_id) or type(self.secret) is not bytes or len(self.secret) < 32:
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        if self.operation not in {"projection", "recovery", "approval_outcome", "forms_readiness", "approval_readiness"}:
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        uuid(self.organization_id), uuid(self.instance_agent_id)
        if self.workspace_id is not None:
            uuid(self.workspace_id)

    def check_scope(self, scope: dict[str, Any]) -> None:
        if (scope.get("organization_id"), scope.get("workspace_id"), scope.get("instance_agent_id")) != (
            self.organization_id,
            self.workspace_id,
            self.instance_agent_id,
        ):
            raise JournalError("JOURNAL_SCOPE_DENIED")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _message(key, timestamp, nonce, body_hash, *, response_status=None, request_hash=None):
    values = [
        "maestro-journal-v1",
        "POST" if response_status is None else "RESPONSE",
        key.audience,
        key.operation,
        key.key_id,
        timestamp,
        nonce,
    ]
    if response_status is not None:
        values.extend([request_hash, str(response_status)])
    return "\n".join([*values, body_hash]).encode()


def sign_request(key: JournalServiceKey, raw: bytes) -> str:
    timestamp, nonce = str(int(time.time())), str(uuid4())
    sig = hmac.new(key.secret, _message(key, timestamp, nonce, digest(raw)), hashlib.sha256).hexdigest()
    return f"v1 {key.key_id} {timestamp} {nonce} {sig}"


def verify_request(key: JournalServiceKey, raw: bytes, header: str) -> tuple[str, str]:
    parts = header.split(" ")
    if len(raw) > MAX_BODY or len(parts) != 5 or parts[:2] != ["v1", key.key_id]:
        raise JournalError("JOURNAL_UNAUTHENTICATED")
    timestamp, nonce, sig = parts[2:]
    if not re.fullmatch(r"[0-9]{10}", timestamp) or abs(time.time() - int(timestamp)) > 30 or not re.fullmatch(r"[a-f0-9]{64}", sig):
        raise JournalError("JOURNAL_UNAUTHENTICATED")
    uuid(nonce)
    expected = hmac.new(key.secret, _message(key, timestamp, nonce, digest(raw)), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise JournalError("JOURNAL_UNAUTHENTICATED")
    return timestamp, nonce


def sign_response(key, raw, request_raw, authorization, status):
    timestamp, nonce = verify_request(key, request_raw, authorization)
    signature = hmac.new(
        key.secret, _message(key, timestamp, nonce, digest(raw), response_status=status, request_hash=digest(request_raw)), hashlib.sha256
    ).hexdigest()
    return f"v1 {key.key_id} {timestamp} {nonce} {signature}"


def verify_response(key, raw, request_raw, authorization, status, response_authorization):
    expected = sign_response(key, raw, request_raw, authorization, status)
    if not hmac.compare_digest(expected, response_authorization):
        raise JournalError("JOURNAL_UNAUTHENTICATED")


async def post_operation(key: JournalServiceKey, body: dict[str, Any]) -> dict[str, Any]:
    raw = canonical(body)
    if len(raw) > MAX_BODY:
        raise JournalError("JOURNAL_PAYLOAD_INVALID")
    authorization = sign_request(key, raw)
    try:
        async with asyncio.timeout(5):
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False), trust_env=False, follow_redirects=False, timeout=4
            ) as client:
                async with client.stream(
                    "POST",
                    key.audience,
                    content=raw,
                    headers={HEADER: authorization, "Content-Type": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if (
                        response.headers.get("content-type", "").split(";")[0] != "application/json"
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise JournalError()
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        data.extend(chunk)
                        if len(data) > MAX_BODY:
                            raise JournalError()
                    verify_response(key, bytes(data), raw, authorization, response.status_code, response.headers.get(HEADER, ""))
                    if response.status_code != 200:
                        raise JournalError()
                    return decode(bytes(data))
    except JournalError:
        raise
    except Exception:
        raise JournalError() from None


class HttpEffectProjectionSink:
    def __init__(self, key: JournalServiceKey):
        if key.operation != "projection":
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.key = key

    async def apply(self, projection: EffectProjection):
        body = projection_data(projection)
        self.key.check_scope(body["scope"])
        response = await post_operation(self.key, body)
        if set(response) != {"decision"} or response["decision"] not in {"applied", "duplicate", "superseded"}:
            raise JournalError("JOURNAL_PROJECTION_INVALID")
        return response["decision"]


async def synchronize_projections(journal, sink, *, limit: int = 100) -> int:
    count = 0
    for projection in await journal.pending_projections(limit=limit):
        await sink.apply(projection)
        await journal.acknowledge_projection(projection)
        count += 1
    return count


def validate_recovery_request(body: Any) -> None:
    from dooers.agents.server.protocol.maestro.journal import JournalScope

    expected = {"scope", "delivery_id", "phase_id", "execution_id", "maestro_fence", "context_fingerprint", "retry_policy"}
    if not isinstance(body, dict) or set(body) != expected:
        raise JournalError("JOURNAL_BINDING_INVALID")
    scope_data(JournalScope(**body["scope"]))
    uuid(body["delivery_id"]), uuid(body["execution_id"]), decimal(body["maestro_fence"])
    if body["phase_id"] != "initial":
        uuid(body["phase_id"])
    if not re.fullmatch(r"[a-f0-9]{64}", body["context_fingerprint"]) or body["retry_policy"] not in {"unsafe", "journal_only"}:
        raise JournalError("JOURNAL_BINDING_INVALID")


class JournalRecoveryAdapter:
    """ASGI interno montável; key resolver atual permite revogação/rotação imediata."""

    operation = "recovery"

    def validate(self, body):
        validate_recovery_request(body)

    async def read(self, body):
        return await self.journal.recovery(body)

    def __init__(self, journal: PostgresEffectJournal, *, audience: str, resolve_key):
        self.journal, self.audience, self.resolve_key = journal, local_url(audience), resolve_key

    async def __call__(self, scope, receive, send):
        status, result, reply_auth = 503, {"error": "JOURNAL_UNAVAILABLE"}, None
        deadline = time.monotonic() + 5
        try:
            if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("query_string"):
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            path, root = scope.get("path", ""), scope.get("root_path", "")
            full_path = path if path.startswith(root + "/") else root + path
            if full_path != urlsplit(self.audience).path:
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            headers = {}
            for name, value in scope.get("headers", []):
                name = name.lower()
                if name in {b"origin", b"cookie", b"content-encoding"} or name in headers:
                    raise JournalError("JOURNAL_UNAUTHENTICATED")
                headers[name] = value.decode("ascii")
            authorization = headers.get(HEADER.lower().encode(), "")
            parts = authorization.split(" ")
            if len(parts) != 5 or headers.get(b"content-type") != "application/json":
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            key = self.resolve_key(parts[1])
            if not isinstance(key, JournalServiceKey) or key.audience != self.audience or key.operation != self.operation:
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            raw = bytearray()
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        raise JournalError()
                    raw.extend(message.get("body", b""))
                    if len(raw) > MAX_BODY:
                        raise JournalError("JOURNAL_PAYLOAD_INVALID")
                    if not message.get("more_body"):
                        break
            verify_request(key, bytes(raw), authorization)
            body = decode(bytes(raw))
            self.validate(body)
            key.check_scope(body["scope"])
            # Resolver novamente imediatamente antes da operação: rotação não reabre grant.
            if self.resolve_key(key.key_id) != key:
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            async with asyncio.timeout(min(5, max(0, deadline - time.monotonic())) if self.operation == "approval_outcome" else 5):
                result = await self.read(body)
            if self.resolve_key(key.key_id) != key:
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            status = 200
            encoded = canonical(result)
            if self.operation == "approval_outcome" and (time.monotonic() >= deadline or self.resolve_key(key.key_id) != key):
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            reply_auth = sign_response(key, encoded, bytes(raw), authorization, status)
        except JournalError as error:
            status = 401 if error.code in {"JOURNAL_UNAUTHENTICATED", "JOURNAL_SCOPE_DENIED"} else 503
            result = {"error": "JOURNAL_UNAUTHENTICATED" if status == 401 else "JOURNAL_UNAVAILABLE"}
        except Exception:
            pass
        encoded = canonical(result)
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if reply_auth:
            headers.append((HEADER.lower().encode(), reply_auth.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": encoded})


class JournalApprovalOutcomeAdapter(JournalRecoveryAdapter):
    """Separate operation/audience key; read-only receipt projection, no execution."""

    operation = "approval_outcome"

    def validate(self, body):
        from .approvals import MaestroApprovalOutcomeReader

        MaestroApprovalOutcomeReader.validate(body)

    async def read(self, body):
        from .approvals import MaestroApprovalOutcomeReader

        return await MaestroApprovalOutcomeReader(self.journal).read(body)
