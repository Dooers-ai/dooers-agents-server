"""Infra candidata privada E12-04; não é API de consentimento nem autorização.

O gate E12-05 ainda deve resolver decisão/ACL/lease atuais antes de reserve/begin.
Nenhuma capability é anunciada. Mesma autoridade E05 e checkpoints E11, sem ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from dooers.agents.server.protocol.maestro.identity import StableIdentity
from dooers.agents.server.protocol.maestro.journal import EffectIntent

from .journal_store import JournalError, canonical, decimal, decode, instant, make_intent, scope_data, uuid


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def commitment(r):
    return digest(
        {
            "format": "maestro.tool-approval-commitment.local.v1",
            "binding": r["binding"],
            "interaction_id": r["interaction_id"],
            "intent_sha256": r["intent_sha256"],
            "private_nonce": r["private_nonce"],
            "expires_at": r["expires_at"],
            "delivery_expires_at": r["delivery_expires_at"],
            "eligible_participant_ids": sorted(r["eligible_participant_ids"]),
            "policy_id": r["policy_id"],
            "effect_key": r["effect_key"],
        }
    )


def project(r):
    a, b = r["arguments"], r["binding"]
    return dict(
        interaction_id=r["interaction_id"],
        origin_event_id=b["origin_event_id"],
        agent_participant_id=b["agent_participant_id"],
        execution_id=b["source_execution_id"],
        delivery_id=b["delivery_id"],
        state="pending",
        definition=dict(
            kind="tool_approval",
            schema_version=1,
            title="Confirm sandbox flag change",
            tool_name=r["tool_binding"]["tool"],
            action_description=f"Set {a['flag']}={str(a['value']).lower()} on sandbox target {a['target_id']}.",
            effect_scope=f"One sandbox flag update; account {r['tool_binding']['account']}; target {a['target_id']}.",
            arguments_hash=r["arguments_hash"],
            expires_at=r["expires_at"],
            eligible_participant_ids=sorted(r["eligible_participant_ids"]),
            actions=["approve", "reject"],
        ),
    )


def snapshot_schema(schema):
    """Historical proposed fixtures stay valid; runtime uses the confirmed local pair."""
    schema = deepcopy(schema)
    schema["properties"]["policy_id"] = {"enum": ["fixture.current-human.first-valid.v1", "local.explicit-current-human.first-valid.v1"]}
    schema["properties"]["policy_status"] = {"enum": ["proposed", "confirmed_local_trial_only"]}
    return schema


def validate_snapshot(r):
    """Estrutura fechada E12-01 e invariantes; jamais comprovação de ACL/consentimento."""
    try:
        schemas = json.loads(Path(__file__).with_name("approval-candidate.schema.json").read_bytes())
        Draft202012Validator(snapshot_schema(schemas["Candidate.ToolApprovalSnapshot"]), format_checker=FormatChecker()).validate(r)
        canonical(r)
        if (r["policy_id"], r["policy_status"]) not in {
            ("fixture.current-human.first-valid.v1", "proposed"),
            ("local.explicit-current-human.first-valid.v1", "confirmed_local_trial_only"),
        }:
            raise ValueError
        b, c = r["binding"], r["continuity"]
        if (
            decimal(b["source_fence"]) > 9223372036854775807
            or len(set(r["eligible_participant_ids"])) != len(r["eligible_participant_ids"])
            or b["agent_participant_id"] in r["eligible_participant_ids"]
            or r["interaction_id"] in [b["origin_event_id"], r["request_event_id"], b["source_phase_id"]]
            or r["request_event_id"] == b["origin_event_id"]
            or int(r["request_sequence"]) <= int(b["origin_sequence"])
            or instant(r["expires_at"]) > instant(r["delivery_expires_at"])
        ):
            raise ValueError
        if any(
            c[k] != b[k]
            for k in (
                "organization_id",
                "workspace_id",
                "thread_id",
                "delivery_id",
                "agent_participant_id",
                "instance_agent_id",
                "source_execution_id",
                "source_phase_id",
            )
        ) or instant(c["retain_until"]) < instant(r["delivery_expires_at"]):
            raise ValueError
        identity = identity_for(r)
        tool = r["tool_binding"]
        scope = scope_for(r)
        intent = make_intent(
            scope,
            identity,
            tool=tool["tool"],
            version=tool["version"],
            account=tool["account"],
            arguments=r["arguments"],
            policy="non_reconcilable",
        )
        if (
            r["intent_sha256"] != intent.arguments_sha256
            or r["arguments_hash"] != commitment(r)
            or canonical(r["public_interaction"]) != canonical(project(r))
        ):
            raise ValueError
        if r["effect_key"] != identity.effect_key:
            raise ValueError
        return deepcopy(r)
    except Exception:
        raise JournalError("JOURNAL_APPROVAL_INVALID") from None


def identity_for(r):
    d, phase, n = r["binding"]["delivery_id"], r["interaction_id"], r["effect_ordinal"]
    return StableIdentity(d, phase, n, f"output:{n}", f"effect:{n}", f"maestro:v1:effect:{d}:{phase}:{n}")


def scope_for(r):
    from dooers.agents.server.protocol.maestro.journal import JournalScope

    b = r["binding"]
    return JournalScope(b["organization_id"], b["workspace_id"], b["thread_id"], b["agent_participant_id"], b["instance_agent_id"])


def request_bytes(r):
    """Retry conserva origem histórica; todos os outros bytes da proposta são imutáveis."""
    value = deepcopy(r)
    for k in ("private_nonce", "arguments_hash", "public_interaction"):
        value.pop(k, None)
    value["binding"].pop("source_execution_id")
    value["binding"].pop("source_fence")
    value["continuity"].pop("source_execution_id")
    return canonical(value)


PREPARED_PLAN_SCHEMA = "maestro.tool-approval-plan.local.v1"
ADOPTED_PLAN_SCHEMA = "maestro.tool-approval-plan-adopted.local.v1"


def validate_plan_descriptor(row, reference, snapshot):
    """Validate immutable plan provenance, independently from current execution grants.

    Prepared plans retain their original writer. Adopted plans are physically
    created by the current approved phase; their entire snapshot remains historical.
    SQL's unchanged INSERT guard proves that writer was live when persisted.
    """
    d, b = json.loads(row["descriptor"]), snapshot["binding"]
    scope_keys = ("organization_id", "workspace_id", "thread_id", "delivery_id",
                  "instance_agent_id", "agent_participant_id")
    expected = {"checkpoint_ref", "checkpoint_schema", "checkpoint_version",
                "source_execution_id", "source_phase_id", "retain_until", "content_sha256", *scope_keys}
    if (set(d) != expected or type(d["checkpoint_version"]) is not int or d["checkpoint_version"] != 1
            or d["checkpoint_ref"] != reference or d["content_sha256"] != digest(snapshot)
            or d["retain_until"] != snapshot["delivery_expires_at"]
            or any(d[k] != b[k] for k in scope_keys)):
        raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    fence = str(row["maestro_fence"])
    decimal(fence)
    uuid(d["source_execution_id"])
    if d["checkpoint_schema"] == PREPARED_PLAN_SCHEMA:
        if (d["source_execution_id"] != b["source_execution_id"]
                or d["source_phase_id"] != b["source_phase_id"] or fence != b["source_fence"]):
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    elif d["checkpoint_schema"] == ADOPTED_PLAN_SCHEMA:
        if (d["source_phase_id"] != snapshot["interaction_id"]
                or d["source_execution_id"] == b["source_execution_id"]
                or int(fence) <= int(b["source_fence"])):
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    else:
        raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    return d


class MaestroApprovalPlans:
    """Snapshot + nonce persistidos/read-back antes da projeção. Não decide aprovação."""

    def __init__(self, journal, *, checkpoints):
        from .journal_store import PostgresEffectJournal

        if not isinstance(journal, PostgresEffectJournal):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        from .interactions import MaestroCheckpointStore

        if not isinstance(checkpoints, MaestroCheckpointStore) or checkpoints.journal is not journal:
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.journal, self.checkpoints = journal, checkpoints

    def reference(self, delivery, phase, ordinal):
        raw = canonical({"delivery": delivery, "phase": phase, "ordinal": ordinal, "kind": "approval-plan:v1"})
        return f"checkpoint:v1:{UUID(bytes=hashlib.sha256(raw).digest()[:16], version=4)}"

    async def prepare(self, execution, proposal):
        """Trusted candidate input, not a public request. No call to any effect provider."""
        e, store = execution.admission.envelope, execution.store
        if not execution.active or not execution.claim or store.db is not self.journal.db:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        r = deepcopy(proposal)
        # Generate cryptographic entropy ourselves; caller cannot choose nonce/hash/public text.
        r["private_nonce"] = secrets.token_hex(32)
        t = r["tool_binding"]
        r["intent_sha256"] = make_intent(
            scope_for(r),
            identity_for(r),
            tool=t["tool"],
            version=t["version"],
            account=t["account"],
            arguments=r["arguments"],
            policy="non_reconcilable",
        ).arguments_sha256
        r["arguments_hash"] = commitment(r)
        r["public_interaction"] = project(r)
        validate_snapshot(r)
        b = r["binding"]
        if (
            scope_for(r) != execution.scope
            or b["delivery_id"] != execution.delivery
            or b["source_phase_id"] != execution.phase
            or b["source_execution_id"] != execution.authority.execution_id
            or b["source_fence"] != execution.authority.maestro_fence
            or b["origin_event_id"] != e["trigger_event_id"]
            or b["origin_sequence"] != e["trigger_sequence"]
            or r["delivery_expires_at"] != e["delivery_expires_at"]
        ):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        ref = self.reference(execution.delivery, execution.phase, r["effect_ordinal"])
        valid = await store._fresh(execution.scope, execution.delivery, execution.phase, execution.authority)
        async with store.db.transaction() as conn:
            await store._ready(conn)
            phase = await store._invocation(conn, execution.claim)
            row = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", ref)
            if row:
                saved = self._read(row, ref, execution)
                if request_bytes(saved) != request_bytes(r):
                    raise JournalError("JOURNAL_INTENT_CONFLICT")
                r = saved
            else:
                await verify_continuity(conn, store, r, self.checkpoints)
                if datetime.now(UTC) >= instant(r["expires_at"]):
                    raise JournalError("JOURNAL_AUTHORIZATION_EXPIRED")
                descriptor = {
                    "checkpoint_ref": ref,
                    "checkpoint_schema": "maestro.tool-approval-plan.local.v1",
                    "checkpoint_version": 1,
                    "organization_id": b["organization_id"],
                    "workspace_id": b["workspace_id"],
                    "thread_id": b["thread_id"],
                    "delivery_id": b["delivery_id"],
                    "instance_agent_id": b["instance_agent_id"],
                    "agent_participant_id": b["agent_participant_id"],
                    "source_execution_id": b["source_execution_id"],
                    "source_phase_id": b["source_phase_id"],
                    "retain_until": r["delivery_expires_at"],
                    "content_sha256": digest(r),
                }
                await conn.execute(
                    "INSERT INTO maestro_journal.checkpoints(checkpoint_ref,generation,maestro_fence,descriptor,state) "
                    "VALUES($1,$2,$3,$4::jsonb,$5::jsonb)",
                    ref,
                    UUID(store.generation),
                    decimal(b["source_fence"]),
                    canonical(descriptor).decode(),
                    canonical(r).decode(),
                )
                row = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", ref)
                r = self._read(row, ref, execution)
            await verify_continuity(conn, store, r, self.checkpoints)
            await store._fresh_commit(conn, valid, phase["lease_until"], instant(r["delivery_expires_at"]))
        return {"checkpoint_ref": ref, "snapshot_sha256": digest(r), "snapshot": deepcopy(r)}

    def _read(self, row, ref, execution):
        if not row or str(row["generation"]) != execution.store.generation:
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
        r = json.loads(row["state"])
        validate_snapshot(r)
        d = validate_plan_descriptor(row, ref, r)
        if (scope_for(r) != execution.scope or r["binding"]["delivery_id"] != execution.delivery
                or datetime.now(UTC) >= instant(d["retain_until"])):
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
        return r

    async def load(self, execution, reference, snapshot_sha256):
        """Current claim required; original execution/fence are historical, never replaced."""
        store = execution.store
        if not execution.active or not execution.claim or store.db is not self.journal.db:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        valid = await store._fresh(execution.scope, execution.delivery, execution.phase, execution.authority)
        async with store.db.transaction() as conn:
            await store._ready(conn)
            phase = await store._invocation(conn, execution.claim)
            row = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", reference)
            r = self._read(row, reference, execution)
            await verify_continuity(conn, store, r, self.checkpoints)
            if digest(r) != snapshot_sha256:
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            await store._fresh_commit(conn, valid, phase["lease_until"], instant(r["delivery_expires_at"]))
        return deepcopy(r)

    async def adopt(self, execution, context):
        """Import the authenticated canonical request into the SAME journal/checkpoint authority.

        No caller-selected nonce or arguments: all must equal the previously saved
        continuity checkpoint. The current private transport is rechecked after I/O.
        """
        r = validate_snapshot(context["approval_snapshot"])
        if (r["policy_id"], r["policy_status"]) != ("local.explicit-current-human.first-valid.v1", "confirmed_local_trial_only"):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        if execution.phase != r["interaction_id"] or context["continuity"] != r["continuity"]:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        store, b = execution.store, r["binding"]
        if (not execution.active or not execution.claim or store.db is not self.journal.db
                or scope_for(r) != execution.scope or execution.delivery != b["delivery_id"]):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        reference = self.reference(execution.delivery, b["source_phase_id"], r["effect_ordinal"])
        valid = await store._fresh(execution.scope, execution.delivery, execution.phase, execution.authority)
        async with store.db.transaction() as conn:
            await store._ready(conn)
            phase = await store._invocation(conn, execution.claim)
            await verify_continuity(conn, store, r, self.checkpoints)
            original = await conn.fetchrow(
                "SELECT state FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", r["continuity"]["checkpoint_ref"]
            )
            private = json.loads(original["state"])["approval"]
            if canonical(private) != canonical(
                {k: r[k] for k in ("interaction_id", "tool_binding", "arguments", "private_nonce", "effect_ordinal")}
            ):
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            descriptor = {
                "checkpoint_ref": reference,
                "checkpoint_schema": ADOPTED_PLAN_SCHEMA,
                "checkpoint_version": 1,
                **{
                    k: b[k]
                    for k in (
                        "organization_id",
                        "workspace_id",
                        "thread_id",
                        "delivery_id",
                        "instance_agent_id",
                        "agent_participant_id",
                    )
                },
                "source_execution_id": execution.authority.execution_id,
                "source_phase_id": execution.phase,
                "retain_until": r["delivery_expires_at"],
                "content_sha256": digest(r),
            }
            prior = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", reference)
            if prior:
                if canonical(self._read(prior, reference, execution)) != canonical(r):
                    raise JournalError("JOURNAL_INTENT_CONFLICT")
            else:
                await conn.execute(
                    "INSERT INTO maestro_journal.checkpoints(checkpoint_ref,generation,maestro_fence,descriptor,state) "
                    "VALUES($1,$2,$3,$4::jsonb,$5::jsonb)",
                    reference,
                    UUID(store.generation),
                    decimal(execution.authority.maestro_fence),
                    canonical(descriptor).decode(),
                    canonical(r).decode(),
                )
            await store._fresh_commit(conn, valid, phase["lease_until"], instant(r["delivery_expires_at"]))
        return {"checkpoint_ref": reference, "snapshot_sha256": digest(r), "snapshot": deepcopy(r)}

    async def bind(self, execution, reference, snapshot_sha256, *, decision_event_id, policy):
        """Infra only. The caller E12-05 must resolve and revalidate canonical consent."""
        r = await self.load(execution, reference, snapshot_sha256)
        if execution.phase != r["interaction_id"]:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        uuid(decision_event_id)
        t = r["tool_binding"]
        intent = make_intent(
            execution.scope,
            identity_for(r),
            tool=t["tool"],
            version=t["version"],
            account=t["account"],
            arguments=r["arguments"],
            policy=policy,
        )
        binding = {
            "format_version": "maestro.tool-approval-journal.local.v1",
            "approval_id": r["interaction_id"],
            "decision_event_id": decision_event_id,
            "arguments_hash": r["arguments_hash"],
            "intent_sha256": r["intent_sha256"],
            "scope": scope_data(intent.scope),
            "delivery_id": intent.identity.delivery_id,
            "phase_id": intent.identity.phase_id,
            "operation_id": intent.identity.operation_id,
            "effect_key": intent.identity.effect_key,
        }
        return replace(
            intent, approval_binding=canonical({"checkpoint_ref": reference, "snapshot_sha256": snapshot_sha256, "binding": binding})
        )


def validate_effect_binding(intent: EffectIntent):
    value = decode(intent.approval_binding)
    if type(value) is not dict or set(value) != {"checkpoint_ref", "snapshot_sha256", "binding"}:
        raise JournalError("JOURNAL_BINDING_INVALID")
    b = value["binding"]
    expected = {
        "format_version",
        "approval_id",
        "decision_event_id",
        "arguments_hash",
        "intent_sha256",
        "scope",
        "delivery_id",
        "phase_id",
        "operation_id",
        "effect_key",
    }
    if (
        type(b) is not dict
        or set(b) != expected
        or b["format_version"] != "maestro.tool-approval-journal.local.v1"
        or b["approval_id"] != intent.identity.phase_id
        or b["scope"] != scope_data(intent.scope)
        or any(b[k] != getattr(intent.identity, k) for k in ("delivery_id", "phase_id", "operation_id", "effect_key"))
        or b["intent_sha256"] != intent.arguments_sha256
        or not re.fullmatch(r"[a-f0-9]{64}", b["arguments_hash"])
        or not re.fullmatch(r"[a-f0-9]{64}", value["snapshot_sha256"])
        or not re.fullmatch(r"checkpoint:v1:[0-9a-f-]{36}", value["checkpoint_ref"])
    ):
        raise JournalError("JOURNAL_BINDING_INVALID")
    uuid(b["approval_id"]), uuid(b["decision_event_id"])
    return value


async def verify_persisted_binding(conn, store, intent):
    """Inside the SAME journal transaction before CAS; no trusted public cache."""
    x = validate_effect_binding(intent)
    row = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", x["checkpoint_ref"])
    if not row or str(row["generation"]) != store.generation:
        raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    r = json.loads(row["state"])
    validate_snapshot(r)
    validate_plan_descriptor(row, x["checkpoint_ref"], r)
    b = x["binding"]
    await verify_continuity(conn, store, r)
    if (
        digest(r) != x["snapshot_sha256"]
        or scope_for(r) != intent.scope
        or identity_for(r) != intent.identity
        or r["intent_sha256"] != intent.arguments_sha256
        or r["arguments_hash"] != b["arguments_hash"]
    ):
        raise JournalError("JOURNAL_INTENT_CONFLICT")


async def private_approval_correlation(store, intent):
    """Private receipt correlation; no receipt/result/snapshot goes to public output."""
    async with store.db.transaction() as conn:
        await store._ready(conn)
        row = await store._effect(conn, intent)
        if not row or intent.approval_binding is None:
            raise JournalError("JOURNAL_APPROVAL_UNKNOWN")
        return {**decode(intent.approval_binding)["binding"], "receipt_id": str(row["receipt_id"])}


async def verify_continuity(conn, store, r, checkpoints=None):
    from .interactions import MaestroCheckpoint, checkpoint_bytes

    c = r["continuity"]
    row = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", c["checkpoint_ref"])
    if not row or str(row["generation"]) != store.generation:
        raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    state, descriptor = json.loads(row["state"]), json.loads(row["descriptor"])
    if (
        canonical(descriptor) != canonical(c)
        or hashlib.sha256(checkpoint_bytes(state)).hexdigest() != c["content_sha256"]
        or str(row["maestro_fence"]) != r["binding"]["source_fence"]
    ):
        raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
    if checkpoints is not None:
        if c["backend_id"] != checkpoints.backend_id:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        checkpoints._state(MaestroCheckpoint(c["checkpoint_schema"], c["checkpoint_version"], state))
