"""PostgreSQL privado real: CAS, crash/ACK e contagem efetiva de chamadas."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

from dooers.agents.server.database import SqlDatabase
from dooers.agents.server.maestro.journal_store import (
    JournalError,
    PostgresEffectJournal,
    canonical,
    make_intent,
    projection_data,
)
from dooers.agents.server.maestro.journal_transport import (
    JournalServiceKey,
    sign_request,
    sign_response,
    verify_request,
    verify_response,
)
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.agents.server.protocol.maestro.identity import StableIdentity
from dooers.agents.server.protocol.maestro.journal import ExecutionAuthority, JournalScope, PrivateEffectResult, ReconciliationEvidence


@pytest.fixture
async def private_journal(request):
    url = os.environ.get("MAESTRO_TEST_DATABASE_ADMIN_URL")
    if not url:
        pytest.skip("dedicated PostgreSQL required")
    parsed = urlsplit(url)
    assert (parsed.hostname, parsed.port, parsed.username, parsed.path) == ("127.0.0.1", 55434, "maestro_admin", "/postgres")
    name = f"maestro_e0509_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(url)
    role = f"{name}_private"
    password = uuid4().hex
    await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    await admin.execute(f"CREATE DATABASE {name} OWNER {role}")
    await admin.execute(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
    persistence = PostgresPersistence(host="127.0.0.1", port=55434, user=role, database=name, password=password)
    await persistence.connect()
    db = SqlDatabase(persistence)
    await db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    scope = JournalScope(str(uuid4()), None, str(uuid4()), str(uuid4()), str(uuid4()))
    if getattr(request, "param", None) == "sdk":
        from tests.test_maestro_admission import BASE

        e = BASE["envelope"]
        scope = JournalScope(e["organization_id"], e["workspace_id"], e["thread_id"], e["recipient_participant_id"], e["instance_agent_id"])
    active = True
    calls = 0

    async def authorize(given, delivery, phase, authority):
        nonlocal calls
        calls += 1
        assert given == scope
        if not active:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        return datetime.now(UTC) + timedelta(seconds=60)

    store = PostgresEffectJournal(
        db,
        generation=str(uuid4()),
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        instance_agent_id=scope.instance_agent_id,
        fingerprint_keys={"1": b"synthetic-fingerprint-key-32-bytes-only"},
        active_key_version="1",
        authorize=authorize,
        integrity_verified=True,
    )
    await store.provision_fresh()
    delivery = str(uuid4())
    authority = ExecutionAuthority(str(uuid4()), str(uuid4()), "1", (datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    fingerprint = hashlib.sha256(b"synthetic-context").hexdigest()

    async def invocation(policy="journal_only"):
        claim = await store.reserve_invocation(scope, delivery, "initial", authority, context_fingerprint=fingerprint, retry_policy=policy)
        await store.start_invocation(claim, authority)
        return claim

    def intent(ordinal=0, policy="non_reconcilable", args=None):
        identity = StableIdentity(
            delivery, "initial", ordinal, f"output:{ordinal}", f"effect:{ordinal}", f"maestro:v1:effect:{delivery}:initial:{ordinal}"
        )
        return make_intent(
            scope, identity, tool="sandbox", version="1", account="synthetic-account", arguments=args or {"units": 1}, policy=policy
        )

    def recovery(policy="journal_only"):
        from dooers.agents.server.maestro.journal_store import scope_data

        return {
            "scope": scope_data(scope),
            "delivery_id": delivery,
            "phase_id": "initial",
            "execution_id": authority.execution_id,
            "maestro_fence": authority.maestro_fence,
            "context_fingerprint": fingerprint,
            "retry_policy": policy,
        }

    try:
        yield locals()
    finally:
        await persistence.disconnect()
        await admin.execute(f"DROP DATABASE {name}")
        await admin.execute(f"DROP ROLE {role}")
        await admin.close()


def result():
    return PrivateEffectResult(canonical({"charged": True}), canonical({"sandbox_receipt": "private-1"}))


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf"), 9007199254740992, {1: "key"}, "\ud800", {"x": object()}])
def test_closed_canonicalizer(value):
    with pytest.raises(JournalError):
        canonical(value)


def test_canonical_hash_is_computed_by_adapter():
    scope = JournalScope(*(str(uuid4()) for _ in range(5)))
    delivery = str(uuid4())
    identity = StableIdentity(delivery, "initial", 0, "output:0", "effect:0", f"maestro:v1:effect:{delivery}:initial:0")
    a = make_intent(scope, identity, tool="sandbox", version="1", account="one", arguments={"b": 1, "a": "2"}, policy="non_reconcilable")
    b = make_intent(scope, identity, tool="sandbox", version="1", account="one", arguments={"a": "2", "b": 1}, policy="non_reconcilable")
    assert a.arguments_sha256 == b.arguments_sha256
    assert (
        a.arguments_sha256
        != make_intent(
            scope, identity, tool="sandbox", version="1", account="two", arguments={"a": "2", "b": 1}, policy="non_reconcilable"
        ).arguments_sha256
    )
    assert "sandbox" not in repr(a) and "private" not in repr(result())


async def test_two_replicas_reserve_one_invocation(private_journal):
    h = private_journal
    store = h["store"]

    async def reserve():
        return await store.reserve_invocation(h["scope"], h["delivery"], "initial", h["authority"], context_fingerprint=h["fingerprint"])

    values = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
    assert sum(not isinstance(v, Exception) for v in values) == 1
    assert sum(isinstance(v, JournalError) and v.code == "JOURNAL_INVOCATION_RESERVED" for v in values) == 1
    assert (await h["db"].fetchrow("SELECT count(*) n FROM maestro_journal.invocations"))["n"] == 1


async def test_result_ack_lost_reuses_private_receipt_and_exact_intention(private_journal):
    h = private_journal
    await h["invocation"]()
    intent, store = h["intent"](), h["store"]
    reservation = await store.reserve(intent, h["authority"])
    claim = await store.begin(intent, reservation.claim, h["authority"])
    external_calls = 1
    await store.record_result(intent, claim, result())  # commit happened; client lost ACK
    readback = await store.read(intent.scope, intent.identity)
    assert readback.result == result()
    repeated = await store.reserve(intent, h["authority"])
    assert repeated.decision == "reuse" and external_calls == 1
    with pytest.raises(JournalError, match="JOURNAL_INTENT_CONFLICT"):
        await store.reserve(h["intent"](args={"units": 2}), h["authority"])
    with pytest.raises(JournalError, match="JOURNAL_INTENT_CONFLICT"):
        await store.reserve(replace(intent, arguments_sha256="a" * 64), h["authority"])
    projection = await store.pending_projections(limit=100)
    assert [p.revision for p in projection] == ["1", "2", "3"]
    assert all("sandbox" not in str(projection_data(p)) for p in projection)
    await store.acknowledge_projection(projection[1])
    assert [p.revision for p in await store.pending_projections(limit=100)] == ["1", "3"]
    with pytest.raises(JournalError, match="JOURNAL_PROJECTION_CONFLICT"):
        await store.acknowledge_projection(replace(projection[0], status="unknown"))


async def test_crash_after_external_effect_never_calls_provider_twice(private_journal):
    h = private_journal
    await h["invocation"]()
    intent, store = h["intent"](), h["store"]
    reservation = await store.reserve(intent, h["authority"])
    claim = await store.begin(intent, reservation.claim, h["authority"])
    calls = 1  # sandbox applied, process died before record_result
    recovery = await store.recovery(h["recovery"]())
    assert recovery["decision"] == "manual_review" and calls == 1
    with pytest.raises(JournalError, match="JOURNAL_STALE_WRITER"):
        await store.record_result(intent, claim, result())
    with pytest.raises(JournalError):
        await store.reserve(intent, h["authority"])
    # No projection delivered at all; absence did not affect the decision.
    assert len(await store.pending_projections(limit=100)) == 2


async def test_unsafe_handler_without_recorded_effect_is_manual_review(private_journal):
    h = private_journal
    await h["invocation"]("unsafe")
    assert (await h["store"].recovery(h["recovery"]("unsafe")))["decision"] == "manual_review"


async def test_safe_retry_reuses_result_and_fences_old_invocation(private_journal):
    h = private_journal
    old = await h["invocation"]()
    intent, store = h["intent"](), h["store"]
    r = await store.reserve(intent, h["authority"])
    c = await store.begin(intent, r.claim, h["authority"])
    await store.record_result(intent, c, result())
    response = await store.recovery(h["recovery"]())
    assert response["decision"] == "safe_retry"
    with pytest.raises(JournalError):
        await store.renew_invocation(old, h["authority"])
    authority = replace(h["authority"], execution_id=str(uuid4()), maestro_fence="3")
    new = await store.reserve_invocation(
        h["scope"], h["delivery"], "initial", authority, context_fingerprint=h["fingerprint"], retry_policy="journal_only"
    )
    await store.start_invocation(new, authority)
    assert (await store.reserve(intent, authority)).decision == "reuse"


async def test_recovery_without_delivery_is_a_durable_late_dispatch_barrier(private_journal):
    h = private_journal
    assert (await h["store"].recovery(h["recovery"]()))["decision"] == "safe_retry"
    with pytest.raises(JournalError, match="JOURNAL_INVOCATION_RESERVED"):
        await h["invocation"]()


async def test_tombstone_scope_generation_and_partial_restore_are_closed(private_journal):
    h = private_journal
    await h["invocation"]()
    intent, store = h["intent"](), h["store"]
    await store.reserve(intent, h["authority"])
    forged = replace(intent.scope, recipient_participant_id=str(uuid4()))
    with pytest.raises(JournalError, match="JOURNAL_SCOPE_DENIED"):
        await store.read(forged, intent.identity)
    await store.tombstone(intent.scope, intent.identity)
    with pytest.raises(JournalError, match="JOURNAL_TOMBSTONE"):
        await store.read(intent.scope, intent.identity)
    with pytest.raises(JournalError, match="JOURNAL_TOMBSTONE"):
        await store.reserve(intent, h["authority"])
    store.generation = str(uuid4())
    with pytest.raises(JournalError, match="JOURNAL_INTEGRITY_BLOCKED"):
        await store.pending_projections(limit=10)
    store.generation = str((await h["db"].fetchrow("SELECT generation FROM maestro_journal.authority"))["generation"])
    await h["db"].execute("DELETE FROM maestro_journal.outbox; DELETE FROM maestro_journal.effects")
    with pytest.raises(JournalError, match="JOURNAL_INTEGRITY_BLOCKED"):
        await store.recovery(h["recovery"]())


async def test_reconciliation_requires_explicit_trusted_evidence(private_journal):
    h = private_journal
    await h["invocation"]()
    intent, store = h["intent"](policy="provider_reconcilable"), h["store"]
    r = await store.reserve(intent, h["authority"])
    c = await store.begin(intent, r.claim, h["authority"])
    await store.record_unknown(intent, c)
    await h["db"].execute("UPDATE maestro_journal.effects SET lease_until=clock_timestamp()-interval '1 second'")
    r = await store.reserve(intent, h["authority"])
    assert r.decision == "reconcile"
    recovered = await store.reconcile(
        intent, r.claim, ReconciliationEvidence("applied", b"trusted-sandbox-receipt", result()), h["authority"]
    )
    assert recovered.decision == "reuse" and recovered.record.result == result()


async def test_rotation_retains_existing_fingerprint(private_journal):
    h = private_journal
    await h["invocation"]()
    store = h["store"]
    await store.reserve(h["intent"](), h["authority"])
    old = (await store.pending_projections(limit=10))[0]
    store.keys["2"] = b"another-synthetic-key-32bytes-long"
    store.active_key_version = "2"
    await store.reserve(h["intent"](1), h["authority"])
    current = await store.pending_projections(limit=10)
    assert current[0] == old
    assert current[1].fingerprint_key_version == "2"


def test_hmac_binds_request_response_direction_operation_and_current_nonce():
    key = JournalServiceKey(
        "one",
        b"synthetic-service-key-32-bytes-only",
        "http://127.0.0.1:18787/internal/journal",
        "projection",
        str(uuid4()),
        None,
        str(uuid4()),
    )
    raw = canonical({"synthetic": True})
    signed = sign_request(key, raw)
    verify_request(key, raw, signed)
    for forged in (replace(key, operation="recovery"), replace(key, audience="http://127.0.0.1:18787/other"), replace(key, key_id="two")):
        with pytest.raises(JournalError):
            verify_request(forged, raw, signed)
    with pytest.raises(JournalError):
        verify_request(key, raw + b" ", signed)
    response = canonical({"decision": "applied"})
    proof = sign_response(key, response, raw, signed, 200)
    verify_response(key, response, raw, signed, 200, proof)
    with pytest.raises(JournalError):
        verify_response(key, response, raw, sign_request(key, raw), 200, proof)
    with pytest.raises(JournalError):
        verify_response(key, response, raw, signed, 201, proof)


async def test_real_process_dies_after_sandbox_commit_and_replica_never_reexecutes(private_journal):
    import json
    import sys
    from dataclasses import asdict
    from pathlib import Path

    from dooers.agents.server.maestro.journal_store import scope_data

    h = private_journal
    await h["db"].execute("CREATE TABLE public.sandbox_effects(id bigserial PRIMARY KEY,effect_key text NOT NULL)")
    # Estes objetos da fixture só contêm dados sintéticos; journal/results não são exportados.
    config = {
        "database": {"host": "127.0.0.1", "port": 55434, "user": h["role"], "database": h["name"], "password": h["password"]},
        "scope": scope_data(h["scope"]),
        "generation": h["store"].generation,
        "authority": asdict(h["authority"]),
        "delivery_id": h["delivery"],
        "fingerprint": h["fingerprint"],
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("maestro_journal_crash_worker.py")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(config).encode()), 15)
    assert process.returncode == 75, (process.returncode, len(stdout), len(stderr))
    assert (await h["db"].fetchrow("SELECT count(*) n FROM public.sandbox_effects"))["n"] == 1
    assert (await h["store"].recovery(h["recovery"]()))["decision"] == "manual_review"
    with pytest.raises(JournalError):
        await h["store"].reserve(h["intent"](), h["authority"])
    assert (await h["db"].fetchrow("SELECT count(*) n FROM public.sandbox_effects"))["n"] == 1


async def test_blocked_integrity_attestation_and_freshness_fail_before_mutation(private_journal):
    h = private_journal
    h["store"].integrity_verified = False
    with pytest.raises(JournalError, match="JOURNAL_INTEGRITY_BLOCKED"):
        await h["invocation"]()
    h["store"].integrity_verified = True

    async def revoked(*args):
        raise JournalError("JOURNAL_SCOPE_DENIED")

    h["store"].authorize = revoked
    with pytest.raises(JournalError, match="JOURNAL_SCOPE_DENIED"):
        await h["invocation"]()
    assert (await h["db"].fetchrow("SELECT count(*) n FROM maestro_journal.invocations"))["n"] == 0


@pytest.mark.parametrize("state", ["pending", "succeeded"])
async def test_reuse_wait_authorization_expiring_behind_row_lock_is_rejected(private_journal, state):
    h = private_journal
    await h["invocation"]()
    store, intent = h["store"], h["intent"]()
    r = await store.reserve(intent, h["authority"])
    if state == "succeeded":
        claim = await store.begin(intent, r.claim, h["authority"])
        await store.record_result(intent, claim, result())
    authorized = asyncio.Event()

    async def brief(*args):
        authorized.set()
        return datetime.now(UTC) + timedelta(milliseconds=150)

    store.authorize = brief
    async with h["db"].connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT * FROM maestro_journal.effects FOR UPDATE")
            pending = asyncio.create_task(store.reserve(intent, h["authority"]))
            await authorized.wait()
            await asyncio.sleep(0.2)
    with pytest.raises(JournalError, match="JOURNAL_AUTHORIZATION_EXPIRED"):
        await pending


async def test_confirmed_accounting_after_private_expiry_preserves_cas(private_journal):
    from dooers.agents.server.maestro.adapter import MaestroConfirmedResult

    h = private_journal
    claim = await h["invocation"]()
    confirmed = MaestroConfirmedResult(
        {"delivery_id": h["delivery"], "execution_id": h["authority"].execution_id, "state": "silent", "result_event_ids": [str(uuid4())]},
        confirmed=True,
    )
    await h["db"].execute("UPDATE maestro_journal.phases SET lease_until=clock_timestamp()-interval '1 second'")
    await h["store"].confirm_invocation(claim, confirmed)
    proof = await h["store"].recovery(h["recovery"]())
    assert proof["decision"] == "confirmed" and proof["confirmed_result"] == confirmed.response
    with pytest.raises(JournalError):
        await h["store"].confirm_invocation(claim, confirmed)


async def test_late_provider_receipt_accounting_does_not_reopen_phase_or_restore_writer(private_journal):
    h = private_journal
    await h["invocation"]()
    store, intent = h["store"], h["intent"]()
    reserved = await store.reserve(intent, h["authority"])
    old = await store.begin(intent, reserved.claim, h["authority"])
    assert (await store.recovery(h["recovery"]()))["decision"] == "manual_review"
    with pytest.raises(JournalError):
        await store.record_result(intent, old, result())
    record = await store.account_late_result(
        intent, expected_revision=old.revision, evidence=ReconciliationEvidence("applied", b"operator-sandbox-proof", result())
    )
    assert record.status == "succeeded"
    assert (await h["db"].fetchrow("SELECT state FROM maestro_journal.phases"))["state"] == "manual_review"
    assert (await store.pending_projections(limit=100))[-1].status == "succeeded"
    with pytest.raises(JournalError):
        await store.reserve(intent, h["authority"])
    with pytest.raises(JournalError):
        await store.account_late_result(
            intent, expected_revision=old.revision, evidence=ReconciliationEvidence("applied", b"old", result())
        )


async def test_effect_claim_expiry_requires_reconciliation_even_if_phase_is_alive(private_journal):
    h = private_journal
    await h["invocation"]()
    h["store"].claim_seconds = 1
    intent, store = h["intent"](), h["store"]
    reserved = await store.reserve(intent, h["authority"])
    claim = await store.begin(intent, reserved.claim, h["authority"])
    await asyncio.sleep(1.05)
    with pytest.raises(JournalError, match="JOURNAL_STALE_WRITER"):
        await store.record_result(intent, claim, result())
    assert (await store.recovery(h["recovery"]()))["decision"] == "manual_review"


async def test_repeated_admission_failure_advances_only_safe_retry_barrier(private_journal):
    h = private_journal
    store = h["store"]
    first = h["recovery"]("unsafe")
    assert (await store.recovery(first))["decision"] == "safe_retry"
    second = {**first, "execution_id": str(uuid4()), "maestro_fence": "3"}
    proof = await store.recovery(second)
    assert proof["decision"] == "safe_retry" and proof["binding"] == second
    assert (await store.recovery(second))["decision"] == "safe_retry"
    with pytest.raises(JournalError, match="JOURNAL_STALE_WRITER"):
        await store.recovery(first)
    with pytest.raises(JournalError, match="JOURNAL_STALE_WRITER"):
        await store.recovery({**second, "execution_id": str(uuid4())})
    with pytest.raises(JournalError, match="JOURNAL_BINDING_INVALID"):
        await store.recovery({**second, "execution_id": str(uuid4()), "maestro_fence": "5", "context_fingerprint": "a" * 64})


async def test_recovery_cannot_advance_a_running_writer(private_journal):
    h = private_journal
    await h["invocation"]("unsafe")
    with pytest.raises(JournalError, match="JOURNAL_STALE_WRITER"):
        await h["store"].recovery({**h["recovery"]("unsafe"), "execution_id": str(uuid4()), "maestro_fence": "3"})
