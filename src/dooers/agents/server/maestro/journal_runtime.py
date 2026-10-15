"""Composição durável: exclusão antes do handler e efeitos mediados por journal."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from weakref import WeakKeyDictionary

from dooers.agents.server.protocol.maestro.identity import StableIdentity
from dooers.agents.server.protocol.maestro.journal import ExecutionAuthority, JournalScope, PrivateEffectResult, ReconciliationEvidence

from .adapter import MaestroAdmission, MaestroAdmissionError
from .callbacks import MaestroCallbackClient
from .journal_store import JournalError, PostgresEffectJournal, canonical, decode, make_intent


@dataclass(frozen=True)
class MaestroEffectTool:
    """Adapter confiável configurado pelo operador, nunca parâmetros vindos do LLM.

    execute recebe argumentos já validados e a chave estável. reconcile só retorna
    not_applied após prova conclusiva do provider, nunca um timeout/404 eventual.
    """

    name: str
    version: str
    account: str
    policy: Literal["provider_idempotent", "provider_reconcilable", "non_reconcilable"]
    validate: Callable[[Any], Any] = field(repr=False)
    execute: Callable[[Any, str], Awaitable[PrivateEffectResult]] = field(repr=False)
    reconcile: Callable[[Any, str], Awaitable[ReconciliationEvidence]] | None = field(default=None, repr=False)


class MaestroEffectUnknown(MaestroAdmissionError):
    """Efeito indeterminado; execução corrente pode completar failed/manual_review."""

    retryable = False
    terminal_reason = "manual_review"


def invocation_fingerprint(envelope: dict[str, Any]) -> str:
    # Campos estáveis da fase; nunca attempt/fence/deadline/runtime/capability.
    value = {key: envelope[key] for key in ("delivery_id", "phase_id", "trigger_event_id", "trigger_sequence", "context_cutoff_sequence")}
    return hashlib.sha256(canonical(value)).hexdigest()


class JournalExecution:
    def __init__(self, store: PostgresEffectJournal, client: MaestroCallbackClient, admission: MaestroAdmission, retry_policy: str):
        self.client, self.admission, self.retry_policy = client, admission, retry_policy
        e = admission.envelope
        self.scope = JournalScope(
            e["organization_id"], e["workspace_id"], e["thread_id"], e["recipient_participant_id"], e["instance_agent_id"]
        )
        self.authority = ExecutionAuthority(e["execution_id"], e["runtime_id"], e["lease_fence"], e["deadline_at"])
        self.delivery, self.phase = e["delivery_id"], e["phase_id"]
        self.fingerprint = invocation_fingerprint(e)
        self.claim = None

        async def authorize(scope, delivery, phase, authority):
            if scope != self.scope or delivery != self.delivery or phase != self.phase or authority != self.authority:
                raise JournalError("JOURNAL_SCOPE_DENIED")
            from .journal_store import instant

            return instant((await client.heartbeat()).lease_until)

        self.store = PostgresEffectJournal(
            store.db,
            generation=store.generation,
            organization_id=store.organization_id,
            workspace_id=store.workspace_id,
            instance_agent_id=store.instance_agent_id,
            fingerprint_keys=store.keys,
            active_key_version=store.active_key_version,
            authorize=authorize,
            claim_seconds=store.claim_seconds,
            integrity_verified=store.integrity_verified,
        )
        self.active = True

    async def reserve(self):
        self.claim = await self.store.reserve_invocation(
            self.scope, self.delivery, self.phase, self.authority, context_fingerprint=self.fingerprint, retry_policy=self.retry_policy
        )
        await self.store.start_invocation(self.claim, self.authority)

    async def renew(self):
        if self.claim:
            await self.store.renew_invocation(self.claim, self.authority)

    async def confirm(self, result):
        if not result.confirmed or not self.claim:
            raise JournalError("JOURNAL_RESULT_UNCONFIRMED")
        await self.store.confirm_invocation(self.claim, result)

    async def effect(self, tool: MaestroEffectTool, ordinal: int, arguments: Any) -> Any:
        if not self.active or not self.claim or not isinstance(tool, MaestroEffectTool):
            raise JournalError("JOURNAL_STALE_WRITER")
        identity = StableIdentity(
            self.delivery,
            self.phase,
            ordinal,
            f"output:{ordinal}",
            f"effect:{ordinal}",
            f"maestro:v1:effect:{self.delivery}:{self.phase}:{ordinal}",
        )
        args = tool.validate(arguments)
        intent = make_intent(
            self.scope, identity, tool=tool.name, version=tool.version, account=tool.account, arguments=args, policy=tool.policy
        )
        return await self.effect_intent(tool, intent)

    async def effect_intent(self, tool: MaestroEffectTool, intent) -> Any:
        """Trusted infrastructure seam; E12-05 must supply the live consent gate.

        It never infers consent from a projection or a receipt. Not exposed as a
        handler approval API and not a capability announcement.
        """
        if not self.active or not self.claim or not isinstance(tool, MaestroEffectTool):
            raise JournalError("JOURNAL_STALE_WRITER")
        if intent.scope != self.scope or intent.identity.delivery_id != self.delivery or intent.identity.phase_id != self.phase:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        identity = intent.identity
        args = tool.validate(decode(intent.canonical_arguments))
        checked = make_intent(
            self.scope, identity, tool=tool.name, version=tool.version, account=tool.account, arguments=args, policy=tool.policy
        )
        if checked.arguments_sha256 != intent.arguments_sha256 or checked.policy != intent.policy:
            raise JournalError("JOURNAL_INTENT_CONFLICT")
        reservation = await self.store.reserve(intent, self.authority)
        if reservation.decision == "reconcile" and reservation.claim and tool.reconcile:
            try:
                evidence = await tool.reconcile(args, identity.effect_key)
                reservation = await self.store.reconcile(intent, reservation.claim, evidence, self.authority)
            except Exception:
                raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE") from None
        if reservation.decision == "reuse" and reservation.record.result:
            return decode(reservation.record.result.canonical_result)
        if reservation.decision != "start" or not reservation.claim:
            raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE")
        claim = await self.store.begin(intent, reservation.claim, self.authority)
        try:
            result = await tool.execute(args, identity.effect_key)
        except BaseException as error:
            # Cancelamento também pode ocorrer depois da ação externa. Não repetir.
            try:
                await self.store.record_unknown(intent, claim)
            except Exception:
                pass
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE") from None
        try:
            record = await self.store.record_result(intent, claim, result)
        except Exception:
            # Commit pode ter ocorrido sem ACK; consultar mesma intenção, nunca provider.
            record = await self.store.read(intent.scope, intent.identity)
            if (
                record is None
                or record.status != "succeeded"
                or record.result is None
                or record.approval_binding != intent.approval_binding
            ):
                raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE") from None
        if not record.result:
            raise MaestroEffectUnknown("DEPENDENCY_UNAVAILABLE")
        return decode(record.result.canonical_result)


_bound: WeakKeyDictionary[asyncio.Task, JournalExecution] = WeakKeyDictionary()


@contextmanager
def bind_effects(execution: JournalExecution):
    task = asyncio.current_task()
    if task is None or task in _bound:
        raise JournalError("JOURNAL_SCOPE_DENIED")
    _bound[task] = execution
    try:
        yield
    finally:
        execution.active = False
        _bound.pop(task, None)


async def run_maestro_effect(tool: MaestroEffectTool, *, ordinal: int, arguments: Any) -> Any:
    """Ordinal semântico explícito, estável em retries; não é ordem de ACKs."""
    current = _bound.get(asyncio.current_task())
    if current is None:
        raise JournalError("JOURNAL_SCOPE_DENIED")
    return await current.effect(tool, ordinal, arguments)


def create_durable_text_delivery_adapter(
    config,
    handler,
    private_resolver,
    *,
    journal: PostgresEffectJournal,
    checkpoints=None,
    approvals=None,
    options_resolver=None,
    retry_policy: Literal["unsafe", "journal_only"] = "unsafe",
    terminal_replay: bool = False,
):
    from dooers.agents.server.protocol.maestro import MaestroContracts

    from .adapter import MaestroDeliveryAdapter
    from .callbacks import MaestroHeartbeatGate
    from .executor import MaestroTextExecutor

    if not isinstance(journal, PostgresEffectJournal) or retry_policy not in {"unsafe", "journal_only"}:
        raise ValueError("MAESTRO_DURABLE_JOURNAL_REQUIRED")
    if (config.organization_id, config.workspace_id, config.instance_agent_id) != (
        journal.organization_id,
        journal.workspace_id,
        journal.instance_agent_id,
    ):
        raise ValueError("MAESTRO_DURABLE_JOURNAL_SCOPE")
    if checkpoints is not None:
        from .interactions import MaestroCheckpointStore

        if not isinstance(checkpoints, MaestroCheckpointStore) or checkpoints.journal is not journal:
            raise ValueError("MAESTRO_CHECKPOINT_JOURNAL_REQUIRED")
    if approvals is not None:
        from .approvals import MaestroApprovalRuntime

        if (
            not isinstance(approvals, MaestroApprovalRuntime)
            or approvals.journal is not journal
            or approvals.checkpoints is not checkpoints
        ):
            raise ValueError("MAESTRO_APPROVAL_JOURNAL_REQUIRED")
    if type(terminal_replay) is not bool:
        raise ValueError("MAESTRO_TERMINAL_REPLAY_CONFIGURATION")
    contracts = MaestroContracts()
    from .terminal_replay import MaestroDurableReplayGate

    return MaestroDeliveryAdapter(
        config,
        lease_gate=(
            MaestroDurableReplayGate(config, journal=journal, retry_policy=retry_policy, contracts=contracts)
            if terminal_replay
            else MaestroHeartbeatGate(config, contracts=contracts)
        ),
        executor=MaestroTextExecutor(
            config,
            handler,
            private_resolver,
            contracts=contracts,
            journal=journal,
            retry_policy=retry_policy,
            checkpoints=checkpoints,
            approvals=approvals,
            options_resolver=options_resolver,
        ),
    )
