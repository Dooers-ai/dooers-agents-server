"""Legacy runner: each scenario on the production code path (``thread_management="agent"``).

``run_legacy`` builds a real ``AgentServer``. Only the infrastructure is replaced:

- ``_build_persistence`` returns ``MemoryPersistence`` (Postgres row semantics);
- ``init_otel`` is a no-op and OTEL stays off (the platform default would export spans);
- ``upload_store`` gets sequential UUIDs, so ``ref_id`` values repeat between runs;
- ``respx`` serves a fake tools-whatsapp and a fake core. Any other HTTP request
  raises (``assert_all_mocked``), so nothing reaches the network.

Flows: ``dispatch`` calls ``AgentServer.dispatch`` per turn; ``websocket`` sends frames
through ``AgentServer.handle`` (Router); ``script`` runs the scenario's own calls on the
public ``AgentServer`` API.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import respx

import dooers.agents.server.observability.otel as otel
import dooers.agents.server.server as server_module
import dooers.agents.server.upload_store as upload_store_module
from dooers.agents.server.auth_validation import (
    CORE_ISSUER,
    DASHBOARD_VALIDATION_PATH,
    PUBLIC_CHAT_VALIDATION_PATH,
)
from dooers.agents.server.config import AgentConfig
from dooers.agents.server.server import AgentServer
from dooers.tools.whatsapp.transport import build_x_message_sig
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

__all__ = ["ParityResult", "run_legacy"]


class WebSocketDisconnect(Exception):
    """Same name as Starlette's, so ``AgentServer.handle`` treats it as a normal close."""


class _SequentialUuid:
    """Stands in for the ``uuid`` module inside ``upload_store``."""

    def __init__(self) -> None:
        self._count = 0

    def uuid4(self) -> uuid.UUID:
        self._count += 1
        return uuid.UUID(int=self._count)


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
                    "system_role": "user",
                },
                "organization": {"id": ORG_ID, "role": "member", "plan": "free"},
                "workspace": {"id": WORKSPACE_ID, "role": "member", "queues": []},
                "agent": {"id": AGENT_ID, "owner_user_id": None, "can_configure_settings": False},
                "policies": {"rate_limit_msgs_per_min": 15, "thread_ttl_hours": 24},
            },
        )


@contextmanager
def _offline_environment(persistence: MemoryPersistence, connect: Connect) -> Iterator[_FakeServices]:
    fakes = _FakeServices(connect)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"DOOERS_WHATSAPP_TOOLS_BASE": TOOLS_BASE}))
        os.environ.pop("DOOERS_RUNTIME_ID", None)  # restored by patch.dict
        stack.enter_context(patch.object(server_module, "_build_persistence", lambda _config: persistence))
        stack.enter_context(patch.object(otel, "init_otel", lambda **_kwargs: None))
        stack.enter_context(patch.object(otel, "_otel_enabled", False))
        stack.enter_context(patch.object(upload_store_module, "uuid", _SequentialUuid()))
        mock = stack.enter_context(respx.mock(assert_all_called=False))
        mock.post(f"{TOOLS_BASE}/api/v1/messages").mock(side_effect=fakes.whatsapp_tools)
        mock.post(f"{CORE_BASE}{DASHBOARD_VALIDATION_PATH}").mock(side_effect=fakes.core_validate)
        mock.post(f"{CORE_BASE}{PUBLIC_CHAT_VALIDATION_PATH}").mock(side_effect=fakes.core_validate)
        yield fakes


def _config(spec: ScenarioInput) -> AgentConfig:
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
    with _offline_environment(persistence, spec.connect or DASHBOARD_CONNECT) as fakes:
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
