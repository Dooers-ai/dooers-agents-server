"""Smoke durável do wheel instalado: PostgreSQL privado e callbacks HTTP de fixture."""

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from dooers.agents.server.database import SqlDatabase
from dooers.agents.server.maestro import (
    MaestroConfig,
    MaestroDeliveryAdapter,
    MaestroInstanceCredential,
    MaestroMemory,
    MaestroPrivateResources,
    create_durable_text_delivery_adapter,
)
from dooers.agents.server.maestro.journal_runtime import MaestroEffectTool, run_maestro_effect
from dooers.agents.server.maestro.journal_store import PostgresEffectJournal, canonical
from dooers.agents.server.persistence.postgres import PostgresPersistence
from dooers.agents.server.protocol.maestro import verify_contract_bundle
from dooers.agents.server.protocol.maestro.journal import PrivateEffectResult
from dooers.tools.rag.runtime import current_execution_context


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(key, header, claims):
    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    value = b64(encoded(header)) + "." + b64(encoded(claims))
    return value + "." + b64(key.sign(value.encode(), padding.PKCS1v15(), hashes.SHA256()))


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def main():
    assert "site-packages" in str(Path(sys.modules[MaestroMemory.__module__].__file__))
    assert find_spec("pytest") is None
    cases = json.loads(verify_contract_bundle()["fixtures.json"])["cases"]
    binding = deepcopy(next(case["data"] for case in cases if case["schema"] == "Delivery.DispatchBinding" and case["valid"]))
    page = deepcopy(next(case["data"] for case in cases if case["schema"] == "Public.ContextResponse" and case["valid"]))
    agent_claims = deepcopy(next(case["data"] for case in cases if case["schema"] == "Identity.AgentTokenClaims" and case["valid"]))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    requests = []
    server_tasks = set()
    outputs = []
    completions = []

    async def handler(reader, writer):
        task = asyncio.current_task()
        server_tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            assert f"Authorization: Bearer {core_token}\r\n".encode() in request
            headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in request.decode().split("\r\n") if ": " in line)}
            size = int(headers.get("content-length", "0"))
            value = json.loads(await reader.readexactly(size)) if size else None
            path = request.decode().split()[1]
            requests.append(path)
            if "/context?" in path:
                result = page
            elif path.endswith("/heartbeat"):
                result = {"lease_until": timestamp(time.time() + 50)}
            elif path.endswith("/outputs"):
                event = deepcopy(trigger)
                event.update(
                    event_id=str(uuid4()),
                    sequence="9007199254740993",
                    delivery_id=envelope["delivery_id"],
                    execution_id=envelope["execution_id"],
                    output_id=value["output_id"],
                    causation_event_id=envelope["trigger_event_id"],
                    actor={
                        "kind": "agent",
                        "principal_id": envelope["instance_agent_id"],
                        "instance_agent_id": envelope["instance_agent_id"],
                        "participant_id": envelope["recipient_participant_id"],
                        "display_name": "A",
                    },
                    payload={**value["payload"], "execution_id": envelope["execution_id"], "output_id": value["output_id"]},
                )
                outputs.append(event)
                result = {"status": "persisted", "duplicate": False, "event": event, "cursor": "fixture-output"}
            else:
                assert path.endswith("/complete")
                completions.append(value)
                result = {
                    "delivery_id": envelope["delivery_id"],
                    "execution_id": envelope["execution_id"],
                    "state": value["outcome"],
                    "result_event_ids": [event["event_id"] for event in outputs],
                }
            body = encoded(result)
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            server_tasks.discard(task)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    now = int(time.time())
    envelope = binding["envelope"]
    envelope.update(
        callback_base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/v1",
        deadline_at=timestamp(now + 120),
        delivery_expires_at=timestamp(now + 3600),
    )
    agent_claims.update(
        sub=envelope["instance_agent_id"],
        agent_id=envelope["instance_agent_id"],
        runtime_id=envelope["runtime_id"],
        organization_id=envelope["organization_id"],
        workspace_id=envelope["workspace_id"],
        iat=now - 1,
        exp=now + 90,
        scopes=["deliveries:read", "context:read", "processing:write", "responses:write"],
    )
    core_token = sign(key, {"alg": "RS256", "typ": "maestro-access+jwt", "kid": "fixture"}, agent_claims)

    async def credential(request):
        assert request.instance_agent_id == envelope["instance_agent_id"]
        assert set(request.scopes) <= set(agent_claims["scopes"])
        return MaestroInstanceCredential(core_token, now + 90)

    config = MaestroConfig(
        runtime_endpoint=binding["configured_runtime_endpoint"],
        callback_base_url=envelope["callback_base_url"],
        core_base_url="http://127.0.0.1:8788",
        organization_id=envelope["organization_id"],
        workspace_id=None,
        instance_agent_id=envelope["instance_agent_id"],
        runtime_id=envelope["runtime_id"],
        trusted_public_keys={
            binding["dispatch_header"]["kid"]: key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        },
        credential_resolver=credential,
        allow_local_http=True,
    )
    for claims in (binding["dispatch_claims"], binding["capability_claims"]):
        claims.update(iat=now - 1, exp=now + 59, deadline_at=envelope["deadline_at"])
    binding["dispatch_claims"]["body_sha256"] = hashlib.sha256(encoded(envelope)).hexdigest()
    capability = sign(key, binding["capability_header"], binding["capability_claims"])
    adapter = MaestroDeliveryAdapter(config)
    adapter.authenticate(
        encoded(envelope),
        {
            "Authorization": "Bearer " + sign(key, binding["dispatch_header"], binding["dispatch_claims"]),
            "Maestro-Execution-Capability": capability,
            "Content-Type": "application/json",
        },
    )
    trigger = page["trigger_event"]
    trigger.update(
        event_id=envelope["trigger_event_id"],
        thread_id=envelope["thread_id"],
        sequence=envelope["trigger_sequence"],
        root_event_id=envelope["root_event_id"],
    )
    page.update(delivery_id=envelope["delivery_id"], context_cursor=envelope["context_cursor"])
    page["participants"].append(
        {
            "participant_id": envelope["recipient_participant_id"],
            "kind": "agent",
            "principal_id": envelope["instance_agent_id"],
            "instance_agent_id": envelope["instance_agent_id"],
            "display_name": "Recipient",
            "role": "member",
            "state": "active",
            "joined_sequence": "1",
            "left_sequence": None,
            "capabilities": [],
            "availability": "available",
        }
    )
    page["history"] = {
        "items": [deepcopy(trigger)],
        "next_cursor": None,
        "has_more": False,
        "watermark": envelope["context_cutoff_sequence"],
    }

    async def private(scope):
        return MaestroPrivateResources(scope, object(), {"private": scope.instance_agent_id}, object())

    url = os.environ["MAESTRO_TEST_DATABASE_ADMIN_URL"]
    parsed = urlsplit(url)
    assert (parsed.hostname, parsed.port, parsed.username, parsed.path) == ("127.0.0.1", 55434, "maestro_admin", "/postgres")
    name = f"maestro_e0509_smoke_{uuid4().hex[:12]}"
    role, password = name + "_private", uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    await admin.execute(f"CREATE DATABASE {name} OWNER {role}")
    await admin.execute(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
    persistence = PostgresPersistence(host="127.0.0.1", port=55434, user=role, database=name, password=password)
    await persistence.connect()
    database = SqlDatabase(persistence)
    await database.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    await database.execute("CREATE TABLE sandbox_effects(effect_key text NOT NULL)")

    async def authorize(*_args):
        # The durable factory replaces this with its actual callback heartbeat authorizer.
        return datetime.now(UTC) + timedelta(seconds=60)

    journal = PostgresEffectJournal(
        database,
        generation=str(uuid4()),
        organization_id=envelope["organization_id"],
        workspace_id=None,
        instance_agent_id=envelope["instance_agent_id"],
        fingerprint_keys={"1": os.urandom(32)},
        active_key_version="1",
        authorize=authorize,
        integrity_verified=True,
    )
    await journal.provision_fresh()
    handler_calls = 0

    async def provider(arguments, effect_key):
        assert arguments == {"units": 1}
        # Deliberately no unique constraint: the assertion counts every actual external call.
        await database.execute("INSERT INTO sandbox_effects VALUES($1)", effect_key)
        return PrivateEffectResult(canonical({"applied": True}), canonical({"receipt": str(uuid4())}))

    tool = MaestroEffectTool("sandbox", "1", "synthetic", "non_reconcilable", lambda value: value, provider)

    async def creator(on, send, memory, analytics, settings):
        nonlocal handler_calls
        handler_calls += 1
        assert await run_maestro_effect(tool, ordinal=0, arguments={"units": 1}) == {"applied": True}
        assert on.message == "Olá" and on.context.workspace_id is None
        assert current_execution_context().agent_id == envelope["instance_agent_id"]
        assert settings["private"] == envelope["instance_agent_id"]
        assert (await memory.get_history())[0]["role"] == "user"
        yield send.text("Resposta do wheel")

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_durable_text_delivery_adapter(config, creator, private, journal=journal, retry_policy="journal_only")
            ),
            base_url=config.runtime_endpoint,
        ) as client:
            response = await client.post(
                config.runtime_endpoint,
                content=encoded(envelope),
                headers={
                    "Authorization": "Bearer " + sign(key, binding["dispatch_header"], binding["dispatch_claims"]),
                    "Maestro-Execution-Capability": capability,
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 200 and response.json()["state"] == "responded", response.text
        assert len(outputs) == 1 and completions[-1]["outcome"] == "responded"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_durable_text_delivery_adapter(config, creator, private, journal=journal, retry_policy="journal_only")
            ),
            base_url=config.runtime_endpoint,
        ) as client:
            response = await client.post(
                config.runtime_endpoint,
                content=encoded(envelope),
                headers={
                    "Authorization": "Bearer " + sign(key, binding["dispatch_header"], binding["dispatch_claims"]),
                    "Maestro-Execution-Capability": capability,
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 503
        assert handler_calls == 1 and len(outputs) == len(completions) == 1
        assert (await database.fetchrow("SELECT count(*) AS total FROM sandbox_effects"))["total"] == 1
        assert (await database.fetchrow("SELECT state FROM maestro_journal.phases"))["state"] == "confirmed"
        assert current_execution_context() is None
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "maestro-heartbeat"]
    finally:
        await persistence.disconnect()
        await admin.execute(f"DROP DATABASE {name}")
        await admin.execute(f"DROP ROLE {role}")
        await admin.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(server_tasks))
    print(
        json.dumps(
            {
                "installed_package": True,
                "real_loopback_http_fixture": True,
                "text_complete_confirmed": True,
                "durable_duplicate_no_second_handler_or_provider": True,
                "sandbox_actual_effect_count": 1,
                "private_postgresql": True,
                "private_instance_scope": True,
                "cleanup_and_close": True,
                "no_test_dependencies": True,
            }
        )
    )


asyncio.run(main())
