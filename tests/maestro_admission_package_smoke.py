"""Rodar com python -I fora do checkout após instalar wheel e extra maestro."""

import asyncio
import base64
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from dooers.agents.server import AgentConfig, MaestroConfig, MaestroDeliveryAdapter
from dooers.agents.server.maestro import MaestroConfirmedResult, MaestroCurrentLease
from dooers.agents.server.protocol.maestro import verify_contract_bundle


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def token(key, header, claims):
    def part(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    message = part(canonical(header)) + "." + part(canonical(claims))
    return message + "." + part(key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256()))


async def main():
    assert "site-packages" in str(Path(sys.modules[MaestroConfig.__module__].__file__))
    assert find_spec("pytest") is None and find_spec("uvicorn") is None
    assert AgentConfig(database_type="postgres").thread_management == "agent"
    cases = json.loads(verify_contract_bundle()["fixtures.json"])["cases"]
    data = next(c["data"] for c in cases if c["schema"] == "Delivery.DispatchBinding" and c["valid"])
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def resolver(_request):
        raise AssertionError("No credential call during admission")

    config = MaestroConfig(
        runtime_endpoint=data["configured_runtime_endpoint"],
        callback_base_url=data["configured_callback_base_url"],
        core_base_url="http://127.0.0.1:8788",
        organization_id=data["envelope"]["organization_id"],
        workspace_id=None,
        instance_agent_id=data["authorized_instance_agent_id"],
        runtime_id=data["authorized_runtime_id"],
        trusted_public_keys={
            data["dispatch_header"]["kid"]: key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        },
        credential_resolver=resolver,
        allow_local_http=True,
    )
    now = int(time.time())
    deadline = datetime.fromtimestamp(now + 120, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    data["envelope"].update(deadline_at=deadline, delivery_expires_at=deadline)
    for claims in (data["dispatch_claims"], data["capability_claims"]):
        claims.update(iat=now - 1, exp=now + 59, deadline_at=deadline)
    data["dispatch_claims"]["body_sha256"] = hashlib.sha256(canonical(data["envelope"])).hexdigest()
    headers = {
        "Authorization": "Bearer " + token(key, data["dispatch_header"], data["dispatch_claims"]),
        "Maestro-Execution-Capability": token(key, data["capability_header"], data["capability_claims"]),
        "Content-Type": "application/json",
    }
    calls = []

    class Gate:
        async def admit(self, admission):
            calls.append("gate")
            return MaestroCurrentLease(admission.envelope["execution_id"], "1", deadline)

    class Executor:
        async def execute(self, admission, _lease):
            calls.append("executor")
            body = admission.envelope
            return MaestroConfirmedResult(
                {"delivery_id": body["delivery_id"], "execution_id": body["execution_id"], "state": "silent", "result_event_ids": []}, True
            )

    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=Executor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter), base_url="http://127.0.0.1:9000") as client:
        result = await client.post(config.delivery_path, content=canonical(data["envelope"]), headers=headers)
        assert result.status_code == 200 and calls == ["gate", "executor"]
        data["envelope"]["context_cursor"] = "tampered"
        result = await client.post(config.delivery_path, content=canonical(data["envelope"]), headers=headers)
        assert result.status_code == 401 and calls == ["gate", "executor"]
    assert adapter.capabilities == ()
    print(
        json.dumps(
            {
                "installed_package": True,
                "cryptographic_admission": True,
                "tampering_rejected": True,
                "trusted_fixture_ports_only": True,
                "test_dependencies_absent": True,
            }
        )
    )


asyncio.run(main())
