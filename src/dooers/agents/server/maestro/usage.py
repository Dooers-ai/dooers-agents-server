"""Uso privado durável no MESMO journal de efeitos; nunca uma wallet do SDK.

Opt-in sintético local. Provider adapter é código confiável e deve devolver fatos
medidos, não estimar preço. Nenhuma rede roda em transação do journal.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx

from dooers.agents.server.protocol.maestro.journal import (
    EffectClaim,
    EffectIntent,
    ExecutionAuthority,
    PrivateEffectResult,
    ReconciliationEvidence,
)

from .commercial import commercial_denial
from .journal_store import JournalError, PostgresEffectJournal, canonical, decimal, decode, scope_data, uuid

GROUPS = [
    ["format_version", "usage_id", "provider_operation_id"],
    ["organization_id", "workspace_id", "thread_id", "recipient_participant_id", "instance_agent_id"],
    ["delivery_id", "phase_id", "execution_id", "attempt", "effect_key"],
    ["provider_id", "provider_account_ref", "provider_request_ref", "model_id", "operation_kind", "service_tier", "provider_key_source"],
    ["measurement", "provider_outcome", "units", "reasoning_tokens", "prompt_tokens_total"],
    ["billing_organization_id", "commercial_decision_id", "policy_version"],
]
UNIT_NAMES = [
    "cache_read_tokens",
    "cache_write_1h_tokens",
    "cache_write_tokens",
    "input_audio_seconds",
    "input_audio_tokens",
    "input_characters",
    "input_image_tokens",
    "input_images",
    "input_tokens",
    "output_audio_seconds",
    "output_audio_tokens",
    "output_image_tokens",
    "output_images",
    "output_tokens",
]
FIELDS = {key for group in GROUPS for key in group} | {"content_sha256"}
DDL = """
ALTER TABLE maestro_journal.authority ADD COLUMN usage_operation_count bigint NOT NULL DEFAULT 0,
 ADD COLUMN usage_link_count bigint NOT NULL DEFAULT 0;
CREATE TABLE maestro_journal.provider_operations (
 provider_operation_id uuid PRIMARY KEY, delivery_id uuid NOT NULL, phase_id text NOT NULL, operation_id text NOT NULL,
 operation_slot integer NOT NULL CHECK(operation_slot>=0),
 parent_provider_operation_id uuid REFERENCES maestro_journal.provider_operations(provider_operation_id),
 reason text NOT NULL CHECK(reason IN ('initial','new_step','fallback','retry_real')),
 private_result bytea, private_receipt bytea, reconciliation_evidence bytea, binding jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('prepared','started','closed')),
 decision jsonb, fact jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(delivery_id,phase_id,operation_id,operation_slot),
 FOREIGN KEY(delivery_id,phase_id,operation_id) REFERENCES maestro_journal.effects(delivery_id,phase_id,operation_id),
 CHECK((state='closed')=(fact IS NOT NULL)));
CREATE TABLE maestro_journal.usage_outbox (
 usage_id text PRIMARY KEY,
 provider_operation_id uuid NOT NULL UNIQUE REFERENCES maestro_journal.provider_operations(provider_operation_id),
 content_sha256 text NOT NULL, fact jsonb NOT NULL, acked boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE maestro_journal.usage_output_links (
 usage_id text NOT NULL REFERENCES maestro_journal.usage_outbox(usage_id),event_id uuid NOT NULL,
 output_id text NOT NULL,body jsonb NOT NULL,acked boolean NOT NULL DEFAULT false,
 PRIMARY KEY(usage_id,event_id));
CREATE FUNCTION maestro_journal.protect_usage_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'USAGE_IMMUTABLE'; END IF;
 IF TG_TABLE_NAME='provider_operations' THEN
  IF NEW.provider_operation_id<>OLD.provider_operation_id OR NEW.binding<>OLD.binding
   OR NEW.delivery_id<>OLD.delivery_id OR NEW.phase_id<>OLD.phase_id OR NEW.operation_id<>OLD.operation_id
   OR NEW.operation_slot<>OLD.operation_slot
   OR NEW.parent_provider_operation_id IS DISTINCT FROM OLD.parent_provider_operation_id OR NEW.reason<>OLD.reason
   OR (OLD.decision IS NOT NULL AND NEW.decision IS DISTINCT FROM OLD.decision)
   OR (OLD.state='closed' AND NEW IS DISTINCT FROM OLD)
   OR (OLD.state='started' AND NEW.state='prepared') THEN RAISE EXCEPTION 'USAGE_IMMUTABLE'; END IF;
 ELSIF TG_TABLE_NAME='usage_output_links' THEN
  IF NEW.usage_id<>OLD.usage_id OR NEW.event_id<>OLD.event_id OR NEW.output_id<>OLD.output_id
   OR NEW.body<>OLD.body OR (OLD.acked AND NOT NEW.acked) THEN RAISE EXCEPTION 'USAGE_IMMUTABLE'; END IF;
 ELSE
  IF NEW.usage_id<>OLD.usage_id OR NEW.provider_operation_id<>OLD.provider_operation_id
   OR NEW.content_sha256<>OLD.content_sha256 OR NEW.fact<>OLD.fact
   OR NEW.created_at<>OLD.created_at OR (OLD.acked AND NOT NEW.acked) THEN RAISE EXCEPTION 'USAGE_IMMUTABLE'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER usage_output_link_immutable BEFORE UPDATE OR DELETE ON maestro_journal.usage_output_links
 FOR EACH ROW EXECUTE FUNCTION maestro_journal.protect_usage_fact();
CREATE TRIGGER provider_operation_immutable BEFORE UPDATE OR DELETE ON maestro_journal.provider_operations
 FOR EACH ROW EXECUTE FUNCTION maestro_journal.protect_usage_fact();
CREATE TRIGGER usage_outbox_immutable BEFORE UPDATE OR DELETE ON maestro_journal.usage_outbox
 FOR EACH ROW EXECUTE FUNCTION maestro_journal.protect_usage_fact();
"""


def usage_hash(fact: dict[str, Any]) -> str:
    # Campos fechados ASCII: JSON array canônico E10-01, sem números/objetos livres.
    raw = json.dumps([[fact.get(key) for key in group] for group in GROUPS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_usage(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != FIELDS:
        raise JournalError("USAGE_FACT_INVALID")
    u = json.loads(canonical(value))
    for key in [
        "provider_operation_id",
        "organization_id",
        "thread_id",
        "recipient_participant_id",
        "instance_agent_id",
        "delivery_id",
        "execution_id",
        "billing_organization_id",
        "commercial_decision_id",
    ]:
        uuid(u[key])
    if u["workspace_id"] is not None:
        uuid(u["workspace_id"])
    if u["phase_id"] != "initial":
        uuid(u["phase_id"])
    if (
        u["format_version"] != "maestro.usage.v1"
        or u["usage_id"] != f"maestro:v1:usage:{u['organization_id']}:{u['provider_operation_id']}"
    ):
        raise JournalError("USAGE_FACT_INVALID")
    if not isinstance(u["attempt"], str) or not re.fullmatch(r"[1-9][0-9]{0,15}", u["attempt"]):
        raise JournalError("USAGE_FACT_INVALID")
    for key in ["provider_id", "provider_account_ref", "model_id", "policy_version"]:
        if not isinstance(u[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", u[key]):
            raise JournalError("USAGE_FACT_INVALID")
    if u["provider_request_ref"] is not None and (
        not isinstance(u["provider_request_ref"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", u["provider_request_ref"])
    ):
        raise JournalError("USAGE_FACT_INVALID")
    if u["effect_key"] is not None:
        prefix = f"maestro:v1:effect:{u['delivery_id']}:{u['phase_id']}:"
        ordinal = u["effect_key"][len(prefix) :] if isinstance(u["effect_key"], str) and u["effect_key"].startswith(prefix) else ""
        if not re.fullmatch(r"0|[1-9][0-9]{0,15}", ordinal) or int(ordinal) > 9007199254740991:
            raise JournalError("USAGE_FACT_INVALID")
    if (
        u["operation_kind"] != "llm"
        or u["service_tier"] not in {"standard", "flex", "priority"}
        or u["provider_key_source"] not in {"dooers", "organization"}
        or u["provider_outcome"] not in {"succeeded", "failed", "cancelled", "unknown"}
    ):
        raise JournalError("USAGE_FACT_INVALID")
    if u["measurement"] == "unknown":
        if any(u[k] is not None for k in ["units", "reasoning_tokens", "prompt_tokens_total"]):
            raise JournalError("USAGE_FACT_INVALID")
    elif u["measurement"] in {"measured", "estimated"}:
        if (
            type(u["units"]) is not list
            or len(u["units"]) != 14
            or any(type(pair) is not list or len(pair) != 2 for pair in u["units"])
            or [pair[0] for pair in u["units"]] != UNIT_NAMES
        ):
            raise JournalError("USAGE_FACT_INVALID")
        for name, quantity in u["units"]:
            if (
                not isinstance(quantity, str)
                or len(quantity) > 24
                or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?", quantity)
                or (not name.endswith("_seconds") and "." in quantity)
            ):
                raise JournalError("USAGE_FACT_INVALID")
        for key in ["reasoning_tokens", "prompt_tokens_total"]:
            if u[key] is not None and (not isinstance(u[key], str) or not re.fullmatch(r"0|[1-9][0-9]{0,23}", u[key])):
                raise JournalError("USAGE_FACT_INVALID")
        if u["reasoning_tokens"] is not None and int(u["reasoning_tokens"]) > int(dict(u["units"])["output_tokens"]):
            raise JournalError("USAGE_FACT_INVALID")
    else:
        raise JournalError("USAGE_FACT_INVALID")
    if usage_hash(u) != u["content_sha256"]:
        raise JournalError("USAGE_FACT_INVALID")
    return u


@dataclass(frozen=True)
class UsageOperation:
    provider_operation_id: str
    binding: dict[str, Any] = field(repr=False)


class JournalUsage:
    """Extensão do journal existente: mesmos locks, CAS, integridade e backend.

    provision é upgrade administrativo explícito, nunca chamado pelo runtime.
    Métodos de contabilização não têm acesso a callbacks/lease/output Maestro.
    """

    def __init__(self, journal: PostgresEffectJournal):
        if not isinstance(journal, PostgresEffectJournal):
            raise JournalError("USAGE_JOURNAL_REQUIRED")
        self.journal = journal

    async def provision(self):
        async with self.journal.db.transaction() as conn:
            await self.journal._ready(conn)
            await conn.execute(DDL)

    async def _ready(self, conn):
        await self.journal._ready(conn)
        row = await conn.fetchrow(
            """SELECT usage_operation_count,usage_link_count,(SELECT count(*) FROM maestro_journal.usage_output_links) links,
                (SELECT count(*) FROM maestro_journal.provider_operations) actual FROM maestro_journal.authority WHERE id"""
        )
        if row["usage_operation_count"] != row["actual"] or row["usage_link_count"] != row["links"]:
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
        missing = await conn.fetchval("""SELECT count(*) FROM maestro_journal.provider_operations p
          LEFT JOIN maestro_journal.usage_outbox o ON o.provider_operation_id=p.provider_operation_id
          WHERE (p.state='closed') IS DISTINCT FROM (o.usage_id IS NOT NULL)
           OR (p.state='closed' AND p.fact IS DISTINCT FROM o.fact)""")
        if missing:
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")

    async def prepare(
        self,
        intent: EffectIntent,
        claim: EffectClaim,
        authority: ExecutionAuthority,
        *,
        attempt: str,
        provider_id: str,
        provider_account_ref: str,
        model_id: str,
        max_micro_coins: str,
        max_tokens: str,
        debit_owner: str = "core_synthetic",
        operation_slot: int = 0,
        parent_provider_operation_id: str | None = None,
        reason: str = "initial",
    ) -> UsageOperation:
        j = self.journal
        for value in [provider_id, provider_account_ref, model_id]:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                raise JournalError("USAGE_BINDING_INVALID")
        if not isinstance(attempt, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", attempt):
            raise JournalError("USAGE_BINDING_INVALID")
        for value in [max_micro_coins, max_tokens]:
            if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,17}", value):
                raise JournalError("USAGE_BINDING_INVALID")
        if debit_owner not in {"core_synthetic", "legacy_gateway"}:
            raise JournalError("USAGE_BINDING_INVALID")
        if (
            type(operation_slot) is not int
            or not 0 <= operation_slot <= 1000
            or reason not in {"initial", "new_step", "fallback", "retry_real"}
            or (operation_slot == 0) != (reason == "initial" and parent_provider_operation_id is None)
        ):
            raise JournalError("USAGE_BINDING_INVALID")
        if parent_provider_operation_id is not None:
            uuid(parent_provider_operation_id)
        valid = await j._fresh(intent.scope, intent.identity.delivery_id, intent.identity.phase_id, authority)
        async with j.db.transaction() as conn:
            await self._ready(conn)
            effect = await j._cas(conn, intent, claim)
            if effect["status"] != "pending" or str(effect["execution_id"]) != authority.execution_id:
                raise JournalError("JOURNAL_STALE_WRITER")
            old = await conn.fetchrow(
                "SELECT * FROM maestro_journal.provider_operations WHERE delivery_id=$1 AND phase_id=$2 AND "
                "operation_id=$3 AND operation_slot=$4 FOR UPDATE",
                effect["delivery_id"],
                effect["phase_id"],
                effect["operation_id"],
                operation_slot,
            )
            operation = str(old["provider_operation_id"]) if old else str(uuid4())
            binding = {
                **scope_data(intent.scope),
                "provider_operation_id": operation,
                "delivery_id": intent.identity.delivery_id,
                "phase_id": intent.identity.phase_id,
                "execution_id": authority.execution_id,
                "attempt": attempt,
                "effect_key": intent.identity.effect_key,
                "provider_id": provider_id,
                "provider_account_ref": provider_account_ref,
                "model_id": model_id,
                "operation_kind": "llm",
                "service_tier": "standard",
                "provider_key_source": "dooers",
                "max_micro_coins": max_micro_coins,
                "max_tokens": max_tokens,
                "debit_owner": debit_owner,
            }
            if old:
                persisted = json.loads(old["binding"])
                if any(persisted[k] != value for k, value in binding.items() if k not in {"execution_id", "attempt"}):
                    raise JournalError("USAGE_BINDING_CONFLICT")
                binding = persisted  # Retry conserva a chamada/execução que a originou.
            if old and (
                old["reason"] != reason
                or (str(old["parent_provider_operation_id"]) if old["parent_provider_operation_id"] else None)
                != parent_provider_operation_id
            ):
                raise JournalError("USAGE_BINDING_CONFLICT")
            if not old:
                if operation_slot:
                    parent = await conn.fetchrow(
                        "SELECT * FROM maestro_journal.provider_operations WHERE provider_operation_id=$1 FOR UPDATE",
                        UUID(parent_provider_operation_id),
                    )
                    if (
                        not parent
                        or any(parent[k] != effect[k] for k in ["delivery_id", "phase_id", "operation_id"])
                        or parent["operation_slot"] >= operation_slot
                        or parent["state"] != "closed"
                        or json.loads(parent["fact"])["measurement"] == "unknown"
                    ):
                        raise JournalError("USAGE_PARENT_UNCONFIRMED")
                await conn.execute(
                    "INSERT INTO "
                    "maestro_journal.provider_operations(provider_operation_id,delivery_id,phase_id,operation_id,binding,state,"
                    "operation_slot,parent_provider_operation_id,reason) "
                    "VALUES($1,$2,$3,$4,$5::jsonb,'prepared',$6,$7,$8)",
                    UUID(operation),
                    effect["delivery_id"],
                    effect["phase_id"],
                    effect["operation_id"],
                    canonical(binding).decode(),
                    operation_slot,
                    UUID(parent_provider_operation_id) if parent_provider_operation_id else None,
                    reason,
                )
                await conn.execute("UPDATE maestro_journal.authority SET usage_operation_count=usage_operation_count+1")
            await j._fresh_commit(conn, valid, effect["lease_until"])
            return UsageOperation(operation, binding)

    async def decision(self, op: UsageOperation, decision: dict[str, Any]):
        # Authenticated Core adapter only; record exact decision so retry never reprices.
        if type(decision) is dict and decision.get("allowed") is False:
            raise commercial_denial(decision)
        if (
            decision.get("allowed") is not True
            or decision.get("policy_version") != "sandbox-v1"
            or decision.get("sandbox") is not True
            or decision.get("max_micro_coins") != op.binding["max_micro_coins"]
            or decision.get("max_tokens") != op.binding["max_tokens"]
        ):
            raise JournalError("USAGE_COMMERCIAL_DENIED")
        uuid(decision.get("decision_id"))
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            row = await self._operation(conn, op)
            if row["decision"] is not None and json.loads(row["decision"]) != decision:
                raise JournalError("USAGE_DECISION_CONFLICT")
            await conn.execute(
                "UPDATE maestro_journal.provider_operations SET decision=$2::jsonb WHERE provider_operation_id=$1",
                UUID(op.provider_operation_id),
                canonical(decision).decode(),
            )

    async def _operation(self, conn, op: UsageOperation):
        row = await conn.fetchrow(
            "SELECT * FROM maestro_journal.provider_operations WHERE provider_operation_id=$1 FOR UPDATE",
            UUID(uuid(op.provider_operation_id)),
        )
        if not row or json.loads(row["binding"]) != op.binding:
            raise JournalError("USAGE_BINDING_CONFLICT")
        return row

    def _bound_effect(self, row, effect):
        if any(row[key] != effect[key] for key in ["delivery_id", "phase_id", "operation_id"]):
            raise JournalError("USAGE_BINDING_CONFLICT")

    async def begin(self, intent, claim, authority, op, finance):
        # Financial start has own current auth, then Maestro begin rechecks current execution.
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            row = await self._operation(conn, op)
            decision = json.loads(row["decision"]) if row["decision"] else None
            if not decision or row["state"] != "prepared":
                raise JournalError("USAGE_COMMERCIAL_UNCONFIRMED")
        started = await finance.call("start", {"decision_id": decision["decision_id"]})
        if started.get("state") != "started":
            raise JournalError("USAGE_COMMERCIAL_DENIED")
        j = self.journal
        valid = await j._fresh(intent.scope, intent.identity.delivery_id, intent.identity.phase_id, authority)
        async with j.db.transaction() as conn:
            await self._ready(conn)
            phase = await j._phase(conn, intent.scope, intent.identity.delivery_id, intent.identity.phase_id)
            effect = await j._cas(conn, intent, claim)
            row = await self._operation(conn, op)
            self._bound_effect(row, effect)
            if (
                not phase
                or phase["state"] != "running"
                or str(phase["execution_id"]) != authority.execution_id
                or phase["maestro_fence"] != decimal(authority.maestro_fence)
                or row["state"] != "prepared"
                or effect["status"] != "pending"
                or str(effect["execution_id"]) != authority.execution_id
                or effect["maestro_fence"] != decimal(authority.maestro_fence)
            ):
                raise JournalError("JOURNAL_STALE_WRITER")
            updated = await conn.fetchrow(
                "UPDATE maestro_journal.effects SET invocation_started=true,revision=revision+1 WHERE delivery_id=$1 AND "
                "phase_id=$2 AND operation_id=$3 RETURNING *",
                effect["delivery_id"],
                effect["phase_id"],
                effect["operation_id"],
            )
            await conn.execute(
                "UPDATE maestro_journal.provider_operations SET state='started' WHERE provider_operation_id=$1",
                UUID(op.provider_operation_id),
            )
            await j._project(conn, updated, intent)
            await j._fresh_commit(conn, valid, phase["lease_until"], effect["lease_until"])
            return j._claim(updated)

    def fact(self, op, decision, measurement):
        binding = {key: value for key, value in op.binding.items() if key not in {"max_micro_coins", "max_tokens", "debit_owner"}}
        if set(measurement) != {
            "provider_request_ref",
            "measurement",
            "provider_outcome",
            "units",
            "reasoning_tokens",
            "prompt_tokens_total",
        }:
            raise JournalError("USAGE_FACT_INVALID")
        value = {
            **binding,
            **measurement,
            "format_version": "maestro.usage.v1",
            "usage_id": f"maestro:v1:usage:{binding['organization_id']}:{op.provider_operation_id}",
            "billing_organization_id": op.binding["organization_id"],
            "commercial_decision_id": decision["decision_id"],
            "policy_version": decision["policy_version"],
        }
        value["content_sha256"] = usage_hash(value)
        return parse_usage(value)

    async def finish(self, intent, claim, op, result: PrivateEffectResult | None, measurement, *, final_effect=True):
        """Fecha operação/fato/outbox/efeito atomicamente sob o claim vigente."""
        return await self._finish(intent, claim, op, result, measurement, final_effect=final_effect)

    async def account_late_result(
        self, intent, op, *, expected_revision: str, evidence: ReconciliationEvidence, measurement, final_effect=True
    ):
        """Port operacional privado; adapter confiável verifica recibo no provider.

        Reusa o tipo de evidência do journal. Nenhuma capacidade de executar ação,
        heartbeat ou output é devolvida. Não disponível em rotas de handler.
        """
        if (
            not isinstance(evidence, ReconciliationEvidence)
            or evidence.outcome != "applied"
            or not isinstance(evidence.evidence, bytes)
            or not 1 <= len(evidence.evidence) <= 1024 * 1024
            or evidence.result is None
        ):
            raise JournalError("JOURNAL_EVIDENCE_INVALID")
        return await self._finish(
            intent,
            None,
            op,
            evidence.result,
            measurement,
            operational=True,
            expected_revision=expected_revision,
            final_effect=final_effect,
            evidence=evidence.evidence,
        )

    async def _finish(
        self, intent, claim, op, result, measurement, *, operational=False, expected_revision=None, final_effect=True, evidence=None
    ):
        """Op, resultado privado, fato e intenção de reporte no MESMO commit."""
        if result:
            decode(result.canonical_result), decode(result.provider_receipt)
        j = self.journal
        async with j.db.transaction() as conn:
            await self._ready(conn)
            effect = await j._effect(conn, intent) if operational else await j._cas(conn, intent, claim)
            row = await self._operation(conn, op)
            if effect:
                self._bound_effect(row, effect)
            decision = json.loads(row["decision"]) if row["decision"] else None
            if not effect or not decision or row["state"] not in {"started", "closed"} or not effect["invocation_started"]:
                raise JournalError("USAGE_STATE_INVALID")
            fact = self.fact(op, decision, measurement)
            if row["state"] == "closed":
                if json.loads(row["fact"]) != fact or (
                    result
                    and (
                        bytes(row["private_result"] or b"") != result.canonical_result
                        or bytes(row["private_receipt"] or b"") != result.provider_receipt
                    )
                ):
                    raise JournalError("USAGE_FACT_CONFLICT")
                return fact
            if operational and (expected_revision is None or effect["revision"] != decimal(expected_revision)):
                raise JournalError("JOURNAL_STALE_WRITER")
            if final_effect and await conn.fetchval(
                "SELECT count(*) FROM maestro_journal.provider_operations WHERE delivery_id=$1 AND phase_id=$2 AND "
                "operation_id=$3 AND provider_operation_id<>$4 AND state<>'closed'",
                effect["delivery_id"],
                effect["phase_id"],
                effect["operation_id"],
                UUID(op.provider_operation_id),
            ):
                raise JournalError("USAGE_OPERATION_PENDING")
            updated = await conn.fetchrow(
                "UPDATE maestro_journal.effects SET "
                "status=$4,result=$5,provider_receipt=$6,revision=revision+1,journal_fence=journal_fence+$7 WHERE "
                "delivery_id=$1 AND phase_id=$2 AND operation_id=$3 RETURNING *",
                effect["delivery_id"],
                effect["phase_id"],
                effect["operation_id"],
                "succeeded" if result and final_effect else "unknown" if result is None else "pending",
                result.canonical_result if result and final_effect else None,
                result.provider_receipt if result and final_effect else None,
                1 if operational else 0,
            )
            await conn.execute(
                "UPDATE maestro_journal.provider_operations SET "
                "state='closed',fact=$2::jsonb,private_result=$3,private_receipt=$4,reconciliation_evidence=$5 "
                "WHERE provider_operation_id=$1",
                UUID(op.provider_operation_id),
                canonical(fact).decode(),
                result.canonical_result if result else None,
                result.provider_receipt if result else None,
                evidence,
            )
            await conn.execute(
                "INSERT INTO maestro_journal.usage_outbox(usage_id,provider_operation_id,content_sha256,fact) VALUES($1,$2,$3,$4::jsonb)",
                fact["usage_id"],
                UUID(op.provider_operation_id),
                fact["content_sha256"],
                canonical(fact).decode(),
            )
            await j._project(conn, updated, intent)
            if not operational:
                await j._fresh_commit(conn, effect["lease_until"])
            return fact

    async def result(self, op):
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            row = await self._operation(conn, op)
            if row["state"] == "closed" and row["private_result"] is not None:
                return PrivateEffectResult(bytes(row["private_result"]), bytes(row["private_receipt"]))
            return None

    async def existing(self, intent, operation_slot, expected_parent=None, expected_reason="initial"):
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            await self.journal._effect(conn, intent)
            row = await conn.fetchrow(
                "SELECT * FROM maestro_journal.provider_operations WHERE delivery_id=$1 AND phase_id=$2 AND "
                "operation_id=$3 AND operation_slot=$4",
                UUID(intent.identity.delivery_id),
                intent.identity.phase_id,
                intent.identity.operation_id,
                operation_slot,
            )
            if row and (
                row["reason"] != expected_reason
                or (str(row["parent_provider_operation_id"]) if row["parent_provider_operation_id"] else None) != expected_parent
            ):
                raise JournalError("USAGE_BINDING_CONFLICT")
            return UsageOperation(str(row["provider_operation_id"]), json.loads(row["binding"])) if row else None

    async def next_claim(self, intent, old):
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            row = await self.journal._effect(conn, intent)
            if not row or str(row["claim_token"]) != old.claim_token or row["journal_fence"] != decimal(old.journal_fence):
                raise JournalError("JOURNAL_STALE_WRITER")
            return self.journal._claim(row)

    async def record_output_receipt(self, op: UsageOperation, receipt):
        # Caller supplies the callback-authenticated receipt. Schema alone is not
        # proof of commit: Maestro rechecks the canonical event/output at ingestion.
        from dooers.agents.server.protocol.maestro import MaestroContracts

        response = MaestroContracts().validate("Public.PersistedResponse", receipt)
        event = response["event"]
        if (
            event["type"] != "message.created"
            or event["thread_id"] != op.binding["thread_id"]
            or event["actor"].get("participant_id") != op.binding["recipient_participant_id"]
            or event["actor"].get("instance_agent_id") != op.binding["instance_agent_id"]
            or event.get("delivery_id") != op.binding["delivery_id"]
            or not re.fullmatch(r"output:(0|[1-9][0-9]{0,15})", event.get("output_id", ""))
        ):
            raise JournalError("USAGE_SCOPE_DENIED")
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            operation = await self._operation(conn, op)
            if operation["state"] != "closed":
                raise JournalError("USAGE_STATE_INVALID")
            fact = json.loads(operation["fact"])
            body = {
                "type": "usage_output_link",
                "usage_id": fact["usage_id"],
                "content_sha256": fact["content_sha256"],
                "event_id": event["event_id"],
                "output_id": event["output_id"],
                "relation": "contributed",
            }
            old = await conn.fetchrow(
                "SELECT body FROM maestro_journal.usage_output_links WHERE usage_id=$1 AND event_id=$2 FOR UPDATE",
                fact["usage_id"],
                UUID(event["event_id"]),
            )
            if old:
                if json.loads(old["body"]) != body:
                    raise JournalError("USAGE_FACT_CONFLICT")
                return body
            await conn.execute(
                "INSERT INTO maestro_journal.usage_output_links(usage_id,event_id,output_id,body) VALUES($1,$2,$3,$4::jsonb)",
                fact["usage_id"],
                UUID(event["event_id"]),
                event["output_id"],
                canonical(body).decode(),
            )
            await conn.execute("UPDATE maestro_journal.authority SET usage_link_count=usage_link_count+1")
            return body

    async def pending_links(self, limit):
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            rows = await conn.fetch(
                "SELECT body FROM maestro_journal.usage_output_links WHERE NOT acked ORDER BY usage_id,event_id LIMIT $1", limit
            )
            return [json.loads(row["body"]) for row in rows]

    async def acknowledge_link(self, body):
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            row = await conn.fetchrow(
                "SELECT body FROM maestro_journal.usage_output_links WHERE usage_id=$1 AND event_id=$2 FOR UPDATE",
                body["usage_id"],
                UUID(body["event_id"]),
            )
            if not row or json.loads(row["body"]) != body:
                raise JournalError("USAGE_FACT_CONFLICT")
            await conn.execute(
                "UPDATE maestro_journal.usage_output_links SET acked=true WHERE usage_id=$1 AND event_id=$2",
                body["usage_id"],
                UUID(body["event_id"]),
            )

    async def health(self):
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            rows = await conn.fetch(
                "SELECT state,count(*)::text count,max(extract(epoch FROM clock_timestamp()-created_at))::text "
                "oldest_seconds FROM maestro_journal.provider_operations GROUP BY state ORDER BY state"
            )
            return [dict(row) for row in rows]

    async def pending(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise JournalError("USAGE_LIMIT_INVALID")
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            rows = await conn.fetch(
                "SELECT fact FROM maestro_journal.usage_outbox WHERE NOT acked ORDER BY created_at,usage_id LIMIT $1", limit
            )
            return [parse_usage(json.loads(row["fact"])) for row in rows]

    async def acknowledge(self, fact):
        fact = parse_usage(fact)
        async with self.journal.db.transaction() as conn:
            await self._ready(conn)
            row = await conn.fetchrow("SELECT fact FROM maestro_journal.usage_outbox WHERE usage_id=$1 FOR UPDATE", fact["usage_id"])
            if not row or json.loads(row["fact"]) != fact:
                raise JournalError("USAGE_FACT_CONFLICT")
            await conn.execute("UPDATE maestro_journal.usage_outbox SET acked=true WHERE usage_id=$1", fact["usage_id"])


class LocalFinancialClient:
    """Transporte candidato Core sintético; credencial atual separada por operação.

    Resolver do operador permite rotação sem alterar IDs/fatos. Não usa cookie,
    proxy de ambiente, redirect ou capability Maestro. Default runtime não ativa.
    """

    def __init__(self, base_url: str, credential):
        u = httpx.URL(base_url)
        if (
            u.scheme != "http"
            or u.host != "127.0.0.1"
            or not u.port
            or u.userinfo
            or u.query
            or u.fragment
            or str(u) != base_url
            or u.path != "/api/v2/maestro/usage-sandbox/"
        ):
            raise JournalError("USAGE_CONFIGURATION_INVALID")
        if not callable(credential):
            raise JournalError("USAGE_CONFIGURATION_INVALID")
        self.base_url, self.credential = base_url, credential

    async def call(self, operation, body):
        if operation not in {"authorize", "start", "release", "report"}:
            raise JournalError("USAGE_CONFIGURATION_INVALID")
        token = self.credential(operation)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise JournalError("USAGE_CREDENTIAL_INVALID")
        try:
            async with asyncio.timeout(5), httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    self.base_url + operation,
                    json=body,
                    headers={"authorization": "Bearer " + token, "accept-encoding": "identity"},
                ) as response:
                    if (
                        response.status_code not in {200, 409}
                        or response.headers.get("content-type", "").split(";")[0] != "application/json"
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise JournalError("USAGE_UNAVAILABLE")
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        data.extend(chunk)
                        if len(data) > 32768:
                            raise JournalError("USAGE_UNAVAILABLE")
                    result = json.loads(data)
                    if type(result) is not dict:
                        raise JournalError("USAGE_UNAVAILABLE")
                    if response.status_code == 409:
                        if (
                            operation == "start"
                            and set(result) == {"error", "commercial_block"}
                            and result["error"] in {"decision_not_executable", "reconciliation_required"}
                        ):
                            raise commercial_denial(result["commercial_block"])
                        raise JournalError("USAGE_UNAVAILABLE")
                    return result
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise JournalError("USAGE_UNAVAILABLE") from None


@dataclass(frozen=True)
class UsageResult:
    provider_operation_id: str
    value: Any = field(repr=False)


async def run_maestro_usage(
    tool,
    *,
    ordinal: int,
    arguments: Any,
    finance: LocalFinancialClient,
    provider_id: str,
    provider_account_ref: str,
    model_id: str,
    max_micro_coins: str,
    max_tokens: str,
    operation_slot=0,
    parent_provider_operation_id=None,
    reason="initial",
    final_effect=True,
):
    """Chamada real deliberada = novo slot semântico; retry conserva slot.

    O tool é adapter confiável: execute(args, provider_operation_id, decision_id)
    retorna (PrivateEffectResult, medição estrita). Reconciliation é operacional,
    não faz nova chamada silenciosamente em uma execução de recuperação.
    """
    import asyncio

    from dooers.agents.server.protocol.maestro.identity import StableIdentity

    from .journal_runtime import MaestroEffectUnknown, _bound
    from .journal_store import make_intent

    current = _bound.get(asyncio.current_task())
    if current is None or not current.active or not current.claim:
        raise JournalError("JOURNAL_SCOPE_DENIED")
    identity = StableIdentity(
        current.delivery,
        current.phase,
        ordinal,
        f"output:{ordinal}",
        f"effect:{ordinal}",
        f"maestro:v1:effect:{current.delivery}:{current.phase}:{ordinal}",
    )
    args = tool.validate(arguments)
    # Config financeira também integra o hash privado do efeito; alterar catálogo/teto
    # no mesmo ordinal não abre uma operação com o mesmo slot e outra autorização.
    intent = make_intent(
        current.scope,
        identity,
        tool=tool.name,
        version=tool.version,
        account=tool.account,
        arguments={"arguments": args, "usage_workflow_version": 1},
        policy=tool.policy,
    )
    usage = JournalUsage(current.store)
    claims = getattr(current, "_usage_claims", {})
    prior = await usage.existing(intent, operation_slot, parent_provider_operation_id, reason)
    if prior:
        await current.store._fresh(current.scope, current.delivery, current.phase, current.authority)
        for key, value in {
            "provider_id": provider_id,
            "provider_account_ref": provider_account_ref,
            "model_id": model_id,
            "max_micro_coins": max_micro_coins,
            "max_tokens": max_tokens,
        }.items():
            if prior.binding[key] != value:
                raise JournalError("USAGE_BINDING_CONFLICT")
        saved = await usage.result(prior)
        if saved is not None:
            return UsageResult(prior.provider_operation_id, decode(saved.canonical_result))
    if identity.effect_key not in claims:
        reservation = await current.store.reserve(intent, current.authority)
        if reservation.decision == "reuse":
            raise JournalError("USAGE_EFFECT_CLOSED")
        if reservation.decision != "start" or not reservation.claim:
            raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE")
        claims[identity.effect_key] = reservation.claim
        current._usage_claims = claims
    claim = claims[identity.effect_key]
    op = await usage.prepare(
        intent,
        claim,
        current.authority,
        attempt=str(current.admission.envelope["attempt_number"]),
        provider_id=provider_id,
        provider_account_ref=provider_account_ref,
        model_id=model_id,
        max_micro_coins=max_micro_coins,
        max_tokens=max_tokens,
        operation_slot=operation_slot,
        parent_provider_operation_id=parent_provider_operation_id,
        reason=reason,
    )
    previous = await usage.result(op)
    if previous is not None:
        return UsageResult(op.provider_operation_id, decode(previous.canonical_result))
    decision = await finance.call("authorize", op.binding)
    await usage.decision(op, decision)
    claim = await usage.begin(intent, claim, current.authority, op, finance)
    try:
        result, measurement = await tool.execute(args, op.provider_operation_id, decision["decision_id"])
    except BaseException:
        try:
            await usage.finish(
                intent,
                claim,
                op,
                None,
                {
                    "provider_request_ref": None,
                    "measurement": "unknown",
                    "provider_outcome": "unknown",
                    "units": None,
                    "reasoning_tokens": None,
                    "prompt_tokens_total": None,
                },
            )
        except Exception:
            pass
        raise
    try:
        await usage.finish(intent, claim, op, result, measurement, final_effect=final_effect)
        claims[identity.effect_key] = await usage.next_claim(intent, claim)
    except Exception:
        # ACK privado perdido não concede custo novo nem perde output confirmado.
        previous = await usage.result(op)
        if previous != result:
            raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE") from None
        claims[identity.effect_key] = await usage.next_claim(intent, claim)
    return UsageResult(op.provider_operation_id, decode(result.canonical_result))


async def retransmit_usage(journal: JournalUsage, sink, *, limit=10):
    """O ACK do Maestro confirma inbox durável, não inventa recibo financeiro Core.

    Máximo 100 fatos/tick. Falha conserva backlog e termina o lote, sem busy retry.
    sink retorna identity/hash recebidos após COMMIT; nova credencial não muda fato.
    """
    confirmed = 0
    for fact in await journal.pending(limit):
        ack = await sink(fact)
        if ack != {"usage_id": fact["usage_id"], "content_sha256": fact["content_sha256"], "durable": True}:
            raise JournalError("USAGE_ACK_INVALID")
        await journal.acknowledge(fact)
        confirmed += 1
    for link in await journal.pending_links(max(0, limit - confirmed)):
        ack = await sink(link)
        if ack != {**link, "durable": True}:
            raise JournalError("USAGE_ACK_INVALID")
        await journal.acknowledge_link(link)
        confirmed += 1
    return confirmed


class LocalUsageSink:
    """Inbox Maestro candidata; bearer próprio, distinto da identidade financeira."""

    def __init__(self, endpoint, credential):
        u = httpx.URL(endpoint)
        if (
            u.scheme != "http"
            or u.host != "127.0.0.1"
            or not u.port
            or u.userinfo
            or u.query
            or u.fragment
            or str(u) != endpoint
            or u.path != "/internal/maestro/usage"
            or not callable(credential)
        ):
            raise JournalError("USAGE_CONFIGURATION_INVALID")
        self.endpoint, self.credential = endpoint, credential

    async def __call__(self, fact):
        if fact.get("type") != "usage_output_link":
            fact = parse_usage(fact)
        token = self.credential()
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise JournalError("USAGE_CREDENTIAL_INVALID")
        try:
            async with asyncio.timeout(5), httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "POST", self.endpoint, json=fact, headers={"authorization": "Bearer " + token, "accept-encoding": "identity"}
                ) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0] != "application/json"
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise JournalError("USAGE_UNAVAILABLE")
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        data.extend(chunk)
                        if len(data) > 32768:
                            raise JournalError("USAGE_UNAVAILABLE")
                    return json.loads(data)
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise JournalError("USAGE_UNAVAILABLE") from None
