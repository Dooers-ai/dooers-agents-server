"""Trusted per-turn identity for managed RAG.

Context variables propagate into child asyncio tasks, including agent tool tasks.
Only the SDK's authenticated connection handling may attach an assertion.
Do not accept user identity or assertions from LLM/tool arguments.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RAGExecutionContext:
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    user_id: str = ""
    on_behalf: bool = False
    auth_assertion: str | None = None


_current: ContextVar[RAGExecutionContext | None] = ContextVar("dooers_rag_context", default=None)


def bind_execution_context(
    *,
    agent_id: str,
    organization_id: str = "",
    workspace_id: str = "",
    user_id: str = "",
    channel: str = "",
    auth_assertion: str | None = None,
) -> None:
    agent_id = (agent_id or "").strip()
    prior = _current.get()
    # Child-tool rebinds must not erase a previously verified delegation.
    # Never reuse it if the caller changes agent or user identity.
    if prior and prior.agent_id == agent_id and not user_id and not organization_id and not workspace_id:
        user_id = prior.user_id
        organization_id = prior.organization_id
        workspace_id = prior.workspace_id
        channel = channel or ("dooers-platform" if prior.on_behalf else "")
        auth_assertion = auth_assertion or prior.auth_assertion
    user_id = (user_id or "").strip()
    authenticated_user = (
        user_id if user_id and not user_id.startswith("guest:") and channel == "dooers-platform" else ""
    )
    _current.set(RAGExecutionContext(
        agent_id=agent_id,
        organization_id=(organization_id or "").strip(),
        workspace_id=(workspace_id or "").strip(),
        user_id=authenticated_user,
        on_behalf=bool(authenticated_user and auth_assertion),
        auth_assertion=auth_assertion if authenticated_user else None,
    ))


def current_execution_context() -> RAGExecutionContext | None:
    return _current.get()
