"""Deterministic handler execution context for managed RAG authorization.

The context is keyed by the current asyncio Task rather than passed through LLM/tool
arguments. AgentIncoming binds it before creator handler code runs; weak task keys
are discarded when request tasks are collected.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from weakref import WeakKeyDictionary


@dataclass(frozen=True)
class RAGExecutionContext:
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user_id: str = ""
    on_behalf: bool = False


@dataclass
class RAGContextToken:
    task: asyncio.Task
    previous: RAGExecutionContext | None
    bound: RAGExecutionContext | None = None
    used: bool = False


_contexts: WeakKeyDictionary[asyncio.Task, RAGExecutionContext] = WeakKeyDictionary()


def _current_task() -> asyncio.Task | None:
    """Synchronous callers have no execution scope; never inherit another task."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def bind_execution_context(
    *,
    agent_id: str,
    organization_id: str = "",
    workspace_id: str = "",
    user_id: str = "",
    channel: str = "",
) -> RAGContextToken | None:
    task = _current_task()
    if task is None:
        return
    token = RAGContextToken(task, _contexts.get(task))
    user_id = (user_id or "").strip()
    channel = (channel or "").strip()
    authenticated_user = user_id if user_id and not user_id.startswith("guest:") and channel == "dooers-platform" else ""
    _contexts[task] = RAGExecutionContext(
        agent_id=(agent_id or "").strip(),
        organization_id=(organization_id or "").strip(),
        workspace_id=(workspace_id or "").strip(),
        user_id=authenticated_user,
        on_behalf=bool(authenticated_user),
    )

    token.bound = _contexts[task]
    return token


def reset_execution_context(token: RAGContextToken | None) -> None:
    """Restaura o contexto anterior na mesma task, inclusive bindings aninhados."""
    if token is None:
        return
    if _current_task() is not token.task or token.used or _contexts.get(token.task) is not token.bound:
        raise RuntimeError("RAG_CONTEXT_TOKEN_INVALID")
    token.used = True
    if token.previous is None:
        _contexts.pop(token.task, None)
    else:
        _contexts[token.task] = token.previous


def current_execution_context() -> RAGExecutionContext | None:
    task = _current_task()
    return _contexts.get(task) if task is not None else None
