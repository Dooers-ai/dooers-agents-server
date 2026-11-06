"""Real local TLS connections with sandbox hostnames; not a deployed Cloud Run test."""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dooers.agents.server.maestro import (
    MaestroCallbackClient,
    MaestroContextClient,
    MaestroCredentialRequest,
    MaestroCurrentLease,
    MaestroDeliveryAdapter,
)
from dooers.agents.server.maestro.config import MaestroConfigurationError
from dooers.agents.server.maestro.journal_transport import JournalServiceKey, sign_request, verify_request
from dooers.agents.server.maestro.remote import MaestroRemoteError, MaestroRemoteTransport, cloud_run_transport, sandbox_url
from dooers.agents.server.maestro.remote_credentials import remote_core_credentials
from dooers.agents.server.protocol.maestro import MaestroContracts
from tests.test_maestro_admission import config_for, json_bytes, sign, signed_request, timestamp
from tests.test_maestro_context import AGENT, prepare
from tests.test_maestro_executor import CallbackFixture

CORE = "https://maestro-core-68544666733.southamerica-east1.run.app"
API = "https://maestro-api-68544666733.southamerica-east1.run.app"
AGENT_URL = "https://maestro-agent-test-68544666733.southamerica-east1.run.app"


def identity_token(aud, *, exp=None):
    payload = base64.urlsafe_b64encode(json.dumps({"aud": aud, "exp": exp or int(time.time()) + 300}).encode()).decode().rstrip("=")
    return f"e30.{payload}.fixture"


async def id_token(aud):
    return identity_token(aud)


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000/path",
        CORE + "/a/../b",
        CORE + "/%2e",
        CORE + "?query=1",
        CORE + "#fragment",
        CORE + ".evil.example/",
        "https://another-123.southamerica-east1.run.app/",
        CORE + ":444/",
        CORE + "/a//b",
    ],
)
def test_remote_url_rejects_untrusted_or_ambiguous_destinations(url):
    with pytest.raises(MaestroRemoteError):
        sandbox_url(url)


def test_profile_is_explicit_and_does_not_widen_local_mode(key):
    local = config_for(key)
    transport = MaestroRemoteTransport((CORE, API, AGENT_URL), id_token)
    with pytest.raises(MaestroConfigurationError):
        replace(local, remote_transport=transport)
    with pytest.raises(MaestroConfigurationError):
        replace(local, callback_base_url=API + "/api/v1")
    remote = replace(
        local,
        allow_local_http=False,
        remote_transport=transport,
        core_base_url=CORE,
        callback_base_url=API + "/api/v1",
        runtime_endpoint=AGENT_URL + "/maestro/deliveries",
    )
    assert remote.delivery_path == "/maestro/deliveries"
    with pytest.raises(MaestroRemoteError):
        replace(remote, core_base_url="https://other-68544666733.southamerica-east1.run.app")


async def test_oidc_audience_and_expiry_are_checked_before_transport():
    async def wrong(_aud):
        return identity_token(CORE)

    transport = MaestroRemoteTransport((CORE, API), wrong)
    with pytest.raises(MaestroRemoteError):
        await transport.headers(API + "/api/v1")

    async def expired(aud):
        return identity_token(aud, exp=int(time.time()) - 1)

    with pytest.raises(MaestroRemoteError):
        await MaestroRemoteTransport((API,), expired).headers(API)


@asynccontextmanager
async def tls_fixture(key, tmp_path, monkeypatch, *, trusted=True, wrong_hostname=False):
    fixture = CallbackFixture(key)
    fixture.url = "http://127.0.0.1:9000/api/v1"
    contracts = MaestroContracts()
    memory, _, _, _ = prepare(key, contracts, fixture)
    scopes = ("processing:write", "responses:write", "deliveries:read", "context:read")
    claims = deepcopy(AGENT)
    original = memory._client.config
    claims.update(
        sub=original.instance_agent_id,
        agent_id=original.instance_agent_id,
        runtime_id=original.runtime_id,
        organization_id=original.organization_id,
        workspace_id=original.workspace_id,
        iat=int(time.time()) - 1,
        exp=int(time.time()) + 180,
        scopes=list(scopes),
    )
    fixture.core_token = sign(key, {"typ": "maestro-access+jwt", "alg": "RS256", "kid": "core-fixture"}, claims)
    fixture.token_requests = []
    fixture.token_status = 200
    fixture.token_extra = {}
    fixture.token_override = None
    fixture.tls_errors = []
    fixture.journal_key = None
    fixture.journal_calls = 0
    fixture.bad_journal_signature = False

    async def handle(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            first, *lines = raw.decode().split("\r\n")
            headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines if ": " in line)}
            body_raw = await reader.readexactly(int(headers.get("content-length", "0")))
            audience = "https://" + headers["host"]
            assert headers["x-serverless-authorization"] == "Bearer " + identity_token(audience)
            if first.startswith("POST /api/v2/maestro/agent-token "):
                fixture.token_requests.append((headers, json.loads(body_raw)))
                assert headers["authorization"] == "Bearer " + "runtime-secret".ljust(32, "x")
                body = fixture.token_override or {
                    "access_token": fixture.core_token,
                    "token_type": "Bearer",
                    "expires_in": 180,
                    "scope": {
                        "organization_id": identity.organization_id,
                        "workspace_id": identity.workspace_id,
                        "scope_kind": "personal" if identity.workspace_id is None else "workspace",
                    },
                    "scopes": list(scopes),
                }
                data = json_bytes(body)
                extra = "".join(f"{k}: {v}\r\n" for k, v in fixture.token_extra.items())
                writer.write(
                    (
                        f"HTTP/1.1 {fixture.token_status} Fixture\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(data)}\r\nConnection: close\r\n{extra}\r\n"
                    ).encode()
                    + data
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            elif fixture.journal_key and first.split()[1] == "/internal/journal/projection":
                from dooers.agents.server.maestro.journal_transport import sign_response

                fixture.journal_calls += 1
                data = b'{"decision":"applied"}'
                signature = sign_response(fixture.journal_key, data, body_raw, headers["maestro-journal-authorization"], 200)
                if fixture.bad_journal_signature:
                    signature = "invalid"
                writer.write(
                    (
                        "HTTP/1.1 200 Fixture\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(data)}\r\nMaestro-Journal-Authorization: {signature}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    + data
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            else:
                replay = asyncio.StreamReader()
                replay.feed_data(raw + body_raw)
                replay.feed_eof()
                await fixture.handle(replay, writer)
        except (ConnectionError, ssl.SSLError):
            writer.close()
        except Exception as error:
            fixture.tls_errors.append(type(error).__name__)
            writer.close()

    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sandbox TLS fixture")])
    hosts = ["wrong.example"] if wrong_hostname else [u.removeprefix("https://") for u in (CORE, API, AGENT_URL)]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
        .sign(tls_key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(tls_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    from httpcore._backends.anyio import AnyIOBackend

    connect = AnyIOBackend.connect_tcp

    async def local_connect(backend, host, requested_port, *args, **kwargs):
        text = host.decode() if isinstance(host, bytes) else host
        if text in [u.removeprefix("https://") for u in (CORE, API, AGENT_URL)]:
            return await connect(backend, "127.0.0.1", port, *args, **kwargs)
        raise AssertionError("unexpected network destination")

    # Only route the test socket. HTTP Host, SNI and certificate verification keep the remote hostname.
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", local_connect)
    if trusted:
        transport_class = httpx.AsyncHTTPTransport
        trust = ssl.create_default_context(cafile=str(cert_path))
        # Add this fixture CA only. TLS and hostname verification remain enabled.
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **kw: transport_class(*a, **{**kw, "verify": trust}))
        # AsyncClient's internal constructor imports its own transport symbol.
        from httpx import _client as httpx_client

        monkeypatch.setattr(httpx_client, "AsyncHTTPTransport", httpx.AsyncHTTPTransport)
    transport = MaestroRemoteTransport((CORE, API, AGENT_URL), id_token)
    identity = MaestroCredentialRequest(
        original.organization_id, original.workspace_id, original.instance_agent_id, original.runtime_id, scopes
    )
    fixture.resolver = remote_core_credentials(CORE, transport, identity, lambda: "runtime-secret".ljust(32, "x"))
    fixture.identity = identity
    fixture.config = replace(
        original,
        allow_local_http=False,
        remote_transport=transport,
        core_base_url=CORE,
        callback_base_url=API + "/api/v1",
        runtime_endpoint=AGENT_URL + "/maestro/deliveries",
        credential_resolver=fixture.resolver,
    )

    def dispatch(data):
        data["envelope"]["callback_base_url"] = fixture.config.callback_base_url
        data["configured_callback_base_url"] = fixture.config.callback_base_url

    fixture.envelope, fixture.headers = signed_request(key, fixture.config, change=dispatch)
    fixture.deadline = time.time() + 120
    fixture.admission = MaestroDeliveryAdapter(fixture.config).authenticate(json_bytes(fixture.envelope), fixture.headers)
    fixture.lease = MaestroCurrentLease(fixture.envelope["execution_id"], fixture.envelope["lease_fence"], timestamp(time.time() + 50))
    try:
        yield fixture
        assert not fixture.tls_errors
    finally:
        server.close()
        await server.wait_closed()


async def test_real_tls_core_context_heartbeat_output_complete(key, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    async with tls_fixture(key, tmp_path, monkeypatch) as f:
        context = MaestroContextClient(f.config, f.admission, f.lease)
        page = await context.page(f.envelope["context_cursor"])
        assert page["history"]["items"]
        callback = MaestroCallbackClient(f.config, f.admission, f.lease)
        await callback.heartbeat()
        output = await callback.output(
            {
                "output_id": "output:1",
                "type": "message.create",
                "payload": {"content": [{"type": "text", "text": "HTTPS confirmado"}], "mentions": []},
            },
            1,
        )
        result = await callback.complete("responded", "responded", (output["event"]["event_id"],))
        assert result.confirmed and result.response["state"] == "responded"
        assert len(f.token_requests) == 4
        assert [entry[0] for entry in f.requests] == ["context", "heartbeat", "outputs", "complete"]


@pytest.mark.parametrize("trusted,wrong_hostname", [(False, False), (True, True)])
async def test_real_tls_rejects_untrusted_ca_or_wrong_hostname(key, tmp_path, monkeypatch, trusted, wrong_hostname):
    async with tls_fixture(key, tmp_path, monkeypatch, trusted=trusted, wrong_hostname=wrong_hostname) as f:
        with pytest.raises(MaestroRemoteError):
            await f.resolver(f.identity)
        assert not f.token_requests


async def test_real_tls_does_not_follow_redirect_or_expand_identity(key, tmp_path, monkeypatch):
    async with tls_fixture(key, tmp_path, monkeypatch) as f:
        with pytest.raises(MaestroRemoteError):
            await f.resolver(replace(f.identity, instance_agent_id="11111111-1111-4111-8111-111111111111"))
        assert not f.token_requests
        f.token_status = 302
        f.token_extra = {"Location": CORE + "/redirected"}
        with pytest.raises(MaestroRemoteError):
            await f.resolver(f.identity)
        assert len(f.token_requests) == 1


async def test_remote_journal_preserves_signed_audience():
    transport = MaestroRemoteTransport((API,), id_token)
    service_key = JournalServiceKey(
        "journal",
        b"x" * 32,
        API + "/internal/journal/projection",
        "projection",
        "11111111-1111-4111-8111-111111111111",
        None,
        "22222222-2222-4222-8222-222222222222",
        transport,
    )
    raw = b"{}"
    header = sign_request(service_key, raw)
    verify_request(service_key, raw, header)
    from dooers.agents.server.maestro.journal_store import JournalError

    with pytest.raises(JournalError):
        verify_request(replace(service_key, audience=API + "/other"), raw, header)


def test_cloud_run_factory_refuses_personal_or_other_project_environment(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "maestro-agent-test")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other")
    with pytest.raises(MaestroRemoteError):
        cloud_run_transport((API,), service_name="maestro-agent-test")


@pytest.mark.parametrize(
    "project,email,allowed",
    [
        ("sandbox-499421", "maestro-agent-test@sandbox-499421.iam.gserviceaccount.com", True),
        ("other", "maestro-agent-test@sandbox-499421.iam.gserviceaccount.com", False),
        ("sandbox-499421", "raphael.rissoli@dooers.ai", False),
        ("sandbox-499421", "other@sandbox-499421.iam.gserviceaccount.com", False),
    ],
)
async def test_metadata_checks_project_and_assigned_service_before_token(monkeypatch, project, email, allowed):
    monkeypatch.setenv("K_SERVICE", "maestro-agent-test")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "sandbox-499421")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("PUBSUB_EMULATOR_HOST", raising=False)
    calls = []

    def metadata(request):
        calls.append(request)
        assert request.url.host == "169.254.169.254"
        assert request.headers["metadata-flavor"] == "Google"
        if request.url.path.endswith("/project-id"):
            value = project
        elif request.url.path.endswith("/email"):
            value = email
        else:
            assert request.url.params["audience"] == API
            value = identity_token(API)

        class MetadataStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield value.encode()

        return httpx.Response(200, stream=MetadataStream(), headers={"Metadata-Flavor": "Google"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(metadata)))
    transport = cloud_run_transport((API,), service_name="maestro-agent-test")
    if allowed:
        headers = await transport.headers(API + "/api/v1")
        assert set(headers) == {"X-Serverless-Authorization"}
        assert len(calls) == 3
    else:
        with pytest.raises(MaestroRemoteError):
            await transport.headers(API + "/api/v1")
        assert len(calls) == 2


async def test_real_tls_journal_verifies_response_signature(key, tmp_path, monkeypatch):
    from dooers.agents.server.maestro.journal_store import JournalError
    from dooers.agents.server.maestro.journal_transport import post_operation

    async with tls_fixture(key, tmp_path, monkeypatch) as f:
        service_key = JournalServiceKey(
            "journal",
            b"x" * 32,
            API + "/internal/journal/projection",
            "projection",
            f.identity.organization_id,
            f.identity.workspace_id,
            f.identity.instance_agent_id,
            f.config.remote_transport,
        )
        f.journal_key = service_key
        assert await post_operation(service_key, {}) == {"decision": "applied"}
        f.bad_journal_signature = True
        with pytest.raises(JournalError):
            await post_operation(service_key, {})
        assert f.journal_calls == 2
