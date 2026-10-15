"""Port privado do journal definido pela ADR-0005; persistência é E05-09.

Dataclasses são dados internos de adapter confiável, não schemas de request nem
comprovação de autorização. Nenhuma implementação em memória ou default existe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from .identity import StableIdentity

EffectStatus = Literal["pending", "succeeded", "unknown"]
EffectPolicy = Literal["provider_idempotent", "provider_reconcilable", "non_reconcilable"]


@dataclass(frozen=True, slots=True)
class JournalScope:
    organization_id: str
    workspace_id: str | None
    thread_id: str
    recipient_participant_id: str
    instance_agent_id: str


@dataclass(frozen=True, slots=True)
class EffectIntent:
    scope: JournalScope
    identity: StableIdentity
    # Nome/versão da ferramenta e conta de destino fazem parte do hash privado.
    tool_binding: bytes = field(repr=False)
    canonical_arguments: bytes = field(repr=False)
    arguments_sha256: str = field(repr=False)
    policy: EffectPolicy
    approval_binding: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    """Snapshot verificado externamente; store precisa verificar freshness/CAS."""

    execution_id: str
    runtime_id: str
    maestro_fence: str
    deadline_at: str


@dataclass(frozen=True, slots=True)
class EffectClaim:
    # journal_fence é exclusão privada; não substitui o fence do Maestro.
    journal_fence: str
    revision: str
    claim_token: str = field(repr=False)
    lease_until: str


@dataclass(frozen=True, slots=True)
class PrivateEffectResult:
    canonical_result: bytes = field(repr=False)
    provider_receipt: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class EffectRecord:
    scope: JournalScope
    identity: StableIdentity
    status: EffectStatus
    revision: str
    # True é persistido ANTES da chamada externa. Crash subsequente é incerto.
    invocation_started: bool
    policy: EffectPolicy
    result: PrivateEffectResult | None = field(default=None, repr=False)
    approval_binding: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class JournalReservation:
    decision: Literal["start", "reuse", "wait", "reconcile", "manual_review"]
    record: EffectRecord
    claim: EffectClaim | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    outcome: Literal["applied", "not_applied", "unknown"]
    # Evidência do adapter de provider confiável, nunca flag vinda do handler.
    evidence: bytes = field(repr=False)
    result: PrivateEffectResult | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class EffectProjection:
    """Somente metadata sanitizada; não pode autorizar chamada externa."""

    scope: JournalScope
    identity: StableIdentity
    revision: str
    status: EffectStatus
    receipt_id: str
    # HMAC persistido + versão; nunca SHA bruto de argumentos ou de resultado.
    intent_fingerprint: str
    fingerprint_key_version: str
    tombstone: bool = False


class EffectJournal(Protocol):
    """Autoridade única privada, compartilhada entre réplicas da instância.

    Toda mutação é CAS, incrementa revision e grava outbox de projeção na MESMA
    transação. Divergência de namespace/hash falha, sem alocar nova effect_key.
    Falta de linha/projeção após perda do journal não é prova de efeito ausente.
    """

    async def reserve(self, intent: EffectIntent, authority: ExecutionAuthority) -> JournalReservation:
        """Unique + intenção + claim privado atômicos; sem chamada ao provider."""
        ...

    async def begin(self, intent: EffectIntent, claim: EffectClaim, authority: ExecutionAuthority) -> EffectClaim:
        """Marca invocation_started sob CAS/freshness antes do efeito e renova revision."""
        ...

    async def record_result(self, intent: EffectIntent, claim: EffectClaim, result: PrivateEffectResult) -> EffectRecord:
        """Recibo confirmado é durável; não concede nova execução após cancelamento."""
        ...

    async def record_unknown(self, intent: EffectIntent, claim: EffectClaim) -> EffectRecord:
        """Perda de resposta/crash após begin nunca significa efeito não aplicado."""
        ...

    async def reconcile(
        self, intent: EffectIntent, claim: EffectClaim, evidence: ReconciliationEvidence, authority: ExecutionAuthority
    ) -> JournalReservation:
        """Consulta/idempotência do provider com claim exclusivo; unknown bloqueia."""
        ...

    async def read(self, scope: JournalScope, identity: StableIdentity) -> EffectRecord | None:
        """Só namespace exato; None não é autorização de repetição."""
        ...

    async def pending_projections(self, *, limit: int) -> tuple[EffectProjection, ...]:
        """Lê outbox durável com limite; nenhum dado privado deve sair daqui."""
        ...

    async def acknowledge_projection(self, projection: EffectProjection) -> None:
        """ACK somente da revision/hash enviada; não consome revisões posteriores."""
        ...


class EffectProjectionSink(Protocol):
    """Destino Maestro; transporte autenticado será fechado em E05-09."""

    async def apply(self, projection: EffectProjection) -> Literal["applied", "duplicate", "superseded"]:
        """CAS monotônico; mesmo número/conteúdo diferente conflita, tombstone final."""
        ...
