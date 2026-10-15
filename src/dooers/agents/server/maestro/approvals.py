"""API candidata local: consentimento não substitui ACL, fence ou autorização comercial.

Somente sandbox.set_flag. O port comercial de ensaio é sintético, sem débito e
sem transformar tools em tokens LLM. Nenhum import/configuração legado o ativa.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .approval_journal import MaestroApprovalPlans, commitment, digest, project, validate_snapshot
from .callbacks import MaestroCallbackError
from .interactions import MaestroCheckpoint, MaestroCheckpointStore, private_context_bytes
from .journal_runtime import MaestroEffectTool, MaestroEffectUnknown, _bound
from .journal_store import JournalError, canonical, decode, instant

LOCAL_POLICY = "local.explicit-current-human.first-valid.v1"


_pending_reads: set[asyncio.Task] = set()


async def bounded_approval_read(operation, seconds):
    """Bound caller even if a trusted read port ignores cancellation.

    Only read preparation lives in this task; late bytes are discarded and cannot
    advance the gate to a journal mutation or effect. Physical I/O is cooperative.
    Retain at most 16 unfinished reads and consume all late errors.
    """
    if seconds <= 0 or not math.isfinite(seconds) or len(_pending_reads) >= 16:
        raise JournalError("APPROVAL_GATE_EXPIRED")
    task = asyncio.create_task(operation())
    _pending_reads.add(task)

    def finish(completed):
        _pending_reads.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finish)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            raise TimeoutError
        return task.result()
    finally:
        if not task.done():
            task.cancel()


def source_binding(e):
    return {
        **{k: e[k] for k in ("organization_id", "workspace_id", "thread_id", "delivery_id", "instance_agent_id")},
        "agent_participant_id": e["recipient_participant_id"],
        "source_execution_id": e["execution_id"],
        "source_phase_id": e["phase_id"],
        "source_fence": e["lease_fence"],
        "origin_event_id": e["trigger_event_id"],
        "origin_sequence": e["trigger_sequence"],
    }


def material(e, private, expires_at, eligible):
    from .approval_journal import identity_for, scope_for
    from .journal_store import make_intent

    r = {
        **deepcopy(private),
        "binding": source_binding(e),
        "expires_at": expires_at,
        "delivery_expires_at": e["delivery_expires_at"],
        "eligible_participant_ids": sorted(eligible),
        "policy_id": LOCAL_POLICY,
    }
    t = r["tool_binding"]
    r["effect_key"] = identity_for(r).effect_key
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
    return r


def validate_approval_suspend(value, envelope):
    """Closed local shape; immutable public definition reconstructed from private intent."""
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    try:
        if (
            set(value) != {"lease_fence", "output_id", "checkpoint_ref", "interaction", "approval"}
            or value["lease_fence"] != envelope["lease_fence"]
        ):
            raise ValueError
        p, i = value["approval"], value["interaction"]
        if set(p) != {"interaction_id", "tool_binding", "arguments", "private_nonce", "effect_ordinal"}:
            raise ValueError
        base = json.loads(Path(__file__).with_name("approval-candidate.schema.json").read_bytes())["Candidate.ToolApprovalSnapshot"][
            "properties"
        ]
        for k, v in p.items():
            Draft202012Validator(base[k], format_checker=FormatChecker()).validate(v)
        for k in ("expires_at", "eligible_participant_ids"):
            Draft202012Validator(base[k], format_checker=FormatChecker()).validate(i[k])
        if (
            len(set(i["eligible_participant_ids"])) != len(i["eligible_participant_ids"])
            or envelope["recipient_participant_id"] in i["eligible_participant_ids"]
        ):
            raise ValueError
        if instant(i["expires_at"]) > instant(envelope["delivery_expires_at"]) or instant(i["expires_at"]).timestamp() <= time.time():
            raise ValueError
        r = material(envelope, p, i["expires_at"], i["eligible_participant_ids"])
        if canonical(i) != canonical(project(r)["definition"]):
            raise ValueError
        import re

        if (
            not re.fullmatch(r"output:(0|[1-9][0-9]{0,15})", value["output_id"])
            or int(value["output_id"][7:]) > 9007199254740991
            or not re.fullmatch(r"checkpoint:v1:[0-9a-f-]{36}", value["checkpoint_ref"])
        ):
            raise ValueError
    except Exception:
        raise MaestroCallbackError("INVALID_REQUEST") from None


def validate_approval_context(value, e):
    """Machine-only canonical approval + checkpoint; never public history or a grant by itself."""
    try:
        if (
            set(value) != {"format_version", "resume", "continuity", "response_data", "form_event_id", "approval_snapshot"}
            or value["format_version"] != "maestro.approval-context.local.v1"
        ):
            raise ValueError
        r = validate_snapshot(value["approval_snapshot"])
        resume = value["resume"]
        if (
            (r["policy_id"], r["policy_status"]) != (LOCAL_POLICY, "confirmed_local_trial_only")
            or resume != e["resume"]
            or resume["kind"] != "tool_approval"
        ):
            raise ValueError
        if (
            resume["interaction_id"] != e["phase_id"]
            or resume["interaction_id"] != r["interaction_id"]
            or resume["response_sequence"] != e["context_cutoff_sequence"]
            or resume["resume_of_execution_id"] != r["binding"]["source_execution_id"]
            or resume["original_trigger_event_id"] != e["trigger_event_id"]
            or resume["checkpoint_ref"] != r["continuity"]["checkpoint_ref"]
        ):
            raise ValueError
        b = r["binding"]
        if (
            any(b[k] != e[k] for k in ("organization_id", "workspace_id", "thread_id", "delivery_id", "instance_agent_id"))
            or b["agent_participant_id"] != e["recipient_participant_id"]
            or b["origin_event_id"] != e["trigger_event_id"]
            or b["origin_sequence"] != e["trigger_sequence"]
        ):
            raise ValueError
        if (
            value["continuity"] != r["continuity"]
            or value["form_event_id"] != r["request_event_id"]
            or value["response_data"] != {"arguments_hash": r["arguments_hash"]}
        ):
            raise ValueError
        # Consent happened while pending. Approval expiry is not a new execution
        # deadline; resumed execution/delivery/current policy supply that authority.
        if instant(r["delivery_expires_at"]).timestamp() <= time.time():
            raise ValueError
        return deepcopy(value)
    except Exception:
        from .client import MaestroContextError

        raise MaestroContextError("CONTEXT_INVALID") from None


@dataclass(frozen=True)
class MaestroApprovalCommercialProof:
    """Trusted local test double only; no real pricing/unit/debit policy is implied."""

    binding: bytes = field(repr=False)
    decision_id: str
    valid_until: float
    assert_current: Callable[[], None] = field(repr=False)
    sandbox: bool = True
    policy_version: str = "fixture.tool-sandbox.no-debit.v1"


@dataclass(frozen=True)
class MaestroApprovalOutcome:
    interaction_id: str
    decision_event_id: str
    arguments_hash: str
    effect_key: str
    receipt_id: str
    state: str = "succeeded"
    # Never expose raw provider result/receipt or nonce as public output.


class MaestroApprovalRuntime:
    """Explicit trusted composition, disabled without a current scope capability.

    commercial resolves an exact operation binding and returns a short-lived
    synthetic proof; it must not accept browser grants or approval as payment.
    """

    def __init__(
        self,
        journal,
        checkpoints,
        *,
        capability: Callable[[Any], bool],
        commercial: Callable[[dict], Awaitable[MaestroApprovalCommercialProof]],
    ):
        if (
            not isinstance(checkpoints, MaestroCheckpointStore)
            or checkpoints.journal is not journal
            or not callable(capability)
            or not callable(commercial)
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.journal, self.checkpoints = journal, checkpoints
        self.plans = MaestroApprovalPlans(journal, checkpoints=checkpoints)
        self.capability, self.commercial = capability, commercial

    def current(self, execution):
        if (
            not execution
            or not execution.active
            or not execution.claim
            or execution.store.db is not self.journal.db
            or self.capability(execution.scope) is not True
        ):
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        remaining = execution.client._check_current()
        if type(remaining) not in {int, float} or not math.isfinite(remaining) or remaining <= 0:
            raise JournalError("APPROVAL_GATE_EXPIRED")

    def request(self, execution, tool, *, arguments, checkpoint, expires_at, eligible_participant_ids, ordinal):
        self.current(execution)
        if (
            "delivery.suspend" not in execution.client._claims["operations"]
            or not isinstance(tool, MaestroEffectTool)
            or not isinstance(checkpoint, MaestroCheckpoint)
        ):
            raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
        if "approval" in checkpoint.state:
            raise MaestroCallbackError("INVALID_REQUEST")
        private = {
            "interaction_id": str(uuid4()),
            "tool_binding": {"format": 1, "tool": tool.name, "version": tool.version, "account": tool.account},
            "arguments": tool.validate(deepcopy(arguments)),
            "private_nonce": secrets.token_hex(32),
            "effect_ordinal": ordinal,
        }
        r = material(execution.admission.envelope, private, expires_at, eligible_participant_ids)
        interaction = project(r)["definition"]
        validate_approval_suspend(
            {
                "lease_fence": execution.authority.maestro_fence,
                "output_id": "output:0",
                "checkpoint_ref": "checkpoint:v1:00000000-0000-4000-8000-000000000000",
                "interaction": interaction,
                "approval": private,
            },
            execution.admission.envelope,
        )
        prepared = MaestroCheckpoint(checkpoint.schema, checkpoint.version, {**deepcopy(checkpoint.state), "approval": private})
        self.checkpoints._state(prepared)
        data = {"interaction": interaction, "approval": private, "checkpoint": prepared}
        execution._approval_requests = getattr(execution, "_approval_requests", {})
        execution._approval_requests[id(data)] = digest(
            {
                "interaction": interaction,
                "approval": private,
                "checkpoint_state": prepared.state,
                "schema": prepared.schema,
                "version": prepared.version,
            }
        )
        return data

    def fresh(self, execution, deadline):
        self.current(execution)
        if not math.isfinite(deadline) or time.monotonic() >= deadline:
            raise JournalError("APPROVAL_GATE_EXPIRED")

    async def gate(self, execution, expected, tool, *, deadline=None):
        if deadline is None:
            started = time.monotonic()
            self.current(execution)
            deadline = started + min(10.0, execution.client._check_current(), execution.client.config.network_timeout_seconds)
        self.fresh(execution, deadline)
        r = expected["approval_snapshot"]
        if (tool.name, tool.version, tool.account) != tuple(r["tool_binding"][k] for k in ("tool", "version", "account")) or canonical(
            tool.validate(deepcopy(r["arguments"]))
        ) != canonical(r["arguments"]):
            raise JournalError("JOURNAL_INTENT_CONFLICT")
        binding = {
            "scope": r["binding"],
            "approval_id": r["interaction_id"],
            "decision_event_id": expected["resume"]["response_event_id"],
            "arguments_hash": r["arguments_hash"],
            "effect_key": r["effect_key"],
            "tool_binding": r["tool_binding"],
            "execution_id": execution.authority.execution_id,
            "fence": execution.authority.maestro_fence,
        }
        self.fresh(execution, deadline)
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            proof = await bounded_approval_read(lambda: self.commercial(deepcopy(binding)), deadline - time.monotonic())
            self.fresh(execution, deadline)
            self._proof(proof, binding)
            fresh = await bounded_approval_read(lambda: execution.client.interaction_context(approval=True), deadline - time.monotonic())
            self.fresh(execution, deadline)
            self._proof(proof, binding)
            if private_context_bytes(fresh) != private_context_bytes(expected):
                raise JournalError("JOURNAL_INTENT_CONFLICT")
        if time.monotonic() >= deadline:
            raise JournalError("APPROVAL_GATE_EXPIRED")
        # CPU validation and callback returns cannot extend the aggregate budget.
        self.fresh(execution, deadline)
        self._proof(proof, binding)
        self.fresh(execution, deadline)
        return proof

    def _proof(self, proof, binding):
        from .journal_store import uuid

        if (
            not isinstance(proof, MaestroApprovalCommercialProof)
            or proof.sandbox is not True
            or proof.policy_version != "fixture.tool-sandbox.no-debit.v1"
            or proof.binding != canonical(binding)
            or not math.isfinite(proof.valid_until)
            or time.time() >= proof.valid_until
            or not callable(proof.assert_current)
        ):
            raise JournalError("APPROVAL_COMMERCIAL_DENIED")
        uuid(proof.decision_id)
        if proof.assert_current() is not None or time.time() >= proof.valid_until:
            raise JournalError("APPROVAL_COMMERCIAL_DENIED")

    async def run(self, execution, tool, *, arguments):
        """Current approved phase only. Recover authoritative receipt before any repeat."""
        started = time.monotonic()
        self.current(execution)
        deadline = started + min(10.0, execution.client._check_current(), execution.client.config.network_timeout_seconds)
        self.fresh(execution, deadline)
        if not isinstance(tool, MaestroEffectTool):
            raise JournalError("JOURNAL_INTENT_CONFLICT")
        raw_arguments = canonical(tool.validate(deepcopy(arguments)))
        self.fresh(execution, deadline)
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            context = await bounded_approval_read(lambda: execution.client.interaction_context(approval=True), deadline - time.monotonic())
            self.fresh(execution, deadline)
            if raw_arguments != canonical(context["approval_snapshot"]["arguments"]):
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            await self.gate(execution, context, tool, deadline=deadline)
            self.fresh(execution, deadline)
            plan = await self.plans.adopt(execution, context)
            await self.gate(execution, context, tool, deadline=deadline)
            self.fresh(execution, deadline)
            intent = await self.plans.bind(
                execution,
                plan["checkpoint_ref"],
                plan["snapshot_sha256"],
                decision_event_id=context["resume"]["response_event_id"],
                policy=tool.policy,
            )
            await self.gate(execution, context, tool, deadline=deadline)
            self.fresh(execution, deadline)
            reservation = await execution.store.reserve(intent, execution.authority)
            await self.gate(execution, context, tool, deadline=deadline)
            if reservation.decision == "reconcile" and reservation.claim and tool.reconcile:
                evidence = await bounded_approval_read(
                    lambda: tool.reconcile(decode(raw_arguments), intent.identity.effect_key), deadline - time.monotonic()
                )
                await self.gate(execution, context, tool, deadline=deadline)
                self.fresh(execution, deadline)
                reservation = await execution.store.reconcile(intent, reservation.claim, evidence, execution.authority)
                await self.gate(execution, context, tool, deadline=deadline)
            if reservation.decision == "reuse" and reservation.record.result:
                return await self.outcome(execution, context, tool, intent, deadline=deadline)
            if reservation.decision != "start" or not reservation.claim:
                raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE")
            self.fresh(execution, deadline)
            claim = await execution.store.begin(intent, reservation.claim, execution.authority)
            try:
                await self.gate(execution, context, tool, deadline=deadline)
            except BaseException:
                # begin precedes the call. A process crash here is conservatively
                # uncertain; never infer a third-party rollback from cancellation.
                try:
                    await execution.store.record_unknown(intent, claim)
                except Exception:
                    pass
                raise
        try:
            self.fresh(execution, deadline)
            # No await between final current gate and invoking the trusted adapter.
            if time.monotonic() >= deadline:
                raise JournalError("APPROVAL_GATE_EXPIRED")
            result = await tool.execute(decode(raw_arguments), intent.identity.effect_key)
        except BaseException as error:
            try:
                await execution.store.record_unknown(intent, claim)
            except Exception:
                pass
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE") from None
        try:
            record = await execution.store.record_result(intent, claim, result)
        except Exception:
            record = await execution.store.read(intent.scope, intent.identity)
            if not record or record.status != "succeeded" or record.result is None or record.approval_binding != intent.approval_binding:
                raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE") from None
        return await self.outcome(execution, context, tool, intent)

    async def outcome(self, execution, context, tool, intent, *, deadline=None):
        from .approval_journal import private_approval_correlation

        # Receipt persistence after a provider call remains operational accounting,
        # not permission for another effect. Reuse stays within its original budget.
        if deadline is None:
            deadline = time.monotonic() + min(10.0, execution.client._check_current(), execution.client.config.network_timeout_seconds)
        self.fresh(execution, deadline)
        correlation = await bounded_approval_read(
            lambda: private_approval_correlation(execution.store, intent), deadline - time.monotonic()
        )
        await self.gate(execution, context, tool, deadline=deadline)
        result = MaestroApprovalOutcome(
            context["resume"]["interaction_id"],
            context["resume"]["response_event_id"],
            context["approval_snapshot"]["arguments_hash"],
            intent.identity.effect_key,
            correlation["receipt_id"],
        )
        execution._approval_outcomes = getattr(execution, "_approval_outcomes", {})
        execution._approval_outcomes[id(result)] = (result, context, tool, intent)
        return result


async def run_maestro_approved_effect(tool: MaestroEffectTool, *, arguments: Any) -> MaestroApprovalOutcome:
    current = _bound.get(asyncio.current_task())
    runtime = getattr(current, "approvals", None)
    if not isinstance(runtime, MaestroApprovalRuntime):
        raise MaestroCallbackError("UNSUPPORTED_CAPABILITY")
    return await runtime.run(current, tool, arguments=arguments)


class MaestroApprovalOutcomeReader:
    """Restricted operational read of the EXISTING authority. No execution grant."""

    def __init__(self, journal):
        from .journal_store import PostgresEffectJournal

        if not isinstance(journal, PostgresEffectJournal):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.journal = journal

    @staticmethod
    def validate(body):
        import re

        from dooers.agents.server.protocol.maestro.journal import JournalScope

        from .journal_store import scope_data, uuid

        if type(body) is not dict or set(body) != {
            "scope",
            "delivery_id",
            "approval_id",
            "decision_event_id",
            "arguments_hash",
            "effect_key",
            "tool_binding",
            "intent_sha256",
            "checkpoint_ref",
            "snapshot_sha256",
        }:
            raise JournalError("JOURNAL_BINDING_INVALID")
        scope_data(JournalScope(**body["scope"]))
        for k in ("delivery_id", "approval_id", "decision_event_id"):
            uuid(body[k])
        for k in ("arguments_hash", "intent_sha256", "snapshot_sha256"):
            if not isinstance(body[k], str) or not re.fullmatch(r"[a-f0-9]{64}", body[k]):
                raise JournalError("JOURNAL_BINDING_INVALID")
        if not isinstance(body["checkpoint_ref"], str) or not re.fullmatch(r"checkpoint:v1:[0-9a-f-]{36}", body["checkpoint_ref"]):
            raise JournalError("JOURNAL_BINDING_INVALID")
        prefix = f"maestro:v1:effect:{body['delivery_id']}:{body['approval_id']}:"
        key = body["effect_key"]
        ordinal = key[len(prefix) :] if isinstance(key, str) and key.startswith(prefix) else ""
        if not re.fullmatch(r"0|[1-9][0-9]{0,15}", ordinal) or int(ordinal) > 9007199254740991:
            raise JournalError("JOURNAL_BINDING_INVALID")
        tool = body["tool_binding"]
        if (
            type(tool) is not dict
            or set(tool) != {"format", "tool", "version", "account"}
            or type(tool["format"]) is not int
            or tool["format"] != 1
            or tool["tool"] != "sandbox.set_flag"
            or tool["version"] != "1"
        ):
            raise JournalError("JOURNAL_BINDING_INVALID")
        uuid(tool["account"])
        canonical(body)

    async def read(self, body):
        from dataclasses import replace

        from .approval_journal import identity_for, scope_for, validate_plan_descriptor, verify_continuity
        from .journal_store import make_intent, scope_data

        self.validate(body)
        j = self.journal
        if (body["scope"]["organization_id"], body["scope"]["workspace_id"], body["scope"]["instance_agent_id"]) != (
            j.organization_id,
            j.workspace_id,
            j.instance_agent_id,
        ):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        async with j.db.transaction() as conn:
            # Operational readers hold SELECT only. One repeatable, read-only snapshot
            # keeps authority inventories, continuity and receipt/fingerprint coherent.
            # All executor/CAS callers retain the default FOR UPDATE locks.
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await j._ready(conn, for_update=False)
            row = await conn.fetchrow("SELECT * FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", body["checkpoint_ref"])
            if row is None:
                return {"generation": j.generation, "binding": deepcopy(body), "status": "unobserved", "revision": None}
            if str(row["generation"]) != j.generation:
                raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
            import json

            r = json.loads(row["state"])
            validate_snapshot(r)
            validate_plan_descriptor(row, body["checkpoint_ref"], r)
            await verify_continuity(conn, j, r)
            if (
                r["policy_id"] != LOCAL_POLICY
                or digest(r) != body["snapshot_sha256"]
                or r["interaction_id"] != body["approval_id"]
                or r["binding"]["delivery_id"] != body["delivery_id"]
                or scope_data(scope_for(r)) != body["scope"]
                or any(r[k] != body[k] for k in ("arguments_hash", "effect_key", "tool_binding", "intent_sha256"))
            ):
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            identity = identity_for(r)
            # read() verifies the current fingerprint/HMAC, immutable namespace and
            # corruption guard even though caller receives only safe status metadata.
            from uuid import UUID

            header = await conn.fetchrow(
                "SELECT policy,approval_binding FROM maestro_journal.effects WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3",
                UUID(identity.delivery_id),
                identity.phase_id,
                identity.operation_id,
            )
            if header is None:
                return {"generation": j.generation, "binding": deepcopy(body), "status": "unobserved", "revision": None}
            intent = replace(
                make_intent(
                    scope_for(r),
                    identity,
                    tool=r["tool_binding"]["tool"],
                    version=r["tool_binding"]["version"],
                    account=r["tool_binding"]["account"],
                    arguments=r["arguments"],
                    policy=header["policy"],
                ),
                approval_binding=bytes(header["approval_binding"]) if header["approval_binding"] else None,
            )
            effect = await j._effect(conn, intent, for_update=False)
            if not effect:
                return {"generation": j.generation, "binding": deepcopy(body), "status": "unobserved", "revision": None}
            record = j._record(effect, intent)
            binding = decode(record.approval_binding) if record.approval_binding else None
            if not binding or binding["checkpoint_ref"] != body["checkpoint_ref"] or binding["snapshot_sha256"] != body["snapshot_sha256"]:
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            b = binding["binding"]
            if (
                any(b[k] != body[k] for k in ("decision_event_id", "arguments_hash", "effect_key", "intent_sha256"))
                or b["approval_id"] != body["approval_id"]
                or b["phase_id"] != body["approval_id"]
                or b["scope"] != body["scope"]
                or b["delivery_id"] != body["delivery_id"]
                or b["operation_id"] != identity.operation_id
            ):
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            status = record.status
            if status == "succeeded" and record.result is None:
                raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
            if status == "unknown" and record.policy == "non_reconcilable":
                status = "manual_review"
            return {"generation": j.generation, "binding": deepcopy(body), "status": status, "revision": record.revision}
