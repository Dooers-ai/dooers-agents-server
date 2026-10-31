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


_contexts: WeakKeyDictionary[asyncio.Task, RAGExecutionContext] = WeakKeyDictionary()
#: Same context as a ContextVar so child tasks (``asyncio.gather`` of parallel tool calls)
#: inherit it; the task map stays authoritative for the task that bound it.
_context_var: ContextVar[RAGExecutionContext | None] = ContextVar("dooers_tools_execution_context", default=None)


def bind_execution_context(
    *,
    agent_id: str,
    organization_id: str = "",
    workspace_id: str = "",
    user_id: str = "",
    channel: str = "",
    thread_id: str = "",
) -> None:
    task = _current_task()
    if task is None:
        return
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
    _context_var.set(context)


def _current_task() -> asyncio.Task | None:
    # ``asyncio.current_task`` raises when no loop is running (e.g. an ``AgentIncoming``
    # built in synchronous code or tests). No task means no context to bind.
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def current_execution_context() -> RAGExecutionContext | None:
    task = _current_task()
    if task is None:
        return None
    return _contexts.get(task) or _context_var.get()
