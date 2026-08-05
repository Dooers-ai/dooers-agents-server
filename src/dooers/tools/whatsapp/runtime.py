"""Handler-turn context for ``WhatsAppClient`` (persistence + agent_id)."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dooers.agents.server.persistence.base import Persistence

_turn_persistence: ContextVar[Persistence | None] = ContextVar(
    "dooers_whatsapp_turn_persistence",
    default=None,
)
_turn_agent_id: ContextVar[str | None] = ContextVar(
    "dooers_whatsapp_turn_agent_id",
    default=None,
)


class WhatsAppRuntimeError(RuntimeError):
    pass


def bind_handler_context(
    *,
    persistence: Persistence,
    agent_id: str,
) -> tuple[Token, Token]:
    """Bind persistence and agent_id for the current handler/dispatch turn."""
    t1 = _turn_persistence.set(persistence)
    t2 = _turn_agent_id.set((agent_id or "").strip())
    return t1, t2


def reset_handler_context(tokens: tuple[Token, Token]) -> None:
    t1, t2 = tokens
    _turn_persistence.reset(t1)
    _turn_agent_id.reset(t2)


def require_persistence() -> Persistence:
    persistence = _turn_persistence.get()
    if persistence is None:
        raise WhatsAppRuntimeError(
            "WhatsAppClient requires an active handler turn (use inside handler or dispatch)"
        )
    return persistence


def require_agent_id() -> str:
    agent_id = (_turn_agent_id.get() or "").strip()
    if not agent_id:
        raise WhatsAppRuntimeError(
            "WhatsAppClient requires an active handler turn (use inside handler or dispatch)"
        )
    return agent_id
