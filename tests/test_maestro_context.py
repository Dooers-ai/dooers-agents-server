from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.handlers.incoming import AgentIncoming
from dooers.agents.server.maestro import MaestroCurrentLease, MaestroDeliveryAdapter, MaestroInstanceCredential
from dooers.agents.server.maestro.client import MaestroContextClient, MaestroContextError
from dooers.agents.server.maestro.context import (
    MaestroPrivateResources,
    current_maestro_context,
    maestro_handler_context,
)
from dooers.agents.server.maestro.converters import history_messages
from dooers.agents.server.maestro.memory import MaestroMemory
from dooers.agents.server.protocol.maestro import MaestroContracts, verify_contract_bundle
from dooers.tools.rag.runtime import bind_execution_context, current_execution_context, reset_execution_context
from dooers.tools.whatsapp.runtime import require_agent_id, require_persistence
from tests.test_maestro_admission import BASE, config_for, json_bytes, sign, signed_request, timestamp

CASES = json.loads(verify_contract_bundle()["fixtures.json"])["cases"]
PUBLIC = next(c["data"] for c in CASES if c["schema"] == "Public.ContextResponse" and c["valid"])
AGENT = next(c["data"] for c in CASES if c["schema"] == "Identity.AgentTokenClaims" and c["valid"])


@pytest.fixture(scope="module")
def private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def contracts():
    return MaestroContracts()


class HTTPFixture:
    """Servidor real local de contrato; não implementa core/lease/ACL Maestro."""

    def __init__(self):
        self.requests = []
        self.responses = []
        self.response = None
        self.status = 200
        self.extra_headers = {}
        self.delay = 0
        self.tasks = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            first, *lines = raw.decode().split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines if ": " in line)
            self.requests.append((first, headers))
            await asyncio.sleep(self.delay)
            value = self.responses.pop(0) if self.responses else self.response
            if callable(value):
                value = value(first)
            body = value if isinstance(value, bytes) else json_bytes(value)
            headers = {"Content-Type": "application/json", "Content-Length": str(len(body)), "Connection": "close", **self.extra_headers}
            writer.write(f"HTTP/1.1 {self.status} Fixture\r\n".encode())
            writer.write("".join(f"{key}: {value}\r\n" for key, value in headers.items()).encode() + b"\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)


@asynccontextmanager
async def fixture_server():
    fixture = HTTPFixture()
    server = await asyncio.start_server(fixture.handle, "127.0.0.1", 0)
    fixture.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/v1"
    try:
        yield fixture
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(fixture.tasks):
            task.cancel()
        await asyncio.gather(*tuple(fixture.tasks), return_exceptions=True)


def prepare(private_key, contracts, server, *, change=None, claim_change=None, instance_id=None):
    requests = []
    config = replace(config_for(private_key), callback_base_url=server.url)
    if instance_id is not None:
        config = replace(config, instance_agent_id=instance_id)
    claims = deepcopy(AGENT)
    claims.update(
        sub=config.instance_agent_id,
        agent_id=config.instance_agent_id,
        runtime_id=config.runtime_id,
        organization_id=config.organization_id,
        workspace_id=config.workspace_id,
        iat=int(time.time()) - 1,
        exp=int(time.time()) + 90,
        scopes=["deliveries:read", "context:read"],
    )
    if claim_change:
        claim_change(claims)
    core_token = sign(private_key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "core-fixture"}, claims)

    async def credential(request):
        requests.append(request)
        return MaestroInstanceCredential(core_token, claims["exp"])

    config = replace(config, credential_resolver=credential)
    adapter = MaestroDeliveryAdapter(config)

    def dispatch(data):
        data["envelope"]["callback_base_url"] = config.callback_base_url
        data["configured_callback_base_url"] = config.callback_base_url
        if instance_id is not None:
            data["authorized_instance_agent_id"] = instance_id
            data["envelope"]["instance_agent_id"] = instance_id
            data["capability_claims"]["sub"] = instance_id
            for claim in (data["dispatch_claims"], data["capability_claims"]):
                claim["instance_agent_id"] = instance_id
        if change:
            change(data)

    body, headers = signed_request(private_key, config, change=dispatch)
    admission = adapter.authenticate(json_bytes(body), headers)
    lease = MaestroCurrentLease(admission.envelope["execution_id"], admission.envelope["lease_fence"], timestamp(time.time() + 50))
    client = MaestroContextClient(config, admission, lease, contracts=contracts)
    page = deepcopy(PUBLIC)
    envelope = admission.envelope
    trigger = page["trigger_event"]
    trigger.update(
        event_id=envelope["trigger_event_id"],
        thread_id=envelope["thread_id"],
        sequence=envelope["trigger_sequence"],
        root_event_id=envelope["root_event_id"],
    )
    human = page["participants"][0]
    recipient = {
        "participant_id": envelope["recipient_participant_id"],
        "kind": "agent",
        "principal_id": config.instance_agent_id,
        "instance_agent_id": config.instance_agent_id,
        "display_name": "Recipient",
        "role": "member",
        "state": "active",
        "joined_sequence": "1",
        "left_sequence": None,
        "capabilities": [],
        "availability": "available",
    }
    page.update(delivery_id=envelope["delivery_id"], context_cursor=envelope["context_cursor"], participants=[human, recipient])
    page["history"] = {
        "items": [deepcopy(trigger)],
        "next_cursor": None,
        "has_more": False,
        "watermark": envelope["context_cutoff_sequence"],
    }
    server.response = page
    return MaestroMemory(client), page, requests, core_token


async def private(scope):
    return MaestroPrivateResources(
        scope, {"private": scope.instance_agent_id}, {"settings": scope.instance_agent_id}, {"analytics": scope.instance_agent_id}
    )


async def test_real_http_scope_headers_fixed_cutoff_and_no_cache(private_key, contracts, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    async with fixture_server() as server:
        memory, page, requests, token = prepare(private_key, contracts, server)
        raw = await memory.get_history_raw()
        assert raw == page["history"]["items"]
        raw[0]["payload"]["content"][0]["text"] = "changed locally"
        assert (await memory.get_history_raw())[0]["payload"]["content"][0]["text"] == "Olá"
        view = await memory.get_thread()
        assert view.workspace_id is None and view.mode == "maestro"
        assert not hasattr(view, "title") and not hasattr(view, "permissions")
        assert len(requests) == len(server.requests) == 3
        for request in requests:
            assert request.scopes == ("deliveries:read", "context:read")
            assert request.instance_agent_id == memory._envelope["instance_agent_id"]
        for first, headers in server.requests:
            assert first.startswith(f"GET /api/v1/deliveries/{memory._envelope['delivery_id']}/context?cursor=")
            assert headers["Authorization"] == f"Bearer {token}"
            assert headers["Maestro-Execution-Capability"] == memory._client.admission.execution_capability
            assert "Cookie" not in headers


async def test_paginated_snapshot_latest_response_is_excluded(private_key, contracts):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        first = deepcopy(page)
        first["history"].update(
            items=[{**deepcopy(page["trigger_event"]), "event_id": str(uuid4()), "sequence": "2"}], has_more=True, next_cursor="page2"
        )
        server.responses = [first, page]
        raw = await memory.get_history_raw(order="desc")
        assert [event["sequence"] for event in raw] == ["10", "2"]
        late = deepcopy(page["trigger_event"])
        late.update(event_id=str(uuid4()), sequence="11")
        late["actor"] = {
            "kind": "agent",
            "principal_id": str(uuid4()),
            "instance_agent_id": str(uuid4()),
            "participant_id": str(uuid4()),
            "display_name": "Other agent",
        }
        page["history"]["items"].append(late)
        with pytest.raises(MaestroContextError, match="CONTEXT_INVALID"):
            await memory.get_history_raw()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(delivery_id=str(uuid4())),
        lambda p: p.update(context_cursor="other-snapshot"),
        lambda p: p["history"].update(watermark="11"),
        lambda p: p["trigger_event"].update(thread_id=str(uuid4())),
        lambda p: p["participants"][1].update(instance_agent_id=str(uuid4())),
        lambda p: p["participants"][1].update(state="removed"),
        lambda p: p["history"].update(has_more=True, next_cursor=None),
        lambda p: p["history"]["items"].append(deepcopy(p["history"]["items"][0])),
        lambda p: p.update(secret="not-in-contract"),
    ],
)
async def test_invalid_cross_binding_pages_fail_closed(private_key, contracts, change):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        change(page)
        with pytest.raises(MaestroContextError):
            await memory.get_history_raw()


@pytest.mark.parametrize("status", [401, 403, 404, 409, 410])
async def test_revoked_access_never_returns_previously_fetched_memory(private_key, contracts, status):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)
        assert await memory.get_history_raw()
        server.status = status
        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            await memory.get_history_raw()
        count = len(server.requests)
        server.status = 200
        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            await memory.get_thread()
        assert len(server.requests) == count


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(agent_id=str(uuid4()), sub=str(uuid4())),
        lambda c: c.update(organization_id=str(uuid4())),
        lambda c: c.update(workspace_id=str(uuid4())),
        lambda c: c.update(runtime_id=str(uuid4())),
        lambda c: c.update(aud="runtime"),
        lambda c: c.update(scopes=["responses:write"]),
        lambda c: c.update(exp=int(time.time()) - 1),
    ],
)
async def test_infrastructure_or_wrong_instance_credential_never_sent(private_key, contracts, change):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server, claim_change=change)
        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            await memory.get_history_raw()
        assert server.requests == []


@pytest.mark.parametrize(
    "format", ["openai", "anthropic", "google", "cohere", "voyage", "langchain", "openai_completions", "openai_responses"]
)
async def test_converters_preserve_agents_and_never_promote_system(private_key, contracts, format):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        human = deepcopy(page["trigger_event"])
        own = deepcopy(human)
        own["actor"] = {
            "kind": "agent",
            "principal_id": memory._envelope["instance_agent_id"],
            "instance_agent_id": memory._envelope["instance_agent_id"],
            "participant_id": memory._envelope["recipient_participant_id"],
            "display_name": "Own",
        }
        other = deepcopy(own)
        other["actor"].update(instance_agent_id=str(uuid4()), display_name="Other")
        system = deepcopy(human)
        system["actor"] = {"kind": "system", "principal_id": "system", "participant_id": None, "display_name": "System event"}
        result = history_messages([human, own, other, system], memory._envelope["instance_agent_id"], format)
        assert result[1]["role"] in {"assistant", "model", "CHATBOT"}
        assert all(result[index]["role"] in {"user", "USER"} for index in (0, 2, 3))
        assert "Other" in json.dumps(result[2]) and "instance_agent_id" in json.dumps(result[2])
        assert "system" not in [row["role"] for row in result]


@pytest.mark.parametrize("exit_kind", ["normal", "error", "cancel"])
async def test_context_private_recipient_binding_and_cleanup(private_key, contracts, exit_kind):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        actor = {
            "kind": "agent",
            "principal_id": str(uuid4()),
            "instance_agent_id": str(uuid4()),
            "participant_id": str(uuid4()),
            "display_name": "Sender",
        }
        page["trigger_event"]["actor"] = actor
        page["history"]["items"][0]["actor"] = actor
        token = bind_execution_context(agent_id="outer")
        try:
            try:
                async with maestro_handler_context(memory, private) as prepared:
                    assert prepared.on.context.user is None
                    assert prepared.on.context.agent_id == memory._envelope["instance_agent_id"]
                    assert current_execution_context().agent_id == prepared.on.context.agent_id
                    assert current_execution_context().on_behalf is False
                    assert current_maestro_context().actor == actor
                    assert require_agent_id() == prepared.on.context.agent_id
                    assert require_persistence() == {"private": prepared.on.context.agent_id}
                    assert prepared.settings == {"settings": prepared.on.context.agent_id}

                    async def child():
                        assert current_execution_context() is None
                        assert current_maestro_context() is None

                    await asyncio.create_task(child())
                    if exit_kind == "error":
                        raise ValueError("creator failure")
                    if exit_kind == "cancel":
                        raise asyncio.CancelledError()
            except (ValueError, asyncio.CancelledError):
                assert exit_kind != "normal"
            assert current_execution_context().agent_id == "outer"
            assert current_maestro_context() is None
            with pytest.raises(RuntimeError):
                require_persistence()
        finally:
            reset_execution_context(token)
        assert current_execution_context() is None


async def test_private_scope_mismatch_before_any_binding(private_key, contracts):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)

        async def wrong(scope):
            return MaestroPrivateResources(replace(scope, instance_agent_id=str(uuid4())), {}, {}, {})

        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            async with maestro_handler_context(memory, wrong):
                pytest.fail("must not enter")
        assert current_execution_context() is current_maestro_context() is None


async def test_legacy_incoming_bind_restore_and_no_fake_workspace():
    token = bind_execution_context(agent_id="previous")
    incoming = AgentIncoming("legacy", [], AgentContext("thread", "legacy-agent", "event"))
    assert current_execution_context().agent_id == "legacy-agent"
    incoming.reset_runtime_context()
    assert current_execution_context().agent_id == "previous"
    reset_execution_context(token)


@pytest.mark.parametrize("mode", ["redirect", "encoding", "oversize", "duplicate_json", "timeout", "repeated_cursor", "page_budget"])
async def test_network_and_pagination_bounds(private_key, contracts, mode):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        if mode == "redirect":
            server.status, server.extra_headers = 307, {"Location": "http://127.0.0.1:1/private"}
        elif mode == "encoding":
            server.extra_headers = {"Content-Encoding": "gzip"}
        elif mode == "oversize":
            server.extra_headers = {"Content-Length": "100000000"}
        elif mode == "duplicate_json":
            server.response = b'{"delivery_id":"a","delivery_id":"b"}'
        elif mode == "timeout":
            server.delay = 1.1
            memory._client.config = replace(memory._client.config, network_timeout_seconds=1)
        else:
            page["history"].update(has_more=True, next_cursor=memory._envelope["context_cursor"] if mode == "repeated_cursor" else "next")
            memory._max_pages = 1
        with pytest.raises(MaestroContextError) as captured:
            await memory.get_history_raw()
        assert str(captured.value) in {"CONTEXT_UNAVAILABLE", "CONTEXT_INVALID", "CONTEXT_LIMIT"}
        assert len(server.requests) == 1


async def test_artifact_references_are_authorized_not_downloaded(private_key, contracts):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        part = {"type": "attachment", "artifact_id": str(uuid4()), "filename": "example.png", "mime_type": "image/png", "size_bytes": 42}
        page["history"]["items"][0]["payload"]["content"].append(part)
        refs, cursor, more = await memory.list_artifacts()
        assert refs[0].artifact_id == part["artifact_id"] and refs[0].actor["kind"] == "human"
        assert cursor is None and more is False
        assert not hasattr(refs[0], "url") and not hasattr(refs[0], "data")
        page["trigger_event"]["payload"]["content"].append(part)
        with pytest.raises(MaestroContextError, match="UNSUPPORTED_CAPABILITY"):
            async with maestro_handler_context(memory, private):
                pytest.fail("unhydrated attachment")


async def test_capability_renewal_verified_same_binding_and_used_on_next_read(private_key, contracts):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)
        client = memory._client
        claims = client.admission.capability_claims
        claims.update(jti=str(uuid4()), exp=claims["exp"] + 20)
        header = deepcopy(BASE["capability_header"])
        replacement = sign(private_key, header, claims)
        client.update_capability(replacement)
        await memory.get_thread()
        assert server.requests[-1][1]["Maestro-Execution-Capability"] == replacement
        for change in [
            lambda c: c.update(instance_agent_id=str(uuid4())),
            lambda c: c.update(lease_fence="99"),
            lambda c: c.update(operations=["delivery.complete"]),
            lambda c: c.update(exp=c["exp"] - 1),
        ]:
            wrong = deepcopy(claims)
            wrong["jti"] = str(uuid4())
            change(wrong)
            with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
                client.update_capability(sign(private_key, header, wrong))
        assert client._capability == replacement


async def test_concurrent_deliveries_same_runtime_keep_private_instances_isolated(private_key, contracts):
    async with fixture_server() as first, fixture_server() as second:
        memory_a, _, _, _ = prepare(private_key, contracts, first)
        memory_b, _, _, _ = prepare(private_key, contracts, second, instance_id=str(uuid4()))
        assert memory_a._client.config.runtime_id == memory_b._client.config.runtime_id
        assert memory_a._envelope["instance_agent_id"] != memory_b._envelope["instance_agent_id"]
        entered = 0
        both = asyncio.Event()

        async def run(memory, label):
            nonlocal entered

            async def resources(scope):
                return MaestroPrivateResources(scope, {"database": label}, {"settings": label}, {"analytics": label})

            async with maestro_handler_context(memory, resources) as prepared:
                entered += 1
                if entered == 2:
                    both.set()
                await both.wait()
                assert require_persistence() == {"database": label}
                assert prepared.settings == {"settings": label}
                assert current_execution_context().agent_id == prepared.on.context.agent_id
            assert current_execution_context() is current_maestro_context() is None

        await asyncio.gather(run(memory_a, "A"), run(memory_b, "B"))


async def test_revocation_during_private_resolution_blocks_input(private_key, contracts):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)

        async def resolver(scope):
            server.status = 403
            return await private(scope)

        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            async with maestro_handler_context(memory, resolver):
                pytest.fail("revoked before handler")
        assert current_execution_context() is current_maestro_context() is None


async def test_revocation_on_second_page_discards_partial_history(private_key, contracts):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        page["history"].update(has_more=True, next_cursor="page-two")
        resolver = memory._client.config.credential_resolver
        count = 0

        async def credential(request):
            nonlocal count
            count += 1
            if count == 2:
                server.status = 403
            return await resolver(request)

        memory._client.config = replace(memory._client.config, credential_resolver=credential)
        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            await memory.get_history_raw()
        assert count == 2


@pytest.mark.parametrize("failure", ["exception", "cancel"])
async def test_private_resolution_exception_before_generator_does_not_bind(private_key, contracts, failure):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)

        async def broken(_scope):
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("private-secret must not escape")

        with pytest.raises((MaestroContextError, asyncio.CancelledError)) as captured:
            async with maestro_handler_context(memory, broken):
                pytest.fail("must not run")
        assert "private-secret" not in str(captured.value)
        assert current_execution_context() is current_maestro_context() is None


async def test_rag_nested_handles_wrong_task_and_order_rejected():
    outer = bind_execution_context(agent_id="outer")
    inner = bind_execution_context(agent_id="inner")
    with pytest.raises(RuntimeError):
        reset_execution_context(outer)

    async def child():
        with pytest.raises(RuntimeError):
            reset_execution_context(inner)

    await asyncio.create_task(child())
    assert current_execution_context().agent_id == "inner"
    reset_execution_context(inner)
    assert current_execution_context().agent_id == "outer"
    reset_execution_context(outer)
    with pytest.raises(RuntimeError):
        reset_execution_context(outer)
    assert current_execution_context() is None


async def test_resume_context_preserves_trigger_and_uses_response_cutoff(private_key, contracts):
    async with fixture_server() as server:
        interaction_id, response_id = str(uuid4()), str(uuid4())

        def change(data):
            envelope = data["envelope"]
            envelope.update(
                phase_id=interaction_id,
                attempt_number=2,
                context_cutoff_sequence="15",
                context_cursor="snapshot-15",
                resume={
                    "interaction_id": interaction_id,
                    "kind": "form",
                    "resume_of_execution_id": str(uuid4()),
                    "original_trigger_event_id": envelope["trigger_event_id"],
                    "response_event_id": response_id,
                    "response_sequence": "15",
                },
            )
            for claims in (data["dispatch_claims"], data["capability_claims"]):
                claims["phase_id"] = interaction_id

        memory, page, _, _ = prepare(private_key, contracts, server, change=change)
        response = deepcopy(page["trigger_event"])
        response.update(
            event_id=response_id,
            sequence="15",
            type="interaction.responded",
            payload={
                "interaction_id": interaction_id,
                "action": "submit",
                "shared_data": {"answer": "approved input"},
                "delivery_id": memory._envelope["delivery_id"],
                "phase_id": interaction_id,
            },
        )
        page["history"]["items"].append(response)
        async with maestro_handler_context(memory, private) as prepared:
            context = prepared.on.context.maestro
            assert context.trigger_event_id == memory._envelope["trigger_event_id"]
            assert context.origin_event_id == response_id and context.context_cutoff_sequence == "15"
            assert prepared.on.form_data == {"answer": "approved input"}
            assert prepared.on.form_cancelled is False and prepared.on.form_event_id is None
            assert prepared.on.context.user.user_id == response["actor"]["principal_id"]
            assert current_execution_context().on_behalf is False
            assert context.cancellation.cancelled is False
        assert context.cancellation.cancelled is True


@pytest.mark.parametrize(
    "change",
    [
        lambda lease: replace(lease, execution_id=str(uuid4())),
        lambda lease: replace(lease, lease_fence="999"),
        lambda lease: replace(lease, lease_until=timestamp(time.time() - 1)),
        lambda lease: replace(lease, lease_until=timestamp(time.time() + 400)),
    ],
)
async def test_bad_lease_update_does_not_replace_current_lease(private_key, contracts, change):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)
        lease = memory._client._lease
        with pytest.raises(MaestroContextError, match="CONTEXT_EXPIRED"):
            memory._client.update_lease(change(lease))
        assert memory._client._lease == lease
        assert await memory.get_thread()


async def test_jwt_expiry_during_read_uses_signed_exp_not_resolver_metadata(private_key, contracts):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)
        now = int(time.time())
        claims = deepcopy(AGENT)
        config = memory._client.config
        claims.update(
            sub=config.instance_agent_id,
            agent_id=config.instance_agent_id,
            runtime_id=config.runtime_id,
            organization_id=config.organization_id,
            workspace_id=None,
            iat=now - 1,
            exp=now + 1,
            scopes=["deliveries:read", "context:read"],
        )
        token = sign(private_key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "fixture"}, claims)

        async def credential(_request):
            return MaestroInstanceCredential(token, now + 90)

        memory._client.config = replace(config, credential_resolver=credential)
        server.delay = 1.2
        with pytest.raises(MaestroContextError, match="ACCESS_REVOKED"):
            await memory.get_history_raw()
        assert len(server.requests) == 1


async def test_cancel_and_finally_close_prevent_further_memory_requests(private_key, contracts):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)
        async with maestro_handler_context(memory, private) as prepared:
            assert await memory.get_thread()
            before = len(server.requests)
            prepared.on.context.maestro.cancellation.cancel()
            with pytest.raises(MaestroContextError, match="CONTEXT_EXPIRED"):
                await memory.get_history_raw()
            assert len(server.requests) == before
        with pytest.raises(MaestroContextError, match="CONTEXT_EXPIRED"):
            await memory.get_thread()
        assert len(server.requests) == before


async def test_child_task_cannot_use_inherited_private_context_before_or_after_scope(private_key, contracts):
    async with fixture_server() as server:
        memory, _, _, _ = prepare(private_key, contracts, server)
        ready, finished = asyncio.Event(), asyncio.Event()

        async def child():
            for access in (require_persistence, require_agent_id):
                with pytest.raises(RuntimeError, match="scope is not active"):
                    access()
            ready.set()
            await finished.wait()
            for access in (require_persistence, require_agent_id):
                with pytest.raises(RuntimeError, match="scope is not active"):
                    access()
            assert current_execution_context() is current_maestro_context() is None

        async with maestro_handler_context(memory, private):
            child_task = asyncio.create_task(child())
            await ready.wait()
            assert require_agent_id() == memory._envelope["instance_agent_id"]
        finished.set()
        await child_task
        with pytest.raises(RuntimeError):
            require_persistence()


async def test_legacy_whatsapp_binding_still_allows_existing_child_task_semantics():
    from dooers.tools.whatsapp.runtime import bind_handler_context, reset_handler_context

    persistence = object()
    tokens = bind_handler_context(persistence=persistence, agent_id="legacy")
    assert len(tokens) == 2
    try:

        async def child():
            assert require_agent_id() == "legacy" and require_persistence() is persistence

        await asyncio.create_task(child())
    finally:
        reset_handler_context(tokens)


@pytest.mark.parametrize("mutation", [None, "root", "thread", "cutoff", "recipient", "version", "history"])
async def test_invocation_event_is_bound_to_current_execution(private_key, contracts, mutation):
    async with fixture_server() as server:
        memory, page, _, _ = prepare(private_key, contracts, server)
        invocation = deepcopy(page["trigger_event"])
        invocation["payload"]["routing_decision"] = {
            "version": 1,
            "mode": "direct",
            "execution": "sequential",
            "recipients": [memory._envelope["recipient_participant_id"]],
        }
        page["invocation_event"] = invocation
        page["history"]["items"] = [deepcopy(invocation)]
        if mutation in {"root", "thread"}:
            invocation[f"{mutation}_id" if mutation == "thread" else "root_event_id"] = str(uuid4())
        elif mutation == "cutoff":
            invocation["sequence"] = str(int(memory._envelope["context_cutoff_sequence"]) + 1)
        elif mutation == "recipient":
            invocation["payload"]["routing_decision"]["recipients"] = [str(uuid4())]
        elif mutation == "version":
            invocation["payload"]["routing_decision"]["version"] = 2
        elif mutation == "history":
            page["history"]["items"][0]["payload"]["content"] = [{"type": "text", "text": "altered"}]
        if mutation:
            with pytest.raises(MaestroContextError):
                memory._validate_page(page)
        else:
            assert (await memory.snapshot())["invocation_event"] == invocation
