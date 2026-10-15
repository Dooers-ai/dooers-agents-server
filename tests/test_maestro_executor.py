"""Transporte HTTP real com fixtures; não é integração core/PG Maestro E05-11."""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.maestro import (
    MaestroCallbackClient,
    MaestroCallbackError,
    MaestroCurrentLease,
    MaestroDeliveryAdapter,
    MaestroEventSink,
    MaestroInstanceCredential,
    MaestroTextExecutor,
    create_text_delivery_adapter,
    current_maestro_context,
)
from dooers.agents.server.maestro.security import _decode, strict_json
from dooers.agents.server.protocol.maestro import MaestroContracts
from dooers.tools.rag.runtime import current_execution_context
from tests.test_maestro_admission import BASE, json_bytes, request, sign, signed_request, timestamp
from tests.test_maestro_context import AGENT, prepare, private


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return MaestroContracts()


class CallbackFixture:
    def __init__(self, key):
        self.key = key
        self.requests = []
        self.outputs = {}
        self.completions = []
        self.tasks = set()
        self.drop_output = 0
        self.drop_complete = False
        self.status = {}
        self.renew_change = None
        self.delay_heartbeat = 0
        self.mutate_response = None
        self.heartbeat_count = 0
        self.callback_seen = asyncio.Event()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            first, *lines = raw.decode().split("\r\n")
            headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines if ": " in line)}
            body_raw = await reader.readexactly(int(headers.get("content-length", "0")))
            body = strict_json(body_raw) if body_raw else None
            action = "context" if "/context?" in first else first.split()[1].rsplit("/", 1)[-1]
            self.requests.append((action, headers, body, body_raw))
            self.callback_seen.set()
            assert headers["authorization"] == f"Bearer {self.core_token}"
            assert headers["maestro-execution-capability"] != self.core_token
            claims = strict_json(_decode(headers["maestro-execution-capability"].split(".")[1]))
            code = self.status.get(action, 200)
            extra = {}
            if action == "context":
                result = self.response
            elif action == "heartbeat":
                self.heartbeat_count += 1
                result = {"lease_until": timestamp(min(time.time() + 59, self.deadline))}
                now = int(time.time())
                if now + 60 > claims["exp"] or self.renew_change:
                    renewal = {**claims, "iat": now, "exp": min(now + 60, int(self.deadline)), "jti": str(uuid4())}
                    if self.renew_change:
                        self.renew_change(renewal)
                    extra["Maestro-Execution-Capability"] = sign(self.key, BASE["capability_header"], renewal)
                await asyncio.sleep(self.delay_heartbeat)
            elif action == "stream":
                result = {"status": "accepted", "ephemeral": True, "output_id": body["output_id"], "chunk_index": body["chunk_index"]}
            elif action == "outputs":
                key = headers["idempotency-key"]
                semantic = {k: v for k, v in body.items() if k != "lease_fence"}
                previous = self.outputs.get(key)
                if previous:
                    if previous[0] != semantic:
                        code = 409
                    result = {**previous[1], "duplicate": True}
                else:
                    event = deepcopy(self.response["trigger_event"])
                    event.update(
                        event_id=str(uuid4()),
                        sequence=str(9007199254740993 + len(self.outputs)),
                        actor={
                            "kind": "agent",
                            "principal_id": self.envelope["instance_agent_id"],
                            "instance_agent_id": self.envelope["instance_agent_id"],
                            "participant_id": self.envelope["recipient_participant_id"],
                            "display_name": "A",
                        },
                        causation_event_id=self.envelope["trigger_event_id"],
                        delivery_id=self.envelope["delivery_id"],
                        execution_id=self.envelope["execution_id"],
                        output_id=body["output_id"],
                        payload={**body["payload"], "execution_id": self.envelope["execution_id"], "output_id": body["output_id"]},
                    )
                    result = {"status": "persisted", "duplicate": False, "event": event, "cursor": "cursor-output"}
                    self.outputs[key] = (deepcopy(semantic), deepcopy(result))
                if self.drop_output:
                    self.drop_output -= 1
                    return
            else:
                assert action == "complete"
                result = {
                    "delivery_id": self.envelope["delivery_id"],
                    "execution_id": self.envelope["execution_id"],
                    "state": body["outcome"],
                    "result_event_ids": [v[1]["event"]["event_id"] for v in self.outputs.values()] + [str(uuid4())],
                }
                self.completions.append(deepcopy(body))
                if self.drop_complete:
                    return
            if self.mutate_response:
                result = self.mutate_response(action, deepcopy(result))
            raw = json_bytes(result)
            headers = {"Content-Type": "application/json", "Content-Length": str(len(raw)), "Connection": "close", **extra}
            writer.write(
                f"HTTP/1.1 {code} Fixture\r\n".encode() + "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode() + b"\r\n" + raw
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)


@asynccontextmanager
async def setup(key, contracts, *, config_change=None, dispatch_change=None):
    fixture = CallbackFixture(key)
    server = await asyncio.start_server(fixture.handle, "127.0.0.1", 0)
    fixture.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/v1"
    memory, _, _, _ = prepare(key, contracts, fixture)
    config = memory._client.config
    claims = deepcopy(AGENT)
    claims.update(
        sub=config.instance_agent_id,
        agent_id=config.instance_agent_id,
        runtime_id=config.runtime_id,
        organization_id=config.organization_id,
        workspace_id=config.workspace_id,
        iat=int(time.time()) - 1,
        exp=int(time.time()) + 180,
        scopes=["processing:write", "responses:write", "deliveries:read", "context:read"],
    )
    fixture.core_token = sign(key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "core-fixture"}, claims)
    fixture.credentials = []

    async def credential(value):
        fixture.credentials.append(value)
        return MaestroInstanceCredential(fixture.core_token, claims["exp"])

    config = replace(config, credential_resolver=credential)
    if config_change:
        config = replace(config, **config_change)

    def change(data):
        data["envelope"]["callback_base_url"] = fixture.url
        data["configured_callback_base_url"] = fixture.url
        if dispatch_change:
            dispatch_change(data)

    fixture.envelope, fixture.headers = signed_request(key, config, change=change)
    fixture.deadline = time.time() + 120
    fixture.config = config
    fixture.admission = MaestroDeliveryAdapter(config).authenticate(json_bytes(fixture.envelope), fixture.headers)
    fixture.lease = MaestroCurrentLease(fixture.envelope["execution_id"], fixture.envelope["lease_fence"], timestamp(time.time() + 50))
    try:
        yield fixture
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(fixture.tasks):
            task.cancel()
        await asyncio.gather(*tuple(fixture.tasks), return_exceptions=True)


async def execute_fixture(fixture, handler):
    adapter = create_text_delivery_adapter(fixture.config, handler, private)
    result = await request(adapter, json_bytes(fixture.envelope), fixture.headers)
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "maestro-heartbeat"]
    assert current_maestro_context() is None and current_execution_context() is None
    return result


@pytest.mark.parametrize(
    "kind,expected,reason",
    [
        ("empty", "silent", "implicit_empty"),
        ("explicit", "silent", "explicit_silent"),
        ("reason", "silent", "not_relevant"),
        ("text", "responded", "responded"),
        ("error", "failed", "handler_error"),
        ("partial_error", "failed", "handler_error"),
        ("silent_after_text", "failed", "handler_contract_error"),
        ("run_failed", "failed", "handler_contract_error"),
        ("run_only", "silent", "implicit_empty"),
    ],
)
async def test_outcome_is_confirmed_only_after_callback(private_key, contracts, kind, expected, reason):
    async with setup(private_key, contracts) as fixture:

        async def handler(on, send, memory, analytics, settings):
            assert on.context.maestro.execution_id == fixture.envelope["execution_id"]
            assert settings["settings"] == on.context.agent_id
            assert analytics["analytics"] == on.context.agent_id
            assert (await memory.get_thread()).thread_id == on.context.thread_id
            if kind in {"text", "partial_error", "silent_after_text"}:
                yield send.text("Olá mundo")
            if kind == "text":
                yield send.text("Outra resposta completa")
            if kind in {"error", "partial_error"}:
                raise RuntimeError("secret-not-in-response")
            if kind in {"explicit", "silent_after_text", "reason"}:
                yield send.silence("not_relevant" if kind == "reason" else None)
            if kind in {"run_failed", "run_only"}:
                yield send.run_start()
                yield send.run_end(status="failed" if kind == "run_failed" else "succeeded")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == expected
        assert fixture.completions[-1]["reason_code"] == reason
        assert "secret-not-in-response" not in response.text
        assert len(fixture.outputs) == (2 if kind == "text" else 1 if kind in {"partial_error", "silent_after_text"} else 0)
        assert all("outputs" not in value for value in fixture.completions)
        assert fixture.credentials[0].scopes == ("processing:write",)
        assert create_text_delivery_adapter(fixture.config, handler, private).capabilities == ()


async def test_coroutine_empty_handler(private_key, contracts):
    async with setup(private_key, contracts) as fixture:

        async def handler(*_args):
            return None

        assert (await execute_fixture(fixture, handler)).json()["state"] == "silent"


@pytest.mark.parametrize(
    "method", ["image", "reasoning", "tool_call", "form", "thread_update", "participant_add", "event_update", "whatsapp"]
)
async def test_unsupported_never_publishes(private_key, contracts, method):
    async with setup(private_key, contracts) as fixture:

        async def handler(on, send, *_args):
            if method == "thread_update":
                yield send.update_thread(title="bad")
            elif method == "participant_add":
                yield send.add_participant("bad")
            elif method == "event_update":
                yield send.update_user_event(on.context.event_id, [])
            elif method == "whatsapp":
                yield send.whatsapp.text("bad", to_e164="+5511111111111", instance_id="bad")
            else:
                yield AgentEvent(method, {})

        response = await execute_fixture(fixture, handler)
        assert response.status_code != 202
        assert response.status_code in {200, 403}
        if response.status_code == 200:
            assert response.json()["state"] == "failed"
        assert not fixture.outputs


async def test_output_ack_loss_reuses_same_bytes_key_and_event(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        fixture.drop_output = 1

        async def handler(_on, send, *_args):
            yield send.text("uma intenção")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 200 and response.json()["state"] == "responded"
        outputs = [r for r in fixture.requests if r[0] == "outputs"]
        assert len(outputs) == 2 and len(fixture.outputs) == 1
        assert outputs[0][1]["idempotency-key"] == outputs[1][1]["idempotency-key"]
        assert outputs[0][3] == outputs[1][3]


@pytest.mark.parametrize("failure", ["complete_ack", "output_acks", "complete_409", "output_409", "heartbeat_403"])
async def test_uncertainty_and_revocation_never_silent_or_false_success(private_key, contracts, failure):
    async with setup(private_key, contracts) as fixture:
        fixture.drop_complete = failure == "complete_ack"
        fixture.drop_output = 2 if failure == "output_acks" else 0
        if failure.endswith("409"):
            fixture.status["complete" if failure.startswith("complete") else "outputs"] = 409
        if failure == "heartbeat_403":
            fixture.status["heartbeat"] = 403
        calls = []

        async def handler(_on, send, *_args):
            calls.append(True)
            yield send.text("texto")

        response = await execute_fixture(fixture, handler)
        assert response.status_code in {403, 409, 503}
        assert not any(c["outcome"] == "silent" for c in fixture.completions)
        if failure == "heartbeat_403":
            assert not calls
        if failure == "complete_ack":
            assert len(fixture.completions) == 1
            assert len([r for r in fixture.requests if r[0] == "complete"]) == 1


async def test_heartbeat_failure_cancels_handler_and_closes_context(private_key, contracts):
    async with setup(private_key, contracts, config_change={"heartbeat_seconds": 1}) as fixture:
        closed = asyncio.Event()
        contexts = []

        async def handler(on, _send, memory, *_args):
            contexts.append((on.context.maestro, memory))
            fixture.status["heartbeat"] = 409
            try:
                await asyncio.sleep(5)
            finally:
                closed.set()
            if False:
                yield

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 409 and closed.is_set()
        assert contexts[0][0].cancellation.cancelled
        with pytest.raises(Exception):
            await contexts[0][1].get_thread()
        assert not fixture.completions and not fixture.outputs


async def test_handler_timeout_reserves_failed_completion(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        adapter = create_text_delivery_adapter(fixture.config, lambda: None, private)
        closed = []

        async def handler(*_args):
            try:
                await asyncio.sleep(5)
            finally:
                closed.append(True)
            if False:
                yield

        executor = MaestroTextExecutor(fixture.config, handler, private, contracts=contracts)
        admission = replace(fixture.admission, deadline_monotonic=time.monotonic() + 41)
        result = await executor.execute(admission, fixture.lease)
        assert result.confirmed and result.response["state"] == "failed" and closed
        assert fixture.completions[-1]["reason_code"] == "handler_timeout"
        assert adapter.capabilities == ()


async def test_external_cancellation_never_completes_or_leaks_sdk_task(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        started, ended = asyncio.Event(), asyncio.Event()

        async def handler(*_args):
            started.set()
            try:
                await asyncio.sleep(5)
            finally:
                ended.set()
            if False:
                yield

        task = asyncio.create_task(MaestroTextExecutor(fixture.config, handler, private).execute(fixture.admission, fixture.lease))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and not fixture.completions
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "maestro-heartbeat"]


@pytest.mark.parametrize("mutation", ["binding", "operations", "deadline", "jti", "exp"])
async def test_gate_rejects_invalid_renewal_before_handler(private_key, contracts, mutation):
    async with setup(private_key, contracts) as fixture:

        def change(claims):
            if mutation == "binding":
                claims["delivery_id"] = str(uuid4())
            elif mutation == "operations":
                claims["operations"] = ["delivery.heartbeat"]
            elif mutation == "deadline":
                claims["deadline_at"] = timestamp(time.time() + 121)
            else:
                claims[mutation] = fixture.admission.capability_claims[mutation]

        fixture.renew_change = change

        async def handler(*_args):
            pytest.fail("handler must not run")
            yield

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 401


async def test_same_adapter_concurrent_dispatch_runs_handler_once(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(*_args):
            calls.append(True)
            started.set()
            await release.wait()
            if False:
                yield

        adapter = create_text_delivery_adapter(fixture.config, handler, private)
        first = asyncio.create_task(request(adapter, json_bytes(fixture.envelope), fixture.headers))
        await asyncio.wait_for(started.wait(), 5)
        duplicate = await request(adapter, json_bytes(fixture.envelope), fixture.headers)
        assert duplicate.status_code == 409 and len(calls) == 1
        release.set()
        assert (await first).status_code == 200
        assert not adapter._executing and adapter._active == 0


async def test_sink_retry_keeps_semantic_identity_across_attempts(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        for attempt in (1, 2):
            admission = replace(fixture.admission, _envelope={**fixture.admission.envelope, "attempt_number": attempt})
            client = MaestroCallbackClient(fixture.config, admission, fixture.lease, contracts=contracts)
            sink = MaestroEventSink(client)
            await sink.accept(AgentSend().text("fixo"))
            if attempt == 1:
                original = sink.intents[0]
                receipt_id = sink.event_ids[0]
            else:
                assert sink.intents[0] == original and sink.event_ids == (receipt_id,)
                await sink.persist(original)
            client.close()
        assert len(fixture.outputs) == 1


@pytest.mark.parametrize("disconnect", [False, True])
async def test_runtime_and_callbacks_real_http_loopback(private_key, contracts, disconnect):
    import uvicorn

    async with setup(private_key, contracts) as fixture:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}/api/dev/agent/maestro/deliveries"
        config = replace(fixture.config, runtime_endpoint=endpoint)

        ended = asyncio.Event()

        async def handler(_on, send, *_args):
            try:
                if disconnect:
                    await asyncio.sleep(5)
                yield send.text("real socket")
            finally:
                ended.set()

        adapter = create_text_delivery_adapter(config, handler, private)
        server = uvicorn.Server(uvicorn.Config(adapter, lifespan="off", ws="none", log_config=None, access_log=False, log_level="critical"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)

            def change(data):
                data["envelope"]["callback_base_url"] = fixture.url
                data["configured_callback_base_url"] = fixture.url

            body, headers = signed_request(private_key, config, change=change)
            async with httpx.AsyncClient(trust_env=False) as client:
                if disconnect:
                    with pytest.raises(httpx.ReadTimeout):
                        await client.post(endpoint, content=json_bytes(body), headers=headers, timeout=0.3)
                    await asyncio.wait_for(ended.wait(), 5)
                    assert not fixture.outputs and not fixture.completions
                else:
                    response = await client.post(endpoint, content=json_bytes(body), headers=headers)
                    assert response.status_code == 200 and response.json()["state"] == "responded"
                    assert len(fixture.outputs) == len(fixture.completions) == 1
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()


async def test_gate_renewal_crosses_old_expiration_and_executor_uses_new_token(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        observed = []

        async def handler(_on, send, *_args):
            observed.append(True)
            yield send.text("renovado")

        adapter = create_text_delivery_adapter(fixture.config, handler, private)

        def change(data):
            data["envelope"]["callback_base_url"] = fixture.url
            data["configured_callback_base_url"] = fixture.url
            data["capability_claims"]["exp"] = int(time.time()) + 2

        body, headers = signed_request(private_key, fixture.config, change=change)
        fixture.delay_heartbeat = 3
        response = await request(adapter, json_bytes(body), headers)
        assert response.status_code == 200, response.text
        assert observed
        old_token = headers["Maestro-Execution-Capability"]
        old_exp = strict_json(_decode(old_token.split(".")[1]))["exp"]
        assert time.time() > old_exp
        following = [r for r in fixture.requests if r[0] != "heartbeat"]
        assert following and all(r[1]["maestro-execution-capability"] != old_token for r in following)


@pytest.mark.parametrize(
    "kind", ["wrong_thread", "wrong_author", "wrong_output", "wrong_content", "complete_wrong_state", "complete_missing_output"]
)
async def test_mismatched_callback_receipt_never_confirms(private_key, contracts, kind):
    async with setup(private_key, contracts) as fixture:

        def mutate(action, value):
            if action == "outputs":
                if kind == "wrong_thread":
                    value["event"]["thread_id"] = str(uuid4())
                elif kind == "wrong_author":
                    value["event"]["actor"]["instance_agent_id"] = str(uuid4())
                elif kind == "wrong_output":
                    value["event"]["output_id"] = "output:12"
                elif kind == "wrong_content":
                    value["event"]["payload"]["content"][0]["text"] = "trocado"
            elif action == "complete":
                if kind == "complete_wrong_state":
                    value["state"] = "silent"
                elif kind == "complete_missing_output":
                    value["result_event_ids"] = []
            return value

        fixture.mutate_response = mutate

        async def handler(_on, send, *_args):
            yield send.text("original")

        response = await execute_fixture(fixture, handler)
        assert response.status_code in {409, 503}


async def test_sink_limits_and_repeated_existing_output_at_limit(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        client = MaestroCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
        sink = MaestroEventSink(client)
        for number in range(98):
            await sink.accept(AgentSend().text(f"output {number}"))
        await sink.persist(sink.intents[0])
        assert len(fixture.outputs) == len(sink.event_ids) == 98
        with pytest.raises(MaestroCallbackError, match="PAYLOAD_TOO_LARGE"):
            await sink.accept(AgentSend().text("99"))
        client.close()


async def test_child_task_cannot_use_sink_or_private_context(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        client = MaestroCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
        sink = MaestroEventSink(client)

        async def child():
            assert current_maestro_context() is None and current_execution_context() is None
            with pytest.raises(MaestroCallbackError, match="LEASE_STALE"):
                await sink.accept(AgentSend().text("child"))

        await asyncio.create_task(child())
        assert not fixture.outputs
        client.close()


@pytest.mark.parametrize("external_first", [True, False])
async def test_external_cancel_racing_heartbeat_preserves_cancel(private_key, contracts, monkeypatch, external_first):
    async with setup(private_key, contracts, config_change={"heartbeat_seconds": 1}) as fixture:
        started, ended = asyncio.Event(), asyncio.Event()
        owner = None

        async def heartbeat(_client, _progress="running"):
            assert owner is not None
            if external_first:
                owner.cancel()
            else:
                asyncio.get_running_loop().call_soon(owner.cancel)
            raise MaestroCallbackError("LEASE_STALE")

        monkeypatch.setattr(MaestroCallbackClient, "heartbeat", heartbeat)

        async def handler(*_args):
            started.set()
            try:
                await asyncio.sleep(5)
            finally:
                ended.set()
            if False:
                yield

        owner = asyncio.create_task(MaestroTextExecutor(fixture.config, handler, private).execute(fixture.admission, fixture.lease))
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert ended.is_set() and owner.cancelled()
        assert not fixture.completions
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "maestro-heartbeat"]


async def test_conflicting_output_reports_permanent_code_without_retry(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        fixture.status["outputs"] = 409

        def mutate(action, value):
            if action == "outputs":
                return {
                    "error": {"code": "IDEMPOTENCY_CONFLICT", "message": "fixture conflict", "retryable": False, "request_id": str(uuid4())}
                }
            return value

        fixture.mutate_response = mutate

        async def handler(_on, send, *_args):
            yield send.text("conflito")

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert response.json()["error"]["retryable"] is False
        assert len([r for r in fixture.requests if r[0] == "outputs"]) == 1
        assert not fixture.completions


async def test_expired_lease_denies_callback_before_network(private_key, contracts):
    async with setup(private_key, contracts) as fixture:
        client = MaestroCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
        client._lease = replace(fixture.lease, lease_until=timestamp(time.time() - 1))
        with pytest.raises(MaestroCallbackError, match="LEASE_STALE"):
            await client.complete("silent", "implicit_empty")
        assert not fixture.requests


async def test_invalid_renewal_signature_denies_handler(private_key, contracts):
    from cryptography.hazmat.primitives.asymmetric import rsa

    async with setup(private_key, contracts) as fixture:
        fixture.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fixture.renew_change = lambda claims: None

        async def handler(*_args):
            pytest.fail("invalid renewal must not run handler")
            yield

        response = await execute_fixture(fixture, handler)
        assert response.status_code == 401 and not fixture.completions


@pytest.mark.parametrize("status,code", [(413, "PAYLOAD_TOO_LARGE"), (422, "QUOTA_EXCEEDED"), (429, "QUOTA_EXCEEDED")])
async def test_permanent_payload_and_quota_never_retry_or_silence(private_key, contracts, status, code):
    async with setup(private_key, contracts) as fixture:
        fixture.status["outputs"] = status

        def mutate(action, value):
            if action == "outputs":
                return {"error": {"code": code, "message": "fixture", "retryable": code == "QUOTA_EXCEEDED", "request_id": str(uuid4())}}
            return value

        fixture.mutate_response = mutate

        async def handler(_on, send, *_args):
            yield send.text("rejeitado")

        response = await execute_fixture(fixture, handler)
        assert len([r for r in fixture.requests if r[0] == "outputs"]) == 1
        assert not any(c["outcome"] == "silent" for c in fixture.completions)
        if code == "QUOTA_EXCEEDED":
            assert response.status_code == 429 and response.json()["error"]["code"] == code
            assert not fixture.completions
        else:
            assert response.status_code == 200 and response.json()["state"] == "failed"
        client = MaestroCallbackClient(fixture.config, fixture.admission, fixture.lease, contracts=contracts)
        with pytest.raises(MaestroCallbackError) as caught:
            await client.output(
                {
                    "output_id": "output:1",
                    "type": "message.create",
                    "payload": {"content": [{"type": "text", "text": "outro"}], "mentions": []},
                },
                1,
            )
        assert caught.value.code == code and caught.value.unknown is False
