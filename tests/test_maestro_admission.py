from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from dooers.agents.server import AgentConfig, AgentServer, MaestroConfig
from dooers.agents.server.exceptions import ThreadManagementModeError
from dooers.agents.server.handlers.pipeline import HandlerPipeline
from dooers.agents.server.maestro import (
    MaestroAdmissionError,
    MaestroConfigurationError,
    MaestroConfirmedResult,
    MaestroCurrentLease,
    MaestroDeliveryAdapter,
)
from dooers.agents.server.maestro.security import strict_json
from dooers.agents.server.protocol.maestro import MaestroContracts, verify_contract_bundle

CASES = json.loads(verify_contract_bundle()["fixtures.json"])["cases"]
BASE = next(c["data"] for c in CASES if c["schema"] == "Delivery.DispatchBinding" and c["valid"])


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()


def sign(key, header, claims):
    def part(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    message = part(json_bytes(header)) + "." + part(json_bytes(claims))
    signature = key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return message + "." + part(signature)


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def no_credential(_request):
    raise AssertionError("Admission must not resolve instance credentials")


def config_for(key, endpoint="http://127.0.0.1:9000/api/dev/agent/maestro/deliveries", **overrides):
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return MaestroConfig(
        runtime_endpoint=endpoint,
        callback_base_url=BASE["configured_callback_base_url"],
        core_base_url="http://127.0.0.1:8788/api/v1",
        organization_id=BASE["envelope"]["organization_id"],
        workspace_id=None,
        instance_agent_id=BASE["authorized_instance_agent_id"],
        runtime_id=BASE["authorized_runtime_id"],
        trusted_public_keys={BASE["dispatch_header"]["kid"]: public},
        credential_resolver=no_credential,
        allow_local_http=True,
        **overrides,
    )


def signed_request(key, config, *, change=None):
    data = deepcopy(BASE)
    now = int(time.time())
    data["envelope"]["deadline_at"] = timestamp(now + 120)
    data["envelope"]["delivery_expires_at"] = timestamp(now + 3600)
    for claims in (data["dispatch_claims"], data["capability_claims"]):
        claims.update(iat=now - 1, exp=now + 59, deadline_at=timestamp(now + 120))
    data["dispatch_claims"]["aud"] = config.runtime_endpoint
    if change:
        change(data)
    data["dispatch_claims"]["body_sha256"] = hashlib.sha256(json_bytes(data["envelope"])).hexdigest()
    headers = {
        "Authorization": "Bearer " + sign(key, data["dispatch_header"], data["dispatch_claims"]),
        "Maestro-Execution-Capability": sign(key, data["capability_header"], data["capability_claims"]),
        "Content-Type": "application/json",
    }
    return data["envelope"], headers


class Gate:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result

    async def admit(self, admission):
        self.calls += 1
        if self.result is not None:
            return self.result
        body = admission.envelope
        return MaestroCurrentLease(body["execution_id"], body["lease_fence"], timestamp(time.time() + 50))


class Executor:
    def __init__(self, state="silent", confirmed=True):
        self.calls = 0
        self.state = state
        self.confirmed = confirmed

    async def execute(self, admission, lease):
        self.calls += 1
        body = admission.envelope
        assert lease.execution_id == body["execution_id"]
        return MaestroConfirmedResult(
            {"delivery_id": body["delivery_id"], "execution_id": body["execution_id"], "state": self.state, "result_event_ids": []},
            self.confirmed,
        )


async def request(adapter, body, headers, path=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter), base_url="http://127.0.0.1:9000") as client:
        return await client.post(path or adapter.config.delivery_path, content=body, headers=headers)


def test_defaults_legacy_import_and_configuration(private_key):
    old = AgentConfig(database_type="postgres")
    assert old.thread_management == "agent" and old.maestro is None
    AgentServer(old)
    old.require_agent_thread_management()
    with pytest.raises(MaestroConfigurationError, match="^MAESTRO_CONFIG_REQUIRED$"):
        AgentConfig(database_type="postgres", thread_management="maestro")
    with pytest.raises(MaestroConfigurationError):
        AgentConfig(database_type="postgres", thread_management="unknown")
    with pytest.raises(MaestroConfigurationError):
        AgentConfig(database_type="postgres", maestro={"token": "secret"})
    configured = AgentConfig(database_type="postgres", thread_management="maestro", maestro=config_for(private_key))
    AgentServer(configured)
    configured.maestro = None
    with pytest.raises(MaestroConfigurationError):
        AgentServer(configured)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allow_local_http", False),
        ("runtime_endpoint", " http://127.0.0.1:9000/maestro/deliveries"),
        ("runtime_endpoint", "http://127.0.0.1:9000/maestro/deliveries?"),
        ("runtime_endpoint", "http://127.0.0.1:9000/maestro/deliveries#"),
        ("runtime_endpoint", "https://remote.invalid/maestro/deliveries"),
        ("runtime_endpoint", "http://localhost:9000/maestro/deliveries"),
        ("runtime_endpoint", "http://127.0.0.1:9000/api/../maestro/deliveries"),
        ("runtime_endpoint", "http://127.0.0.1:9000/maestro/deliveries?token=secret"),
        ("runtime_endpoint", "http://user:secret@127.0.0.1:9000/maestro/deliveries"),
        ("runtime_endpoint", "http://127.0.0.1:9000/maestro/deliveries#secret"),
        ("runtime_endpoint", "http://127.0.0.1:9000/other"),
        ("workspace_id", ""),
        ("organization_id", "wrong"),
        ("credential_resolver", None),
        ("max_concurrency", 0),
        ("max_concurrency", True),
        ("max_dispatch_bytes", 999999),
        ("request_timeout_seconds", 300),
        ("execution_deadline_seconds", 30),
        ("lease_seconds", 30),
        ("trusted_public_keys", {}),
        ("trusted_public_keys", {"bad/kid": b"bad"}),
        ("trusted_public_keys", {"kid": b"private" * 50}),
    ],
)
def test_bad_configuration_is_closed(private_key, field, value):
    with pytest.raises(MaestroConfigurationError) as error:
        replace(config_for(private_key), **{field: value})
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "key_factory",
    [
        lambda: rsa.generate_private_key(public_exponent=65537, key_size=1024),
        lambda: ec.generate_private_key(ec.SECP256R1()),
    ],
)
def test_reject_small_rsa_and_ec(key_factory):
    with pytest.raises(MaestroConfigurationError, match="^MAESTRO_KEYSET_INVALID$"):
        config_for(key_factory())


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("dispatch_header", "alg", "none"),
        ("dispatch_header", "alg", "HS256"),
        ("dispatch_header", "typ", "JWT"),
        ("capability_header", "typ", "maestro-dispatch+jwt"),
        ("dispatch_header", "kid", "unknown"),
        ("dispatch_header", "jku", "https://evil.invalid/key"),
        ("dispatch_header", "x5u", "https://evil.invalid/key"),
        ("dispatch_header", "crit", []),
        ("dispatch_header", "jwk", {}),
        ("dispatch_claims", "iss", "dooers-service-core"),
        ("dispatch_claims", "aud", "http://127.0.0.1:9000/maestro/deliveries"),
        ("capability_claims", "aud", "agent-server"),
        ("dispatch_claims", "exp", 1),
        ("capability_claims", "exp", 1),
        ("dispatch_claims", "iat", 9999999999),
        ("dispatch_claims", "organization_id", "00000099-0000-4000-8000-000000000000"),
        ("dispatch_claims", "lease_fence", "2"),
        ("capability_claims", "execution_id", "00000099-0000-4000-8000-000000000000"),
        ("envelope", "runtime_id", "00000099-0000-4000-8000-000000000000"),
        ("envelope", "instance_agent_id", "00000099-0000-4000-8000-000000000000"),
        ("envelope", "recipient_participant_id", "00000099-0000-4000-8000-000000000000"),
        ("envelope", "workspace_id", "00000099-0000-4000-8000-000000000000"),
        ("envelope", "callback_base_url", "http://127.0.0.1:8787/evil"),
        ("envelope", "context_cursor", ""),
        ("envelope", "extra", "secret"),
    ],
)
async def test_invalid_signed_binding_never_calls_ports(private_key, section, key, value):
    config = config_for(private_key)
    gate, executor = Gate(), Executor()
    adapter = MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor)
    body, headers = signed_request(private_key, config, change=lambda data: data[section].update({key: value}))
    response = await request(adapter, json_bytes(body), headers)
    assert response.status_code == 401
    assert gate.calls == executor.calls == 0
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "secret" not in response.text
    assert MaestroContracts().is_valid("Public.Error", response.json())


async def test_whole_scope_forged_consistently_still_rejected(private_key):
    config = config_for(private_key)
    adapter = MaestroDeliveryAdapter(config)

    def change(data):
        for part in ("envelope", "dispatch_claims", "capability_claims"):
            data[part]["organization_id"] = "00000099-0000-4000-8000-000000000000"

    body, headers = signed_request(private_key, config, change=change)
    assert (await request(adapter, json_bytes(body), headers)).status_code == 401


async def test_wrong_signature_and_payload_mutation(private_key):
    config = config_for(private_key)
    gate, executor = Gate(), Executor()
    adapter = MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body, headers = signed_request(other, config)
    assert (await request(adapter, json_bytes(body), headers)).status_code == 401
    body, headers = signed_request(private_key, config)
    body["context_cursor"] = "altered-input-with-same-identifiers"
    assert (await request(adapter, json_bytes(body), headers)).status_code == 401
    assert gate.calls == executor.calls == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda token: token + ".extra",
        lambda token: token + "=",
        lambda token: token.replace(".", "..", 1),
        lambda token: "a" * 16401,
        lambda token: token.split(".")[0] + ".not_json." + token.split(".")[2],
    ],
)
async def test_malformed_jwt(private_key, mutation):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    headers["Authorization"] = "Bearer " + mutation(headers["Authorization"][7:])
    assert (await request(MaestroDeliveryAdapter(config), json_bytes(body), headers)).status_code in {400, 401}


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b"\xff", b'{"a":NaN}', b'{"a":Infinity}', b"\xef\xbb\xbf{}"])
def test_json_strict(raw):
    with pytest.raises((ValueError, UnicodeError)):
        strict_json(raw)


async def test_noop_ports_and_unconfirmed_or_noncanonical_result_closed(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    for adapter in [
        MaestroDeliveryAdapter(config),
        MaestroDeliveryAdapter(config, lease_gate=Gate()),
        MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=Executor(confirmed=False)),
        MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=Executor(state="running")),
        MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=Executor(state="responded")),
    ]:
        response = await request(adapter, json_bytes(body), headers)
        assert response.status_code == 503
        assert not adapter.capabilities


@pytest.mark.parametrize("state", ["silent", "failed", "canceled"])
async def test_confirmed_result_only_after_both_ports(private_key, state):
    config = config_for(private_key)
    gate, executor = Gate(), Executor(state)
    adapter = MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor)
    body, headers = signed_request(private_key, config)
    response = await request(adapter, json_bytes(body), headers)
    assert response.status_code == 200 and response.json()["state"] == state
    assert gate.calls == executor.calls == 1


async def test_stale_current_lease_rejects_before_executor(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    for lease in [
        MaestroCurrentLease(body["execution_id"], "2", timestamp(time.time() + 10)),
        MaestroCurrentLease(body["execution_id"], "1", timestamp(time.time() - 1)),
        MaestroCurrentLease(body["execution_id"], "1", timestamp(time.time() + 400)),
    ]:
        executor = Executor()
        response = await request(MaestroDeliveryAdapter(config, lease_gate=Gate(lease), executor=executor), json_bytes(body), headers)
        assert response.status_code == 409
        assert executor.calls == 0


async def test_reconciled_duplicate_does_not_execute(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    result = MaestroConfirmedResult(
        {"delivery_id": body["delivery_id"], "execution_id": body["execution_id"], "state": "silent", "result_event_ids": []}, True
    )
    executor = Executor()
    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(result), executor=executor)
    assert (await request(adapter, json_bytes(body), headers)).status_code == 200
    assert executor.calls == 0


async def test_request_bounds_origin_cookie_route_and_body(private_key):
    config = config_for(private_key)
    gate, executor = Gate(), Executor()
    adapter = MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor)
    body, headers = signed_request(private_key, config)
    for extra in [{"Origin": "http://127.0.0.1"}, {"Cookie": "session=secret"}, {"Content-Encoding": "gzip"}]:
        assert (await request(adapter, json_bytes(body), headers | extra)).status_code == 403
    assert (await request(adapter, b" " * 65537, headers)).status_code == 413
    assert (await request(adapter, json_bytes(body), headers, path="/maestro/deliveries")).status_code == 400
    assert (await request(adapter, json_bytes(body), headers, path=config.delivery_path + "?secret=1")).status_code == 400
    assert gate.calls == executor.calls == 0


async def test_semantic_json_order_unicode_and_integral_numbers(private_key):
    config = config_for(private_key)
    gate, executor = Gate(), Executor()
    adapter = MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor)
    body, headers = signed_request(private_key, config, change=lambda d: d["envelope"].update(context_cursor="context-😀-ç-\u2028"))
    # Mesmo hash assinado; diferentes ordem, whitespace, escapes e 1.0 canônico.
    body["attempt_number"] = 1.0
    wire = json.dumps(dict(reversed(list(body.items()))), indent=2, ensure_ascii=True).encode()
    assert (await request(adapter, wire, headers)).status_code == 200


@pytest.mark.parametrize("method", ["dispatch", "ingest", "memory", "repository", "chat_upload"])
async def test_legacy_entries_reject_before_persistence(private_key, method):
    server = AgentServer(AgentConfig(database_type="postgres", thread_management="maestro", maestro=config_for(private_key)))
    server._ensure_initialized = AsyncMock(side_effect=AssertionError("DB must not initialize"))
    kwargs = {
        "dispatch": {"handler": None, "agent_id": "a", "message": "persisted"},
        "ingest": {"agent_id": "a", "message": "persisted"},
        "memory": {"thread_id": "legacy-text-id"},
        "repository": {},
        "chat_upload": {"agent_id": "a", "data": b"secret", "filename": "a", "mime_type": "text/plain"},
    }[method]
    with pytest.raises(ThreadManagementModeError, match="^THREAD_MANAGEMENT_MISMATCH$"):
        await getattr(server, method)(**kwargs)
    server._ensure_initialized.assert_not_called()


async def test_pipeline_defense_before_canonical_write(private_key):
    config = AgentConfig(database_type="postgres", thread_management="maestro", maestro=config_for(private_key))
    pipeline = HandlerPipeline(persistence=AsyncMock(), agent_config=config)
    with pytest.raises(ThreadManagementModeError):
        await pipeline.setup(None)
    assert not pipeline._persistence.mock_calls


async def test_real_loopback_http_prefix_and_signature(private_key):
    # Uvicorn é ferramenta isolada de teste, não dependência do SDK/adaptador ASGI.
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/api/dev/agent/maestro/deliveries"
    config = config_for(private_key, endpoint)
    gate, executor = Gate(), Executor()
    adapter = MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor)
    server = uvicorn.Server(uvicorn.Config(adapter, lifespan="off", ws="none", log_config=None, access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        body, headers = signed_request(private_key, config)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(endpoint, content=json_bytes(body), headers=headers)
            assert response.status_code == 200
            headers["Authorization"] = headers["Maestro-Execution-Capability"]
            denied = await client.post(endpoint, content=json_bytes(body), headers=headers)
            assert denied.status_code == 401
        assert gate.calls == executor.calls == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def test_current_gate_denial_and_exception_never_execute(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    for exception in [MaestroAdmissionError("FORBIDDEN"), RuntimeError("private-token-secret")]:
        gate = Gate()
        gate.admit = AsyncMock(side_effect=exception)
        executor = Executor()
        response = await request(MaestroDeliveryAdapter(config, lease_gate=gate, executor=executor), json_bytes(body), headers)
        assert response.status_code in {403, 503}
        assert executor.calls == 0 and "private-token-secret" not in response.text


@pytest.mark.parametrize("kind", ["expired", "future", "ttl", "deadline"])
async def test_clock_claim_boundaries(private_key, kind):
    config = config_for(private_key)

    def change(data):
        now = int(time.time())
        claims = data["dispatch_claims"]
        if kind == "expired":
            claims.update(iat=now - 59, exp=now)
        elif kind == "future":
            claims.update(iat=now + 30)
        elif kind == "ttl":
            claims.update(iat=now - 299, exp=now + 2)
        else:
            claims.update(exp=now + 200)

    body, headers = signed_request(private_key, config, change=change)
    assert (await request(MaestroDeliveryAdapter(config), json_bytes(body), headers)).status_code == 401


async def test_duplicate_json_jwt_header_and_noncanonical_base64(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    token = headers["Authorization"][7:]
    first, second, _third = token.split(".")
    header_bytes = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
    duplicate = header_bytes[:-1] + b',"alg":"RS256"}'
    duplicate_first = base64.urlsafe_b64encode(duplicate).rstrip(b"=").decode()
    message = duplicate_first + "." + second
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    headers["Authorization"] = "Bearer " + message + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    assert (await request(MaestroDeliveryAdapter(config), json_bytes(body), headers)).status_code == 401
    # Último sexteto RS256 tem quatro bits não usados; alterar só esses bits
    # decodifica para os mesmos bytes, mas representação JWT não é canônica.
    headers["Authorization"] = "Bearer " + token[:-1] + ("B" if token[-1] == "A" else chr(ord(token[-1]) + 1))
    assert (await request(MaestroDeliveryAdapter(config), json_bytes(body), headers)).status_code == 401


async def test_http_duplicate_auth_headers(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    duplicate = list(headers.items()) + [("authorization", headers["Authorization"])]
    assert (await request(MaestroDeliveryAdapter(config), json_bytes(body), duplicate)).status_code == 400


async def test_backpressure_has_no_local_wait_queue_and_cancels_inside_request(private_key):
    config = config_for(private_key, max_concurrency=1)
    entered, finished = asyncio.Event(), asyncio.Event()

    class WaitingExecutor:
        async def execute(self, _admission, _lease):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=WaitingExecutor())
    body, headers = signed_request(private_key, config)
    running = asyncio.create_task(request(adapter, json_bytes(body), headers))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        overload = await request(adapter, json_bytes(body), headers)
        assert overload.status_code == 429 and overload.headers["retry-after"] == "1"
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert finished.is_set() and adapter._active == 0


async def test_mounted_asgi_prefix_and_no_unconfirmed_success(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=Executor())
    queue = asyncio.Queue()
    await queue.put({"type": "http.request", "body": json_bytes(body), "more_body": False})
    messages = []

    async def send(message):
        messages.append(message)

    await adapter(
        {
            "type": "http",
            "method": "POST",
            "path": "/maestro/deliveries",
            "root_path": "/api/dev/agent",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        },
        queue.get,
        send,
    )
    assert messages[0]["status"] == 200


async def test_disconnect_cancels_executor_before_response(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    entered, finished = asyncio.Event(), asyncio.Event()

    class WaitingExecutor:
        async def execute(self, _admission, _lease):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=WaitingExecutor())
    queue = asyncio.Queue()
    await queue.put({"type": "http.request", "body": json_bytes(body), "more_body": False})
    messages = []

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(
        adapter(
            {
                "type": "http",
                "method": "POST",
                "path": config.delivery_path,
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            },
            queue.get,
            send,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    await queue.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 5)
    assert finished.is_set() and messages[0]["status"] == 503


async def test_admission_minimum_lease_margin(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    executor = Executor()
    lease = MaestroCurrentLease(body["execution_id"], body["lease_fence"], timestamp(time.time() + 19))
    response = await request(MaestroDeliveryAdapter(config, lease_gate=Gate(lease), executor=executor), json_bytes(body), headers)
    assert response.status_code == 409 and executor.calls == 0
    with pytest.raises(MaestroConfigurationError):
        replace(config, lease_seconds=20, heartbeat_seconds=5)


async def test_ws_thread_commands_reject_without_db_and_settings_stay_authenticated(private_key):
    from dooers.agents.server.handlers.router import Router
    from dooers.agents.server.protocol import frames
    from dooers.agents.server.registry import ConnectionRegistry

    config = AgentConfig(database_type="postgres", thread_management="maestro", maestro=config_for(private_key))
    persistence = AsyncMock()
    router = Router(persistence=persistence, handler=None, registry=ConnectionRegistry(), subscriptions={}, agent_config=config)
    ws = AsyncMock()
    # Tipos internos já parseados pelo boundary legado. Nenhum campo do payload
    # é consultado porque o modo rejeita estes comandos primeiro.
    for name in [
        "C2S_ThreadList",
        "C2S_ThreadSubscribe",
        "C2S_ThreadUnsubscribe",
        "C2S_ThreadDelete",
        "C2S_ThreadParticipantsAdd",
        "C2S_EventCreate",
        "C2S_EventList",
        "C2S_ThreadArtifactsList",
        "C2S_Feedback",
    ]:
        frame = getattr(frames, name).model_construct(id="synthetic-frame", payload=None)
        await router.route(ws, frame)
        message = json.loads(ws.send_text.call_args.args[0])
        assert message["payload"]["error"]["code"] == "THREAD_MANAGEMENT_MISMATCH"
    assert not persistence.mock_calls
    # Guard não substitui as permissões normais de settings: request não
    # conectado segue rejeitado pelo handler existente, sem acessar DB.
    frame = frames.C2S_SettingsPatch(type="settings.patch", id="settings-frame", payload={"field_id": "private", "value": "secret"})
    await router.route(ws, frame)
    message = json.loads(ws.send_text.call_args.args[0])
    assert message["payload"]["error"]["code"] != "THREAD_MANAGEMENT_MISMATCH"
    assert not persistence.mock_calls


async def test_private_settings_analytics_and_database_still_available(private_key):
    from dooers.agents.server import SqlDatabase
    from dooers.agents.server.persistence.postgres import PostgresPersistence

    config = AgentConfig(database_type="postgres", thread_management="maestro", maestro=config_for(private_key))
    server = AgentServer(config)
    persistence = PostgresPersistence(host="127.0.0.1", port=1, user="synthetic", database="private", password="")
    from dooers.agents.server.features.settings.models import SettingsSchema

    config.settings_schema = SettingsSchema(fields=[])
    server._settings_broadcaster = AsyncMock()
    persistence.get_settings = AsyncMock(return_value={"private": "synthetic"})
    server._ensure_initialized = AsyncMock(return_value=persistence)
    assert isinstance(await server.database(), SqlDatabase)
    settings = await server.settings(config.maestro.instance_agent_id)
    assert await settings.get_all() == {"private": "synthetic"}
    assert await server.analytics(config.maestro.instance_agent_id) is not None
    assert server._guest_cleanup_task is None


async def test_maestro_initialization_does_not_start_legacy_cleanup(private_key, monkeypatch):
    import dooers.agents.server.observability.otel as otel
    import dooers.agents.server.server as module

    config = AgentConfig(database_type="postgres", thread_management="maestro", maestro=config_for(private_key), analytics_enabled=False)
    server = AgentServer(config)
    persistence = AsyncMock()
    monkeypatch.setattr(module, "_build_persistence", lambda _config: persistence)
    monkeypatch.setattr(otel, "init_otel", lambda **_kwargs: None)
    await server.ensure_initialized()
    try:
        assert server._guest_cleanup_task is None
        persistence.connect.assert_awaited_once()
        persistence.migrate.assert_awaited_once()
        assert not persistence.create_thread.called and not persistence.create_event.called
    finally:
        await server.close()


async def test_signed_duplicate_claim_and_duplicate_body_members(private_key):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    first, second, _third = headers["Authorization"][7:].split(".")
    raw = base64.urlsafe_b64decode(second + "=" * (-len(second) % 4))
    raw = raw[:-1] + b',"iss":"dooers-service-maestro"}'
    second = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    message = first + "." + second
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    headers["Authorization"] = "Bearer " + message + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    adapter = MaestroDeliveryAdapter(config)
    assert (await request(adapter, json_bytes(body), headers)).status_code == 401
    body, headers = signed_request(private_key, config)
    duplicate = json_bytes(body)[:-1] + b',"protocol_version":1}'
    assert (await request(adapter, duplicate, headers)).status_code == 401


async def test_admission_timeout_never_calls_executor(private_key):
    config = config_for(private_key, admission_timeout_seconds=1)
    body, headers = signed_request(private_key, config)
    finished = asyncio.Event()

    class SlowGate:
        async def admit(self, _admission):
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    executor = Executor()
    response = await request(MaestroDeliveryAdapter(config, lease_gate=SlowGate(), executor=executor), json_bytes(body), headers)
    assert response.status_code == 503 and executor.calls == 0 and finished.is_set()


async def test_body_read_timeout_and_chunked_oversize(private_key):
    config = config_for(private_key, network_timeout_seconds=1)
    _body, headers = signed_request(private_key, config)
    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(), executor=Executor())
    for message, expected in [(None, 503), ({"type": "http.request", "body": b"x" * 65537, "more_body": True}, 413)]:
        queue = asyncio.Queue()
        if message:
            await queue.put(message)
        sent = []

        async def send(frame):
            sent.append(frame)

        await adapter(
            {
                "type": "http",
                "path": config.delivery_path,
                "method": "POST",
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            },
            queue.get,
            send,
        )
        assert sent[0]["status"] == expected
    assert adapter._lease_gate.calls == adapter._executor.calls == 0


def test_keyset_and_admitted_data_do_not_leak_or_mutate(private_key):
    config = config_for(private_key)
    assert "BEGIN PUBLIC KEY" not in repr(config)
    with pytest.raises(TypeError):
        config.trusted_public_keys["other"] = b"bad"
    adapter = MaestroDeliveryAdapter(config)
    body, headers = signed_request(private_key, config)
    admission = adapter.authenticate(json_bytes(body), headers)
    snapshot = admission.envelope
    snapshot["context_cursor"] = "mutated"
    claims = admission.capability_claims
    claims["operations"].clear()
    assert admission.envelope["context_cursor"] == body["context_cursor"]
    assert admission.capability_claims["operations"]
    assert headers["Maestro-Execution-Capability"] not in repr(admission)
    assert body["context_cursor"] not in repr(admission)


@pytest.mark.parametrize("lease_until", ["2099-10-13T00:00:00", "not-a-date", None, 9999999999])
async def test_gate_snapshot_timestamp_is_canonical(private_key, lease_until):
    config = config_for(private_key)
    body, headers = signed_request(private_key, config)
    executor = Executor()
    lease = MaestroCurrentLease(body["execution_id"], body["lease_fence"], lease_until)
    adapter = MaestroDeliveryAdapter(config, lease_gate=Gate(lease), executor=executor)
    response = await request(adapter, json_bytes(body), headers)
    assert response.status_code == 409 and executor.calls == 0
