"""Controlled public adapter/HTTP/SQL ports, no native processes or database."""

import json
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest

from dooers.agents.server.maestro import MaestroInstanceCredential, create_durable_text_delivery_adapter
from dooers.agents.server.maestro.journal_runtime import invocation_fingerprint
from dooers.agents.server.maestro.journal_store import PostgresEffectJournal
from tests.test_maestro_admission import config_for, json_bytes, request, sign, signed_request
from tests.test_maestro_context import AGENT


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class SQLPort:
    def __init__(self, generation, envelope, result):
        self.generation, self.envelope, self.result = generation, envelope, result
        self.phase_state = "confirmed"
        self.inv_state = "confirmed"
        self.exists = True
        self.receipt_exists = True
        self.statements = []
        self.reads = 0
        self.invalid_generation_after_remote = False
        self.change = {}

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql, *values):
        self.statements.append(sql)
        e = self.envelope
        if "maestro_journal.authority" in sql:
            return {
                "generation": uuid4() if self.invalid_generation_after_remote else self.generation,
                "organization_id": UUID(e["organization_id"]),
                "workspace_id": e["workspace_id"],
                "instance_agent_id": UUID(e["instance_agent_id"]),
                "integrity": "ready",
                "effect_count": 0,
                "invocation_count": 1,
            }
        if "SELECT (SELECT count" in sql:
            return {"effects": 0, "invocations": 1}
        if "maestro_journal.phases" in sql:
            if not self.exists:
                return None
            self.reads += 1
            namespace = {k: e[k] for k in ("organization_id", "workspace_id", "thread_id", "recipient_participant_id", "instance_agent_id")}
            return {
                "namespace": json.dumps(namespace),
                "context_fingerprint": invocation_fingerprint(e),
                "retry_policy": "journal_only",
                "execution_id": UUID(e["execution_id"]),
                "maestro_fence": int(e["lease_fence"]),
                "state": self.phase_state,
                **self.change,
            }
        if "maestro_journal.invocations" in sql:
            if not self.receipt_exists:
                return None
            return {
                "state": self.inv_state,
                "delivery_id": UUID(e["delivery_id"]),
                "phase_id": e["phase_id"],
                "maestro_fence": int(e["lease_fence"]),
                "result": json.dumps(self.result),
            }
        raise AssertionError("UNEXPECTED_SQL_PORT_READ")

    async def fetchval(self, sql, *values):
        self.statements.append(sql)
        if "pg_attribute" in sql:
            return True
        raise AssertionError("UNEXPECTED_SQL_PORT_VALUE")

    async def execute(self, sql, *values):
        self.statements.append(sql)
        raise AssertionError("TERMINAL_REPLAY_MUST_NOT_WRITE")


def setup(key, monkeypatch, *, enabled=True, denial="EXECUTION_TERMINAL", remote_status=200, remote_change=None):
    config = config_for(key)
    claims = deepcopy(AGENT)
    claims.update(
        sub=config.instance_agent_id,
        agent_id=config.instance_agent_id,
        runtime_id=config.runtime_id,
        organization_id=config.organization_id,
        workspace_id=config.workspace_id,
        iat=int(time.time()) - 1,
        exp=int(time.time()) + 90,
        scopes=["processing:write"],
    )
    token = sign(key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "test"}, claims)
    credentials = []

    async def credential(value):
        credentials.append(value)
        return MaestroInstanceCredential(token, claims["exp"])

    config = replace(config, credential_resolver=credential)
    envelope, headers = signed_request(key, config)
    result = {
        "delivery_id": envelope["delivery_id"],
        "execution_id": envelope["execution_id"],
        "state": "responded",
        "result_event_ids": [str(uuid4())],
    }
    db = SQLPort(str(uuid4()), envelope, result)

    async def forbidden_authorize(*args):
        raise AssertionError("TERMINAL_REPLAY_MUST_NOT_AUTHORIZE_NEW_INVOCATION")

    store = PostgresEffectJournal(
        db,
        generation=db.generation,
        organization_id=config.organization_id,
        workspace_id=config.workspace_id,
        instance_agent_id=config.instance_agent_id,
        fingerprint_keys={"1": b"j" * 32},
        active_key_version="1",
        authorize=forbidden_authorize,
        integrity_verified=True,
    )
    calls = []

    def response(status, body):
        return httpx.Response(status, stream=httpx.ByteStream(json_bytes(body)), headers={"content-type": "application/json"})

    async def transport(req):
        calls.append(req)
        if req.url.path.endswith("/heartbeat"):
            return response(
                409 if denial == "EXECUTION_TERMINAL" else 403,
                {"error": {"code": denial, "message": "safe", "retryable": False, "request_id": str(uuid4())}},
            )
        assert req.url.path.endswith("/confirmed-result")
        assert req.headers["Idempotency-Key"] == f"complete:{envelope['delivery_id']}:{envelope['phase_id']}"
        assert json.loads(req.content) == {"format_version": "maestro.confirmed-result.local.v1", "lease_fence": envelope["lease_fence"]}
        assert req.headers["Authorization"] == "Bearer " + token
        if remote_change:
            remote_change(db)
        return response(
            remote_status,
            getattr(db, "remote_body", result)
            if remote_status == 200
            else {"error": {"code": "FORBIDDEN", "message": "safe", "retryable": False, "request_id": str(uuid4())}},
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(transport))

    async def forbidden(*args):
        raise AssertionError("TERMINAL_REPLAY_MUST_NOT_ENTER_HANDLER_OR_CONTEXT")

    adapter = create_durable_text_delivery_adapter(
        config,
        forbidden,
        forbidden,
        journal=store,
        retry_policy="journal_only",
        **({} if enabled is None else {"terminal_replay": enabled}),
    )
    return adapter, envelope, headers, db, result, calls, credentials


async def test_factory_terminal_replay_uses_current_remote_and_durable_result_before_context(private_key, monkeypatch):
    adapter, envelope, headers, db, result, calls, credentials = setup(private_key, monkeypatch)
    response = await request(adapter, json_bytes(envelope), headers)
    assert response.status_code == 200 and response.json() == result
    assert len(calls) == 2 and db.reads == 2 and len(credentials) == 2
    assert all("SELECT" in sql for sql in db.statements)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: setattr(d, "exists", False),
        lambda d: setattr(d, "receipt_exists", False),
        lambda d: setattr(d, "phase_state", "running"),
        lambda d: setattr(d, "phase_state", "manual_review"),
        lambda d: setattr(d, "inv_state", "abandoned"),
        lambda d: d.change.update(maestro_fence=int(d.envelope["lease_fence"]) + 1),
        lambda d: d.change.update(execution_id=uuid4()),
        lambda d: d.change.update(context_fingerprint="0" * 64),
        lambda d: d.change.update(retry_policy="unsafe"),
        lambda d: d.change.update(
            namespace=json.dumps(
                {
                    **{
                        k: d.envelope[k]
                        for k in ("organization_id", "workspace_id", "thread_id", "recipient_participant_id", "instance_agent_id")
                    },
                    "thread_id": str(uuid4()),
                }
            )
        ),
        lambda d: d.result.update(delivery_id=str(uuid4())),
        lambda d: d.result.update(result_event_ids=[]),
        lambda d: d.result.update(state="waiting_input"),
    ],
)
async def test_missing_unknown_wrong_scope_fence_intent_or_receipt_never_enters_remote_or_handler(private_key, monkeypatch, change):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch)
    change(db)
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 503 and response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert len(calls) == 1
    assert all("SELECT" in sql for sql in db.statements)


@pytest.mark.parametrize("status", [401, 403, 409, 503])
async def test_current_remote_authority_denies_even_with_confirmed_private_receipt(private_key, monkeypatch, status):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch, remote_status=status)
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == status
    assert (
        response.json()["error"]["code"]
        == {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 409: "LEASE_STALE", 503: "DEPENDENCY_UNAVAILABLE"}[status]
    )
    assert len(calls) == 2 and db.reads == 1


async def test_revoked_current_heartbeat_does_not_try_terminal_read(private_key, monkeypatch):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch, denial="FORBIDDEN")
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN"
    assert len(calls) == 1 and db.reads == 0


async def test_generation_invalidated_during_remote_read_fails_closed_after_await(private_key, monkeypatch):
    adapter, e, h, db, result, calls, _ = setup(
        private_key, monkeypatch, remote_change=lambda d: setattr(d, "invalid_generation_after_remote", True)
    )
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 503 and response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert len(calls) == 2 and db.reads == 1


async def test_remote_receipt_mismatch_is_not_a_confirmed_replay(private_key, monkeypatch):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch)
    # Return a schema-valid but different remote receipt without changing the private one.
    original_fetch = db.fetchrow
    saved = deepcopy(result)

    async def fetch(sql, *args):
        value = await original_fetch(sql, *args)
        if "SELECT state,delivery_id" in sql:
            value = {**value, "result": json.dumps(saved)}
        return value

    db.fetchrow = fetch
    result["result_event_ids"] = [str(uuid4())]
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert db.reads == 2


async def test_expired_deadline_denies_before_any_port(private_key, monkeypatch):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch)
    from tests.test_maestro_admission import timestamp

    e, h = signed_request(private_key, adapter.config, change=lambda data: data["envelope"].update(deadline_at=timestamp(time.time() - 1)))
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 401 and not calls and db.reads == 0


async def test_key_removed_after_remote_read_denies_replay(private_key, monkeypatch):
    def revoke_key(_):
        adapter._lease_gate.config = replace(
            adapter.config, trusted_public_keys={"rotated": next(iter(adapter.config.trusted_public_keys.values()))}
        )

    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch, remote_change=revoke_key)
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 401 and len(calls) == 2
    assert db.reads == 1


async def test_default_factory_keeps_legacy_terminal_denial(private_key, monkeypatch):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch, enabled=False)
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code == 409 and response.json()["error"]["code"] == "EXECUTION_TERMINAL"
    assert len(calls) == 1 and db.reads == 0


@pytest.mark.parametrize(
    "remote_body",
    [
        {"untrusted": True},
        {"delivery_id": str(uuid4()), "execution_id": str(uuid4()), "state": "responded", "result_event_ids": [str(uuid4())]},
    ],
)
async def test_malformed_or_other_execution_remote_receipt_fails_closed(private_key, monkeypatch, remote_body):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch)
    db.remote_body = remote_body
    response = await request(adapter, json_bytes(e), h)
    assert response.status_code in {409, 503}
    assert response.json()["error"]["code"] in {"IDEMPOTENCY_CONFLICT", "DEPENDENCY_UNAVAILABLE"}
    assert len(calls) == 2


async def test_same_current_factory_normal_delivery_keeps_heartbeat_gate(private_key, monkeypatch):
    adapter, e, h, db, result, calls, _ = setup(private_key, monkeypatch)
    from dooers.agents.server.maestro import MaestroCurrentLease
    from tests.test_maestro_admission import timestamp

    async def current(_admission):
        return MaestroCurrentLease(e["execution_id"], e["lease_fence"], timestamp(time.time() + 50))

    adapter._lease_gate.heartbeat.admit = current
    # Current branch forwards the exact actual lease without opening the terminal journal path.
    admission = adapter.authenticate(json_bytes(e), h)
    value = await adapter._lease_gate.admit(admission)
    assert isinstance(value, MaestroCurrentLease) and db.reads == 0 and not calls
