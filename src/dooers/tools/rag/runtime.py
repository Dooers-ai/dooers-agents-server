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


_contexts: WeakKeyDictionary[asyncio.Task, RAGExecutionContext] = WeakKeyDictionary()


def bind_execution_context(
    *,
    agent_id: str,
    organization_id: str = "",
    workspace_id: str = "",
    user_id: str = "",
    channel: str = "",
) -> None:
    task = asyncio.current_task()
    if task is None:
        return
    user_id = (user_id or "").strip()
    channel = (channel or "").strip()
    authenticated_user = (
        user_id
        if user_id and not user_id.startswith("guest:") and channel == "dooers-platform"
        else ""
    )
    _contexts[task] = RAGExecutionContext(
        agent_id=(agent_id or "").strip(),
        organization_id=(organization_id or "").strip(),
        workspace_id=(workspace_id or "").strip(),
        user_id=authenticated_user,
        on_behalf=bool(authenticated_user),
    )


def current_execution_context() -> RAGExecutionContext | None:
    task = asyncio.current_task()
    return _contexts.get(task) if task is not None else None
