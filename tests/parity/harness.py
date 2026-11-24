"""Legacy runner: each scenario on the production code path (``thread_management="agent"``).

``run_legacy`` builds a real ``AgentServer``. Only the infrastructure is replaced:

- ``_build_persistence`` returns ``MemoryPersistence`` (Postgres row semantics);
- ``init_otel`` is a no-op and OTEL stays off (the platform default would export spans);
- ``respx`` serves a fake tools-whatsapp and a fake core. Any other HTTP request
  raises (``assert_all_mocked``), so nothing reaches the network.

Flows: ``dispatch`` calls ``AgentServer.dispatch`` per turn; ``websocket`` sends frames
through ``AgentServer.handle`` (Router); ``script`` runs the scenario's own calls on the
public ``AgentServer`` API.

``run_dooers_threads`` runs the same scenario with ``thread_management="dooers_threads"``, against
``FakeDooersThreads``. Each scenario keeps the ingress its flow stands for in production:

- ``dispatch`` flow, channel ``dooers-platform``: production ``AgentServer.dispatch``. Its
  Dooers Threads side is the agent's own publication of the person's message
  (``FakeDooersThreads.agent_publish``: the declared name, e-mail and identity IDs, URL parts as
  ``media_link``, a delivery for the agent). The SDK call that publishes it is plan 5, so
  the harness plays it;
- ``websocket`` flow without the public chat handshake: production web chat. The person
  publishes in Dooers Threads' web chat (``FakeDooersThreads.publish``) and answers the form there
  (``respond_form``);
- in both, the fake creates the thread, and each turn reaches the handler through a Dooers Threads
  delivery to the SDK adapter (``create_text_delivery_adapter``);
- channels (``channel`` other than ``dooers-platform``), public chat (``connect``) and
  ``script`` scenarios: their ingress is the agent's own ``AgentServer``, so they call
  the same ``AgentServer`` method on a Dooers Threads-mode server. Today it raises
  ``THREAD_MANAGEMENT_MISMATCH`` (or the router refuses the frame), and that is the gap.

Dooers Threads is the thread authority in this mode, so events, threads and runs are read back from
the fake through the SDK's own conversion: ``DooersThreadsExecutionPersistence`` (the persistence of
the handler's ``DooersThreadsAgentMemory``) over the whole thread as the agent's context has it, and
``legacy_model.to_runs``. ``ParityResult.deliveries`` records how each delivery ended (HTTP
status, state, reason code). If FakeDooersThreads refused an SDK request that
``EXPECTED_REJECTIONS`` does not list, ``run_dooers_threads`` raises ``ContractViolation``, also
when the scenario itself fails.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import respx
from aiohttp.base_protocol import BaseProtocol as _AioBaseProtocol
from aiohttp.streams import StreamReader as _AioStreamReader
from aiohttp.test_utils import make_mocked_request

import dooers.agents.server.dooers_threads.media_fetch as media_fetch_module
import dooers.agents.server.observability.otel as otel
import dooers.agents.server.server as server_module
from dooers.agents.server.auth_validation import (
    CORE_ISSUER,
    DASHBOARD_VALIDATION_PATH,
    PUBLIC_CHAT_VALIDATION_PATH,
)
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.dooers_threads import create_text_delivery_adapter
from dooers.agents.server.dooers_threads.agent_api import DooersThreadsAgentApi
from dooers.agents.server.dooers_threads.agent_persistence import DooersThreadsAgentPersistence
from dooers.agents.server.dooers_threads.context import DooersThreadsPrivateResources
from dooers.agents.server.dooers_threads.legacy_model import to_runs
from dooers.agents.server.dooers_threads.links import ArtifactLinkMissing, LinkResolver
from dooers.agents.server.dooers_threads.persistence import DooersThreadsExecutionPersistence
from dooers.agents.server.dooers_threads.window import DooersThreadsContextWindow
from dooers.agents.server.server import AgentServer
from dooers.tools.whatsapp.transport import build_x_message_sig
from tests.parity.context_pages import ThreadPages
from tests.parity.fake_agent_api import PREFIX as AGENT_API_PREFIX
from tests.parity.fake_agent_api import FakeAgentApi
from tests.parity.fake_dooers_threads import CALLBACK_BASE_URL, FakeDooersThreads
from tests.parity.memory_persistence import MemoryPersistence
from tests.parity.model import (
    AGENT_ID,
    ASSISTANT_NAME,
    CORE_BASE,
    DASHBOARD_CONNECT,
    FORM_EVENT_REF,
    ORG_ID,
    PUBLIC_CHAT_LINK_ID,
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
from tests.test_dooers_threads_agent_persistence import core_person

__all__ = ["EXPECTED_REJECTIONS", "ContractViolation", "ParityResult", "run_legacy", "run_dooers_threads"]


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


async def _agent_api_response(fake: FakeAgentApi, request: httpx.Request) -> httpx.Response:
    """One ``DooersThreadsAgentApi`` call (``/agent/*``) answered by ``FakeAgentApi``, without a real socket.

    ``FakeAgentApi._handle`` is written against ``aiohttp.web.Request``; ``make_mocked_request`` builds one from the
    same method/path/query/headers/body the SDK's ``httpx.Request`` carries (no translation of the body: ``FakeAgentApi``
    reads and parses it itself, JSON or multipart). The ``aiohttp.web.Response`` it returns goes back as an
    ``httpx.Response`` the same way, by status, headers and body bytes.
    """
    loop = asyncio.get_event_loop()
    protocol = _AioBaseProtocol(loop=loop)
    stream = _AioStreamReader(protocol, limit=64 * 1024 * 1024, loop=loop)
    stream.feed_data(request.content)
    stream.feed_eof()
    raw_path = request.url.raw_path
    path = raw_path.decode() if isinstance(raw_path, bytes) else str(raw_path)
    aio_request = make_mocked_request(request.method, path, headers=dict(request.headers), payload=stream)
    response = await fake._handle(aio_request)  # noqa: SLF001 - the parity harness drives this fake directly
    headers = {key: value for key, value in response.headers.items() if key.lower() not in ("content-length", "transfer-encoding")}
    return httpx.Response(response.status, content=response.body, headers=headers)


def _dooers_threads_or_agent_api(
    dooers_threads: FakeDooersThreads, agent_api: FakeAgentApi
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """Routes one ``callback_base_url`` request to ``FakeAgentApi`` (``/agent/*``) or to ``FakeDooersThreads`` (everything else).

    Both share the same ``callback_base_url`` path prefix (``/api/v1``, index 4.3 / Dooers Threads' own agent-api mount), so
    ``DooersThreadsAgentApi`` (outside a delivery) and the delivery callback protocol (``DooersThreadsDeliveryAdapter``, during a
    delivery) reach the right fake with no extra configuration.
    """

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(AGENT_API_PREFIX + "/agent/"):
            return await _agent_api_response(agent_api, request)
        return dooers_threads.handle(request)

    return route


@contextmanager
def _offline_environment(
    persistence: MemoryPersistence,
    connect: Connect,
    dooers_threads: FakeDooersThreads | None = None,
    media: Mapping[str, tuple[bytes, str]] | None = None,
    agent_api: FakeAgentApi | None = None,
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
        # send.image/audio/document in Dooers Threads mode publish a media_link with the agent's URL for
        # an offline URL (index 4.3), as for an unreachable URL, and upload a served file.
        stack.enter_context(patch.object(media_fetch_module, "_system_resolver", _media_resolver(media)))
        mock = stack.enter_context(respx.mock(assert_all_called=False))
        for url, (data, content_type) in media.items():
            route = mock.get(host=MEDIA_HOST_IP, path=urlsplit(url).path)
            route.mock(return_value=httpx.Response(200, content=data, headers={"Content-Type": content_type}))
        mock.post(f"{TOOLS_BASE}/api/v1/messages").mock(side_effect=fakes.whatsapp_tools)
        mock.post(f"{CORE_BASE}{DASHBOARD_VALIDATION_PATH}").mock(side_effect=fakes.core_validate)
        mock.post(f"{CORE_BASE}{PUBLIC_CHAT_VALIDATION_PATH}").mock(side_effect=fakes.core_validate)
        if dooers_threads is not None:
            if agent_api is not None:
                mock.route(url__startswith=CALLBACK_BASE_URL + "/").mock(
                    side_effect=_dooers_threads_or_agent_api(dooers_threads, agent_api)
                )
            else:
                stack.enter_context(dooers_threads.serving(mock))
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
    from dooers.agents.server.handlers.router import Router

    socket = _ScriptedWebSocket(scenario.incoming, fakes.connect)
    route = Router.route

    async def route_and_wait(self: Router, ws: Any, frame: Any) -> None:
        # Since 0.31 ``event.create`` turns run on a background worker; the scripted socket
        # disconnects when its frames end, which drops queued turns. Wait for each turn so the
        # scenario keeps the one-frame-at-a-time order it was recorded with.
        await route(self, ws, frame)
        await self.wait_idle()

    with patch.object(Router, "route", route_and_wait):
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


# -- Dooers Threads mode --


class ContractViolation(Exception):
    """FakeDooersThreads refused an SDK request (contract or Dooers Threads rule) that the scenario does not expect.

    It is never a parity gap: ``KNOWN_GAPS`` never lists it as the expected failure.
    """


#: SDK callbacks a scenario expects FakeDooersThreads to refuse: scenario -> ((action, error code, why), ...).
#: Empty today: in every scenario the SDK sends only requests that the contract accepts.
EXPECTED_REJECTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {}


def _check_rejections(name: str, dooers_threads: FakeDooersThreads) -> None:
    expected = [(action, code) for action, code, _why in EXPECTED_REJECTIONS.get(name, ())]
    if dooers_threads.rejections != expected:
        raise ContractViolation(f"{name}: FakeDooersThreads refused {dooers_threads.rejections}, expected {expected}")


def _delivered_by_dooers_threads(spec: ScenarioInput) -> bool:
    """Platform turns go through Dooers Threads; channels, public chat and scripts keep the AgentServer ingress."""
    return spec.flow != "script" and spec.connect is None and spec.channel == "dooers-platform"


def _is_public_chat_scenario(spec: ScenarioInput) -> bool:
    """A public chat visitor (``tests/parity/model.py:109``'s ``Connect.connection_type``).

    Section 7.3 of the design: the visitor never reaches the Dooers Threads-mode Router. It talks to Dooers Threads
    directly (the core token exchange and ``ensurePublicChatThread``, plan 6 tasks 2-3), so this is a
    sibling of ``_run_dooers_threads_deliveries``/``_run_dooers_threads_server``, not a case either one handles.
    """
    return spec.connect is not None and spec.connect.connection_type in {"guest", "public_authenticated"}


def _public_chat_external_key(connect: Connect) -> str:
    """``visitorThreadKey`` (``public-chat.ts``): ``public:<link>:<connection_type>:<external_id>``."""
    return f"public:{PUBLIC_CHAT_LINK_ID}:{connect.connection_type}:{connect.core_user.user_id}"


def _public_chat_channel_meta(spec: ScenarioInput) -> dict[str, Any] | None:
    """``channel_meta`` of the visitor's thread: the first turn that carries one (``PUBLIC_CHAT_CHANNEL``)."""
    for turn in spec.turns:
        if turn.metadata and turn.metadata.get("channel_meta"):
            return dict(turn.metadata["channel_meta"])
    return None


def _public_chat_guest_profile(spec: ScenarioInput) -> dict[str, Any] | None:
    """The visitor's declared pre-chat (``guest_name``/``guest_email``/``guest_phone`` of the first turn's
    metadata): what the core's ``thread.create.public_chat`` decision would carry as the visitor's profile.
    """
    if not spec.turns:
        return None
    metadata = spec.turns[0].metadata or {}
    profile = {key[len("guest_") :]: metadata[key] for key in ("guest_name", "guest_email", "guest_phone") if metadata.get(key)}
    return profile or None


async def _run_dooers_threads_public_chat(dooers_threads: FakeDooersThreads, scenario: Scenario) -> dict[str, Any]:
    """The public chat visitor's own path (design section 7.3): never the Dooers Threads-mode Router.

    The visitor's browser talks to Dooers Threads directly (the core token exchange, then
    ``ensurePublicChatThread``): the agent never sees a ``connect``/``event.create`` frame for it. Each
    turn is the visitor's own publication (``FakeDooersThreads.publish_external``), delivered the same way a
    web chat person's message is (``_run_dooers_threads_deliveries``'s own loop).
    """
    spec = scenario.incoming
    connect = spec.connect
    assert connect is not None

    async def resources(scope: Any) -> DooersThreadsPrivateResources:
        return DooersThreadsPrivateResources(scope, MemoryPersistence(), {}, {})

    agent_config = _config(spec, thread_management="dooers_threads", dooers_threads=dooers_threads.config)
    adapter = create_text_delivery_adapter(agent_config, scenario.handler, resources)
    thread_id = dooers_threads.ensure_external_thread(
        connect.core_user,
        external_key=_public_chat_external_key(connect),
        connection_type=connect.connection_type,
        channel_meta=_public_chat_channel_meta(spec),
        guest_profile=_public_chat_guest_profile(spec),
    )
    for number, turn in enumerate(spec.turns, start=1):
        if turn.event_type == "form.response":
            data = dict(turn.data or {})
            form_event_id = (
                _last_form(dooers_threads, thread_id) if data.get("form_event_id") == FORM_EVENT_REF else data.get("form_event_id")
            )
            trigger = dooers_threads.respond_form_external(
                thread_id,
                connect.core_user,
                form_event_id,
                values=copy.deepcopy(data.get("values") or {}),
                cancelled=bool(data.get("cancelled")),
            )
        else:
            trigger = dooers_threads.publish_external(
                thread_id, connect.core_user, _dooers_threads_content(turn), client_event_id=f"client-{number}"
            )
        for delivery in dooers_threads.deliveries_for(trigger):
            await dooers_threads.deliver(adapter, delivery)
    return {}


def _dooers_threads_content(turn: Turn) -> list[dict[str, Any]]:
    """What the web chat publishes: the wire parts, or the message as one text part."""
    return _wire_content(turn) or [{"type": "text", "text": turn.message}]


_MEDIA_LINK_KEYS = ("mime_type", "filename", "alt", "duration")


def _agent_content(turn: Turn) -> list[dict[str, Any]]:
    """The turn as the agent's own publication carries it (plan 5 maps ``AgentServer.dispatch`` to it).

    A production URL part (``image``/``audio``/``document`` with ``url``) is a ``media_link``
    (index 4.3), which has no ``size_bytes``; text and contact parts keep their fields.
    """
    parts = []
    for part in _dooers_threads_content(turn):
        if part["type"] in {"image", "audio", "document"}:
            extra = {key: part[key] for key in _MEDIA_LINK_KEYS if part.get(key) is not None}
            parts.append({"type": "media_link", "kind": part["type"], "url": part["url"], **extra})
        else:
            parts.append(part)
    return parts


def _last_form(dooers_threads: FakeDooersThreads, thread_id: str) -> str:
    """The id of the thread's last production form, as the web chat knows it (``FORM_EVENT_REF``)."""
    return [event["event_id"] for event in dooers_threads.events[thread_id] if event["type"] == "form.created"][-1]


async def _run_dooers_threads_deliveries(dooers_threads: FakeDooersThreads, scenario: Scenario) -> dict[str, Any]:
    """Each turn reaches the handler through a Dooers Threads delivery.

    ``dispatch`` flow (production ``AgentServer.dispatch``): the agent's own publication of the
    person's message (``FakeDooersThreads.agent_publish``: declared name, e-mail and identity IDs, URL
    parts as ``media_link``). ``websocket`` flow (production web chat): the person publishes in
    Dooers Threads' web chat (``publish``) and answers the form there (``respond_form``).
    """
    spec = scenario.incoming

    async def resources(scope: Any) -> DooersThreadsPrivateResources:
        # The queue catalog comes from the delivery context (``workspace``), not from the operator.
        return DooersThreadsPrivateResources(scope, MemoryPersistence(), {}, {})

    # Configured as the legacy side (``_config``): the same AgentConfig (assistant name, content policy).
    agent_config = _config(spec, thread_management="dooers_threads", dooers_threads=dooers_threads.config)
    adapter = create_text_delivery_adapter(agent_config, scenario.handler, resources)
    thread_id = dooers_threads.create_thread(spec.user, title=spec.thread_title)
    for number, turn in enumerate(spec.turns, start=1):
        if turn.event_type == "form.response":
            data = dict(turn.data or {})
            form_event_id = (
                _last_form(dooers_threads, thread_id) if data.get("form_event_id") == FORM_EVENT_REF else data.get("form_event_id")
            )
            trigger = dooers_threads.respond_form(
                thread_id, spec.user, form_event_id, values=copy.deepcopy(data.get("values") or {}), cancelled=bool(data.get("cancelled"))
            )
        elif spec.flow == "dispatch":
            trigger = dooers_threads.agent_publish(thread_id, spec.user, _agent_content(turn), client_event_id=f"client-{number}")
        else:
            trigger = dooers_threads.publish(thread_id, spec.user, _dooers_threads_content(turn), client_event_id=f"client-{number}")
        for delivery in dooers_threads.deliveries_for(trigger):
            await dooers_threads.deliver(adapter, delivery)
    return {}


def _prepare_agent_api(spec: ScenarioInput) -> FakeAgentApi:
    """A loopback agent API for this scenario, with the same identities and core people as ``FakeDooersThreads``.

    ``FakeAgentApi`` keeps its own constants (its own ``AGENT_ID``/``ORGANIZATION_ID``) when nothing else
    uses it (``tests/test_dooers_threads_*.py``); the parity harness overrides them to the scenario's own ids
    (``tests.parity.model``) so a thread it creates passes ``DooersThreadsAgentApi._check_thread`` (same org/workspace
    as ``AgentConfig.dooers_threads``) and its credential's ``agent_id`` claim (``FakeDooersThreads``'s) matches.
    """
    fake = FakeAgentApi(workspace_id=WORKSPACE_ID)
    fake.agent_id = AGENT_ID
    fake.organization_id = ORG_ID
    for person in (spec.user, *spec.core_people):
        fake.core_people[person.user_id] = core_person(person)
    return fake


def _agent_participant(thread: Any) -> dict[str, Any]:
    return next(p for p in thread.participants if p["kind"] == "agent")


#: Core's view of a human (``FakeAgentApi._core_person``), never part of a stored ``Public.Thread`` participant:
#: ``FakeDooersThreads``'s own participants never carry it either (only ``_with_profile`` adds it, for a context/agent-view
#: projection, not for ``self.threads``).
_CORE_PROFILE_KEYS = ("email", "identity_ids", "system_role", "organization_role", "workspace_role")


def _as_thread_participant(participant: Mapping[str, Any]) -> dict[str, Any]:
    """``FakeAgentApi``'s participant, mirrored for ``Public.Thread`` (index 4.3): the core's profile left out."""
    return {key: value for key, value in participant.items() if key not in _CORE_PROFILE_KEYS}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _deliver_agent_trigger(
    dooers_threads: FakeDooersThreads,
    fake: FakeAgentApi,
    adapter: Any,
    thread_id: str,
    trigger_event: dict[str, Any],
    delivery_id: str,
) -> None:
    """A ``trigger: true`` publication of ``FakeAgentApi`` (dispatch/ingest/WhatsApp inbound), delivered for real.

    Mirrors the thread and its events so far into ``FakeDooersThreads`` (the only fake that plays the delivery
    callback protocol: context, outputs, complete), runs the delivery through ``create_text_delivery_adapter``
    (the same adapter ``_run_dooers_threads_deliveries`` uses), then plays back into ``FakeAgentApi`` what a real
    Dooers Threads would also write to the thread: the delivery's own lifecycle (``execution.started``,
    ``execution.completed``/``failed``, ``delivery.updated`` — ``FakeDooersThreads`` has no notion of these; only
    ``DooersThreadsDispatchStream``'s agent-api polling needs them) and the handler's own output events (so a later
    agent-api read, e.g. ``AgentServer.memory()``, sees the reply too).
    """
    agent_thread = fake.threads[thread_id]
    dooers_threads.admit_agent_thread(
        thread_id,
        title=agent_thread.title,
        queue=agent_thread.queue,
        channel=agent_thread.channel,
        channel_meta=agent_thread.channel_meta,
        external_key=agent_thread.external_key,
        owner_instance_agent_id=fake.agent_id,
        participants=[_as_thread_participant(p) for p in agent_thread.participants],
        created_at=agent_thread.created_at,
        updated_at=agent_thread.updated_at or agent_thread.created_at,
        version=agent_thread.version,
    )
    # A file the trigger references (a staged ``chat_upload``) must be in FakeDooersThreads's own store too: the
    # delivery's download/link-issue routes are served there, never by FakeAgentApi.
    for part in trigger_event["payload"].get("content") or []:
        if part.get("type") == "attachment":
            artifact = fake.artifacts[part["artifact_id"]]
            dooers_threads.admit_agent_artifact(
                part["artifact_id"], artifact["data"], filename=artifact["filename"], mime_type=artifact["mime_type"], thread_id=thread_id
            )
    # Only the trigger itself: any earlier event of this thread (a prior dispatch call and its outputs) was
    # already mirrored in an earlier call of this function. Same as a thread participant (index 4.3): the
    # core's profile is never part of a stored actor either.
    trigger_actor = _as_thread_participant(trigger_event["actor"])
    mirrored_trigger = dooers_threads.admit_agent_event(
        thread_id, trigger_event["type"], trigger_actor, copy.deepcopy(trigger_event["payload"])
    )

    delivery = dooers_threads.admit_agent_delivery(thread_id, delivery_id)
    execution_id = delivery.execution_id
    agent_participant_id = _agent_participant(agent_thread)["participant_id"]
    agent_actor = {
        "kind": "agent",
        "participant_id": agent_participant_id,
        "principal_id": fake.agent_id,
        "instance_agent_id": fake.agent_id,
        "display_name": "Agente",
    }

    started_at = _iso_now()
    fake.inject_event(
        thread_id,
        "execution.started",
        {
            "execution_id": execution_id,
            "delivery_id": delivery_id,
            "participant_id": agent_participant_id,
            "phase_id": "initial",
            "state": "running",
            "outcome": None,
            "reason_code": None,
            "deadline_at": started_at,
            "completed_at": None,
        },
        actor=agent_actor,
        delivery_id=delivery_id,
        execution_id=execution_id,
    )

    def mirror_output(appended_thread_id: str, event: dict[str, Any]) -> None:
        """A handler output ``FakeDooersThreads`` just stored, mirrored into ``FakeAgentApi`` as it happens.

        Some of the executor's OWN later calls of this same delivery (``record_channel_delivery`` after a
        WhatsApp send, an artifact link) already need the event through the agent API, so this cannot wait
        for the delivery to end: ``FakeDooersThreads.on_append`` fires it the moment ``_append`` stores the event.
        """
        if appended_thread_id != thread_id or event["type"] == "channel_delivery.updated":
            # A ``channel_delivery.updated`` comes from ``FakeAgentApi`` (``mirror_channel_delivery`` below): it has it already.
            return
        payload = copy.deepcopy(event["payload"])
        if payload.get("trigger_event_id") == mirrored_trigger["event_id"]:
            # ``run.updated`` names the trigger by FakeDooersThreads's own (mirrored) event id; FakeAgentApi's
            # conversion backfills the triggering message's ``run_id`` by matching this id in its own
            # thread, so it must be the trigger's id there, not FakeDooersThreads's copy of it.
            payload["trigger_event_id"] = trigger_event["event_id"]
        fake.inject_event(
            thread_id,
            event["type"],
            payload,
            actor=copy.deepcopy(event["actor"]),
            delivery_id=delivery_id,
            execution_id=execution_id,
            # Same event id as FakeDooersThreads's own copy: a later SDK call (``record_channel_delivery``, an
            # artifact link) names this output by the id the sink already learned when it was first written.
            event_id=event["event_id"],
            root_event_id=event["event_id"],
        )

    dooers_threads.on_append = mirror_output
    try:
        await dooers_threads.deliver(adapter, delivery)
    finally:
        dooers_threads.on_append = None
    state, reason = delivery.state, delivery.reason_code

    completed_at = _iso_now()
    fake.inject_event(
        thread_id,
        "execution.failed" if state == "failed" else "execution.completed",
        {
            "execution_id": execution_id,
            "delivery_id": delivery_id,
            "participant_id": agent_participant_id,
            "phase_id": "initial",
            "state": "failed" if state == "failed" else "completed",
            "outcome": state,
            "reason_code": reason,
            "deadline_at": started_at,
            "completed_at": completed_at,
        },
        actor=agent_actor,
        delivery_id=delivery_id,
        execution_id=execution_id,
    )
    fake.inject_event(
        thread_id,
        "delivery.updated",
        {
            "delivery": {
                "attempt_count": 1,
                "completed_at": completed_at,
                "created_at": started_at,
                "delivery_id": delivery_id,
                "execution_id": execution_id,
                "expires_at": completed_at,
                "participant_id": agent_participant_id,
                "phase_id": "initial",
                "reason_code": reason,
                "state": state,
                "trigger_event_id": trigger_event["event_id"],
                "updated_at": completed_at,
            }
        },
        actor=agent_actor,
        delivery_id=delivery_id,
        execution_id=execution_id,
    )


async def _run_dooers_threads_server(
    dooers_threads: FakeDooersThreads, fake: FakeAgentApi, scenario: Scenario, fakes: _FakeServices
) -> dict[str, Any]:
    """``AgentServer`` ingress outside a delivery (dispatch/ingest/repository/chat_upload/WhatsApp, plan 5, task 8).

    The server's ``DooersThreadsAgentApi`` calls reach ``FakeAgentApi`` (``_offline_environment``'s combined route).
    Any publication with ``trigger: true`` it makes (``AgentServer.dispatch``, the WhatsApp inbound webhook) is
    delivered for real through ``create_text_delivery_adapter``, as the trigger would be in production.
    """
    spec = scenario.incoming
    agent_config = _config(spec, thread_management="dooers_threads", dooers_threads=dooers_threads.config)
    server = AgentServer(agent_config)

    async def resources(scope: Any) -> DooersThreadsPrivateResources:
        # ``AgentServer``'s own persistence (F21): a handler output's ``send.whatsapp.*`` reads the service
        # secrets merged onto it below (``create_dooers_whatsapp_outbound``), the same as the WhatsApp
        # credentials a mounted agent's own executor reads.
        return DooersThreadsPrivateResources(scope, server.persistence, {}, {})

    adapter = create_text_delivery_adapter(agent_config, scenario.handler, resources)

    pending: list[asyncio.Task[None]] = []

    def on_trigger(thread_id: str, trigger_event: dict[str, Any], delivery_id: str) -> None:
        pending.append(asyncio.ensure_future(_deliver_agent_trigger(dooers_threads, fake, adapter, thread_id, trigger_event, delivery_id)))

    def mirror_channel_delivery(thread_id: str, event: dict[str, Any]) -> None:
        """The other direction: the status of a WhatsApp send that ``FakeAgentApi`` stored is in ``FakeDooersThreads``'s thread too.

        Real Dooers Threads keeps one thread, so the next delivery's context has the ``channel_delivery.updated``. The id of its
        target is the same in both fakes (``mirror_output`` gives a handler output the id ``FakeDooersThreads`` chose).
        """
        if thread_id in dooers_threads.threads:
            dooers_threads.admit_agent_event(thread_id, event["type"], event["actor"], event["payload"])

    fake.on_trigger = on_trigger
    fake.on_channel_delivery = mirror_channel_delivery
    try:
        await server.ensure_initialized()
        if spec.service_secrets:
            await server.persistence.merge_service_secrets(spec.agent_id, dict(spec.service_secrets))
        outputs = await _FLOWS[spec.flow](server, scenario, fakes)
        if pending:
            await asyncio.gather(*pending)
        return outputs
    finally:
        await server.close()


async def _agent_api_read_back(
    fake: FakeAgentApi, dooers_threads_config: Any, observe: frozenset[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Events, threads and runs read back the same way ``AgentServer.repository()``/``.memory()`` would.

    ``AgentServer`` ingress scenarios (plan 5, task 8) have no reason to trust a separate, test-only
    conversion of the thread: ``DooersThreadsAgentPersistence`` is the production ``Persistence`` of a Dooers Threads-mode
    agent outside a delivery, so the comparison goes through the same code the scenario itself exercised.
    """
    api = DooersThreadsAgentApi(dooers_threads_config)
    read = DooersThreadsAgentPersistence(api, assistant_name=ASSISTANT_NAME, local=MemoryPersistence())
    events: list[dict[str, Any]] = []
    out: dict[str, Any] = {}
    # The SDK shows a Dooers Threads thread under its external key when it has one (``DooersThreadsAgentPersistence._caller_key``),
    # never under FakeAgentApi's own internal id: the same ref ``AgentServer.ingest``/``.dispatch`` already returned.
    for thread in list(fake.threads.values()):
        ref = thread.external_key or thread.id
        events.extend(event.model_dump() for event in await read.get_events(ref, limit=10_000))
        if "threads" in observe:
            view = await read.get_thread(ref)
            out.setdefault("threads", []).append(view.model_dump() if view is not None else None)
        if "runs" in observe:
            out.setdefault("runs", []).extend(run.model_dump() for run in await read.list_runs(thread_id=ref, limit=10_000))
    for name in observe:
        out.setdefault(name, [])
    return events, out


class _AgentReader:
    """The whole Dooers Threads thread as the agent's delivery context has it, for ``DooersThreadsExecutionPersistence``.

    The same reads a handler's ``DooersThreadsAgentMemory`` makes, without a cutoff: the snapshot is
    ``FakeDooersThreads.agent_view`` and each file gets a temporary link from the fake.
    """

    def __init__(self, dooers_threads: FakeDooersThreads, thread_id: str) -> None:
        self._dooers_threads, self._thread_id = dooers_threads, thread_id
        self.envelope = {
            "thread_id": thread_id,
            "organization_id": ORG_ID,
            "workspace_id": WORKSPACE_ID,
            "instance_agent_id": AGENT_ID,
        }

        async def issue(artifact_id: str) -> dict[str, Any]:
            try:
                return dooers_threads.artifact_link(thread_id, artifact_id)
            except LookupError:
                raise ArtifactLinkMissing("NOT_FOUND") from None

        self.links = LinkResolver(issue)
        view = dooers_threads.agent_view(thread_id)
        self._window = ThreadPages(
            view["events"], view["participants"], workspace=view.get("workspace"), thread=view.get("thread")
        ).window()

    def window(self) -> DooersThreadsContextWindow:
        return self._window


async def _dooers_threads_read_back(
    dooers_threads: FakeDooersThreads, observe: frozenset[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Events, threads and runs read back through the SDK's conversion, as production ``get_events`` reads them."""
    events: list[dict[str, Any]] = []
    out: dict[str, Any] = {}
    for thread_id in dooers_threads.threads:
        reader = _AgentReader(dooers_threads, thread_id)
        persistence = DooersThreadsExecutionPersistence(reader, assistant_name=ASSISTANT_NAME)
        events.extend(event.model_dump() for event in await persistence.get_events(thread_id, limit=10_000))
        if "threads" in observe:
            thread = await persistence.get_thread(thread_id)
            out.setdefault("threads", []).append(thread.model_dump())
        if "runs" in observe:
            # Production ``list_runs``: the run that started last comes first.
            runs = reversed(to_runs(dooers_threads.agent_view(thread_id)["events"]))
            out.setdefault("runs", []).extend(run.model_dump() for run in runs)
    for name in observe:
        out.setdefault(name, [])
    return events, out


async def run_dooers_threads(name: str) -> ParityResult:
    scenario = SCENARIOS[name]
    spec = scenario.incoming
    public_chat = _is_public_chat_scenario(spec)
    #: The public chat visitor is what Core's connection context knows (``spec.user`` is irrelevant to it:
    #: that path never reaches ``AgentServer.handle``/``.dispatch``, so it never sees that identity).
    core_people = [spec.connect.core_user] if public_chat and spec.connect is not None else [spec.user]
    dooers_threads = FakeDooersThreads(assistant_name=ASSISTANT_NAME, core_people=[*core_people, *spec.core_people], queues=spec.queues)
    persistence = MemoryPersistence()
    server_mode = not public_chat and not _delivered_by_dooers_threads(spec)
    fake_agent_api = _prepare_agent_api(spec) if server_mode else None
    with _offline_environment(
        persistence, spec.connect or DASHBOARD_CONNECT, dooers_threads, spec.media, agent_api=fake_agent_api
    ) as fakes:
        capture, token = open_capture(scenario.history_formats)
        try:
            if public_chat:
                outputs = await _run_dooers_threads_public_chat(dooers_threads, scenario)
            elif server_mode:
                assert fake_agent_api is not None
                outputs = await _run_dooers_threads_server(dooers_threads, fake_agent_api, scenario, fakes)
            else:
                outputs = await _run_dooers_threads_deliveries(dooers_threads, scenario)
        except Exception:
            # A refused SDK request outranks the scenario's own failure (a gap never hides it).
            _check_rejections(name, dooers_threads)
            raise
        finally:
            close_capture(token)
        _check_rejections(name, dooers_threads)
        if fake_agent_api is not None:
            agent_config = _config(spec, thread_management="dooers_threads", dooers_threads=dooers_threads.config)
            events, observed = await _agent_api_read_back(fake_agent_api, agent_config.dooers_threads, spec.observe)
        else:
            events, observed = await _dooers_threads_read_back(dooers_threads, spec.observe)
    outputs.update(observed)
    if fakes.whatsapp_requests:
        outputs["whatsapp_requests"] = fakes.whatsapp_requests
    if persistence.thread_ids():
        # Dooers Threads owns the threads in this mode; a local thread is a difference, never hidden.
        outputs["agent_local_events"] = await _persisted_events(persistence)
    deliveries = [{"status": d.status_code, "state": d.state, "reason_code": d.reason_code} for d in dooers_threads.deliveries.values()]
    return ParityResult(events=events, incoming=capture.incoming, histories=capture.histories, outputs=outputs, deliveries=deliveries)
