"""Legacy ``AgentIncoming`` keeps production's dataclass shape (origin/main, v0.31.0).

On production, ``AgentIncoming.__post_init__`` binds the RAG context and drops the token,
so ``fields``/``asdict``/``deepcopy``/``pickle``/``repr``/``==`` see only the six fields.
The Maestro path keeps its token in ``maestro_handler_context``; it must never leak into
the legacy object (final review of plan 1, item I1).

The expected values below were produced by running the same code against
``git archive origin/main src`` (v0.31.0), with this repository's virtualenv.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import inspect
import pickle
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from dooers.agents.server.handlers.context import AgentContext
from dooers.agents.server.handlers.incoming import AgentIncoming
from dooers.agents.server.handlers.pipeline import HandlerContext, HandlerPipeline
from dooers.agents.server.protocol.models import TextPart, User
from dooers.tools.rag.runtime import current_execution_context

# origin/main src/dooers/agents/server/handlers/incoming.py: dataclass fields, __init__, __match_args__.
PRODUCTION_INCOMING_FIELDS = ("message", "content", "context", "form_data", "form_cancelled", "form_event_id")
# origin/main src/dooers/agents/server/handlers/context.py: dataclass fields of AgentContext.
PRODUCTION_CONTEXT_FIELDS = (
    "thread_id",
    "agent_id",
    "event_id",
    "organization_id",
    "workspace_id",
    "channel",
    "channel_meta",
    "user",
    "thread_title",
    "thread_created_at",
    "queue",
    "queues",
    "chat_context",
    "trigger",
    "participants",
    "_active_run",
)
# ``_active_run`` is declared ``repr=False`` on production.
PRODUCTION_REPR_EXCLUDED = ("_active_run",)

USER = User(user_id="user-1", user_name="Ana", user_email="ana@example.com", identity_ids=["idp-ana"])
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# repr() of the object built by ``_fixed_incoming`` on origin/main.
PRODUCTION_REPR = (
    "AgentIncoming(message='Olá', content=[TextPart(type='text', text='Olá')], context=AgentContext("
    "thread_id='thread-1', agent_id='agent-1', event_id='event-1', organization_id='org-1', workspace_id='ws-1', "
    "channel='dooers-platform', channel_meta=None, user=User(user_id='user-1', user_name='Ana', "
    "user_email='ana@example.com', user_mobile_number=None, user_whatsapp_number=None, identity_ids=['idp-ana'], "
    "system_role='user', organization_role='member', workspace_role='member', "
    "organization_member_role=None, permissions=[], connection_type='dashboard'), "
    "thread_title='Pedido', thread_created_at=datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc), "
    "queue=None, queues=[], chat_context=None, trigger=None, participants=[]), "
    "form_data={'nome': 'Ana'}, form_cancelled=False, form_event_id=None)"
)
# dataclasses.asdict() of the object built by ``_fixed_incoming`` on origin/main.
PRODUCTION_ASDICT = {
    "message": "Olá",
    "content": [TextPart(text="Olá")],
    "context": {
        "thread_id": "thread-1",
        "agent_id": "agent-1",
        "event_id": "event-1",
        "organization_id": "org-1",
        "workspace_id": "ws-1",
        "channel": "dooers-platform",
        "channel_meta": None,
        "user": USER,
        "thread_title": "Pedido",
        "thread_created_at": CREATED_AT,
        "queue": None,
        "queues": [],
        "chat_context": None,
        "trigger": None,
        "participants": [],
        "_active_run": None,
    },
    "form_data": {"nome": "Ana"},
    "form_cancelled": False,
    "form_event_id": None,
}


def _fixed_incoming() -> AgentIncoming:
    """Built like ``HandlerPipeline.execute`` builds it, with fixed values."""
    context = AgentContext(
        thread_id="thread-1",
        agent_id="agent-1",
        event_id="event-1",
        organization_id="org-1",
        workspace_id="ws-1",
        channel="dooers-platform",
        channel_meta=None,
        user=USER,
        thread_title="Pedido",
        thread_created_at=CREATED_AT,
        queue=None,
        queues=[],
        chat_context=None,
    )
    incoming = AgentIncoming(message="Olá", content=[TextPart(text="Olá")], context=context)
    incoming.form_data = {"nome": "Ana"}
    return incoming


def _production_asdict(on: AgentIncoming) -> dict:
    """``asdict`` as origin/main computes it: production field names, values read from ``on``."""
    out = {name: copy.deepcopy(getattr(on, name)) for name in PRODUCTION_INCOMING_FIELDS}
    out["context"] = {name: copy.deepcopy(getattr(on.context, name)) for name in PRODUCTION_CONTEXT_FIELDS}
    return out


def _production_repr(on: AgentIncoming) -> str:
    shown = [name for name in PRODUCTION_CONTEXT_FIELDS if name not in PRODUCTION_REPR_EXCLUDED]
    context = ", ".join(f"{name}={getattr(on.context, name)!r}" for name in shown)
    fields = [f"{name}={getattr(on, name)!r}" for name in PRODUCTION_INCOMING_FIELDS if name != "context"]
    fields.insert(2, f"context=AgentContext({context})")
    return f"AgentIncoming({', '.join(fields)})"


def _shape(on: AgentIncoming) -> dict:
    """Every dataclass operation a creator may apply to ``on``, run where it is called."""
    copied = copy.deepcopy(on)
    unpickled = pickle.loads(pickle.dumps(on))
    return {
        "asdict": dataclasses.asdict(on),
        "repr": repr(on),
        "deepcopy_equal": copied == on,
        "pickle_equal": unpickled == on,
        "instance_attributes": sorted(vars(on)),
        "context_attributes": sorted(vars(on.context)),
        "copy_attributes": sorted(vars(copied)),
        "rag_agent_id": current_execution_context().agent_id,
    }


def test_class_shape_matches_production():
    assert tuple(f.name for f in dataclasses.fields(AgentIncoming)) == PRODUCTION_INCOMING_FIELDS
    assert AgentIncoming.__match_args__ == PRODUCTION_INCOMING_FIELDS
    assert tuple(inspect.signature(AgentIncoming).parameters) == PRODUCTION_INCOMING_FIELDS
    assert tuple(f.name for f in dataclasses.fields(AgentContext)) == PRODUCTION_CONTEXT_FIELDS
    assert AgentContext.__match_args__ == PRODUCTION_CONTEXT_FIELDS
    assert tuple(inspect.signature(AgentContext).parameters) == PRODUCTION_CONTEXT_FIELDS


async def test_fixed_incoming_inside_a_task_equals_production_values():
    async def turn():
        return _shape(_fixed_incoming())

    shape = await asyncio.create_task(turn())
    assert shape["asdict"] == PRODUCTION_ASDICT
    assert list(shape["asdict"]) == list(PRODUCTION_ASDICT)
    assert list(shape["asdict"]["context"]) == list(PRODUCTION_ASDICT["context"])
    assert shape["repr"] == PRODUCTION_REPR
    assert shape["deepcopy_equal"] and shape["pickle_equal"]
    assert shape["instance_attributes"] == sorted(PRODUCTION_INCOMING_FIELDS)
    assert shape["context_attributes"] == sorted(PRODUCTION_CONTEXT_FIELDS)
    assert shape["copy_attributes"] == sorted(PRODUCTION_INCOMING_FIELDS)
    # The binding itself is production behavior and stays.
    assert shape["rag_agent_id"] == "agent-1"


async def test_equality_matches_production():
    async def turn():
        first, second = _fixed_incoming(), _fixed_incoming()
        equal = first == second
        second.form_cancelled = True
        return equal, first == second

    assert await asyncio.create_task(turn()) == (True, False)


async def test_incoming_built_by_the_legacy_pipeline_has_the_production_shape():
    """The handler gets the object production builds and applies each operation to it."""
    shapes: list[dict] = []
    seen: list[AgentIncoming] = []

    async def handler(on, send, memory, analytics, settings):
        seen.append(on)
        shapes.append(_shape(on))
        yield send.run_start()
        yield send.run_end()

    persistence = AsyncMock()
    persistence.get_thread.return_value = None
    persistence.get_settings.return_value = {}
    pipeline = HandlerPipeline(persistence=persistence)
    context = HandlerContext(
        handler=handler,
        agent_id="agent-legacy",
        organization_id="org-1",
        workspace_id="ws-1",
        message="Olá",
        user=USER,
    )
    result = await pipeline.setup(context)
    async for _ in pipeline.execute(context, result):
        pass

    (on,) = seen
    (shape,) = shapes
    assert shape["asdict"] == _production_asdict(on)
    assert list(shape["asdict"]) == list(PRODUCTION_INCOMING_FIELDS)
    assert list(shape["asdict"]["context"]) == list(PRODUCTION_CONTEXT_FIELDS)
    assert shape["repr"] == _production_repr(on)
    assert shape["deepcopy_equal"] and shape["pickle_equal"]
    assert shape["instance_attributes"] == sorted(PRODUCTION_INCOMING_FIELDS)
    assert shape["context_attributes"] == sorted(PRODUCTION_CONTEXT_FIELDS)
    assert shape["rag_agent_id"] == "agent-legacy"


async def test_duck_typed_context_without_maestro_still_binds_like_production():
    """Production read only the context fields: a stub without ``maestro`` must keep working."""
    from types import SimpleNamespace

    context = SimpleNamespace(
        agent_id="agent-1",
        organization_id="org-1",
        workspace_id="ws-1",
        user=SimpleNamespace(user_id="user-1"),
        channel="dooers-platform",
    )
    AgentIncoming("oi", [TextPart(text="oi")], context)
    bound = current_execution_context()
    assert bound is not None and bound.agent_id == "agent-1"
