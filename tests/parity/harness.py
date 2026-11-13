"""Legacy runner: each scenario on the production code path (``thread_management="agent"``).

``run_legacy`` builds a real ``AgentServer``. Only the infrastructure is replaced:

- ``_build_persistence`` returns ``MemoryPersistence`` (Postgres row semantics);
- ``init_otel`` is a no-op and OTEL stays off (the platform default would export spans);
- ``respx`` serves a fake tools-whatsapp and a fake core. Any other HTTP request
  raises (``assert_all_mocked``), so nothing reaches the network.

Flows: ``dispatch`` calls ``AgentServer.dispatch`` per turn; ``websocket`` sends frames
through ``AgentServer.handle`` (Router); ``script`` runs the scenario's own calls on the
public ``AgentServer`` API.

``run_maestro`` runs the same scenario with ``thread_management="maestro"``, against
``FakeMaestro``. Each scenario keeps the ingress its flow stands for in production:

- ``dispatch`` flow, channel ``dooers-platform``: production ``AgentServer.dispatch``. Its
  Maestro side is the agent's own publication of the person's message
  (``FakeMaestro.agent_publish``: the declared name, e-mail and identity IDs, URL parts as
  ``media_link``, a delivery for the agent). The SDK call that publishes it is plan 5, so
  the harness plays it;
- ``websocket`` flow without the public chat handshake: production web chat. The person
  publishes in Maestro's web chat (``FakeMaestro.publish``) and answers the form there
  (``respond_form``);
- in both, the fake creates the thread, and each turn reaches the handler through a Maestro
  delivery to the SDK adapter (``create_text_delivery_adapter``);
- channels (``channel`` other than ``dooers-platform``), public chat (``connect``) and
  ``script`` scenarios: their ingress is the agent's own ``AgentServer``, so they call
  the same ``AgentServer`` method on a Maestro-mode server. Today it raises
  ``THREAD_MANAGEMENT_MISMATCH`` (or the router refuses the frame), and that is the gap.

Maestro is the thread authority in this mode, so events, threads and runs are read back from
the fake through the SDK's own conversion: ``MaestroExecutionPersistence`` (the persistence of
the handler's ``MaestroAgentMemory``) over the whole thread as the agent's context has it, and
``legacy_model.to_runs``. ``ParityResult.deliveries`` records how each delivery ended (HTTP
status, state, reason code). If FakeMaestro refused an SDK request that
``EXPECTED_REJECTIONS`` does not list, ``run_maestro`` raises ``ContractViolation``, also
when the scenario itself fails.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import respx

import dooers.agents.server.maestro.media_fetch as media_fetch_module
import dooers.agents.server.observability.otel as otel
import dooers.agents.server.server as server_module
from dooers.agents.server.auth_validation import (
    CORE_ISSUER,
    DASHBOARD_VALIDATION_PATH,
    PUBLIC_CHAT_VALIDATION_PATH,
)
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.maestro import create_text_delivery_adapter
from dooers.agents.server.maestro.context import MaestroPrivateResources
from dooers.agents.server.maestro.legacy_model import to_runs
from dooers.agents.server.maestro.links import ArtifactLinkMissing, LinkResolver
from dooers.agents.server.maestro.persistence import MaestroExecutionPersistence
from dooers.agents.server.maestro.window import MaestroContextWindow
from dooers.agents.server.server import AgentServer
from dooers.tools.whatsapp.transport import build_x_message_sig
from tests.parity.context_pages import ThreadPages
from tests.parity.fake_maestro import FakeMaestro
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import (
    AGENT_ID,
    ASSISTANT_NAME,
    CORE_BASE,
    DASHBOARD_CONNECT,
    FORM_EVENT_REF,
    ORG_ID,
    TOOLS_BASE,
    WHATSAPP_HMAC_SECRET,
    WHATSAPP_OUTBOUND_ID,
    WORKSPACE_ID,
    Connect,
    ParityResult,
    Scenario,
    ScenarioInput,
    Turn,
    close_capture,
    open_capture,
)
from tests.parity.scenarios import SCENARIOS

__all__ = ["EXPECTED_REJECTIONS", "ContractViolation", "ParityResult", "run_legacy", "run_maestro"]


class WebSocketDisconnect(Exception):
    """Same name as Starlette's, so ``AgentServer.handle`` treats it as a normal close."""


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _core_token(connect: Connect) -> str:
    """Unsigned core JWT: the SDK reads ``iss``/``session_token`` to pick the core endpoint."""
    payload: dict[str, Any] = {"iss": CORE_ISSUER, "sub": connect.core_user.user_id}
    if connect.connection_type != "dashboard":
        payload["session_token"] = f"session-{connect.connection_type}"
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}.parity"


class _FakeServices:
    def __init__(self, connect: Connect) -> None:
        self.connect = connect
        self.whatsapp_requests: list[dict[str, Any]] = []
        self.core_validations = 0

    def whatsapp_tools(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        expected = build_x_message_sig(WHATSAPP_HMAC_SECRET, request.content)
        self.whatsapp_requests.append({"body": body, "signed": request.headers.get("X-Message-Signature") == expected})
        return httpx.Response(
            200,
            json={"ok": True, "accepted": True, "send_type": body.get("send_type"), "message_id": WHATSAPP_OUTBOUND_ID},
        )

    def core_validate(self, request: httpx.Request) -> httpx.Response:
        self.core_validations += 1
        user = self.connect.core_user
        return httpx.Response(
            200,
            json={
                "valid": True,
                "connection_type": self.connect.connection_type,
                "user": {
                    "id": user.user_id,
                    "email": user.user_email or "",
                    "name": user.user_name or "",
                    "identity_ids": list(user.identity_ids),
                    "system_role": user.system_role,
                },
                "organization": {"id": ORG_ID, "role": user.organization_role, "plan": "free"},
                "workspace": {"id": WORKSPACE_ID, "role": user.workspace_role, "queues": []},
                "agent": {"id": AGENT_ID, "owner_user_id": None, "can_configure_settings": False},
                "policies": {"rate_limit_msgs_per_min": 15, "thread_ttl_hours": 24},
            },
        )


#: The public address the hosts of ``ScenarioInput.media`` resolve to; respx answers in its place.
MEDIA_HOST_IP = "93.184.215.14"


def _media_resolver(media: Mapping[str, tuple[bytes, str]]) -> Callable[[str, int], Awaitable[list[str]]]:
    hosts = {urlsplit(url).hostname for url in media}

    async def resolve(host: str, port: int) -> list[str]:
        if host in hosts:
            return [MEDIA_HOST_IP]
        raise OSError(f"offline parity run: no DNS for {host}")

    return resolve


@contextmanager
def _offline_environment(
    persistence: MemoryPersistence,
    connect: Connect,
    maestro: FakeMaestro | None = None,
    media: Mapping[str, tuple[bytes, str]] | None = None,
) -> Iterator[_FakeServices]:
    media = media or {}
    fakes = _FakeServices(connect)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"DOOERS_WHATSAPP_TOOLS_BASE": TOOLS_BASE}))
        os.environ.pop("DOOERS_RUNTIME_ID", None)  # restored by patch.dict
        stack.enter_context(patch.object(server_module, "_build_persistence", lambda _config: persistence))
        stack.enter_context(patch.object(otel, "init_otel", lambda **_kwargs: None))
        stack.enter_context(patch.object(otel, "_otel_enabled", False))
        # Offline: no DNS for the agents' media URLs, except the scenario's served files.
        # send.image/audio/document in Maestro mode publish a media_link with the agent's URL for
        # an offline URL (index 4.3), as for an unreachable URL, and upload a served file.
        stack.enter_context(patch.object(media_fetch_module, "_system_resolver", _media_resolver(media)))
        mock = stack.enter_context(respx.mock(assert_all_called=False))
        for url, (data, content_type) in media.items():
            route = mock.get(host=MEDIA_HOST_IP, path=urlsplit(url).path)
            route.mock(return_value=httpx.Response(200, content=data, headers={"Content-Type": content_type}))
        mock.post(f"{TOOLS_BASE}/api/v1/messages").mock(side_effect=fakes.whatsapp_tools)
        mock.post(f"{CORE_BASE}{DASHBOARD_VALIDATION_PATH}").mock(side_effect=fakes.core_validate)
        mock.post(f"{CORE_BASE}{PUBLIC_CHAT_VALIDATION_PATH}").mock(side_effect=fakes.core_validate)
        if maestro is not None:
            stack.enter_context(maestro.serving(mock))
        yield fakes


def _config(spec: ScenarioInput, **mode: Any) -> AgentConfig:
    values: dict[str, Any] = {
        "database_type": "postgres",
        "database_host": "localhost",
        "database_port": 5432,
        "database_user": "parity",
        "database_name": "parity",
        "database_password": "",
        "database_key": "",
        "database_ssl": False,
        "database_instance_uri": "",
        "assistant_name": ASSISTANT_NAME,
        "analytics_enabled": False,
        "guest_thread_cleanup_interval_seconds": 0,
        "agent_seed_secret": "",
        "store_chat_uploads": False,
        "chat_storage_service": "none",
        "gcp_storage_bucket": "",
        "dooers_storage_prefix": "",
        "azure_storage_connection_string": "",
        "azure_storage_container": "",
        "agent_core_base_url": CORE_BASE,
        "otel_service_url": "",
        "otel_service_name": "",
    }
    values.update(spec.config)
    values.update(mode)
    return AgentConfig(**values)


def _wire_content(turn: Turn) -> list[dict[str, Any]] | None:
    return copy.deepcopy([dict(part) for part in turn.content]) if turn.content else None


async def _run_dispatch(server: AgentServer, scenario: Scenario, _fakes: _FakeServices) -> dict[str, Any]:
    spec = scenario.incoming
    thread_id: str | None = None
    for turn in spec.turns:
        stream = await server.dispatch(
            scenario.handler,
            spec.agent_id,
            turn.message,
            user=spec.user,
            organization_id=spec.organization_id,
            workspace_id=spec.workspace_id,
            thread_id=thread_id,
            thread_title=spec.thread_title,
            content=_wire_content(turn),
            channel=spec.channel,
            channel_meta=copy.deepcopy(dict(spec.channel_meta)) if spec.channel_meta else None,
            queues=list(spec.queues),
        )
        await stream.collect()
        thread_id = stream.thread_id
    return {}


class _ScriptedWebSocket:
    """Plays the client: one connect frame, then one ``event.create`` per turn.

    Like the real client, it learns the thread id and the form event id from the
    ``event.append`` frames the server sends back.
    """

    def __init__(self, spec: ScenarioInput, connect: Connect) -> None:
        self._spec = spec
        self._connect = connect
        self._frames = self._script()
        self.sent: list[dict[str, Any]] = []
        self.thread_id: str | None = None
        self.form_event_id: str | None = None

    def _script(self) -> Iterator[dict[str, Any]]:
        frame_user = self._connect.frame_user
        yield {
            "id": "frame-connect",
            "type": "connect",
            "payload": {
                "agent_id": self._spec.agent_id,
                "organization_id": self._spec.organization_id,
                "workspace_id": self._spec.workspace_id,
                "user": {
                    "user_id": frame_user.user_id,
                    "user_name": frame_user.user_name,
                    "user_email": frame_user.user_email,
                },
                "auth_token": _core_token(self._connect),
            },
        }
        for number, turn in enumerate(self._spec.turns, start=1):
            yield self._event_create(number, turn)

    def _event_create(self, number: int, turn: Turn) -> dict[str, Any]:
        if turn.event_type == "message":
            content = _wire_content(turn) or [{"type": "text", "text": turn.message}]
        else:
            content = _wire_content(turn) or []
        data = None
        if turn.data is not None:
            data = {key: (self.form_event_id if value == FORM_EVENT_REF else copy.deepcopy(value)) for key, value in turn.data.items()}
        payload: dict[str, Any] = {
            "thread_id": self.thread_id,
            "client_event_id": f"client-{number}",
            "event": {"type": turn.event_type, "actor": "user", "content": content, "data": data},
        }
        if turn.metadata is not None:
            payload["metadata"] = copy.deepcopy(dict(turn.metadata))
        return {"id": f"frame-{number}", "type": "event.create", "payload": payload}

    async def receive_text(self) -> str:
        frame = next(self._frames, None)
        if frame is None:
            raise WebSocketDisconnect()
        return json.dumps(frame)

    async def send_text(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        if frame.get("type") == "event.append":
            payload = frame["payload"]
            self.thread_id = payload["thread_id"]
            for event in payload["events"]:
                if event.get("type") == "form":
                    self.form_event_id = event["id"]

    async def close(self, code: int = 1000) -> None:
        return None

    @property
    def acks(self) -> list[dict[str, Any]]:
        return [
            {"ack_id": frame["payload"]["ack_id"], "ok": frame["payload"]["ok"], "error": frame["payload"].get("error")}
            for frame in self.sent
            if frame.get("type") == "ack"
        ]


async def _run_websocket(server: AgentServer, scenario: Scenario, fakes: _FakeServices) -> dict[str, Any]:
    socket = _ScriptedWebSocket(scenario.incoming, fakes.connect)
    await server.handle(socket, scenario.handler)
    return {"acks": socket.acks, "core_validations": fakes.core_validations}


async def _run_script(server: AgentServer, scenario: Scenario, _fakes: _FakeServices) -> dict[str, Any]:
    script = scenario.incoming.script
    if script is None:
        raise ValueError("script flow without a script")
    return await script(server, scenario.handler)


_FLOWS = {"dispatch": _run_dispatch, "websocket": _run_websocket, "script": _run_script}


async def _persisted_events(persistence: MemoryPersistence) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for thread_id in persistence.thread_ids():
        events.extend(event.model_dump() for event in await persistence.get_events(thread_id, limit=10_000))
    return events


async def _observed(persistence: MemoryPersistence, observe: frozenset[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "threads" in observe:
        threads = [await persistence.get_thread(tid) for tid in persistence.thread_ids()]
        out["threads"] = [thread.model_dump() for thread in threads if thread is not None]
    if "runs" in observe:
        out["runs"] = [
            run.model_dump() for tid in persistence.thread_ids() for run in await persistence.list_runs(thread_id=tid, limit=10_000)
        ]
    return out


async def run_legacy(name: str) -> ParityResult:
    scenario = SCENARIOS[name]
    spec = scenario.incoming
    persistence = MemoryPersistence()
    with _offline_environment(persistence, spec.connect or DASHBOARD_CONNECT, media=spec.media) as fakes:
        server = AgentServer(_config(spec))
        capture, token = open_capture(scenario.history_formats)
        try:
            await server.ensure_initialized()
            if spec.service_secrets:
                await server.persistence.merge_service_secrets(spec.agent_id, dict(spec.service_secrets))
            outputs = await _FLOWS[spec.flow](server, scenario, fakes)
            events = await _persisted_events(persistence)
            outputs.update(await _observed(persistence, spec.observe))
        finally:
            close_capture(token)
            await server.close()
    if fakes.whatsapp_requests:
        outputs["whatsapp_requests"] = fakes.whatsapp_requests
    return ParityResult(events=events, incoming=capture.incoming, histories=capture.histories, outputs=outputs)


# -- Maestro mode --


class ContractViolation(Exception):
    """FakeMaestro refused an SDK request (contract or Maestro rule) that the scenario does not expect.

    It is never a parity gap: ``KNOWN_GAPS`` never lists it as the expected failure.
    """


#: SDK callbacks a scenario expects FakeMaestro to refuse: scenario -> ((action, error code, why), ...).
#: Empty today: in every scenario the SDK sends only requests that the contract accepts.
EXPECTED_REJECTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {}


def _check_rejections(name: str, maestro: FakeMaestro) -> None:
    expected = [(action, code) for action, code, _why in EXPECTED_REJECTIONS.get(name, ())]
    if maestro.rejections != expected:
        raise ContractViolation(f"{name}: FakeMaestro refused {maestro.rejections}, expected {expected}")


def _delivered_by_maestro(spec: ScenarioInput) -> bool:
    """Platform turns go through Maestro; channels, public chat and scripts keep the AgentServer ingress."""
    return spec.flow != "script" and spec.connect is None and spec.channel == "dooers-platform"


def _maestro_content(turn: Turn) -> list[dict[str, Any]]:
    """What the web chat publishes: the wire parts, or the message as one text part."""
    return _wire_content(turn) or [{"type": "text", "text": turn.message}]


_MEDIA_LINK_KEYS = ("mime_type", "filename", "alt", "duration")


def _agent_content(turn: Turn) -> list[dict[str, Any]]:
    """The turn as the agent's own publication carries it (plan 5 maps ``AgentServer.dispatch`` to it).

    A production URL part (``image``/``audio``/``document`` with ``url``) is a ``media_link``
    (index 4.3), which has no ``size_bytes``; text and contact parts keep their fields.
    """
    parts = []
    for part in _maestro_content(turn):
        if part["type"] in {"image", "audio", "document"}:
            extra = {key: part[key] for key in _MEDIA_LINK_KEYS if part.get(key) is not None}
            parts.append({"type": "media_link", "kind": part["type"], "url": part["url"], **extra})
        else:
            parts.append(part)
    return parts


def _last_form(maestro: FakeMaestro, thread_id: str) -> str:
    """The id of the thread's last production form, as the web chat knows it (``FORM_EVENT_REF``)."""
    return [event["event_id"] for event in maestro.events[thread_id] if event["type"] == "form.created"][-1]


async def _run_maestro_deliveries(maestro: FakeMaestro, scenario: Scenario) -> dict[str, Any]:
    """Each turn reaches the handler through a Maestro delivery.

    ``dispatch`` flow (production ``AgentServer.dispatch``): the agent's own publication of the
    person's message (``FakeMaestro.agent_publish``: declared name, e-mail and identity IDs, URL
    parts as ``media_link``). ``websocket`` flow (production web chat): the person publishes in
    Maestro's web chat (``publish``) and answers the form there (``respond_form``).
    """
    spec = scenario.incoming

    async def resources(scope: Any) -> MaestroPrivateResources:
        # The operator's queue catalog (``MaestroPrivateResources.queues``) until Core sends it (plan 5).
        return MaestroPrivateResources(scope, MemoryPersistence(), {}, {}, queues=spec.queues)

    # Configured as the legacy side (``_config``): the same AgentConfig (assistant name, content policy).
    agent_config = _config(spec, thread_management="maestro", maestro=maestro.config)
    adapter = create_text_delivery_adapter(agent_config, scenario.handler, resources)
    thread_id = maestro.create_thread(spec.user, title=spec.thread_title)
    for number, turn in enumerate(spec.turns, start=1):
        if turn.event_type == "form.response":
            data = dict(turn.data or {})
            form_event_id = _last_form(maestro, thread_id) if data.get("form_event_id") == FORM_EVENT_REF else data.get("form_event_id")
            trigger = maestro.respond_form(
                thread_id, spec.user, form_event_id, values=copy.deepcopy(data.get("values") or {}), cancelled=bool(data.get("cancelled"))
            )
        elif spec.flow == "dispatch":
            trigger = maestro.agent_publish(thread_id, spec.user, _agent_content(turn), client_event_id=f"client-{number}")
        else:
            trigger = maestro.publish(thread_id, spec.user, _maestro_content(turn), client_event_id=f"client-{number}")
        for delivery in maestro.deliveries_for(trigger):
            await maestro.deliver(adapter, delivery)
    return {}


async def _run_maestro_server(maestro: FakeMaestro, scenario: Scenario, fakes: _FakeServices) -> dict[str, Any]:
    spec = scenario.incoming
    server = AgentServer(_config(spec, thread_management="maestro", maestro=maestro.config))
    try:
        await server.ensure_initialized()
        if spec.service_secrets:
            await server.persistence.merge_service_secrets(spec.agent_id, dict(spec.service_secrets))
        return await _FLOWS[spec.flow](server, scenario, fakes)
    finally:
        await server.close()


class _AgentReader:
    """The whole Maestro thread as the agent's delivery context has it, for ``MaestroExecutionPersistence``.

    The same reads a handler's ``MaestroAgentMemory`` makes, without a cutoff: the snapshot is
    ``FakeMaestro.agent_view`` and each file gets a temporary link from the fake.
    """

    def __init__(self, maestro: FakeMaestro, thread_id: str) -> None:
        self._maestro, self._thread_id = maestro, thread_id
        self.envelope = {
            "thread_id": thread_id,
            "organization_id": ORG_ID,
            "workspace_id": WORKSPACE_ID,
            "instance_agent_id": AGENT_ID,
        }

        async def issue(artifact_id: str) -> dict[str, Any]:
            try:
                return maestro.artifact_link(thread_id, artifact_id)
            except LookupError:
                raise ArtifactLinkMissing("NOT_FOUND") from None

        self.links = LinkResolver(issue)
        view = maestro.agent_view(thread_id)
        self._window = ThreadPages(view["events"], view["participants"]).window()

    def window(self) -> MaestroContextWindow:
        return self._window


async def _maestro_read_back(maestro: FakeMaestro, observe: frozenset[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Events, threads and runs read back through the SDK's conversion, as production ``get_events`` reads them."""
    events: list[dict[str, Any]] = []
    out: dict[str, Any] = {}
    for thread_id in maestro.threads:
        reader = _AgentReader(maestro, thread_id)
        persistence = MaestroExecutionPersistence(reader, assistant_name=ASSISTANT_NAME)
        events.extend(event.model_dump() for event in await persistence.get_events(thread_id, limit=10_000))
        if "threads" in observe:
            thread = await persistence.get_thread(thread_id)
            out.setdefault("threads", []).append(thread.model_dump())
        if "runs" in observe:
            # Production ``list_runs``: the run that started last comes first.
            runs = reversed(to_runs(maestro.agent_view(thread_id)["events"]))
            out.setdefault("runs", []).extend(run.model_dump() for run in runs)
    for name in observe:
        out.setdefault(name, [])
    return events, out


async def run_maestro(name: str) -> ParityResult:
    scenario = SCENARIOS[name]
    spec = scenario.incoming
    maestro = FakeMaestro(assistant_name=ASSISTANT_NAME)
    persistence = MemoryPersistence()
    with _offline_environment(persistence, spec.connect or DASHBOARD_CONNECT, maestro, spec.media) as fakes:
        capture, token = open_capture(scenario.history_formats)
        try:
            if _delivered_by_maestro(spec):
                outputs = await _run_maestro_deliveries(maestro, scenario)
            else:
                outputs = await _run_maestro_server(maestro, scenario, fakes)
        except Exception:
            # A refused SDK request outranks the scenario's own failure (a gap never hides it).
            _check_rejections(name, maestro)
            raise
        finally:
            close_capture(token)
    _check_rejections(name, maestro)
    events, observed = await _maestro_read_back(maestro, spec.observe)
    outputs.update(observed)
    if fakes.whatsapp_requests:
        outputs["whatsapp_requests"] = fakes.whatsapp_requests
    if persistence.thread_ids():
        # Maestro owns the threads in this mode; a local thread is a difference, never hidden.
        outputs["agent_local_events"] = await _persisted_events(persistence)
    deliveries = [{"status": d.status_code, "state": d.state, "reason_code": d.reason_code} for d in maestro.deliveries.values()]
    return ParityResult(events=events, incoming=capture.incoming, histories=capture.histories, outputs=outputs, deliveries=deliveries)
