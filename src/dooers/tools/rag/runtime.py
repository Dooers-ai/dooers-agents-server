"""Deterministic handler execution context for managed RAG authorization.

The context is keyed by the current asyncio Task rather than passed through LLM/tool
arguments. AgentIncoming binds it before creator handler code runs; weak task keys
are discarded when request tasks are collected.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from weakref import WeakKeyDictionary


@dataclass(frozen=True)
class RAGExecutionContext:
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user_id: str = ""
    on_behalf: bool = False
    #: Thread of the turn (managed sandbox sessions are per thread). Empty outside a turn.
    thread_id: str = ""


@dataclass
class RAGContextToken:
    task: asyncio.Task
    previous: RAGExecutionContext | None
    bound: RAGExecutionContext | None = None
    used: bool = False
    previous_var: RAGExecutionContext | None = None


_contexts: WeakKeyDictionary[asyncio.Task, RAGExecutionContext] = WeakKeyDictionary()
#: Same context as a ContextVar so child tasks (``asyncio.gather`` of parallel tool calls)
#: inherit it; the task map stays authoritative for the task that bound it.
_context_var: ContextVar[RAGExecutionContext | None] = ContextVar("dooers_tools_execution_context", default=None)


def _current_task() -> asyncio.Task | None:
    """The running task, or None outside an event loop (sync code and tests).

    Synchronous callers have no execution scope; they never inherit another task.
    """
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
    thread_id: str = "",
    inherit_children: bool = True,
) -> RAGContextToken | None:
    """Bind the execution context to the current task and return the token that resets it.

    ``inherit_children=True`` (legacy turns) also exposes the context to child tasks, so parallel
    tool calls keep the scope. Private Maestro scopes pass ``False``: a child task must never see
    (or outlive) the private context, so the ContextVar is cleared for the duration of the scope.
    """
    task = _current_task()
    if task is None:
        return None
    token = RAGContextToken(task, _contexts.get(task), previous_var=_context_var.get())
    user_id = (user_id or "").strip()
    channel = (channel or "").strip()
    authenticated_user = (
        user_id
        if user_id and not user_id.startswith("guest:") and channel == "dooers-platform"
        else ""
    )
    context = RAGExecutionContext(
        agent_id=(agent_id or "").strip(),
        organization_id=(organization_id or "").strip(),
        workspace_id=(workspace_id or "").strip(),
        user_id=authenticated_user,
        on_behalf=bool(authenticated_user),
        thread_id=(thread_id or "").strip(),
    )
    _contexts[task] = context
    _context_var.set(context if inherit_children else None)

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
    _context_var.set(token.previous_var)


def current_execution_context() -> RAGExecutionContext | None:
    task = _current_task()
    if task is None:
        return None
    return _contexts.get(task) or _context_var.get()
