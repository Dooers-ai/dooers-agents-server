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
from typing import Any, Literal

import httpx

from dooers.agents.server.observability.service_token import (
    RUNTIME_API_KEY_SECRET_NAME,
    ServiceTokenClient,
    resolve_runtime_api_key,
)

logger = logging.getLogger(__name__)

_otel_enabled = False
_service_name = "dooers-agent"
_otel_service_url = ""
_token_client: ServiceTokenClient | None = None
_persistence: Any = None

# Per-instrumentor outcome after ``init_otel`` → ``_instrument_llm_clients``.
# ``active`` = patched; ``skipped`` = optional package absent; ``failed`` = present but broken.
LlmInstrumentorStatus = Literal["active", "skipped", "failed", "pending"]
_llm_instrumentation: dict[str, LlmInstrumentorStatus] = {
    "anthropic": "pending",
    "openai": "pending",
    "openai_agents": "pending",
    "google_genai": "pending",
}


def llm_instrumentation_status() -> dict[str, LlmInstrumentorStatus]:
    """Return a copy of LLM auto-instrumentation status (for health checks / tests)."""
    return dict(_llm_instrumentation)


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
    return await resolve_runtime_api_key(_persistence, agent_id)


async def _export_turn(spans: list) -> None:  # noqa: ANN001
    """Best-effort export of one full turn (root span + its LLM children) in a single request.

    Never raises into the caller — tracing must not affect the main agent path (see module
    docstring). Any failure here is logged and the turn's spans are simply not exported.
    """
    if not spans or _token_client is None or not _otel_service_url:
        return

    root = next((s for s in spans if s.parent is None), spans[-1])
    agent_id = root.attributes.get("agent.id")
    workspace_id = root.attributes.get("workspace.id") or ""
    if not agent_id:
        logger.debug("OTEL: turn missing agent.id — skipping export")
        return

    runtime_api_key = await _get_runtime_api_key(agent_id)
    if not runtime_api_key:
        logger.warning(
            "OTEL: no runtime API key for agent_id=%s — skipping export "
            "(set AGENT_SEED_SECRET or wait for settings.seed / %s in service_secrets)",
            agent_id,
            RUNTIME_API_KEY_SECRET_NAME,
        )
        return

    token = await _token_client.get_token(
        agent_id=agent_id,
        workspace_id=str(workspace_id),
        runtime_api_key=runtime_api_key,
        audience="otel-service",
        scopes=["otel:write"],
    )
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

        # Before the instrumentors: for Anthropic's stream helper, openinference must wrap ours.
        _install_llm_key_marking()
        _instrument_llm_clients()
    except ImportError as exc:
        logger.warning("OTEL: missing package (%s) — install observability extras", exc)
    except Exception:
        logger.exception("OTEL: initialization failed")


def _try_instrument(name: str, import_path: str, class_name: str) -> None:
    """Activate one openinference instrumentor; never raise into the agent path."""
    try:
        module = __import__(import_path, fromlist=[class_name])
        instrumentor = getattr(module, class_name)()
        instrumentor.instrument()
        if getattr(instrumentor, "is_instrumented_by_opentelemetry", True) is False:
            # The instrumentor is installed but the client it targets is not (it only logs a
            # DependencyConflict and patches nothing): same outcome as a missing package.
            _llm_instrumentation[name] = "skipped"
            logger.debug("OTEL: %s instrumentation skipped (client library not installed)", name)
            return
        _llm_instrumentation[name] = "active"
        logger.info("OTEL: %s instrumentation active", name)
    except ImportError as exc:
        # Optional SDK not installed in this agent (e.g. no anthropic / no openai-agents).
        _llm_instrumentation[name] = "skipped"
        logger.debug("OTEL: %s instrumentation skipped (not installed): %s", name, exc)
    except Exception as exc:
        # Package present but broken (version skew, missing pkg_resources, etc.).
        # Must be loud: turn spans still export, so creators otherwise think LLM metrics work.
        _llm_instrumentation[name] = "failed"
        logger.warning(
            "OTEL: %s instrumentation FAILED (%s: %s) — LLM model/token spans will be missing. "
            "Reinstall with pip/uv: 'dooers-agents-server[observability]' "
            "(requires opentelemetry-api>=1.33 and a modern setuptools without the old "
            "pkg_resources-only instrumentation stack).",
            name,
            type(exc).__name__,
            exc,
        )


def _instrument_llm_clients() -> None:
    _try_instrument(
        "anthropic",
        "openinference.instrumentation.anthropic",
        "AnthropicInstrumentor",
    )
    _try_instrument(
        "openai",
        "openinference.instrumentation.openai",
        "OpenAIInstrumentor",
    )
    _try_instrument(
        "openai_agents",
        "openinference.instrumentation.openai_agents",
        "OpenAIAgentsInstrumentor",
    )
    _try_instrument(
        "google_genai",
        "openinference.instrumentation.google_genai",
        "GoogleGenAIInstrumentor",
    )
    status = llm_instrumentation_status()
    if all(v == "skipped" for v in status.values()):
        logger.warning(
            "OTEL: no LLM client instrumentors active (openai/anthropic/openai-agents/google-genai not installed). "
            "Turn traces will export without model/token child spans."
        )
    elif any(v == "failed" for v in status.values()):
        logger.warning("OTEL: LLM instrumentation status=%s", status)
    else:
        logger.info("OTEL: LLM instrumentation status=%s", status)


# --- Which key paid for each LLM call -------------------------------------------------------
#
# Every LLM span gets ``dooers.llm.key_source`` (``dooers`` for a Dooers key, i.e. the call went
# through the Dooers gateway and is billed to the organization's wallet; ``third_party`` for any
# other credential, or none) and ``dooers.llm.endpoint`` (host the request went to). The key
# itself is never recorded, not even partially.
#
# The tag is written where every provider's request passes: the HTTP client. OpenAI, Anthropic,
# google-genai and LiteLLM (which the Agents SDK uses for Gemini and Claude) all send through
# httpx (openai >= 3 / anthropic >= 1 through its fork, httpx2) or, for google-genai's async
# calls, aiohttp — so hooks on those cover every provider without knowing any of their clients.
# They tag the span current at send time only when it is an LLM span
# (``openinference.span.kind == "LLM"``, whatever instrumentor opened it).

DOOERS_KEY_PREFIX = "dk_live_"
LLM_KEY_SOURCE_ATTRIBUTE = "dooers.llm.key_source"
LLM_ENDPOINT_ATTRIBUTE = "dooers.llm.endpoint"

_SPAN_KIND_ATTRIBUTE = "openinference.span.kind"
# Where providers put the credential: OpenAI-style bearer, Anthropic, Gemini API, Azure OpenAI.
_CREDENTIAL_HEADERS = ("x-api-key", "x-goog-api-key", "api-key")

_llm_key_marking_installed = False


def _is_dooers_key(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(DOOERS_KEY_PREFIX)


def _key_source(headers: Any, key_param: Any = None) -> str:
    """``dooers`` if any credential on a request is a Dooers key; ``third_party`` otherwise."""
    pairs = headers.items() if hasattr(headers, "items") else (headers or ())
    lowered = {str(name).lower(): value for name, value in pairs}
    candidates = [lowered.get(name) for name in _CREDENTIAL_HEADERS]
    scheme, _, token = str(lowered.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer":
        candidates.append(token)
    # The Gemini API also takes the key as ``?key=``.
    candidates.append(key_param)
    return "dooers" if any(_is_dooers_key(value) for value in candidates) else "third_party"


def _query_key(params: Any) -> Any:
    """The ``key`` query parameter out of aiohttp's ``params`` (mapping, pairs or string)."""
    if params is None:
        return None
    if isinstance(params, str):
        from urllib.parse import parse_qs

        return next(iter(parse_qs(params).get("key", [])), None)
    if hasattr(params, "get"):
        return params.get("key")
    return next((value for name, value in params if name == "key"), None)


def _llm_key_source(client: Any) -> str:
    # Anthropic clients may carry the credential as ``auth_token`` (Bearer) instead of ``api_key``.
    for name in ("api_key", "auth_token"):
        if _is_dooers_key(getattr(client, name, None)):
            return "dooers"
    return "third_party"


def _current_llm_span() -> Any:
    from opentelemetry import trace

    span = trace.get_current_span()
    if not span.is_recording():
        return None
    attributes = getattr(span, "attributes", None) or {}
    return span if attributes.get(_SPAN_KIND_ATTRIBUTE) == "LLM" else None


def _tag_llm_span(key_source: str, host: Any) -> None:
    span = _current_llm_span()
    if span is None:
        return
    span.set_attribute(LLM_KEY_SOURCE_ATTRIBUTE, key_source)
    if isinstance(host, str) and host:
        span.set_attribute(LLM_ENDPOINT_ATTRIBUTE, host)


def _mark_send(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    # Serves Client.send and AsyncClient.send alike: the tag goes on before the request leaves,
    # so a call that fails still says which key it used. Never raises into the agent's call.
    try:
        request = args[0] if args else kwargs.get("request")
        if request is not None:
            _tag_llm_span(_key_source(request.headers, request.url.params.get("key")), request.url.host)
    except Exception:
        logger.debug("OTEL: could not tag LLM span with its key source", exc_info=True)
    return wrapped(*args, **kwargs)


def _mark_aiohttp_request(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    # ClientSession._request(method, url, **kwargs): what every aiohttp verb goes through. The
    # session's default headers count too — a client may set its key once on the session.
    try:
        from yarl import URL

        url = URL(str(args[1] if len(args) > 1 else kwargs.get("str_or_url")))
        headers = [*getattr(instance, "headers", {}).items(), *_header_pairs(kwargs.get("headers"))]
        key_param = _query_key(kwargs.get("params")) or url.query.get("key")
        _tag_llm_span(_key_source(headers, key_param), url.host)
    except Exception:
        logger.debug("OTEL: could not tag LLM span with its key source", exc_info=True)
    return wrapped(*args, **kwargs)


def _header_pairs(headers: Any) -> list[tuple[Any, Any]]:
    if not headers:
        return []
    return list(headers.items()) if hasattr(headers, "items") else list(headers)


def _mark_stream(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
    # Anthropic's messages.stream() only sends its request on __enter__, after openinference has
    # left its span, so the send hook sees the turn's span there. Tag while stream() runs inside it.
    try:
        client = getattr(instance, "_client", None)
        _tag_llm_span(_llm_key_source(client), getattr(getattr(client, "base_url", None), "host", None))
    except Exception:
        logger.debug("OTEL: could not tag LLM span with its key source", exc_info=True)
    return wrapped(*args, **kwargs)


_LLM_KEY_MARKING_TARGETS = (
    ("httpx", "Client.send", _mark_send),
    ("httpx", "AsyncClient.send", _mark_send),
    ("httpx2", "Client.send", _mark_send),
    ("httpx2", "AsyncClient.send", _mark_send),
    ("aiohttp", "ClientSession._request", _mark_aiohttp_request),
    ("anthropic.resources.messages", "Messages.stream", _mark_stream),
    ("anthropic.resources.messages", "AsyncMessages.stream", _mark_stream),
    ("anthropic.resources.beta.messages", "Messages.stream", _mark_stream),
    ("anthropic.resources.beta.messages", "AsyncMessages.stream", _mark_stream),
)


def _install_llm_key_marking() -> None:
    """Wrap the HTTP clients (and Anthropic's stream helper) once per process; never raise."""
    global _llm_key_marking_installed
    if _llm_key_marking_installed:
        return
    _llm_key_marking_installed = True
    try:
        from wrapt import wrap_function_wrapper
    except ImportError:
        logger.debug("OTEL: wrapt not installed — LLM key source will not be recorded")
        return
    for module, name, wrapper in _LLM_KEY_MARKING_TARGETS:
        try:
            wrap_function_wrapper(module, name, wrapper)
        except ImportError:
            pass  # that client library is not installed in this agent
        except Exception:
            logger.debug("OTEL: could not wrap %s.%s for LLM key source", module, name, exc_info=True)


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
