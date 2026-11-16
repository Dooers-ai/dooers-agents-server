"""Turn context (ContextVar) and side-channel event emitter for connector tools."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from dooers.agents.server.exceptions import HandlerError
from dooers.agents.server.handlers.send import AgentEvent
from dooers.tools.connectors.errors import (
    ApprovalDeclined,
    ConnectionRequired,
    ConnectorError,
    InteractionTimeout,
    InvalidArguments,
    PermissionDenied,
)
from dooers.tools.connectors.runtime import current_turn

INTERACTION = {
    "interaction_id": "i-1",
    "kind": "approval",
    "title": "Criar evento",
    "summary": "Criar 'Consulta Ana' amanhã 10:00 em Recepção",
    "connector": {"id": "c-1", "name": "Agenda Recepção", "icon": "google-calendar"},
    "expires_at": "2026-11-16T10:02:00Z",
}


def _appended_types(harness) -> list[list[str]]:
    return [[e.type for e in b["events"]] for b in harness.broadcasts if b["type"] == "event.append"]


async def test_turn_context_visible_in_child_task(pipeline_harness):
    seen = {}

    async def handler(incoming, send, memory, analytics, settings):
        async def child():
            seen["ctx"] = current_turn()

        await asyncio.create_task(child())
        yield send.run_end()

    await pipeline_harness.run(handler, presence_token="jwt", channel="dooers-platform")

    ctx = seen["ctx"]
    assert ctx.presence_token == "jwt" and ctx.thread_id
    assert (ctx.agent_id, ctx.organization_id, ctx.workspace_id) == ("agent-1", "org-1", "ws-1")
    assert (ctx.user_id, ctx.channel) == ("u1", "dooers-platform")
    assert ctx.persistence is pipeline_harness.persistence
    assert ctx.emit is not None
    assert "jwt" not in repr(ctx)


async def test_turn_context_reset_after_turn(pipeline_harness):
    async def ok(incoming, send, memory, analytics, settings):
        assert current_turn() is not None
        yield send.run_end()

    async def boom(incoming, send, memory, analytics, settings):
        yield send.run_start()
        raise RuntimeError("boom")

    assert current_turn() is None
    await pipeline_harness.run(ok)
    assert current_turn() is None
    with pytest.raises(HandlerError):
        await pipeline_harness.run(boom)
    assert current_turn() is None


async def test_emit_persists_and_broadcasts_tool_call_outside_yield(pipeline_harness):
    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        await current_turn().emit(send.tool_call(name="x", args={}, id="c-1"))
        yield send.run_end()

    await pipeline_harness.run(handler)

    calls = [e for e in pipeline_harness.persisted if e.type == "tool.call"]
    assert len(calls) == 1
    # run_id is read when the event is emitted, not when the turn was bound
    (run_id,) = pipeline_harness.persistence.runs
    assert calls[0].run_id == run_id
    assert calls[0].data["id"] == "c-1"
    assert ["tool.call"] in _appended_types(pipeline_harness)


async def test_emit_connector_interaction_creates_event(pipeline_harness):
    async def handler(incoming, send, memory, analytics, settings):
        yield send.run_start()
        await current_turn().emit(AgentEvent(send_type="connector_interaction", data=dict(INTERACTION)))
        yield send.run_end()

    await pipeline_harness.run(handler)

    (ev,) = [e for e in pipeline_harness.persisted if e.type == "connector.interaction"]
    (run_id,) = pipeline_harness.persistence.runs
    assert (ev.actor, ev.run_id, ev.data) == ("system", run_id, INTERACTION)
    assert ["connector.interaction"] in _appended_types(pipeline_harness)


@pytest.mark.parametrize(
    ("cls", "status"),
    [
        (ConnectorError, "error"),
        (ConnectionRequired, "connection_required"),
        (ApprovalDeclined, "declined"),
        (InteractionTimeout, "interaction_timeout"),
        (PermissionDenied, "denied"),
        (InvalidArguments, "invalid_arguments"),
    ],
)
def test_errors_carry_status_and_message(cls, status):
    err = cls("frase curta")
    assert isinstance(err, ConnectorError)
    assert (err.status, err.message, str(err)) == (status, "frase curta", "frase curta")
    assert ConnectorError("x", status="provider_error").status == "provider_error"


def test_connection_required_carries_connect_url():
    assert ConnectionRequired("conecte").connect_url is None
    assert ConnectionRequired("conecte", connect_url="https://app/c").connect_url == "https://app/c"


@pytest.mark.parametrize(
    "order",
    [
        ("dooers.tools.connectors", "dooers.agents.server"),
        ("dooers.agents.server", "dooers.tools.connectors"),
    ],
)
def test_import_order_has_no_cycle(order):
    code = "; ".join(f"import {mod}" for mod in order)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
