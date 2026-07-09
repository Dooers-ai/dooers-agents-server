import asyncio

import httpx
import pytest
import respx

from dooers.agents.server.observability import otel

CORE_URL = "https://core.test"
OTEL_SERVICE_URL = "https://otel.test"
TOKEN_URL = f"{CORE_URL}/api/v2/identity/service-token/worker"
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
async def test_turn_without_workspace_id_is_not_exported():
    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(TOKEN_URL).mock(return_value=httpx.Response(200))

        otel.init_otel(
            otel_service_url=OTEL_SERVICE_URL,
            core_base_url=CORE_URL,
            persistence=FakePersistence({"dooers_runtime_api_key": "rak-1"}),
        )

        # No workspace_id passed — the core's token endpoint requires it, so we must not even try.
        tracker = otel.start_tracker(thread_id="thread-1", event_id="event-1", agent_id="agent-xyz")
        tracker.end()
        await asyncio.sleep(0.2)

    assert not token_route.called


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