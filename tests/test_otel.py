import asyncio
import inspect
import json

import httpx
import pytest
import respx

from dooers.agents.server.observability import otel

CORE_URL = "https://core.test"
OTEL_SERVICE_URL = "https://otel.test"
TOKEN_URL = f"{CORE_URL}/api/v2/identity/service-token/agent"
TRACES_URL = f"{OTEL_SERVICE_URL}/v1/traces"


class FakePersistence:
    def __init__(self, secrets: dict | None = None):
        self._secrets = secrets or {}

    async def get_service_secrets(self, agent_id: str) -> dict:
        return self._secrets


@pytest.fixture(autouse=True)
def _reset_otel_module_state():
    # opentelemetry.trace.set_tracer_provider() only takes effect on the first call per
    # process (later calls are a no-op with a warning) — that's a library-level limitation
    # we can't reset between tests. What we *can* reset are this module's own globals, which
    # _export_turn reads fresh on every call regardless of which init_otel() call originally
    # wired up the (single, process-wide) span processor.
    yield
    otel._otel_enabled = False
    otel._service_name = "dooers-agent"
    otel._otel_service_url = ""
    otel._token_client = None
    otel._persistence = None
    otel._llm_instrumentation = {
        "anthropic": "pending",
        "openai": "pending",
        "openai_agents": "pending",
    }


def test_start_tracker_is_noop_when_otel_disabled():
    otel._otel_enabled = False
    tracker = otel.start_tracker(thread_id="t", event_id="e", agent_id="a")
    assert isinstance(tracker, otel._NoOpTracker)


def test_init_otel_noop_without_urls():
    otel.init_otel(otel_service_url="", core_base_url="", persistence=FakePersistence())
    assert otel._otel_enabled is False


@pytest.mark.asyncio
async def test_full_turn_exports_root_and_llm_child_in_one_request():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"accessToken": "jwt-abc", "expiresIn": 300}}
            )
        )
        traces_route = mock.post(TRACES_URL).mock(return_value=httpx.Response(200))

        otel.init_otel(
            otel_service_url=OTEL_SERVICE_URL,
            core_base_url=CORE_URL,
            persistence=FakePersistence({"dooers_runtime_api_key": "rak-1"}),
            service_name="test-agent",
        )

        tracker = otel.start_tracker(
            thread_id="thread-1",
            event_id="event-1",
            agent_id="agent-xyz",
            workspace_id="workspace-1",
            organization_id="org-1",
        )
        assert not isinstance(tracker, otel._NoOpTracker)

        from opentelemetry import trace

        child = trace.get_tracer("test").start_span("Response", attributes={"openinference.span.kind": "LLM"})
        child.end()
        tracker.end()

        await asyncio.sleep(0.2)  # let the fire-and-forget export task run

    assert traces_route.called
    sent = traces_route.calls.last.request
    assert sent.headers.get("authorization") == "Bearer jwt-abc"

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    parsed = ExportTraceServiceRequest()
    parsed.ParseFromString(sent.content)
    span_names = [s.name for rs in parsed.resource_spans for ss in rs.scope_spans for s in ss.spans]
    # Root + LLM child arrive together — never split across separate export calls, since a
    # shared batch queue could otherwise mix spans (and Bearer tokens) across agents.
    assert sorted(span_names) == sorted(["Response", "agent/agent-xyz"])


@pytest.mark.asyncio
async def test_turn_without_runtime_api_key_is_not_exported():
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(TOKEN_URL).mock(return_value=httpx.Response(200))
        traces_route = mock.post(TRACES_URL).mock(return_value=httpx.Response(200))

        otel.init_otel(
            otel_service_url=OTEL_SERVICE_URL,
            core_base_url=CORE_URL,
            persistence=FakePersistence({}),  # no dooers_runtime_api_key
        )

        tracker = otel.start_tracker(
            thread_id="thread-1", event_id="event-1", agent_id="agent-xyz", workspace_id="workspace-1"
        )
        tracker.end()
        await asyncio.sleep(0.2)

    assert not token_route.called
    assert not traces_route.called


@pytest.mark.asyncio
async def test_turn_without_workspace_id_is_still_exported():
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"accessToken": "jwt-personal", "expiresIn": 300}}
            )
        )
        traces_route = mock.post(TRACES_URL).mock(return_value=httpx.Response(200))

        otel.init_otel(
            otel_service_url=OTEL_SERVICE_URL,
            core_base_url=CORE_URL,
            persistence=FakePersistence({"dooers_runtime_api_key": "rak-1"}),
        )

        tracker = otel.start_tracker(thread_id="thread-1", event_id="event-1", agent_id="agent-xyz")
        tracker.end()
        await asyncio.sleep(0.2)

    assert token_route.called
    assert traces_route.called
    import json

    body = json.loads(token_route.calls.last.request.content)
    assert "workspaceId" not in body


@pytest.mark.asyncio
async def test_failed_turn_is_still_exported_with_error_status():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"success": True, "data": {"accessToken": "jwt-abc", "expiresIn": 300}}
            )
        )
        traces_route = mock.post(TRACES_URL).mock(return_value=httpx.Response(200))

        otel.init_otel(
            otel_service_url=OTEL_SERVICE_URL,
            core_base_url=CORE_URL,
            persistence=FakePersistence({"dooers_runtime_api_key": "rak-1"}),
        )

        tracker = otel.start_tracker(
            thread_id="thread-1", event_id="event-1", agent_id="agent-xyz", workspace_id="workspace-1"
        )
        tracker.fail()
        tracker.record_error(RuntimeError("boom"))
        tracker.end()
        await asyncio.sleep(0.2)

    assert traces_route.called
    sent = traces_route.calls.last.request

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.trace.v1.trace_pb2 import Status as OtlpStatus

    parsed = ExportTraceServiceRequest()
    parsed.ParseFromString(sent.content)
    root = next(
        s for rs in parsed.resource_spans for ss in rs.scope_spans for s in ss.spans if s.name == "agent/agent-xyz"
    )
    assert root.status.code == OtlpStatus.STATUS_CODE_ERROR


def test_try_instrument_marks_failed_and_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="dooers.agents.server.observability.otel"):
        otel._try_instrument(
            "openai",
            "openinference.instrumentation.openai",
            "OpenAIInstrumentorThatDoesNotExist",
        )

    assert otel.llm_instrumentation_status()["openai"] == "failed"
    assert any("openai instrumentation FAILED" in r.message for r in caplog.records)


def test_try_instrument_marks_skipped_on_missing_package():
    otel._try_instrument(
        "anthropic",
        "openinference.instrumentation.this_module_does_not_exist_xyz",
        "AnthropicInstrumentor",
    )
    assert otel.llm_instrumentation_status()["anthropic"] == "skipped"


def test_instrument_llm_clients_logs_status_when_any_failed(caplog, monkeypatch):
    import logging

    def _fake_try(name: str, import_path: str, class_name: str) -> None:
        otel._llm_instrumentation[name] = "failed" if name == "openai" else "skipped"

    monkeypatch.setattr(otel, "_try_instrument", _fake_try)
    with caplog.at_level(logging.WARNING, logger="dooers.agents.server.observability.otel"):
        otel._instrument_llm_clients()

    assert otel.llm_instrumentation_status()["openai"] == "failed"
    assert any("LLM instrumentation status=" in r.message for r in caplog.records)


# --- Which key paid for each LLM call (dooers.llm.key_source / dooers.llm.endpoint) ---------

DOOERS_KEY = "dk_live_S3CR3TD00ERSabcdef0123456789"
THIRD_PARTY_KEY = "sk-proj-TH1RDPARTYabcdef0123456789"
KEY_SOURCE = "dooers.llm.key_source"
ENDPOINT = "dooers.llm.endpoint"

_OPENAI_COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-test",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_ANTHROPIC_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "pong"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}
_ANTHROPIC_SSE = b"".join(
    f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
    for name, data in [
        ("message_start", {"type": "message_start", "message": {**_ANTHROPIC_MESSAGE, "content": [], "stop_reason": None}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
)


@pytest.fixture
def _clean_llm_env(monkeypatch):
    for name in ("OPENAI_BASE_URL", "OPENAI_CUSTOM_HEADERS", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _http_module(sdk):
    # openai >= 3 / anthropic >= 1 ship on httpx2; earlier releases on httpx.
    return getattr(sdk._base_client, "httpx2", None) or sdk._base_client.httpx


def _make_client(sdk_name: str, *, is_async: bool, **client_kwargs):
    """Real OpenAI/Anthropic client on a fake HTTP transport (no network)."""
    sdk = pytest.importorskip(sdk_name)
    http = _http_module(sdk)

    def handler(request):
        if sdk_name == "openai":
            return http.Response(200, json=_OPENAI_COMPLETION)
        if json.loads(request.content).get("stream"):
            return http.Response(200, headers={"content-type": "text/event-stream"}, content=_ANTHROPIC_SSE)
        return http.Response(200, json=_ANTHROPIC_MESSAGE)

    transport = http.MockTransport(handler)
    client_cls = {
        ("openai", False): "OpenAI",
        ("openai", True): "AsyncOpenAI",
        ("anthropic", False): "Anthropic",
        ("anthropic", True): "AsyncAnthropic",
    }[(sdk_name, is_async)]
    http_client = http.AsyncClient(transport=transport) if is_async else http.Client(transport=transport)
    return getattr(sdk, client_cls)(max_retries=0, http_client=http_client, **client_kwargs)


async def _call_llm(sdk_name: str, client) -> str:
    """One plain (non-streaming) call; returns the reply text."""
    if sdk_name == "openai":
        result = client.chat.completions.create(model="gpt-test", messages=[{"role": "user", "content": "ping"}])
    else:
        result = client.messages.create(model="claude-test", max_tokens=16, messages=[{"role": "user", "content": "ping"}])
    if inspect.isawaitable(result):
        result = await result
    return result.choices[0].message.content if sdk_name == "openai" else result.content[0].text


async def _export_turn_around(call):
    """Run ``call`` inside an agent turn and return (call result, raw OTLP body, [(scope, span)])."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"accessToken": "jwt-abc", "expiresIn": 300}})
        )
        traces_route = mock.post(TRACES_URL).mock(return_value=httpx.Response(200))

        otel.init_otel(
            otel_service_url=OTEL_SERVICE_URL,
            core_base_url=CORE_URL,
            persistence=FakePersistence({"dooers_runtime_api_key": "rak-1"}),
        )
        tracker = otel.start_tracker(thread_id="thread-1", event_id="event-1", agent_id="agent-xyz", workspace_id="workspace-1")
        try:
            result = await call()
        finally:
            tracker.end()
        await asyncio.sleep(0.2)

    assert traces_route.call_count == 1
    body = traces_route.calls.last.request.content

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    parsed = ExportTraceServiceRequest()
    parsed.ParseFromString(body)
    spans = [(ss.scope.name, s) for rs in parsed.resource_spans for ss in rs.scope_spans for s in ss.spans]
    return result, body, spans


def _attributes(span) -> dict[str, str]:
    return {kv.key: kv.value.string_value for kv in span.attributes}


def _split_turn(spans):
    roots = [s for scope, s in spans if scope == "dooers.agents"]
    llm = [s for scope, s in spans if scope in ("openinference.instrumentation.openai", "openinference.instrumentation.anthropic")]
    assert len(roots) == 1
    assert len(llm) == 1, [scope for scope, _ in spans]
    return roots[0], llm[0]


def _assert_key_absent(body: bytes, key: str) -> None:
    # Not the key, and no 8-character piece of it, anywhere in what leaves the process.
    for start in range(len(key) - 7):
        assert key[start : start + 8].encode() not in body, key[start : start + 8]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("sdk_name", "key", "base_url", "expected_source", "expected_endpoint"),
    [
        ("openai", DOOERS_KEY, "https://llm.dooers.ai/v1", "dooers", "llm.dooers.ai"),
        ("openai", THIRD_PARTY_KEY, None, "third_party", "api.openai.com"),
        ("anthropic", DOOERS_KEY, "https://llm.dooers.ai", "dooers", "llm.dooers.ai"),
        ("anthropic", THIRD_PARTY_KEY, None, "third_party", "api.anthropic.com"),
    ],
    ids=["openai-dooers", "openai-third-party", "anthropic-dooers", "anthropic-third-party"],
)
async def test_llm_span_records_key_source_and_endpoint(
    _clean_llm_env, sdk_name, key, base_url, expected_source, expected_endpoint, is_async
):
    client = _make_client(sdk_name, is_async=is_async, api_key=key, base_url=base_url)

    reply, body, spans = await _export_turn_around(lambda: _call_llm(sdk_name, client))

    assert reply == "pong"
    root, llm = _split_turn(spans)
    # The openinference LLM span is the one tagged — never the turn's root span.
    assert _attributes(llm)[KEY_SOURCE] == expected_source
    assert _attributes(llm)[ENDPOINT] == expected_endpoint
    assert llm.parent_span_id == root.span_id
    assert KEY_SOURCE not in _attributes(root)
    assert ENDPOINT not in _attributes(root)
    _assert_key_absent(body, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_anthropic_messages_stream_is_tagged_on_its_llm_span(_clean_llm_env, is_async):
    # messages.stream() sends the request on __enter__, outside openinference's span: the tag
    # must still land on the LLM span, and not on whatever span is current at that moment.
    client = _make_client("anthropic", is_async=is_async, api_key=DOOERS_KEY, base_url="https://llm.dooers.ai")
    kwargs = {"model": "claude-test", "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]}

    async def call():
        if is_async:
            async with client.messages.stream(**kwargs) as stream:
                return (await stream.get_final_message()).content[0].text
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message().content[0].text

    reply, body, spans = await _export_turn_around(call)

    assert reply == "pong"
    root, llm = _split_turn(spans)
    assert _attributes(llm)[KEY_SOURCE] == "dooers"
    assert _attributes(llm)[ENDPOINT] == "llm.dooers.ai"
    assert KEY_SOURCE not in _attributes(root)
    assert ENDPOINT not in _attributes(root)
    _assert_key_absent(body, DOOERS_KEY)


@pytest.mark.asyncio
async def test_anthropic_auth_token_counts_as_the_key(_clean_llm_env):
    client = _make_client("anthropic", is_async=False, auth_token=DOOERS_KEY, base_url="https://llm.dooers.ai")

    _, body, spans = await _export_turn_around(lambda: _call_llm("anthropic", client))

    _, llm = _split_turn(spans)
    assert _attributes(llm)[KEY_SOURCE] == "dooers"
    _assert_key_absent(body, DOOERS_KEY)


@pytest.mark.asyncio
async def test_openai_key_given_as_callable_is_resolved_before_tagging(_clean_llm_env):
    client = _make_client("openai", is_async=False, api_key=lambda: DOOERS_KEY, base_url="https://llm.dooers.ai/v1")
    if getattr(client, "_api_key_provider", None) is None:
        pytest.skip("this openai release does not accept a callable api_key")

    _, body, spans = await _export_turn_around(lambda: _call_llm("openai", client))

    _, llm = _split_turn(spans)
    assert _attributes(llm)[KEY_SOURCE] == "dooers"
    _assert_key_absent(body, DOOERS_KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("sdk_name", ["openai", "anthropic"])
async def test_tagging_failure_never_breaks_the_llm_call(_clean_llm_env, monkeypatch, sdk_name, is_async):
    def boom(client):
        raise RuntimeError("tagging exploded")

    monkeypatch.setattr(otel, "_llm_key_source", boom)
    client = _make_client(sdk_name, is_async=is_async, api_key=DOOERS_KEY, base_url="https://llm.dooers.ai/v1")

    reply, _, spans = await _export_turn_around(lambda: _call_llm(sdk_name, client))

    assert reply == "pong"
    _, llm = _split_turn(spans)
    assert KEY_SOURCE not in _attributes(llm)


def test_llm_key_source_is_dooers_only_for_dooers_keys():
    class Client:
        def __init__(self, api_key=None, auth_token=None):
            self.api_key = api_key
            self.auth_token = auth_token

    assert otel._llm_key_source(Client(api_key="dk_live_abc")) == "dooers"
    assert otel._llm_key_source(Client(auth_token="dk_live_abc")) == "dooers"
    assert otel._llm_key_source(Client(api_key="sk-abc")) == "third_party"
    assert otel._llm_key_source(Client(api_key="xdk_live_abc")) == "third_party"
    assert otel._llm_key_source(Client()) == "third_party"
    assert otel._llm_key_source(object()) == "third_party"


def test_install_llm_key_marking_tolerates_missing_llm_sdks(monkeypatch):
    # openai / anthropic are optional in an agent: a missing (or reshaped) SDK is simply skipped.
    monkeypatch.setattr(otel, "_llm_key_marking_installed", False)
    monkeypatch.setattr(
        otel,
        "_LLM_KEY_MARKING_TARGETS",
        (
            ("this_llm_sdk_is_not_installed_xyz", "Client.request", otel._mark_request),
            ("json", "ClassThatDoesNotExist.request", otel._mark_request),
        ),
    )

    otel._install_llm_key_marking()

    assert otel._llm_key_marking_installed is True
