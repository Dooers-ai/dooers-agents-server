"""Local operational ASGI/HMAC guards with explicit SQL stubs; no installed/PG proof."""

import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from dooers.agents.server.maestro import create_durable_text_delivery_adapter
from dooers.agents.server.maestro.adapter import MaestroDeliveryAdapter
from dooers.agents.server.maestro.approval_readiness import ApprovalReadinessAdapter
from dooers.agents.server.maestro.approvals import MaestroApprovalRuntime
from dooers.agents.server.maestro.interactions import MaestroCheckpointStore
from dooers.agents.server.maestro.journal_store import JournalError, PostgresEffectJournal, canonical
from dooers.agents.server.maestro.journal_transport import HEADER, JournalServiceKey, sign_request, verify_response
from tests.test_maestro_admission import config_for
from tests.test_maestro_admission import private_key as private_key


def observe_journal_errors(monkeypatch):
    codes = []
    original = JournalError.__init__

    def observed(error, code="JOURNAL_UNAVAILABLE"):
        codes.append(code)
        original(error, code)

    monkeypatch.setattr(JournalError, "__init__", observed)
    return codes


def fixture(private_key):
    config = config_for(private_key)
    conn = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(return_value=[]), fetchval=AsyncMock(return_value=True))

    @asynccontextmanager
    async def transaction():
        yield conn

    async def authorize(*_args):
        raise AssertionError("readiness cannot authorize an execution/effect")

    async def handler(*_args):
        raise AssertionError("readiness cannot invoke a handler")

    journal = PostgresEffectJournal(
        SimpleNamespace(transaction=transaction),
        generation=str(uuid4()),
        organization_id=config.organization_id,
        workspace_id=config.workspace_id,
        instance_agent_id=config.instance_agent_id,
        fingerprint_keys={"1": b"x" * 32},
        active_key_version="1",
        authorize=authorize,
        integrity_verified=True,
    )
    journal._ready = AsyncMock()
    checkpoints = MaestroCheckpointStore(journal, backend_id="local_approval", schemas={"demo.period": (1, lambda v: v)})
    approvals = MaestroApprovalRuntime(journal, checkpoints, capability=lambda _: True, commercial=AsyncMock())
    adapter = create_durable_text_delivery_adapter(
        config, handler, lambda *_: None, journal=journal, checkpoints=checkpoints, approvals=approvals
    )
    audience = "http://127.0.0.1:9000/candidate/approval-readiness"
    key = JournalServiceKey(
        "approval", b"y" * 32, audience, "approval_readiness", config.organization_id, config.workspace_id, config.instance_agent_id
    )
    state = {"enabled": True, "key": key}
    app = ApprovalReadinessAdapter(
        adapter, audience=audience, runtime_version=7, resolve_key=lambda _: state["key"], enabled=lambda: state["enabled"]
    )
    body = {
        "format": "maestro.approval-readiness.local.v1",
        "policy_id": "local.explicit-current-human.first-valid.v1",
        "commercial_profile": "fixture.tool-sandbox.no-debit.v1",
        "organization_id": config.organization_id,
        "workspace_id": config.workspace_id,
        "instance_agent_id": config.instance_agent_id,
        "runtime_id": config.runtime_id,
        "runtime_version": 7,
        "backend_id": checkpoints.backend_id,
        "generation": journal.generation,
    }
    return SimpleNamespace(app=app, adapter=adapter, key=key, state=state, body=body, journal=journal, conn=conn)


async def request(h, body=None, headers=None, path="/candidate/approval-readiness", method="POST"):
    raw = canonical(body if body is not None else h.body)
    auth = sign_request(h.key, raw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:9000") as client:
        response = await client.request(
            method, path, content=raw, headers={HEADER: auth, "Content-Type": "application/json", **(headers or {})}
        )
    if response.status_code == 200:
        verify_response(h.key, response.content, raw, auth, 200, response.headers[HEADER])
    return response


async def test_readonly_signed_probe_uses_bound_installed_objects_and_same_journal(private_key):
    h = fixture(private_key)
    response = await request(h)
    assert response.status_code == 200
    data = response.json()
    assert {k: data[k] for k in h.body} == h.body
    assert data["ready"] is True and data["checkpoint_schemas"] == [{"schema": "demo.period", "version": 1}]
    assert 0 < data["valid_until_ms"] - data["observed_at_ms"] <= 5000
    assert data["observed_at_ms"] <= int(time.time() * 1000)
    h.journal._ready.assert_awaited_once_with(h.conn, for_update=False)
    h.conn.execute.assert_awaited_once_with("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    h.conn.fetch.assert_awaited_once()
    assert h.conn.fetch.call_args.args[0].startswith("SELECT checkpoint_ref,")
    assert h.adapter.capabilities == ()


@pytest.mark.parametrize(
    "field", ["organization_id", "workspace_id", "instance_agent_id", "runtime_id", "runtime_version", "backend_id", "generation"]
)
async def test_signed_mismatched_binding_never_reads_backend(private_key, field):
    h = fixture(private_key)
    body = {**h.body, field: 8 if field == "runtime_version" else "other" if field == "backend_id" else str(uuid4())}
    assert (await request(h, body)).status_code == 401
    h.journal._ready.assert_not_awaited()


@pytest.mark.parametrize(
    "headers,path,method",
    [
        ({"Cookie": ""}, "/candidate/approval-readiness", "POST"),
        ({"Origin": "http://127.0.0.1:9000"}, "/candidate/approval-readiness", "POST"),
        ({HEADER: "bad"}, "/candidate/approval-readiness", "POST"),
        ({}, "/candidate/approval-readiness?generation=forged", "POST"),
        ({}, "/wrong", "POST"),
        ({}, "/candidate/approval-readiness", "GET"),
    ],
)
async def test_public_or_unauthenticated_transport_never_reads_backend(private_key, headers, path, method):
    h = fixture(private_key)
    assert (await request(h, headers=headers, path=path, method=method)).status_code == 401
    h.journal._ready.assert_not_awaited()


@pytest.mark.parametrize("change", ["disabled", "key", "integrity", "generation", "backend", "executor"])
async def test_revocation_during_backend_await_never_emits_positive_readiness(private_key, change, monkeypatch):
    h = fixture(private_key)
    errors = observe_journal_errors(monkeypatch)

    changed = False

    async def revoke(_conn, *, for_update):
        nonlocal changed
        assert for_update is False
        changed = True
        if change == "disabled":
            h.state["enabled"] = False
        elif change == "key":
            h.state["key"] = None
        elif change == "integrity":
            h.journal.integrity_verified = False
        elif change == "generation":
            h.journal.generation = str(uuid4())
        elif change == "backend":
            h.app.checkpoints.backend_id = "replaced"
        elif change == "executor":
            h.adapter._executor = SimpleNamespace(checkpoints=h.app.checkpoints, journal=h.journal)

    h.journal._ready.side_effect = revoke
    response = await request(h)
    assert changed is True
    assert errors == ["JOURNAL_INTEGRITY_BLOCKED"]
    h.journal._ready.assert_awaited_once_with(h.conn, for_update=False)
    h.conn.fetch.assert_not_awaited()
    assert response.status_code == 503 and response.json() == {"error": "APPROVAL_UNAVAILABLE"}
    assert HEADER not in response.headers


@pytest.mark.parametrize("target", ["adapter", "executor"])
@pytest.mark.parametrize("when", ["before_probe", "after_backend_await"])
async def test_replaced_config_never_emits_old_runtime_readiness(private_key, target, when, monkeypatch):
    h = fixture(private_key)
    errors = observe_journal_errors(monkeypatch)
    mounted_config = h.app.config
    owner = h.adapter if target == "adapter" else h.app.executor
    replacement = replace(mounted_config, runtime_id=str(uuid4()))
    changed = False

    def swap():
        nonlocal changed
        owner.config = replacement
        changed = True

    if when == "before_probe":
        swap()
    else:
        async def after_await(_conn, *, for_update):
            assert for_update is False
            swap()

        h.journal._ready.side_effect = after_await
    response = await request(h)
    assert errors == ["JOURNAL_INTEGRITY_BLOCKED"]
    assert changed is True and owner.config is replacement
    assert h.app.config is mounted_config
    assert response.status_code == 503 and response.json() == {"error": "APPROVAL_UNAVAILABLE"}
    assert HEADER not in response.headers
    h.conn.fetch.assert_not_awaited()
    if when == "before_probe":
        h.journal._ready.assert_not_awaited()
    else:
        h.journal._ready.assert_awaited_once_with(h.conn, for_update=False)


async def test_missing_checkpoint_table_is_unavailable_without_ddl_or_grant(private_key):
    h = fixture(private_key)
    h.conn.fetch.side_effect = RuntimeError("private SQL details must not escape")
    response = await request(h)
    assert response.status_code == 503
    assert response.json() == {"error": "APPROVAL_UNAVAILABLE"}


async def test_missing_or_disabled_checkpoint_immutable_trigger_is_not_ready(private_key):
    h = fixture(private_key)
    h.conn.fetchval.return_value = False
    response = await request(h)
    assert response.status_code == 503 and response.json() == {"error": "APPROVAL_UNAVAILABLE"}
    assert HEADER not in response.headers


def test_absent_checkpoint_executor_refuses_readiness_composition(private_key):
    with pytest.raises(JournalError, match="JOURNAL_CONFIGURATION_INVALID"):
        ApprovalReadinessAdapter(
            MaestroDeliveryAdapter(config_for(private_key)),
            audience="http://127.0.0.1:9000/candidate/approval-readiness",
            runtime_version=7,
            resolve_key=lambda _: None,
            enabled=lambda: True,
        )
