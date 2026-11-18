"""Scheduled tasks on the Dooers Tasks service — reminders and recurring work the agent runs later.

    from dooers.tools import tasks

    task = await tasks.schedule(
        title="Resumo de notícias",
        instruction="Mande um resumo das notícias do setor de energia",
        due_at="2026-11-24T12:00:00Z",
        recurrence={"freq": "weekly", "byWeekday": [0], "atTime": "09:00", "timezone": "America/Sao_Paulo"},
    )
    await tasks.list()
    await tasks.cancel(task["id"])

When the time comes, the Tasks service asks the core to start the run; the core re-checks that the
person who scheduled it may still use the agent and sends the run to the agent's
``/internal/tasks/run`` (:meth:`AgentServer.run_task`). The run answers in the conversation the task
was scheduled from, and the task card in the Tasks panel shows its result.

Auth: a ``tasks-service-core`` token that acts for the person of the turn. The person comes from
the turn's handshake token (``presence_token`` — the chat's, or the run's), which the core signed:
the agent's code cannot schedule something that will later run as somebody else. Must be called
inside a handler turn. Configuration: ``DOOERS_TASKS_SERVICE_URL``.
"""

from __future__ import annotations

import builtins
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from dooers.agents.server.observability.service_token import core_base_url, resolve_runtime_api_key
from dooers.tools.connectors.runtime import current_turn

logger = logging.getLogger(__name__)

__all__ = [
    "Identity",
    "TasksError",
    "TasksUnavailable",
    "available",
    "cancel",
    "get",
    "list",
    "report_run",
    "reschedule",
    "schedule",
]

_AUDIENCE = "tasks-service-core"
_SCOPES = ["tasks:read", "tasks:write"]
_TOOL_PATH = "/api/v1/external/tools/scheduled-tasks"
_REFRESH_MARGIN_SECONDS = 60.0
# (agent, sha256(presence), personal) -> (token, usable_until)
_tokens: dict[tuple[str, str, bool], tuple[str, float]] = {}


class TasksError(RuntimeError):
    """The Tasks service refused or failed. ``status`` is the HTTP status (0 for transport errors)."""

    def __init__(self, message: str, *, status: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class TasksUnavailable(TasksError):
    """No Tasks service configured, no turn, or no person to act for (guest, channel without login)."""


def _base_url() -> str:
    return (os.environ.get("DOOERS_TASKS_SERVICE_URL") or "").strip().rstrip("/")


def available() -> bool:
    """Whether scheduling can work in this turn (service configured and a person proven by the core)."""
    turn = current_turn()
    return bool(_base_url() and turn is not None and turn.presence_token and turn.user_id)


@dataclass(frozen=True)
class Identity:
    """Who calls the Tasks service: the agent, the person's core-signed token, the agent's persistence.

    Inside a handler turn it comes from the turn; :meth:`AgentServer.run_task` passes it explicitly
    to check and close a run outside the turn.
    """

    agent_id: str
    presence_token: str | None
    user_id: str | None
    persistence: Any


def _identity(identity: Identity | None) -> Identity:
    if identity is not None:
        return identity
    turn = current_turn()
    if turn is None:
        raise TasksUnavailable("tasks need an active handler turn")
    return Identity(turn.agent_id, turn.presence_token, turn.user_id, turn.persistence)


async def _token(*, personal: bool, identity: Identity | None = None) -> str:
    turn = _identity(identity)
    if not turn.presence_token or not turn.user_id:
        raise TasksUnavailable("scheduling needs a signed-in person in the conversation")
    key = (turn.agent_id, hashlib.sha256(turn.presence_token.encode()).hexdigest(), personal)
    cached = _tokens.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    runtime_api_key = await resolve_runtime_api_key(turn.persistence, turn.agent_id)
    if not runtime_api_key:
        raise TasksUnavailable("missing agent runtime API key")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{core_base_url()}/api/v2/identity/service-token/agent",
                json={
                    "audience": _AUDIENCE,
                    "workerId": turn.agent_id,
                    "runtimeApiKey": runtime_api_key,
                    "scopes": _SCOPES,
                    "presenceToken": turn.presence_token,
                    "personal": personal,
                },
            )
    except httpx.HTTPError as exc:
        raise TasksError(f"core unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        code, message = _error_of(response)
        raise TasksError(message or f"core refused the tasks token ({response.status_code})", status=response.status_code, code=code)
    data = (response.json() or {}).get("data") or {}
    token = data.get("accessToken")
    if not isinstance(token, str) or not token:
        raise TasksError("core answered without a tasks token")
    expires_in = float(data.get("expiresIn") or 300)
    now = time.monotonic()
    for stale in [k for k, (_, until) in _tokens.items() if until <= now]:
        del _tokens[stale]
    _tokens[key] = (token, now + max(30.0, expires_in - _REFRESH_MARGIN_SECONDS))
    return token


def _error_of(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:200] or None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return error.get("code"), error.get("message")
    return None, None


async def _call(payload: dict[str, Any], *, personal: bool = True, identity: Identity | None = None) -> dict[str, Any]:
    base = _base_url()
    if not base:
        raise TasksUnavailable("DOOERS_TASKS_SERVICE_URL is not configured")
    token = await _token(personal=personal, identity=identity)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{base}{_TOOL_PATH}", json=payload, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise TasksError(f"tasks service unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        code, message = _error_of(response)
        raise TasksError(message or f"tasks service answered {response.status_code}", status=response.status_code, code=code)
    body = response.json()
    return body if isinstance(body, dict) else {}


def _thread_id(thread_id: str | None) -> str | None:
    if thread_id is not None:
        return thread_id or None
    turn = current_turn()
    return turn.thread_id if turn else None


async def schedule(
    *,
    title: str,
    instruction: str,
    due_at: str,
    recurrence: dict[str, Any] | None = None,
    thread_id: str | None = None,
    priority: str | None = None,
    personal: bool = True,
) -> dict[str, Any]:
    """Schedule work the agent will run at ``due_at`` (ISO 8601), once or per ``recurrence``.

    ``recurrence``: ``freq`` (hourly/daily/weekly/monthly), ``interval``, ``byWeekday`` (0 = Monday),
    ``byMonthday``, ``atTime`` (``HH:MM``), ``timezone`` (IANA, required), ``until``, ``count``.
    ``personal`` (default): the person's own task, outside any workspace board; ``False`` files it on
    the workspace board of the conversation (needs the agent's Tasks access in that workspace).
    The run answers in ``thread_id`` (default: this conversation).
    """
    body: dict[str, Any] = {"title": title, "instruction": instruction, "dueAt": due_at}
    if recurrence:
        body["recurrence"] = recurrence
    tid = _thread_id(thread_id)
    if tid:
        body["threadId"] = tid
    if priority:
        body["priority"] = priority
    result = await _call({"operation": "schedule", "body": body}, personal=personal)
    return result.get("task") or {}


async def list(*, include_closed: bool = False, limit: int = 50, personal: bool = True) -> list[dict[str, Any]]:  # noqa: A001
    """This agent's scheduled tasks for the person of the turn (``scheduled``/``running`` unless ``include_closed``)."""
    result = await _call({"operation": "list", "body": {"includeClosed": include_closed, "limit": limit}}, personal=personal)
    data = result.get("data")
    return data if isinstance(data, builtins.list) else []


async def get(task_id: str, *, personal: bool = True, identity: Identity | None = None) -> dict[str, Any]:
    result = await _call({"operation": "get", "taskId": task_id}, personal=personal, identity=identity)
    return result.get("task") or {}


async def reschedule(
    task_id: str,
    *,
    due_at: str | None = None,
    recurrence: dict[str, Any] | None | bool = False,
    title: str | None = None,
    instruction: str | None = None,
    personal: bool = True,
) -> dict[str, Any]:
    """Change when (or how often) the task runs; ``recurrence=None`` makes it a one-off again."""
    body: dict[str, Any] = {}
    if due_at:
        body["dueAt"] = due_at
    if recurrence is not False:
        body["recurrence"] = recurrence
    if title:
        body["title"] = title
    if instruction:
        body["instruction"] = instruction
    result = await _call({"operation": "reschedule", "taskId": task_id, "body": body}, personal=personal)
    return result.get("task") or {}


async def cancel(task_id: str, *, note: str | None = None, personal: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"operation": "cancel", "taskId": task_id}
    if note:
        payload["note"] = note
    result = await _call(payload, personal=personal)
    return result.get("task") or {}


async def report_run(
    task_id: str,
    *,
    run_id: str,
    succeeded: bool,
    summary: str | None = None,
    error: str | None = None,
    personal: bool = True,
    identity: Identity | None = None,
) -> dict[str, Any]:
    """Close a run of the task (called by :meth:`AgentServer.run_task`; handlers rarely need it)."""
    body: dict[str, Any] = {"runId": run_id, "outcome": "succeeded" if succeeded else "failed"}
    if summary:
        body["summary"] = summary[:4000]
    if error:
        body["error"] = error[:2000]
    result = await _call({"operation": "report_run", "taskId": task_id, "body": body}, personal=personal, identity=identity)
    return result.get("task") or {}
