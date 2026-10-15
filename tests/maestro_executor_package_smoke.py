"""Smoke do wheel instalado com Python -I fora de ambos os checkouts."""

import asyncio
import base64
import hashlib
import json
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from dooers.agents.server.maestro import (
    MaestroConfig,
    MaestroDeliveryAdapter,
    MaestroInstanceCredential,
    MaestroMemory,
    MaestroPrivateResources,
    create_text_delivery_adapter,
)
from dooers.agents.server.protocol.maestro import verify_contract_bundle
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
    assert find_spec("pytest") is None and find_spec("uvicorn") is None
    cases = json.loads(verify_contract_bundle()["fixtures.json"])["cases"]
    binding = deepcopy(next(case["data"] for case in cases if case["schema"] == "Delivery.DispatchBinding" and case["valid"]))
    page = deepcopy(next(case["data"] for case in cases if case["schema"] == "Public.ContextResponse" and case["valid"]))
    agent_claims = deepcopy(next(case["data"] for case in cases if case["schema"] == "Identity.AgentTokenClaims" and case["valid"]))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    requests = []
    server_tasks = set()
    outputs = []
    completions = []
    lose_complete = False

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
                if lose_complete:
                    return
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

    async def creator(on, send, memory, analytics, settings):
        assert on.message == "Olá" and on.context.workspace_id is None
        assert current_execution_context().agent_id == envelope["instance_agent_id"]
        assert settings["private"] == envelope["instance_agent_id"]
        assert (await memory.get_history())[0]["role"] == "user"
        yield send.text("Resposta do wheel")

    async def empty(*_args):
        if False:
            yield

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_text_delivery_adapter(config, creator, private)), base_url=config.runtime_endpoint
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
        outputs.clear()
        lose_complete = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_text_delivery_adapter(config, empty, private)), base_url=config.runtime_endpoint
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
        assert response.status_code == 503 and completions[-1]["reason_code"] == "implicit_empty"
        assert current_execution_context() is None
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "maestro-heartbeat"]
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tuple(server_tasks))
    print(
        json.dumps(
            {
                "installed_package": True,
                "real_loopback_http_fixture": True,
                "text_complete_confirmed": True,
                "lost_complete_ack_no_false_200": True,
                "private_instance_scope": True,
                "cleanup_and_close": True,
                "no_test_dependencies": True,
            }
        )
    )


asyncio.run(main())
