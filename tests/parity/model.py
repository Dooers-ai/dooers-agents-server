"""Types shared by the parity scenarios and runners, plus the handler capture.

A scenario is a creator handler plus the input a consumer sends. A runner (legacy
here, Maestro in task 8) executes it and returns a ``ParityResult``. Handlers record
what they observe (``AgentIncoming``, ``memory.get_history``) through ``capture_*``;
the runner opens the capture with ``open_capture`` before it starts the scenario.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from dooers.agents.server.protocol.models import (
    User,
    WorkspaceQueueCatalog,
    WorkspaceQueueMember,
)

if TYPE_CHECKING:
    from dooers.agents.server.server import AgentServer

Handler = Callable[..., AsyncGenerator[Any, None]]
Script = Callable[["AgentServer", Handler], Awaitable[dict[str, Any]]]
Flow = Literal["dispatch", "websocket", "script"]

# -- Shared scenario values --

# Maestro requires UUIDs for the organization, the workspace and each agent, so both modes
# use the same UUIDs (the platform ids are UUIDs too). User ids and caller thread keys are
# free strings in Maestro and stay readable.
AGENT_ID = "33333333-3333-4333-8333-333333333333"
OTHER_AGENT_ID = "44444444-4444-4444-8444-444444444444"
ORG_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
ASSISTANT_NAME = "Assistente"
TOOLS_BASE = "http://127.0.0.1:8810"
CORE_BASE = "https://core.parity.test"
WHATSAPP_INSTANCE = "inst-1"
WHATSAPP_CUSTOMER = "+5511999990000"
WHATSAPP_AGENT_PHONE = "+5511888880000"
WHATSAPP_HMAC_SECRET = "parity-hmac-secret"
WHATSAPP_OUTBOUND_ID = "wamid.out.1"

# Non-default roles, so a mode that loses Core's roles (Maestro until plan 5) shows the difference.
USER = User(
    user_id="user-1",
    user_name="Ana Souza",
    user_email="ana@example.com",
    identity_ids=["idp-ana"],
    system_role="admin",
    organization_role="owner",
    workspace_role="manager",
)
WHATSAPP_USER = User(user_id=WHATSAPP_CUSTOMER, user_name="Cliente WhatsApp")
QUEUES = (
    WorkspaceQueueCatalog(
        slug="suporte",
        name="Suporte",
        members=[
            WorkspaceQueueMember(
                user_id="user-2",
                user_name="Bia Lima",
                user_email="bia@example.com",
                identity_ids=["idp-bia"],
                claims={"nivel": 2},
            )
        ],
    ),
    WorkspaceQueueCatalog(slug="vendas", name="Vendas"),
)
WHATSAPP_CHANNEL_META = {
    "whatsapp": {
        "instance_id": WHATSAPP_INSTANCE,
        "from_e164": WHATSAPP_CUSTOMER,
        "agent_phone_e164": WHATSAPP_AGENT_PHONE,
        "wa_message_id": "wamid.in.1",
    }
}
WHATSAPP_SECRETS = {"dooers_whatsapp_instance_hmac_json": json.dumps({WHATSAPP_INSTANCE: WHATSAPP_HMAC_SECRET})}

#: Placeholder in ``Turn.data``: the runner swaps it for the id of the last ``form`` event.
FORM_EVENT_REF = "$form_event_id"


@dataclass(frozen=True)
class Turn:
    """One inbound message. ``content`` holds C2S wire parts; ``message`` is used without it."""

    message: str = "Olá"
    content: tuple[dict[str, Any], ...] | None = None
    #: WebSocket only: ``message`` or ``form.response``.
    event_type: str = "message"
    #: WebSocket only: ``event.data`` (``FORM_EVENT_REF`` is resolved by the runner).
    data: Mapping[str, Any] | None = None
    #: WebSocket only: ``event.create`` metadata (channel, pre-chat form).
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Connect:
    """WebSocket handshake: the connect frame user and the user the fake core returns."""

    connection_type: Literal["dashboard", "guest", "public_authenticated"]
    core_user: User
    frame_user: User


#: Dashboard session (core JWT validated at ``/api/v2/identity/validate-agent-session``).
DASHBOARD_CONNECT = Connect(connection_type="dashboard", core_user=USER, frame_user=USER)


@dataclass(frozen=True)
class ScenarioInput:
    flow: Flow = "dispatch"
    turns: tuple[Turn, ...] = (Turn(),)
    agent_id: str = AGENT_ID
    user: User = field(default_factory=lambda: USER)
    organization_id: str = ORG_ID
    workspace_id: str = WORKSPACE_ID
    thread_title: str | None = None
    channel: str = "dooers-platform"
    channel_meta: Mapping[str, Any] | None = None
    queues: tuple[WorkspaceQueueCatalog, ...] = ()
    #: ``websocket`` flow: public chat handshake through the fake core.
    connect: Connect | None = None
    #: ``script`` flow: runs public ``AgentServer`` calls and returns their results.
    script: Script | None = None
    #: Extra ``AgentConfig`` values (e.g. ``dooers_whatsapp_service``).
    config: Mapping[str, Any] = field(default_factory=dict)
    #: Seeded with ``Persistence.merge_service_secrets`` before the flow runs.
    service_secrets: Mapping[str, str] | None = None
    #: Extra observables in ``ParityResult.outputs``: ``"threads"``, ``"runs"``.
    observe: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Scenario:
    handler: Handler
    incoming: ScenarioInput
    history_formats: tuple[str, ...] = ()


@dataclass
class ParityResult:
    events: list[dict]
    incoming: dict
    histories: dict[str, list[dict]]
    #: Return values of ``AgentServer`` calls and extra observables (threads, runs, acks).
    outputs: dict[str, Any] = field(default_factory=dict)
    #: Maestro runs only: how each delivery ended (``status``, ``state``, ``reason_code``), in
    #: order. Not compared, because the legacy path has no delivery; the Maestro tests check it.
    deliveries: list[dict[str, Any]] = field(default_factory=list, compare=False)


# -- Handler capture --


@dataclass
class Capture:
    history_formats: tuple[str, ...] = ()
    incoming: dict[str, Any] = field(default_factory=dict)
    histories: dict[str, list[dict]] = field(default_factory=dict)


_CAPTURE: ContextVar[Capture | None] = ContextVar("parity_capture", default=None)


def open_capture(history_formats: tuple[str, ...] = ()) -> tuple[Capture, Any]:
    capture = Capture(history_formats=history_formats)
    return capture, _CAPTURE.set(capture)


def close_capture(token: Any) -> None:
    _CAPTURE.reset(token)


def current_capture() -> Capture:
    capture = _CAPTURE.get()
    if capture is None:
        raise RuntimeError("no parity capture is open; run the scenario through a parity runner")
    return capture


def snapshot_incoming(on: Any) -> dict[str, Any]:
    """Everything a handler can read from ``AgentIncoming`` (``context.maestro`` excluded)."""
    from dooers.tools.rag.runtime import current_execution_context

    ctx = on.context
    rag = current_execution_context()
    return {
        "message": on.message,
        "content": [part.model_dump() for part in on.content],
        "form_data": on.form_data,
        "form_cancelled": on.form_cancelled,
        "form_event_id": on.form_event_id,
        "context": {
            "thread_id": ctx.thread_id,
            "agent_id": ctx.agent_id,
            "event_id": ctx.event_id,
            "organization_id": ctx.organization_id,
            "workspace_id": ctx.workspace_id,
            "channel": ctx.channel,
            "channel_meta": ctx.channel_meta,
            "user": ctx.user.model_dump() if ctx.user is not None else None,
            "thread_title": ctx.thread_title,
            "thread_created_at": ctx.thread_created_at,
            "queue": ctx.queue,
            "queues": [queue.model_dump() for queue in ctx.queues],
            "chat_context": ctx.chat_context.model_dump() if ctx.chat_context is not None else None,
        },
        "members_for_queue": [member.model_dump() for member in ctx.members_for_queue()],
        "rag_context": dataclasses.asdict(rag) if rag is not None else None,
    }


def capture_incoming(on: Any, fields: tuple[str, ...] | None = None) -> None:
    snapshot = snapshot_incoming(on)
    if fields is not None:
        snapshot = {key: snapshot[key] for key in fields}
    current_capture().incoming = snapshot


async def capture_histories(memory: Any) -> None:
    capture = current_capture()
    for fmt in capture.history_formats:
        capture.histories[fmt] = await memory.get_history(format=fmt)
