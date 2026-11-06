"""Autoridade PostgreSQL privada compartilhada por todas as réplicas da instância.

Não inicializa silenciosamente um banco vazio. Provisionamento é explícito; o
operador conserva generation fora do banco e bloqueia a autoridade ao restaurar.
Nenhuma transação inclui rede. Os ports de autorização/provider são confiáveis.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from dooers.agents.server.database import SqlDatabase
from dooers.agents.server.protocol.maestro.identity import StableIdentity
from dooers.agents.server.protocol.maestro.journal import (
    EffectClaim,
    EffectIntent,
    EffectProjection,
    EffectRecord,
    ExecutionAuthority,
    JournalReservation,
    JournalScope,
    PrivateEffectResult,
    ReconciliationEvidence,
)

DDL = """
CREATE SCHEMA IF NOT EXISTS maestro_journal;
REVOKE ALL ON SCHEMA maestro_journal FROM PUBLIC;
CREATE TABLE IF NOT EXISTS maestro_journal.authority (
 id boolean PRIMARY KEY DEFAULT true CHECK(id), generation uuid NOT NULL,
 organization_id uuid NOT NULL, workspace_id uuid, instance_agent_id uuid NOT NULL,
 integrity text NOT NULL CHECK(integrity IN ('ready','blocked')),
 effect_count bigint NOT NULL DEFAULT 0, invocation_count bigint NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS maestro_journal.phases (
 delivery_id uuid NOT NULL, phase_id text NOT NULL, namespace jsonb NOT NULL,
 context_fingerprint text NOT NULL, retry_policy text NOT NULL CHECK(retry_policy IN ('unsafe','journal_only')),
 execution_id uuid NOT NULL, maestro_fence bigint NOT NULL CHECK(maestro_fence>0),
 private_fence bigint NOT NULL DEFAULT 1, claim_token uuid NOT NULL, lease_until timestamptz NOT NULL,
 state text NOT NULL CHECK(state IN ('reserved','running','confirmed','retry_ready','manual_review')),
 PRIMARY KEY(delivery_id,phase_id));
CREATE TABLE IF NOT EXISTS maestro_journal.invocations (
 execution_id uuid PRIMARY KEY, delivery_id uuid NOT NULL, phase_id text NOT NULL,
 maestro_fence bigint NOT NULL, state text NOT NULL, result jsonb,
 FOREIGN KEY(delivery_id,phase_id) REFERENCES maestro_journal.phases(delivery_id,phase_id));
CREATE TABLE IF NOT EXISTS maestro_journal.effects (
 delivery_id uuid NOT NULL, phase_id text NOT NULL, operation_id text NOT NULL,
 namespace jsonb NOT NULL, intent_hash text NOT NULL, binding bytea NOT NULL, arguments bytea NOT NULL,
 approval_binding bytea,
 policy text NOT NULL CHECK(policy IN ('provider_idempotent','provider_reconcilable','non_reconcilable')),
 status text NOT NULL CHECK(status IN ('pending','succeeded','unknown')),
 invocation_started boolean NOT NULL DEFAULT false, revision bigint NOT NULL DEFAULT 1,
 journal_fence bigint NOT NULL DEFAULT 1, claim_token uuid NOT NULL, lease_until timestamptz NOT NULL,
 execution_id uuid NOT NULL, maestro_fence bigint NOT NULL,
 result bytea, provider_receipt bytea, receipt_id uuid NOT NULL,
 fingerprint text NOT NULL, fingerprint_key_version text NOT NULL, tombstone boolean NOT NULL DEFAULT false,
 PRIMARY KEY(delivery_id,phase_id,operation_id),
 FOREIGN KEY(delivery_id,phase_id) REFERENCES maestro_journal.phases(delivery_id,phase_id),
 CHECK((status='succeeded')=(result IS NOT NULL AND provider_receipt IS NOT NULL)));
CREATE TABLE IF NOT EXISTS maestro_journal.outbox (
 delivery_id uuid NOT NULL, phase_id text NOT NULL, operation_id text NOT NULL,
 revision bigint NOT NULL, body jsonb NOT NULL, acked boolean NOT NULL DEFAULT false,
 PRIMARY KEY(delivery_id,phase_id,operation_id,revision),
 FOREIGN KEY(delivery_id,phase_id,operation_id) REFERENCES maestro_journal.effects(delivery_id,phase_id,operation_id));
CREATE OR REPLACE FUNCTION maestro_journal.effect_approval_immutable_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path=maestro_journal,pg_catalog,pg_temp AS $$
BEGIN
 IF OLD.approval_binding IS DISTINCT FROM NEW.approval_binding THEN
  RAISE EXCEPTION 'effect approval binding immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='maestro_journal.effects'::regclass AND tgname='effect_approval_immutable') THEN
  CREATE TRIGGER effect_approval_immutable BEFORE UPDATE ON maestro_journal.effects
   FOR EACH ROW EXECUTE FUNCTION maestro_journal.effect_approval_immutable_guard();
 END IF;
END $$;
"""

APPROVAL_EXTENSION_DDL = """
ALTER TABLE maestro_journal.effects ADD COLUMN IF NOT EXISTS approval_binding bytea;
CREATE OR REPLACE FUNCTION maestro_journal.effect_approval_immutable_guard() RETURNS trigger LANGUAGE plpgsql
SET search_path=maestro_journal,pg_catalog,pg_temp AS $$
BEGIN
 IF OLD.approval_binding IS DISTINCT FROM NEW.approval_binding THEN
  RAISE EXCEPTION 'effect approval binding immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='maestro_journal.effects'::regclass AND tgname='effect_approval_immutable') THEN
  CREATE TRIGGER effect_approval_immutable BEFORE UPDATE ON maestro_journal.effects
   FOR EACH ROW EXECUTE FUNCTION maestro_journal.effect_approval_immutable_guard();
 END IF;
END $$;
"""


class JournalError(Exception):
    def __init__(self, code: str = "JOURNAL_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


def uuid(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise JournalError("JOURNAL_BINDING_INVALID")
    return value


def decimal(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 9223372036854775807:
        raise JournalError("JOURNAL_BINDING_INVALID")
    return int(value)


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise JournalError("JOURNAL_BINDING_INVALID")
    return result.astimezone(UTC)


def canonical(value: Any) -> bytes:
    """Formato fechado privado v1: JSON sem floats, chaves string, inteiros seguros.

    Não é JCS. Recusa ambiguidades, NaN, surrogate e tipos customizados. Números
    decimais financeiros devem ser strings com semântica validada pelo provider.
    """
    nodes = 0

    def check(item: Any, depth: int = 0) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 10000:
            raise JournalError("JOURNAL_PAYLOAD_INVALID")
        if item is None or type(item) is bool:
            return
        if type(item) is int and abs(item) <= 9007199254740991:
            return
        if type(item) is str:
            item.encode("utf-8", "strict")
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                check(key, depth + 1)
                check(child, depth + 1)
            return
        raise JournalError("JOURNAL_PAYLOAD_INVALID")

    try:
        check(value)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > 1024 * 1024:
            raise JournalError("JOURNAL_PAYLOAD_INVALID")
        return raw
    except (UnicodeError, ValueError, TypeError):
        raise JournalError("JOURNAL_PAYLOAD_INVALID") from None


def decode(raw: bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise JournalError("JOURNAL_PAYLOAD_INVALID")
            result[key] = value
        return result

    try:
        result = json.loads(raw, object_pairs_hook=unique)
        if canonical(result) != raw:
            raise JournalError("JOURNAL_PAYLOAD_INVALID")
        return result
    except (UnicodeError, ValueError, TypeError):
        raise JournalError("JOURNAL_PAYLOAD_INVALID") from None


def scope_data(scope: JournalScope) -> dict[str, Any]:
    if not isinstance(scope, JournalScope):
        raise JournalError("JOURNAL_BINDING_INVALID")
    return {
        key: uuid(value) if value is not None else None
        for key, value in (
            ("organization_id", scope.organization_id),
            ("workspace_id", scope.workspace_id),
            ("thread_id", scope.thread_id),
            ("recipient_participant_id", scope.recipient_participant_id),
            ("instance_agent_id", scope.instance_agent_id),
        )
    }


def validate_identity(identity: StableIdentity) -> None:
    uuid(identity.delivery_id)
    if identity.phase_id != "initial":
        uuid(identity.phase_id)
    if type(identity.ordinal) is not int or not 0 <= identity.ordinal <= 9007199254740991:
        raise JournalError("JOURNAL_BINDING_INVALID")
    if (
        identity.output_id != f"output:{identity.ordinal}"
        or identity.operation_id != f"effect:{identity.ordinal}"
        or identity.effect_key != f"maestro:v1:effect:{identity.delivery_id}:{identity.phase_id}:{identity.ordinal}"
    ):
        raise JournalError("JOURNAL_BINDING_INVALID")


def make_intent(
    scope: JournalScope, identity: StableIdentity, *, tool: str, version: str, account: str, arguments: Any, policy: str
) -> EffectIntent:
    scope_data(scope)
    validate_identity(identity)
    if policy not in {"provider_idempotent", "provider_reconcilable", "non_reconcilable"}:
        raise JournalError("JOURNAL_POLICY_INVALID")
    if any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in (tool, version, account)):
        raise JournalError("JOURNAL_BINDING_INVALID")
    binding, args = canonical({"format": 1, "tool": tool, "version": version, "account": account}), canonical(arguments)
    digest = hashlib.sha256(canonical({"binding": decode(binding), "arguments": decode(args)})).hexdigest()
    return EffectIntent(scope, identity, binding, args, digest, policy)


@dataclass(frozen=True)
class InvocationClaim:
    scope: JournalScope
    delivery_id: str
    phase_id: str
    execution_id: str
    maestro_fence: str
    private_fence: str
    token: str = field(repr=False)


Authorize = Callable[[JournalScope, str, str, ExecutionAuthority], Awaitable[datetime]]


class PostgresEffectJournal:
    """Única autoridade. authorize revalida Maestro antes de cada reserve/begin.

    `database` é SqlDatabase privado; não recebe a conexão canônica Maestro.
    A conexão precisa de ACL privada e nunca deve habilitar tracing de parâmetros.
    """

    def __init__(
        self,
        database: SqlDatabase,
        *,
        generation: str,
        organization_id: str,
        workspace_id: str | None,
        instance_agent_id: str,
        fingerprint_keys: Mapping[str, bytes],
        active_key_version: str,
        authorize: Authorize,
        claim_seconds: int = 60,
        integrity_verified: bool = False,
    ) -> None:
        self.db = database
        self.integrity_verified = integrity_verified
        self.generation = uuid(generation)
        self.organization_id, self.workspace_id, self.instance_agent_id = (
            uuid(organization_id),
            uuid(workspace_id) if workspace_id else None,
            uuid(instance_agent_id),
        )
        if not callable(authorize) or type(claim_seconds) is not int or not 1 <= claim_seconds <= 60:
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", active_key_version):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.keys = dict(fingerprint_keys)
        if active_key_version not in self.keys or any(type(v) is not bytes or len(v) < 32 for v in self.keys.values()):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.active_key_version, self.authorize, self.claim_seconds = active_key_version, authorize, claim_seconds

    async def provision_fresh(self) -> None:
        """Ação administrativa explícita, apenas banco novo; nunca chamada pelo executor."""
        async with self.db.transaction() as conn:
            await conn.execute(DDL)
            if await conn.fetchval("SELECT count(*) FROM maestro_journal.authority"):
                raise JournalError("JOURNAL_ALREADY_PROVISIONED")
            if any(
                [
                    await conn.fetchval(f"SELECT count(*) FROM maestro_journal.{table}")
                    for table in ("phases", "effects", "invocations", "outbox")
                ]
            ):
                raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
            await conn.execute(
                """
                INSERT INTO
                maestro_journal.authority(generation,organization_id,workspace_id,instance_agent_id,integrity)
                VALUES($1,$2,$3,$4,'ready')
                """,
                UUID(self.generation),
                UUID(self.organization_id),
                UUID(self.workspace_id) if self.workspace_id else None,
                UUID(self.instance_agent_id),
            )

    async def upgrade_approval_support(self) -> None:
        """Explicit ADMINISTRATIVE migration, admission paused; never executor/startup.

        Requires DDL privileges on the SAME private backend and preserves its
        authority/generation, inventories, checkpoints, receipts and pending outbox.
        Running twice is harmless. Runtime roles must not have these privileges.
        """
        async with self.db.transaction() as conn:
            await self._authority_ready(conn)
            await conn.execute(APPROVAL_EXTENSION_DDL)
            await self._approval_ready(conn)

    async def _approval_ready(self, conn) -> None:
        ready = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='maestro_journal' "
            "AND c.relname='effects' AND a.attname='approval_binding' AND NOT a.attisdropped "
            "AND NOT a.attnotnull AND a.atttypid='bytea'::regtype) "
            "AND EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='maestro_journal' "
            "AND c.relname='effects' AND t.tgname='effect_approval_immutable' AND t.tgenabled IN ('O','A'))"
        )
        if ready is not True:
            raise JournalError("JOURNAL_UPGRADE_REQUIRED")

    async def _ready(self, conn, *, for_update: bool = True) -> None:
        await self._approval_ready(conn)
        await self._authority_ready(conn, for_update=for_update)

    async def _authority_ready(self, conn, *, for_update: bool = True) -> None:
        if self.integrity_verified is not True:
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
        row = await conn.fetchrow("SELECT * FROM maestro_journal.authority WHERE id=true" + (" FOR UPDATE" if for_update else ""))
        if (
            not row
            or str(row["generation"]) != self.generation
            or str(row["organization_id"]) != self.organization_id
            or (str(row["workspace_id"]) if row["workspace_id"] else None) != self.workspace_id
            or str(row["instance_agent_id"]) != self.instance_agent_id
            or row["integrity"] != "ready"
        ):
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
        counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM maestro_journal.effects) effects,(SELECT count(*) FROM maestro_journal.invocations) invocations"
        )
        if counts["effects"] != row["effect_count"] or counts["invocations"] != row["invocation_count"]:
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")

    def _scope(self, scope: JournalScope) -> str:
        value = scope_data(scope)
        if (scope.organization_id, scope.workspace_id, scope.instance_agent_id) != (
            self.organization_id,
            self.workspace_id,
            self.instance_agent_id,
        ):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        return canonical(value).decode()

    async def _fresh(self, scope: JournalScope, delivery: str, phase: str, authority: ExecutionAuthority) -> datetime:
        self._scope(scope)
        uuid(authority.execution_id), uuid(authority.runtime_id), decimal(authority.maestro_fence)
        until = await self.authorize(scope, delivery, phase, authority)
        if not isinstance(until, datetime) or until.tzinfo is None:
            raise JournalError("JOURNAL_AUTHORIZATION_INVALID")
        return min(until, instant(authority.deadline_at))

    async def _fresh_commit(self, conn, *until: datetime) -> None:
        if not await conn.fetchval("SELECT clock_timestamp() < $1", min(until)):
            raise JournalError("JOURNAL_AUTHORIZATION_EXPIRED")

    async def _phase(self, conn, scope: JournalScope, delivery: str, phase: str):
        row = await conn.fetchrow(
            "SELECT *, clock_timestamp() AS now FROM maestro_journal.phases WHERE delivery_id=$1 AND phase_id=$2 FOR UPDATE",
            UUID(uuid(delivery)),
            phase,
        )
        if row and json.loads(row["namespace"]) != json.loads(self._scope(scope)):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        return row

    async def reserve_invocation(
        self,
        scope: JournalScope,
        delivery: str,
        phase: str,
        authority: ExecutionAuthority,
        *,
        context_fingerprint: str,
        retry_policy: Literal["unsafe", "journal_only"] = "unsafe",
    ) -> InvocationClaim:
        valid = await self._fresh(scope, delivery, phase, authority)
        if retry_policy not in {"unsafe", "journal_only"} or not re.fullmatch(r"[a-f0-9]{64}", context_fingerprint):
            raise JournalError("JOURNAL_BINDING_INVALID")
        namespace = self._scope(scope)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._phase(conn, scope, delivery, phase)
            now = await conn.fetchval("SELECT clock_timestamp()")
            if valid <= now:
                raise JournalError("JOURNAL_AUTHORIZATION_EXPIRED")
            if row:
                if row["context_fingerprint"] != context_fingerprint or row["retry_policy"] != retry_policy:
                    raise JournalError("JOURNAL_INTENT_CONFLICT")
                if (
                    str(row["execution_id"]) == authority.execution_id
                    or row["state"] != "retry_ready"
                    or decimal(authority.maestro_fence) <= row["maestro_fence"]
                ):
                    raise JournalError("JOURNAL_INVOCATION_RESERVED")
            token = uuid4()
            private_fence = row["private_fence"] + 1 if row else 1
            await conn.execute(
                """
                INSERT INTO
                maestro_journal.phases(delivery_id,phase_id,namespace,context_fingerprint,retry_policy,execution_id,maestro_fence,private_fence,claim_token,lease_until,state)
                VALUES($1,$2,$3::jsonb,$4,$5,$6,$7,$8,$9,least($10,clock_timestamp()+$11*interval '1 second'),'reserved')
                ON CONFLICT(delivery_id,phase_id) DO UPDATE SET
                execution_id=excluded.execution_id,maestro_fence=excluded.maestro_fence,private_fence=excluded.private_fence,claim_token=excluded.claim_token,lease_until=excluded.lease_until,state='reserved'
                """,
                UUID(delivery),
                phase,
                namespace,
                context_fingerprint,
                retry_policy,
                UUID(authority.execution_id),
                decimal(authority.maestro_fence),
                private_fence,
                token,
                valid,
                self.claim_seconds,
            )
            await conn.execute(
                """
                INSERT INTO maestro_journal.invocations(execution_id,delivery_id,phase_id,maestro_fence,state)
                VALUES($1,$2,$3,$4,'reserved')
                """,
                UUID(authority.execution_id),
                UUID(delivery),
                phase,
                decimal(authority.maestro_fence),
            )
            await conn.execute("UPDATE maestro_journal.authority SET invocation_count=invocation_count+1")
            await self._fresh_commit(conn, valid)
            return InvocationClaim(scope, delivery, phase, authority.execution_id, authority.maestro_fence, str(private_fence), str(token))

    async def _invocation(self, conn, claim: InvocationClaim, *, live: bool = True):
        row = await self._phase(conn, claim.scope, claim.delivery_id, claim.phase_id)
        if (
            not row
            or str(row["execution_id"]) != claim.execution_id
            or row["maestro_fence"] != decimal(claim.maestro_fence)
            or row["private_fence"] != decimal(claim.private_fence)
            or str(row["claim_token"]) != claim.token
            or row["state"] not in {"reserved", "running"}
            or (live and row["lease_until"] <= row["now"])
        ):
            raise JournalError("JOURNAL_STALE_WRITER")
        return row

    async def start_invocation(self, claim: InvocationClaim, authority: ExecutionAuthority) -> None:
        if claim.execution_id != authority.execution_id or claim.maestro_fence != authority.maestro_fence:
            raise JournalError("JOURNAL_STALE_WRITER")
        valid = await self._fresh(claim.scope, claim.delivery_id, claim.phase_id, authority)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._invocation(conn, claim)
            if row["state"] != "reserved" or valid <= row["now"]:
                raise JournalError("JOURNAL_STALE_WRITER")
            await conn.execute(
                "UPDATE maestro_journal.phases SET state='running' WHERE delivery_id=$1 AND phase_id=$2",
                UUID(claim.delivery_id),
                claim.phase_id,
            )
            await conn.execute("UPDATE maestro_journal.invocations SET state='running' WHERE execution_id=$1", UUID(claim.execution_id))
            await self._fresh_commit(conn, valid, row["lease_until"])

    async def renew_invocation(self, claim: InvocationClaim, authority: ExecutionAuthority) -> None:
        if claim.execution_id != authority.execution_id or claim.maestro_fence != authority.maestro_fence:
            raise JournalError("JOURNAL_STALE_WRITER")
        valid = await self._fresh(claim.scope, claim.delivery_id, claim.phase_id, authority)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._invocation(conn, claim)
            if valid <= row["now"]:
                raise JournalError("JOURNAL_AUTHORIZATION_EXPIRED")
            await conn.execute(
                """
                UPDATE maestro_journal.phases SET lease_until=least($3,clock_timestamp()+$4*interval '1 second') WHERE
                delivery_id=$1 AND phase_id=$2
                """,
                UUID(claim.delivery_id),
                claim.phase_id,
                valid,
                self.claim_seconds,
            )
            await self._fresh_commit(conn, valid, row["lease_until"])

    async def confirm_invocation(self, claim: InvocationClaim, confirmed) -> None:
        """Contabiliza ACK canônico já validado pelo callback; não autoriza ação nova.

        A lease privada pode vencer entre complete e este commit. Ainda exige o
        mesmo token/fence sob lock; recovery ou nova reserva invalidam este writer.
        """
        from .adapter import MaestroConfirmedResult

        if not isinstance(confirmed, MaestroConfirmedResult) or confirmed.confirmed is not True:
            raise JournalError("JOURNAL_RESULT_UNCONFIRMED")
        result = confirmed.response
        if (
            set(result) != {"delivery_id", "execution_id", "state", "result_event_ids"}
            or result["state"] not in {"responded", "silent", "failed", "canceled", "waiting_input"}
            or type(result["result_event_ids"]) is not list
            or len(result["result_event_ids"]) > 100
            or len(set(result["result_event_ids"])) != len(result["result_event_ids"])
        ):
            raise JournalError("JOURNAL_BINDING_INVALID")
        for event_id in result["result_event_ids"]:
            uuid(event_id)
        if result.get("delivery_id") != claim.delivery_id or result.get("execution_id") != claim.execution_id:
            raise JournalError("JOURNAL_BINDING_INVALID")
        body = canonical(result).decode()
        async with self.db.transaction() as conn:
            await self._ready(conn)
            await self._invocation(conn, claim, live=False)
            await conn.execute(
                "UPDATE maestro_journal.invocations SET state='confirmed',result=$2::jsonb WHERE execution_id=$1",
                UUID(claim.execution_id),
                body,
            )
            await conn.execute(
                "UPDATE maestro_journal.phases SET state='confirmed' WHERE delivery_id=$1 AND phase_id=$2",
                UUID(claim.delivery_id),
                claim.phase_id,
            )

    async def read_confirmed_invocation(
        self,
        scope: JournalScope,
        delivery: str,
        phase: str,
        authority: ExecutionAuthority,
        *,
        context_fingerprint: str,
        retry_policy: str,
        contracts=None,
    ):
        """Read-only candidate replay proof; never authority for invocation/effect/context.

        The gate must also obtain a current authenticated Maestro completion receipt.
        Locks bind generation/inventory, phase intent, execution and exact fence.
        Missing/incomplete/unknown phases are rejected without reserve or recovery.
        """
        from dooers.agents.server.protocol.maestro import MaestroContracts

        from .adapter import MaestroConfirmedResult

        self._scope(scope)
        uuid(delivery), uuid(authority.execution_id), uuid(authority.runtime_id), decimal(authority.maestro_fence)
        if retry_policy not in {"unsafe", "journal_only"} or not re.fullmatch(r"[a-f0-9]{64}", context_fingerprint):
            raise JournalError("JOURNAL_BINDING_INVALID")
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._phase(conn, scope, delivery, phase)
            if not row or row["state"] != "confirmed":
                raise JournalError("JOURNAL_RESULT_UNCONFIRMED")
            if str(row["execution_id"]) != authority.execution_id or row["maestro_fence"] != decimal(authority.maestro_fence):
                raise JournalError("JOURNAL_STALE_WRITER")
            if row["context_fingerprint"] != context_fingerprint or row["retry_policy"] != retry_policy:
                raise JournalError("JOURNAL_INTENT_CONFLICT")
            inv = await conn.fetchrow(
                "SELECT state,delivery_id,phase_id,maestro_fence,result FROM maestro_journal.invocations WHERE execution_id=$1 FOR UPDATE",
                UUID(authority.execution_id),
            )
            if (
                not inv
                or inv["state"] != "confirmed"
                or str(inv["delivery_id"]) != delivery
                or inv["phase_id"] != phase
                or inv["maestro_fence"] != decimal(authority.maestro_fence)
                or not inv["result"]
            ):
                raise JournalError("JOURNAL_RESULT_UNCONFIRMED")
            try:
                result = (contracts or MaestroContracts()).validate("Public.CallbackResponse", json.loads(inv["result"]))
                if (
                    result["delivery_id"] != delivery
                    or result["execution_id"] != authority.execution_id
                    or result["state"] not in {"responded", "silent", "failed", "canceled"}
                    or not result["result_event_ids"]
                ):
                    raise ValueError
            except Exception:
                raise JournalError("JOURNAL_BINDING_INVALID") from None
            return MaestroConfirmedResult(result, confirmed=True)

    def _intent(self, intent: EffectIntent) -> tuple[str, str]:
        namespace = self._scope(intent.scope)
        validate_identity(intent.identity)
        binding, arguments = decode(intent.tool_binding), decode(intent.canonical_arguments)
        if set(binding) != {"format", "tool", "version", "account"} or binding["format"] != 1:
            raise JournalError("JOURNAL_BINDING_INVALID")
        checked = make_intent(
            intent.scope,
            intent.identity,
            tool=binding["tool"],
            version=binding["version"],
            account=binding["account"],
            arguments=arguments,
            policy=intent.policy,
        )
        if checked.arguments_sha256 != intent.arguments_sha256:
            raise JournalError("JOURNAL_INTENT_CONFLICT")
        if intent.approval_binding is not None:
            from .approval_journal import validate_effect_binding

            validate_effect_binding(intent)
        return namespace, checked.arguments_sha256

    async def _effect(self, conn, intent: EffectIntent, *, for_update: bool = True):
        namespace, digest = self._intent(intent)
        if intent.approval_binding is not None:
            from .approval_journal import verify_persisted_binding

            await verify_persisted_binding(conn, self, intent)
        i = intent.identity
        row = await conn.fetchrow(
            """
            SELECT *,clock_timestamp() AS now FROM maestro_journal.effects WHERE delivery_id=$1 AND phase_id=$2 AND
            operation_id=$3
            """
            + (" FOR UPDATE" if for_update else ""),
            UUID(i.delivery_id),
            i.phase_id,
            i.operation_id,
        )
        if row and (
            json.loads(row["namespace"]) != json.loads(namespace)
            or row["intent_hash"] != digest
            or row["policy"] != intent.policy
            or (bytes(row["approval_binding"]) if row["approval_binding"] is not None else None) != intent.approval_binding
        ):
            raise JournalError("JOURNAL_INTENT_CONFLICT")
        if row:
            self._verify_fingerprint(row, intent)
        if row and row["tombstone"]:
            raise JournalError("JOURNAL_TOMBSTONE")
        return row

    def _verify_fingerprint(self, row, intent: EffectIntent) -> None:
        namespace, digest = self._intent(intent)
        key = self.keys.get(row["fingerprint_key_version"])
        if key is None:
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
        raw = (
            b"maestro-journal-intent-v1\x00"
            + namespace.encode()
            + b"\x00"
            + intent.identity.effect_key.encode()
            + b"\x00"
            + digest.encode()
            + (b"\x00approval:v1\x00" + intent.approval_binding if intent.approval_binding is not None else b"")
        )
        if not hmac.compare_digest(hmac.new(key, raw, hashlib.sha256).hexdigest(), row["fingerprint"]):
            raise JournalError("JOURNAL_INTEGRITY_BLOCKED")

    def _record(self, row, intent: EffectIntent) -> EffectRecord:
        result = PrivateEffectResult(bytes(row["result"]), bytes(row["provider_receipt"])) if row["status"] == "succeeded" else None
        return EffectRecord(
            intent.scope,
            intent.identity,
            row["status"],
            str(row["revision"]),
            row["invocation_started"],
            row["policy"],
            result,
            bytes(row["approval_binding"]) if row["approval_binding"] is not None else None,
        )

    def _claim(self, row) -> EffectClaim:
        return EffectClaim(str(row["journal_fence"]), str(row["revision"]), str(row["claim_token"]), row["lease_until"].isoformat())

    async def _project(self, conn, row, intent: EffectIntent) -> None:
        body = {
            "scope": scope_data(intent.scope),
            "delivery_id": intent.identity.delivery_id,
            "phase_id": intent.identity.phase_id,
            "operation_id": intent.identity.operation_id,
            "effect_key": intent.identity.effect_key,
            "revision": str(row["revision"]),
            "status": row["status"],
            "receipt_id": str(row["receipt_id"]),
            "intent_fingerprint": row["fingerprint"],
            "fingerprint_key_version": row["fingerprint_key_version"],
            "tombstone": row["tombstone"],
        }
        await conn.execute(
            "INSERT INTO maestro_journal.outbox(delivery_id,phase_id,operation_id,revision,body) VALUES($1,$2,$3,$4,$5::jsonb)",
            row["delivery_id"],
            row["phase_id"],
            row["operation_id"],
            row["revision"],
            canonical(body).decode(),
        )

    async def reserve(self, intent: EffectIntent, authority: ExecutionAuthority) -> JournalReservation:
        namespace, digest = self._intent(intent)
        valid = await self._fresh(intent.scope, intent.identity.delivery_id, intent.identity.phase_id, authority)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            phase = await self._phase(conn, intent.scope, intent.identity.delivery_id, intent.identity.phase_id)
            if (
                not phase
                or phase["state"] != "running"
                or str(phase["execution_id"]) != authority.execution_id
                or phase["maestro_fence"] != decimal(authority.maestro_fence)
                or phase["lease_until"] <= phase["now"]
                or valid <= phase["now"]
            ):
                raise JournalError("JOURNAL_STALE_WRITER")
            row = await self._effect(conn, intent)
            if row:
                if row["status"] == "succeeded":
                    await self._fresh_commit(conn, valid, phase["lease_until"])
                    return JournalReservation("reuse", self._record(row, intent))
                if row["lease_until"] > row["now"]:
                    await self._fresh_commit(conn, valid, phase["lease_until"])
                    return JournalReservation("wait", self._record(row, intent))
                uncertain = row["invocation_started"] or row["status"] == "unknown"
                decision = "manual_review" if uncertain and row["policy"] == "non_reconcilable" else "reconcile" if uncertain else "start"
                row = await conn.fetchrow(
                    """
                    UPDATE maestro_journal.effects SET journal_fence=journal_fence+1,revision=revision+1,
                    claim_token=$4,lease_until=least($5,clock_timestamp()+$6*interval '1
                    second'),execution_id=$7,maestro_fence=$8,
                    status=CASE WHEN invocation_started THEN 'unknown' ELSE status END
                    WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 RETURNING *
                    """,
                    UUID(intent.identity.delivery_id),
                    intent.identity.phase_id,
                    intent.identity.operation_id,
                    uuid4(),
                    valid,
                    self.claim_seconds,
                    UUID(authority.execution_id),
                    decimal(authority.maestro_fence),
                )
            else:
                decision = "start"
                fingerprint = hmac.new(
                    self.keys[self.active_key_version],
                    b"maestro-journal-intent-v1\x00"
                    + namespace.encode()
                    + b"\x00"
                    + intent.identity.effect_key.encode()
                    + b"\x00"
                    + digest.encode()
                    + (b"\x00approval:v1\x00" + intent.approval_binding if intent.approval_binding is not None else b""),
                    hashlib.sha256,
                ).hexdigest()
                row = await conn.fetchrow(
                    """
                    INSERT INTO
                    maestro_journal.effects(delivery_id,phase_id,operation_id,namespace,intent_hash,binding,arguments,policy,status,claim_token,lease_until,execution_id,maestro_fence,receipt_id,fingerprint,fingerprint_key_version,approval_binding)
                    VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,$8,'pending',$9,least($10,clock_timestamp()+$11*interval '1
                    second'),$12,$13,$14,$15,$16,$17) RETURNING *
                    """,
                    UUID(intent.identity.delivery_id),
                    intent.identity.phase_id,
                    intent.identity.operation_id,
                    namespace,
                    digest,
                    intent.tool_binding,
                    intent.canonical_arguments,
                    intent.policy,
                    uuid4(),
                    valid,
                    self.claim_seconds,
                    UUID(authority.execution_id),
                    decimal(authority.maestro_fence),
                    uuid4(),
                    fingerprint,
                    self.active_key_version,
                    intent.approval_binding,
                )
                await conn.execute("UPDATE maestro_journal.authority SET effect_count=effect_count+1")
            await self._project(conn, row, intent)
            await self._fresh_commit(conn, valid, phase["lease_until"])
            return JournalReservation(decision, self._record(row, intent), self._claim(row))

    async def _cas(self, conn, intent: EffectIntent, claim: EffectClaim):
        row = await self._effect(conn, intent)
        if (
            not row
            or row["journal_fence"] != decimal(claim.journal_fence)
            or row["revision"] != decimal(claim.revision)
            or str(row["claim_token"]) != claim.claim_token
            or row["lease_until"] <= row["now"]
        ):
            raise JournalError("JOURNAL_STALE_WRITER")
        return row

    async def begin(self, intent: EffectIntent, claim: EffectClaim, authority: ExecutionAuthority) -> EffectClaim:
        valid = await self._fresh(intent.scope, intent.identity.delivery_id, intent.identity.phase_id, authority)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            phase = await self._phase(conn, intent.scope, intent.identity.delivery_id, intent.identity.phase_id)
            row = await self._cas(conn, intent, claim)
            if (
                not phase
                or phase["state"] != "running"
                or str(phase["execution_id"]) != authority.execution_id
                or phase["maestro_fence"] != decimal(authority.maestro_fence)
                or phase["lease_until"] <= row["now"]
                or str(row["execution_id"]) != authority.execution_id
                or row["maestro_fence"] != decimal(authority.maestro_fence)
                or row["invocation_started"]
                or row["status"] != "pending"
                or valid <= row["now"]
            ):
                raise JournalError("JOURNAL_STALE_WRITER")
            updated = await conn.fetchrow(
                """
                UPDATE maestro_journal.effects SET invocation_started=true,revision=revision+1 WHERE delivery_id=$1 AND
                phase_id=$2 AND operation_id=$3 RETURNING *
                """,
                row["delivery_id"],
                row["phase_id"],
                row["operation_id"],
            )
            await self._project(conn, updated, intent)
            await self._fresh_commit(conn, valid, phase["lease_until"], row["lease_until"])
            return self._claim(updated)

    async def record_result(self, intent: EffectIntent, claim: EffectClaim, result: PrivateEffectResult) -> EffectRecord:
        if not isinstance(result, PrivateEffectResult):
            raise JournalError("JOURNAL_PAYLOAD_INVALID")
        decode(result.canonical_result), decode(result.provider_receipt)
        return await self._finish(intent, claim, "succeeded", result)

    async def record_unknown(self, intent: EffectIntent, claim: EffectClaim) -> EffectRecord:
        return await self._finish(intent, claim, "unknown", None)

    async def _finish(self, intent, claim, status, result):
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._cas(conn, intent, claim)
            if not row["invocation_started"] or row["status"] == "succeeded":
                raise JournalError("JOURNAL_STATE_INVALID")
            updated = await conn.fetchrow(
                """UPDATE maestro_journal.effects SET status=$4,result=$5,provider_receipt=$6,revision=revision+1
                WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 RETURNING *""",
                row["delivery_id"],
                row["phase_id"],
                row["operation_id"],
                status,
                result.canonical_result if result else None,
                result.provider_receipt if result else None,
            )
            await self._project(conn, updated, intent)
            await self._fresh_commit(conn, row["lease_until"])
            return self._record(updated, intent)

    async def reconcile(
        self, intent: EffectIntent, claim: EffectClaim, evidence: ReconciliationEvidence, authority: ExecutionAuthority
    ) -> JournalReservation:
        if not isinstance(evidence, ReconciliationEvidence) or not evidence.evidence or len(evidence.evidence) > 1024 * 1024:
            raise JournalError("JOURNAL_EVIDENCE_INVALID")
        valid = await self._fresh(intent.scope, intent.identity.delivery_id, intent.identity.phase_id, authority)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            phase = await self._phase(conn, intent.scope, intent.identity.delivery_id, intent.identity.phase_id)
            row = await self._cas(conn, intent, claim)
            if (
                not phase
                or phase["state"] != "running"
                or str(phase["execution_id"]) != authority.execution_id
                or phase["maestro_fence"] != decimal(authority.maestro_fence)
            ):
                raise JournalError("JOURNAL_STALE_WRITER")
            if row["policy"] == "non_reconcilable" or valid <= row["now"] or evidence.outcome not in {"applied", "not_applied", "unknown"}:
                raise JournalError("JOURNAL_EVIDENCE_INVALID")
            if evidence.outcome == "applied":
                if not evidence.result:
                    raise JournalError("JOURNAL_EVIDENCE_INVALID")
                decode(evidence.result.canonical_result), decode(evidence.result.provider_receipt)
                status, started, decision = "succeeded", True, "reuse"
            elif evidence.outcome == "not_applied":
                if evidence.result is not None:
                    raise JournalError("JOURNAL_EVIDENCE_INVALID")
                status, started, decision = "pending", False, "start"
            else:
                status, started, decision = "unknown", True, "manual_review"
            result = evidence.result if status == "succeeded" else None
            updated = await conn.fetchrow(
                """UPDATE maestro_journal.effects SET status=$4,invocation_started=$5,result=$6,provider_receipt=$7,revision=revision+1
                WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 RETURNING *""",
                row["delivery_id"],
                row["phase_id"],
                row["operation_id"],
                status,
                started,
                result.canonical_result if result else None,
                result.provider_receipt if result else None,
            )
            await self._project(conn, updated, intent)
            await self._fresh_commit(conn, valid, phase["lease_until"], row["lease_until"])
            return JournalReservation(decision, self._record(updated, intent), self._claim(updated) if decision == "start" else None)

    async def account_late_result(self, intent: EffectIntent, *, expected_revision: str, evidence: ReconciliationEvidence) -> EffectRecord:
        """Port operacional privado de contabilização, nunca disponível ao handler.

        O adapter do operador verifica a evidência aplicada no provider. Não admite
        nova ação, não altera fase/terminal e não aceita alegação do writer antigo.
        """
        revision = decimal(expected_revision)
        self._intent(intent)
        if (
            not isinstance(evidence, ReconciliationEvidence)
            or evidence.outcome != "applied"
            or not evidence.evidence
            or len(evidence.evidence) > 1024 * 1024
            or evidence.result is None
        ):
            raise JournalError("JOURNAL_EVIDENCE_INVALID")
        decode(evidence.result.canonical_result), decode(evidence.result.provider_receipt)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._effect(conn, intent)
            if not row or row["revision"] != revision or not row["invocation_started"]:
                raise JournalError("JOURNAL_STALE_WRITER")
            if row["status"] == "succeeded":
                record = self._record(row, intent)
                if record.result != evidence.result:
                    raise JournalError("JOURNAL_INTENT_CONFLICT")
                return record
            updated = await conn.fetchrow(
                """
                UPDATE maestro_journal.effects SET status='succeeded',result=$4,provider_receipt=$5,
                revision=revision+1,journal_fence=journal_fence+1,lease_until=clock_timestamp()
                WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 RETURNING *
                """,
                row["delivery_id"],
                row["phase_id"],
                row["operation_id"],
                evidence.result.canonical_result,
                evidence.result.provider_receipt,
            )
            await self._project(conn, updated, intent)
            return self._record(updated, intent)

    async def read(self, scope: JournalScope, identity: StableIdentity) -> EffectRecord | None:
        self._scope(scope), validate_identity(identity)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await conn.fetchrow(
                "SELECT * FROM maestro_journal.effects WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3",
                UUID(identity.delivery_id),
                identity.phase_id,
                identity.operation_id,
            )
            if not row:
                return None
            if json.loads(row["namespace"]) != scope_data(scope):
                raise JournalError("JOURNAL_SCOPE_DENIED")
            if row["tombstone"]:
                raise JournalError("JOURNAL_TOMBSTONE")
            intent = EffectIntent(
                scope,
                identity,
                bytes(row["binding"]),
                bytes(row["arguments"]),
                row["intent_hash"],
                row["policy"],
                bytes(row["approval_binding"]) if row["approval_binding"] is not None else None,
            )
            self._verify_fingerprint(row, intent)
            return self._record(row, intent)

    async def pending_projections(self, *, limit: int) -> tuple[EffectProjection, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise JournalError("JOURNAL_BINDING_INVALID")
        async with self.db.transaction() as conn:
            await self._ready(conn)
            rows = await conn.fetch(
                "SELECT body FROM maestro_journal.outbox WHERE NOT acked ORDER BY delivery_id,phase_id,operation_id,revision LIMIT $1",
                limit,
            )
            return tuple(projection_from_data(json.loads(row["body"])) for row in rows)

    async def acknowledge_projection(self, projection: EffectProjection) -> None:
        body = projection_data(projection)
        self._scope(projection.scope)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await conn.fetchrow(
                """
                SELECT body FROM maestro_journal.outbox WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 AND
                revision=$4 FOR UPDATE
                """,
                UUID(projection.identity.delivery_id),
                projection.identity.phase_id,
                projection.identity.operation_id,
                decimal(projection.revision),
            )
            if not row or json.loads(row["body"]) != body:
                raise JournalError("JOURNAL_PROJECTION_CONFLICT")
            await conn.execute(
                "UPDATE maestro_journal.outbox SET acked=true WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 AND revision=$4",
                UUID(projection.identity.delivery_id),
                projection.identity.phase_id,
                projection.identity.operation_id,
                decimal(projection.revision),
            )

    async def tombstone(self, scope: JournalScope, identity: StableIdentity) -> None:
        """Contabilização operacional; conserva identidade/hash e não libera reutilização."""
        self._scope(scope), validate_identity(identity)
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await conn.fetchrow(
                "SELECT * FROM maestro_journal.effects WHERE delivery_id=$1 AND phase_id=$2 AND operation_id=$3 FOR UPDATE",
                UUID(identity.delivery_id),
                identity.phase_id,
                identity.operation_id,
            )
            if not row or json.loads(row["namespace"]) != scope_data(scope):
                raise JournalError("JOURNAL_SCOPE_DENIED")
            if row["tombstone"]:
                return
            intent = EffectIntent(
                scope,
                identity,
                bytes(row["binding"]),
                bytes(row["arguments"]),
                row["intent_hash"],
                row["policy"],
                bytes(row["approval_binding"]) if row["approval_binding"] is not None else None,
            )
            updated = await conn.fetchrow(
                """
                UPDATE maestro_journal.effects SET tombstone=true,revision=revision+1,journal_fence=journal_fence+1 WHERE
                delivery_id=$1 AND phase_id=$2 AND operation_id=$3 RETURNING *
                """,
                row["delivery_id"],
                row["phase_id"],
                row["operation_id"],
            )
            await self._project(conn, updated, intent)

    async def recovery(self, request: dict[str, Any]) -> dict[str, Any]:
        """Somente port operacional autenticado. Congela writer antes de emitir prova.

        Não reabre invocação; a próxima tentativa ainda precisa heartbeat vigente.
        Ausência da phase só é comprovada após checar geração/inventário íntegros.
        """
        from .journal_transport import validate_recovery_request

        validate_recovery_request(request)
        scope = JournalScope(**request["scope"])
        namespace = self._scope(scope)
        delivery, phase, execution = uuid(request["delivery_id"]), request["phase_id"], uuid(request["execution_id"])
        fence = decimal(request["maestro_fence"])
        async with self.db.transaction() as conn:
            await self._ready(conn)
            row = await self._phase(conn, scope, delivery, phase)
            if row and (row["context_fingerprint"] != request["context_fingerprint"] or row["retry_policy"] != request["retry_policy"]):
                raise JournalError("JOURNAL_BINDING_INVALID")
            if row and (str(row["execution_id"]) != execution or row["maestro_fence"] != fence):
                # A newer attempt can fail admission before reserving its invocation.
                # Advance only an already fenced, safe-retry barrier. Never adopt a
                # running/confirmed/uncertain writer or an equal/older Maestro fence.
                if row["state"] != "retry_ready" or fence <= row["maestro_fence"]:
                    raise JournalError("JOURNAL_STALE_WRITER")
                existing = await conn.fetchval(
                    "SELECT 1 FROM maestro_journal.invocations WHERE execution_id=$1", UUID(execution)
                )
                if existing:
                    raise JournalError("JOURNAL_STALE_WRITER")
                await conn.execute(
                    "UPDATE maestro_journal.phases SET execution_id=$3,maestro_fence=$4 WHERE delivery_id=$1 AND phase_id=$2",
                    UUID(delivery), phase, UUID(execution), fence,
                )
                await conn.execute(
                    "INSERT INTO maestro_journal.invocations(execution_id,delivery_id,phase_id,maestro_fence,state) VALUES($1,$2,$3,$4,'abandoned')",
                    UUID(execution), UUID(delivery), phase, fence,
                )
                await conn.execute("UPDATE maestro_journal.authority SET invocation_count=invocation_count+1")
            decision = "safe_retry"
            confirmed = None
            if row:
                effects = await conn.fetch(
                    """
                    SELECT status,invocation_started,policy,tombstone FROM maestro_journal.effects WHERE delivery_id=$1 AND
                    phase_id=$2 FOR UPDATE
                    """,
                    UUID(delivery),
                    phase,
                )
                if row["state"] == "confirmed":
                    inv = await conn.fetchrow("SELECT result FROM maestro_journal.invocations WHERE execution_id=$1", UUID(execution))
                    confirmed = json.loads(inv["result"])
                    decision = "confirmed"
                elif row["state"] == "manual_review" or (row["state"] == "running" and row["retry_policy"] != "journal_only"):
                    decision = "manual_review"
                elif any(
                    e["tombstone"] or e["status"] == "unknown" or (e["invocation_started"] and e["status"] != "succeeded") for e in effects
                ):
                    # Mesmo provider idempotente exige reconciliação explícita, jamais ausência da projeção.
                    decision = "manual_review"
                if decision != "confirmed":
                    await conn.execute(
                        """
                        UPDATE maestro_journal.phases SET state=$3,private_fence=private_fence+1,lease_until=clock_timestamp()
                        WHERE delivery_id=$1 AND phase_id=$2
                        """,
                        UUID(delivery),
                        phase,
                        "retry_ready" if decision == "safe_retry" else "manual_review",
                    )
                    await conn.execute("UPDATE maestro_journal.invocations SET state='abandoned' WHERE execution_id=$1", UUID(execution))
                    await conn.execute(
                        """
                        UPDATE maestro_journal.effects SET journal_fence=journal_fence+1,lease_until=clock_timestamp() WHERE
                        delivery_id=$1 AND phase_id=$2 AND status!='succeeded'
                        """,
                        UUID(delivery),
                        phase,
                    )
            else:
                # Barreira durável para um dispatch antigo chegando DEPOIS desta resposta.
                await conn.execute(
                    """
                    INSERT INTO
                    maestro_journal.phases(delivery_id,phase_id,namespace,context_fingerprint,retry_policy,execution_id,maestro_fence,claim_token,lease_until,state)
                    VALUES($1,$2,$3::jsonb,$4,$5,$6,$7,$8,clock_timestamp(),'retry_ready')
                    """,
                    UUID(delivery),
                    phase,
                    namespace,
                    request["context_fingerprint"],
                    request["retry_policy"],
                    UUID(execution),
                    fence,
                    uuid4(),
                )
                await conn.execute(
                    """
                    INSERT INTO maestro_journal.invocations(execution_id,delivery_id,phase_id,maestro_fence,state)
                    VALUES($1,$2,$3,$4,'abandoned')
                    """,
                    UUID(execution),
                    UUID(delivery),
                    phase,
                    fence,
                )
                await conn.execute("UPDATE maestro_journal.authority SET invocation_count=invocation_count+1")
            revision = await conn.fetchval(
                "SELECT private_fence FROM maestro_journal.phases WHERE delivery_id=$1 AND phase_id=$2", UUID(delivery), phase
            )
            return {
                "generation": self.generation,
                "revision": str(revision),
                "decision": decision,
                "binding": request,
                "confirmed_result": confirmed,
            }


def projection_data(projection: EffectProjection) -> dict[str, Any]:
    validate_identity(projection.identity), decimal(projection.revision)
    if projection.status not in {"pending", "succeeded", "unknown"} or type(projection.tombstone) is not bool:
        raise JournalError("JOURNAL_PROJECTION_INVALID")
    uuid(projection.receipt_id)
    if not re.fullmatch(r"[a-f0-9]{64}", projection.intent_fingerprint) or not re.fullmatch(
        r"[1-9][0-9]{0,8}", projection.fingerprint_key_version
    ):
        raise JournalError("JOURNAL_PROJECTION_INVALID")
    return {
        "scope": scope_data(projection.scope),
        "delivery_id": projection.identity.delivery_id,
        "phase_id": projection.identity.phase_id,
        "operation_id": projection.identity.operation_id,
        "effect_key": projection.identity.effect_key,
        "revision": projection.revision,
        "status": projection.status,
        "receipt_id": projection.receipt_id,
        "intent_fingerprint": projection.intent_fingerprint,
        "fingerprint_key_version": projection.fingerprint_key_version,
        "tombstone": projection.tombstone,
    }


def projection_from_data(data: dict[str, Any]) -> EffectProjection:
    ordinal = int(data["operation_id"].split(":")[1])
    identity = StableIdentity(data["delivery_id"], data["phase_id"], ordinal, f"output:{ordinal}", data["operation_id"], data["effect_key"])
    result = EffectProjection(
        JournalScope(**data["scope"]),
        identity,
        data["revision"],
        data["status"],
        data["receipt_id"],
        data["intent_fingerprint"],
        data["fingerprint_key_version"],
        data["tombstone"],
    )
    if projection_data(result) != data:
        raise JournalError("JOURNAL_PROJECTION_INVALID")
    return result
