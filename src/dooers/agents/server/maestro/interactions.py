"""Suspensão opt-in e checkpoint no MESMO journal durável de efeitos.

Nenhuma migração/provisão automática. O operador instala a extensão E11-02
antes do anúncio; os validators são código confiável e não schemas do request.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .journal_store import JournalError, PostgresEffectJournal, canonical, decimal, instant


@dataclass(frozen=True)
class MaestroCheckpoint:
    schema: str
    version: int
    state: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class MaestroFormSuspension:
    """Apenas Maestro: elements são fields canônicos, não controles legados."""

    checkpoint: MaestroCheckpoint
    expires_at: str
    eligible_participant_ids: tuple[str, ...]


class MaestroCheckpointStore:
    def __init__(self, journal: PostgresEffectJournal, *, backend_id: str, schemas: Mapping[str, tuple[int, Callable[[Any], Any]]]):
        if not isinstance(journal, PostgresEffectJournal) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", backend_id):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        checked = dict(schemas)
        if not checked or any(
            not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", k)
            or type(v) is not tuple
            or len(v) != 2
            or type(v[0]) is not int
            or not 1 <= v[0] <= 9007199254740991
            or not callable(v[1])
            for k, v in checked.items()
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID")
        self.journal, self.backend_id, self.schemas = journal, backend_id, checked

    def _state(self, checkpoint: MaestroCheckpoint) -> bytes:
        if not isinstance(checkpoint, MaestroCheckpoint) or type(checkpoint.state) is not dict:
            raise JournalError("JOURNAL_BINDING_INVALID")
        if type(checkpoint.schema) is not str or type(checkpoint.version) is not int:
            raise JournalError("JOURNAL_BINDING_INVALID")
        registered = self.schemas.get(checkpoint.schema)
        if not registered or registered[0] != checkpoint.version:
            raise JournalError("JOURNAL_SCHEMA_UNSUPPORTED")
        try:
            raw = checkpoint_bytes(checkpoint.state)
            if len(raw) > 16384 or checkpoint_bytes(registered[1](deepcopy(checkpoint.state))) != raw:
                raise JournalError("JOURNAL_PAYLOAD_INVALID")
            return raw
        except Exception:
            raise JournalError("JOURNAL_PAYLOAD_INVALID") from None

    async def save(self, execution, checkpoint: MaestroCheckpoint, ordinal: int) -> dict[str, Any]:
        if not execution.active or not execution.claim or execution.store.db is not self.journal.db:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        if type(ordinal) is not int or not 0 <= ordinal <= 9007199254740991:
            raise JournalError("JOURNAL_BINDING_INVALID")
        raw = self._state(checkpoint)
        e, store = execution.admission.envelope, execution.store
        # Stable in one attempt, including ACK-loss retries. A different attempt may
        # write a new source binding; the output/effect namespace stays phase-owned.
        identity = canonical(
            {
                "delivery": e["delivery_id"],
                "phase": e["phase_id"],
                "execution": e["execution_id"],
                "output": ordinal,
                "kind": "checkpoint:v1",
            }
        )
        ref = f"checkpoint:v1:{UUID(bytes=hashlib.sha256(identity).digest()[:16], version=4)}"
        descriptor = {
            "format_version": "maestro.continuity.local.v1",
            "checkpoint_ref": ref,
            "checkpoint_schema": checkpoint.schema,
            "checkpoint_version": checkpoint.version,
            "backend_id": self.backend_id,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            **{k: e[k] for k in ("organization_id", "workspace_id", "thread_id", "delivery_id", "instance_agent_id")},
            "source_phase_id": e["phase_id"],
            "source_execution_id": e["execution_id"],
            "agent_participant_id": e["recipient_participant_id"],
            "retain_until": e["delivery_expires_at"],
        }
        valid = await store._fresh(execution.scope, execution.delivery, execution.phase, execution.authority)
        async with store.db.transaction() as conn:
            await store._ready(conn)
            phase = await store._invocation(conn, execution.claim)
            prior = await conn.fetchrow("SELECT descriptor,state FROM maestro_journal.checkpoints WHERE checkpoint_ref=$1", ref)
            if prior:
                import json

                if (
                    canonical(json.loads(prior["descriptor"])) != canonical(descriptor)
                    or checkpoint_bytes(json.loads(prior["state"])) != raw
                ):
                    raise JournalError("JOURNAL_INTENT_CONFLICT")
            else:
                await conn.execute(
                    "INSERT INTO maestro_journal.checkpoints(checkpoint_ref,generation,maestro_fence,descriptor,state) "
                    "VALUES($1,$2,$3,$4::jsonb,$5::jsonb)",
                    ref,
                    UUID(store.generation),
                    decimal(e["lease_fence"]),
                    canonical(descriptor).decode(),
                    raw.decode(),
                )
            await store._fresh_commit(conn, valid, phase["lease_until"])
        return deepcopy(descriptor)

    async def load(self, execution, private: dict[str, Any]) -> dict[str, Any]:
        """Read current private grant, then same backend; old lease never grants read."""
        if not execution.active or not execution.claim or execution.store.db is not self.journal.db:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        e, store, d = execution.admission.envelope, execution.store, private["continuity"]
        resume = e.get("resume")
        expected = {
            "format_version",
            "checkpoint_ref",
            "checkpoint_schema",
            "checkpoint_version",
            "backend_id",
            "content_sha256",
            "organization_id",
            "workspace_id",
            "thread_id",
            "delivery_id",
            "source_phase_id",
            "source_execution_id",
            "agent_participant_id",
            "instance_agent_id",
            "retain_until",
        }
        if not resume or type(d) is not dict or set(d) != expected or private["resume"] != resume or d["backend_id"] != self.backend_id:
            raise JournalError("JOURNAL_SCOPE_DENIED")
        if any(d[k] != e[k] for k in ("organization_id", "workspace_id", "thread_id", "delivery_id", "instance_agent_id")) or (
            d["agent_participant_id"] != e["recipient_participant_id"]
            or d["source_execution_id"] != resume["resume_of_execution_id"]
            or d["checkpoint_ref"] != resume.get("checkpoint_ref")
            or d["format_version"] != "maestro.continuity.local.v1"
            or instant(d["retain_until"]) < instant(e["delivery_expires_at"])
        ):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        valid = await store._fresh(execution.scope, execution.delivery, execution.phase, execution.authority)
        async with store.db.transaction() as conn:
            await store._ready(conn)
            phase = await store._invocation(conn, execution.claim)
            row = await conn.fetchrow(
                "SELECT c.descriptor,c.state,c.maestro_fence,p.execution_id,p.maestro_fence AS source_fence "
                "FROM maestro_journal.checkpoints c JOIN maestro_journal.phases p "
                "ON p.delivery_id=$2 AND p.phase_id=$3 WHERE c.checkpoint_ref=$1 AND c.generation=$4 "
                "AND octet_length(c.state::text)<=32768 AND octet_length(c.descriptor::text)<=8192",
                d["checkpoint_ref"],
                UUID(e["delivery_id"]),
                d["source_phase_id"],
                UUID(store.generation),
            )
            import json

            if not row or str(row["execution_id"]) != d["source_execution_id"] or row["maestro_fence"] != row["source_fence"]:
                raise JournalError("JOURNAL_SCOPE_DENIED")
            state = json.loads(row["state"])
            raw = self._state(MaestroCheckpoint(d["checkpoint_schema"], d["checkpoint_version"], state))
            if canonical(json.loads(row["descriptor"])) != canonical(d) or hashlib.sha256(raw).hexdigest() != d["content_sha256"]:
                raise JournalError("JOURNAL_INTEGRITY_BLOCKED")
            if datetime.now(UTC) >= instant(d["retain_until"]):
                raise JournalError("JOURNAL_AUTHORIZATION_EXPIRED")
            await store._fresh_commit(conn, valid, phase["lease_until"])
        # Recheck the remote current binding after private backend I/O, before handler.
        if resume["kind"] == "tool_approval":
            fresh = await execution.client.interaction_context(approval=True)
        else:
            fresh = await execution.client.interaction_context()
        if private_context_bytes(fresh) != private_context_bytes(private):
            raise JournalError("JOURNAL_SCOPE_DENIED")
        execution.client._check_current()
        return deepcopy(state)


def validate_private_context(value: Any, envelope: dict[str, Any]) -> dict[str, Any]:
    """Pinned candidate schema, plus correlation; no execution authority granted here."""
    if (envelope.get("resume") or {}).get("kind") == "tool_approval":
        from .approvals import validate_approval_context

        return validate_approval_context(value, envelope)
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    from .client import MaestroContextError

    try:
        raw = (Path(__file__).parent / "interaction-context.schema.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != "996a2f5b3ca8a1b3b143b99860af386cea3ff9391f02041ca694aec721f897c2":
            raise ValueError
        Draft202012Validator(json.loads(raw), format_checker=FormatChecker()).validate(value)
        if (
            value["resume"] != envelope["resume"]
            or value["resume"]["kind"] != "form"
            or (
                value["resume"]["interaction_id"] != envelope["phase_id"]
                or value["resume"]["original_trigger_event_id"] != envelope["trigger_event_id"]
                or value["resume"]["response_sequence"] != envelope["context_cutoff_sequence"]
            )
        ):
            raise ValueError
        return deepcopy(value)
    except Exception:
        raise MaestroContextError("CONTEXT_INVALID") from None


def private_context_bytes(value: Any) -> bytes:
    """Comparison only, never journal serialization: form numbers permit decimals."""
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def checkpoint_bytes(value: Any) -> bytes:
    """E11 continuity uses Maestro canonicalJournal, including JS key ordering.

    The existing effect journal retains its own closed format unchanged. UTF-16
    lexical sorting and integer-index enumeration matter only at this boundary.
    """
    import json

    canonical(value)  # Existing closed JSON validation, sizes, depth, types.

    def key_order(key: str):
        index = int(key) if key.isascii() and key.isdigit() and len(key) <= 10 else -1
        if 0 <= index < 4294967295 and str(index) == key:
            return (0, index)
        return (1, key.encode("utf-16-be"))

    def ordered(item):
        if type(item) is dict:
            return {key: ordered(item[key]) for key in sorted(item, key=key_order)}
        if type(item) is list:
            return [ordered(child) for child in item]
        return item

    return json.dumps(ordered(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
