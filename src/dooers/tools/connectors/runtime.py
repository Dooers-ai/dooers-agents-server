"""Handler-turn context for connector tools (agent, thread, person, channel, presence, emitter).

A ``ContextVar`` rather than a per-Task map (``tools/rag/runtime.py``), so child tasks the
frameworks create see it too. Stdlib-only at import time: the pipeline imports this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dooers.agents.server.handlers.send import AgentEvent
    from dooers.agents.server.persistence.base import Persistence


@dataclass(frozen=True)
class TurnContext:
    agent_id: str
    organization_id: str
    workspace_id: str
    thread_id: str | None
    user_id: str | None
    channel: str
    presence_token: str | None = field(repr=False)
    persistence: Persistence
    #: Persists and broadcasts an event outside the handler's ``yield`` (same path as yielded events).
    #: After the turn ends it drops the event with a warning instead of raising.
    emit: Callable[[AgentEvent], Awaitable[None]] | None = field(repr=False)


_current_turn: ContextVar[TurnContext | None] = ContextVar("dooers_connectors_turn", default=None)


def bind_turn(ctx: TurnContext) -> Token:
    return _current_turn.set(ctx)


def reset_turn(token: Token) -> None:
    _current_turn.reset(token)


def current_turn() -> TurnContext | None:
    return _current_turn.get()
