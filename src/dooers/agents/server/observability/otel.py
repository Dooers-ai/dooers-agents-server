"""OpenTelemetry observability for Dooers agent turns.

Each agent turn becomes a trace exported to dooers-agents-observability (never directly to
GCP — the worker has no cloud credentials). LLM calls (Anthropic, OpenAI) are auto-instrumented
as child spans via openinference when the respective packages are installed.

One process can serve many concurrent agents (see ``ConnectionRegistry``), each authenticated
to dooers-agents-observability with its own short-lived service token (audience
``otel-service``, one per ``agent_id`` == the core's ``workerId``). Because of that, spans are
never handed to a shared ``BatchSpanProcessor`` queue — a single export call could otherwise
mix spans (and thus Bearer tokens) from turns belonging to different tenants. Instead, spans
are buffered per trace_id and the whole turn (root + LLM children) is exported in one request
as soon as the root span ends (see ``_TurnExportSpanProcessor``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from dooers.agents.server.observability.service_token import ServiceTokenClient

logger = logging.getLogger(__name__)

_otel_enabled = False
_service_name = "dooers-agent"
_otel_service_url = ""
_token_client: ServiceTokenClient | None = None
_persistence: Any = None

# service_secrets key delivered by the core via the existing settings.merge_service_secrets
# channel (same mechanism dooers_whatsapp_service_secret already uses) — worker_id in that
# channel is the same UUID as agent_id here, just named differently on the core's side.
_RUNTIME_API_KEY_SECRET_NAME = "dooers_runtime_api_key"


def _make_turn_export_processor():  # noqa: ANN202
    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

    class _TurnExportSpanProcessor(SpanProcessor):
        def __init__(self) -> None:
            self._buffers: dict[int, list[ReadableSpan]] = {}

        def on_start(self, span, parent_context=None) -> None:  # noqa: ANN001
            pass

        def on_end(self, span: ReadableSpan) -> None:
            trace_id = span.context.trace_id
            buffer = self._buffers.setdefault(trace_id, [])
            buffer.append(span)
            if span.parent is not None:
                return  # not the root span yet — keep buffering

            spans = self._buffers.pop(trace_id)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("OTEL: on_end outside a running event loop — dropping turn export")
                return
            loop.create_task(_export_turn(spans))

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
            return True

    return _TurnExportSpanProcessor()


async def _get_runtime_api_key(agent_id: str) -> str | None:
    if _persistence is None:
        return None
    try:
        secrets = await _persistence.get_service_secrets(agent_id)
    except Exception:
        logger.exception("OTEL: failed to fetch service_secrets for agent_id=%s", agent_id)
        return None
    value = secrets.get(_RUNTIME_API_KEY_SECRET_NAME)
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _export_turn(spans: list) -> None:  # noqa: ANN001
    """Best-effort export of one full turn (root span + its LLM children) in a single request.

    Never raises into the caller — tracing must not affect the main agent path (see module
    docstring). Any failure here is logged and the turn's spans are simply not exported.
    """
    if not spans or _token_client is None or not _otel_service_url:
        return

    root = next((s for s in spans if s.parent is None), spans[-1])
    agent_id = root.attributes.get("agent.id")
    workspace_id = root.attributes.get("workspace.id")
    if not agent_id or not workspace_id:
        logger.debug("OTEL: turn missing agent.id/workspace.id — skipping export (agent_id=%s)", agent_id)
        return

    runtime_api_key = await _get_runtime_api_key(agent_id)
    if not runtime_api_key:
        logger.warning(
            "OTEL: no %s in service_secrets for agent_id=%s — skipping export",
            _RUNTIME_API_KEY_SECRET_NAME,
            agent_id,
        )
        return

    token = await _token_client.get_token(agent_id=agent_id, workspace_id=workspace_id, runtime_api_key=runtime_api_key)
    if not token:
        logger.warning("OTEL: could not obtain service token for agent_id=%s — skipping export", agent_id)
        return

    try:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

        body = encode_spans(spans).SerializePartialToString()
        response = await _token_client.http_client.post(
            f"{_otel_service_url}/v1/traces",
            content=body,
            headers={"Content-Type": "application/x-protobuf", "Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 300:
            logger.warning(
                "OTEL: export rejected by dooers-agents-observability status=%d agent_id=%s",
                response.status_code,
                agent_id,
            )
    except httpx.HTTPError as exc:
        logger.warning("OTEL: transport error exporting turn for agent_id=%s: %s", agent_id, exc)
    except Exception:
        logger.exception("OTEL: unexpected error exporting turn for agent_id=%s", agent_id)


def init_otel(
    *,
    otel_service_url: str,
    core_base_url: str,
    persistence: Any,
    service_name: str = "dooers-agent",
) -> None:
    """Initialize OpenTelemetry, exporting to dooers-agents-observability. No-op if either URL
    is absent."""
    global _otel_enabled, _service_name, _otel_service_url, _token_client, _persistence
    if not (otel_service_url or "").strip() or not (core_base_url or "").strip():
        logger.debug("OTEL: AGENT_OTEL_SERVICE_URL/AGENT_CORE_BASE_URL not configured — observability disabled")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(_make_turn_export_processor())
        trace.set_tracer_provider(provider)

        _otel_service_url = otel_service_url.rstrip("/")
        _token_client = ServiceTokenClient(core_base_url=core_base_url)
        _persistence = persistence
        _otel_enabled = True
        _service_name = service_name
        logger.info("OTEL: initialized (otel_service_url=%s, service=%s)", _otel_service_url, service_name)

        _instrument_llm_clients()
    except ImportError as exc:
        logger.warning("OTEL: missing package (%s) — install observability extras", exc)
    except Exception:
        logger.exception("OTEL: initialization failed")


def _instrument_llm_clients() -> None:
    try:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        AnthropicInstrumentor().instrument()
        logger.debug("OTEL: Anthropic instrumentation active")
    except ImportError:
        pass
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()
        logger.debug("OTEL: OpenAI instrumentation active")
    except ImportError:
        pass
    try:
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

        OpenAIAgentsInstrumentor().instrument()
        logger.debug("OTEL: OpenAI Agents SDK instrumentation active")
    except ImportError:
        pass


class _NoOpTracker:
    """Returned when OTEL is disabled; all methods are no-ops."""

    def fail(self) -> None:
        pass

    def record_error(self, exc: Exception) -> None:
        pass

    def end(self) -> None:
        pass


class _OtelTracker:
    """Manages one span for an agent turn."""

    def __init__(self, span: Any, token: Any) -> None:
        self._span = span
        self._token = token
        self._failed = False

    def fail(self) -> None:
        self._failed = True

    def record_error(self, exc: Exception) -> None:
        try:
            from opentelemetry.trace import StatusCode

            self._span.set_status(StatusCode.ERROR, str(exc))
            self._span.record_exception(exc)
        except Exception:
            logger.debug("OTEL: could not record error on span", exc_info=True)

    def end(self) -> None:
        try:
            from opentelemetry.context import detach
            from opentelemetry.trace import StatusCode

            self._span.set_status(StatusCode.ERROR if self._failed else StatusCode.OK)
            self._span.end()
            if self._token is not None:
                detach(self._token)
        except Exception:
            logger.debug("OTEL: could not end span", exc_info=True)


def start_tracker(
    *,
    thread_id: str,
    event_id: str,
    agent_id: str,
    thread_title: str | None = None,
    organization_id: str | None = None,
    workspace_id: str | None = None,
    channel: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    user_email: str | None = None,
) -> _OtelTracker | _NoOpTracker:
    """Start a span for one agent turn.

    Call after pipeline.setup() — that is where thread_id and event_id become stable.
    """
    if not _otel_enabled:
        return _NoOpTracker()
    try:
        from opentelemetry import trace
        from opentelemetry.context import attach

        attributes: dict[str, str] = {
            "agent.id": agent_id,
            "thread.id": thread_id,
            "event.id": event_id,
        }
        if thread_title:
            attributes["thread.title"] = thread_title
        if organization_id:
            attributes["org.id"] = organization_id
        if workspace_id:
            attributes["workspace.id"] = workspace_id
        if channel and channel != "dooers-platform":
            attributes["agent.channel"] = channel
        if user_id:
            attributes["user.id"] = user_id
        if user_name:
            attributes["user.name"] = user_name
        if user_email:
            attributes["user.email"] = user_email

        # Span name: "agent/{agent_id}: {title}" or "agent/{agent_id}" when no title yet.
        span_name = f"agent/{agent_id}: {thread_title}" if thread_title else f"agent/{agent_id}"
        tracer = trace.get_tracer("dooers.agents")
        span = tracer.start_span(span_name, attributes=attributes)
        token = attach(trace.set_span_in_context(span))
        return _OtelTracker(span, token)
    except Exception:
        logger.exception("OTEL: could not start span")
        return _NoOpTracker()
