"""Sandbox de crash real. Config sintética somente stdin; nenhuma credencial sai."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta

from dooers.agents.server.database import SqlDatabase
from dooers.agents.server.maestro.journal_store import PostgresEffectJournal, make_intent
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.agents.server.protocol.maestro.identity import StableIdentity
from dooers.agents.server.protocol.maestro.journal import ExecutionAuthority, JournalScope


async def main():
    data = json.loads(sys.stdin.read())
    persistence = PostgresPersistence(**data["database"])
    await persistence.connect()
    database = SqlDatabase(persistence)
    scope = JournalScope(**data["scope"])

    async def authorize(*args):
        return datetime.now(UTC) + timedelta(seconds=60)

    journal = PostgresEffectJournal(
        database,
        generation=data["generation"],
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        instance_agent_id=scope.instance_agent_id,
        fingerprint_keys={"1": b"synthetic-fingerprint-key-32-bytes-only"},
        active_key_version="1",
        authorize=authorize,
        integrity_verified=True,
    )
    authority = ExecutionAuthority(**data["authority"])
    delivery = data["delivery_id"]
    claim = await journal.reserve_invocation(
        scope, delivery, "initial", authority, context_fingerprint=data["fingerprint"], retry_policy="journal_only"
    )
    await journal.start_invocation(claim, authority)
    identity = StableIdentity(delivery, "initial", 0, "output:0", "effect:0", f"maestro:v1:effect:{delivery}:initial:0")
    intent = make_intent(
        scope, identity, tool="sandbox", version="1", account="synthetic-account", arguments={"units": 1}, policy="non_reconcilable"
    )
    reserved = await journal.reserve(intent, authority)
    await journal.begin(intent, reserved.claim, authority)
    # Provider independente não idempotente: cada chamada efetiva é um INSERT.
    await database.execute("INSERT INTO public.sandbox_effects(effect_key) VALUES($1)", identity.effect_key)
    os._exit(75)  # efeito commitado; sem finally, receipt ou ACK para o invocador


asyncio.run(main())
