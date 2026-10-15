"""Read-only, authenticated local forms readiness; never a public capability grant."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from .adapter import MaestroDeliveryAdapter
from .executor import MaestroTextExecutor
from .interactions import MaestroCheckpointStore
from .journal_store import JournalError, canonical, decode
from .journal_transport import HEADER, MAX_BODY, JournalServiceKey, local_url, sign_response, verify_request


class FormsReadinessAdapter:
    """Mounted only by explicit local composition; no environment/document activation.

    Runtime version is operator metadata, not a replacement for current Core
    authorization during dispatch. The exact installed adapter/checkpoint object
    is checked, and the SAME journal authority/generation is read on every probe.
    """

    def __init__(self, adapter, *, audience, runtime_version, resolve_key, enabled):
        if (
            not isinstance(adapter, MaestroDeliveryAdapter)
            or not isinstance(adapter._executor, MaestroTextExecutor)
            or not isinstance(adapter._executor.checkpoints, MaestroCheckpointStore)
            or adapter._executor.journal is not adapter._executor.checkpoints.journal
            or adapter._executor.config is not adapter.config
            or type(runtime_version) is not int
            or not 1 <= runtime_version <= 9007199254740991
            or not callable(resolve_key)
            or not callable(enabled)
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.adapter, self.executor, self.checkpoints = adapter, adapter._executor, adapter._executor.checkpoints
        self.config = adapter.config
        self.audience, self.runtime_version = local_url(audience), runtime_version
        self.resolve_key, self.enabled = resolve_key, enabled

    async def __call__(self, scope, receive, send):
        status, result, reply_auth = 503, {"error": "FORMS_UNAVAILABLE"}, None
        deadline = time.monotonic() + 5
        try:
            if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("query_string"):
                raise JournalError("JOURNAL_UNAUTHENTICATED")
            path, root = scope.get("path", ""), scope.get("root_path", "")
            if (path if path.startswith(root + "/") else root + path) != urlsplit(self.audience).path:
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
            if not isinstance(key, JournalServiceKey) or key.audience != self.audience or key.operation != "forms_readiness":
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
                journal, config = self.checkpoints.journal, self.config
                expected = {
                    "format": "maestro.forms-readiness.local.v1",
                    "organization_id": journal.organization_id,
                    "workspace_id": journal.workspace_id,
                    "instance_agent_id": journal.instance_agent_id,
                    "runtime_id": config.runtime_id,
                    "runtime_version": self.runtime_version,
                    "backend_id": self.checkpoints.backend_id,
                    "generation": journal.generation,
                }
                if canonical(body) != canonical(expected):
                    raise JournalError("JOURNAL_SCOPE_DENIED")
                key.check_scope(body)

                def fresh():
                    if (
                        time.monotonic() >= deadline
                        or self.enabled() is not True
                        or self.resolve_key(key.key_id) != key
                        or journal.integrity_verified is not True
                        or self.adapter.config is not config
                        or self.executor.config is not config
                        or self.adapter._executor is not self.executor
                        or self.adapter._executor.checkpoints is not self.checkpoints
                        or self.adapter._executor.journal is not journal
                        or journal.generation != expected["generation"]
                        or self.checkpoints.backend_id != expected["backend_id"]
                        or config.runtime_id != expected["runtime_id"]
                        or self.runtime_version != expected["runtime_version"]
                        or (config.organization_id, config.workspace_id, config.instance_agent_id)
                        != (journal.organization_id, journal.workspace_id, journal.instance_agent_id)
                    ):
                        raise JournalError("JOURNAL_INTEGRITY_BLOCKED")

                fresh()
                async with journal.db.transaction() as conn:
                    await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    fresh()
                    await journal._ready(conn, for_update=False)
                    fresh()
                    await conn.fetch(
                        "SELECT checkpoint_ref,generation,maestro_fence,descriptor,state FROM maestro_journal.checkpoints LIMIT 0"
                    )
                    fresh()
                    if (
                        await conn.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid "
                            "JOIN pg_namespace pn ON pn.oid=p.pronamespace WHERE n.nspname='maestro_journal' "
                            "AND c.relname='checkpoints' AND t.tgname='checkpoint_immutable' AND t.tgtype=31 "
                            "AND t.tgenabled IN ('O','A') AND pn.nspname='maestro_journal' "
                            "AND p.proname='checkpoint_immutable_guard')"
                        )
                        is not True
                    ):
                        raise JournalError("JOURNAL_UPGRADE_REQUIRED")
                    fresh()
                fresh()
                observed = int(time.time() * 1000)
                result = {
                    **expected,
                    "ready": True,
                    "checkpoint_schemas": [{"schema": k, "version": v[0]} for k, v in sorted(self.checkpoints.schemas.items())],
                    "observed_at_ms": observed,
                    "valid_until_ms": observed + 5000,
                }
                encoded = canonical(result)
                if len(encoded) > MAX_BODY:
                    raise JournalError("JOURNAL_PAYLOAD_INVALID")
                fresh()
                status = 200
                reply_auth = sign_response(key, encoded, bytes(raw), authorization, status)
                fresh()
        except JournalError as error:
            status = 401 if error.code in {"JOURNAL_UNAUTHENTICATED", "JOURNAL_SCOPE_DENIED"} else 503
            result, reply_auth = {"error": "FORMS_UNAUTHENTICATED" if status == 401 else "FORMS_UNAVAILABLE"}, None
        except Exception:
            status, result, reply_auth = 503, {"error": "FORMS_UNAVAILABLE"}, None
        # Positive bytes are exactly the bytes checked and HMAC-signed above;
        # do not serialize again after the last freshness gate.
        if status != 200:
            encoded = canonical(result)
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if reply_auth:
            headers.append((HEADER.lower().encode(), reply_auth.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": encoded})
