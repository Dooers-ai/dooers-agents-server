"""Managed Dooers sandbox — run untrusted code next to (never inside) the agent.

    from dooers.tools import sandbox

    box = await sandbox.open(profile="documents")      # one sandbox per thread, reused
    await box.write_file("data/input.csv", csv_bytes)
    result = await box.exec(["python3", "analyze.py"], timeout=120)
    chart = await box.read_file("out/chart.png")

Auth: Core-scoped service token (audience ``sandbox-service``, scope ``sandbox:exec``) minted
with the agent runtime API key — the same contract as managed RAG and OTEL. Organization,
agent and thread come from the runtime context of the turn (or an explicit
:class:`SandboxContext`), never from model-controlled arguments.

Configuration: ``DOOERS_SANDBOX_SERVICE_URL`` (injected by dooers-push for hosted agents).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from dooers.agents.server.observability.service_token import (
    RUNTIME_API_KEY_SECRET_NAME,
    ServiceTokenClient,
    resolve_runtime_api_key,
)
from dooers.agents.server.settings import AGENT_CORE_BASE_URL
from dooers.tools.rag.runtime import current_execution_context
from dooers.tools.whatsapp.runtime import require_agent_id, require_persistence

logger = logging.getLogger(__name__)

__all__ = [
    "ExecResult",
    "Sandbox",
    "SandboxContext",
    "SandboxError",
    "SandboxFileMissing",
    "is_configured",
    "open",
]

_AUDIENCE = "sandbox-service"
_SCOPES = ("sandbox:exec",)
Profile = Literal["shell", "coding", "documents"]
_token_client: ServiceTokenClient | None = None
#: (agent, thread, profile) → sandbox id, so a thread keeps its files between tool calls.
_sessions: dict[tuple[str, str, str], str] = {}


class SandboxError(RuntimeError):
    """The sandbox service refused or failed the request."""


class SandboxFileMissing(SandboxError):
    pass


@dataclass(frozen=True)
class SandboxContext:
    agent_id: str
    organization_id: str = ""
    workspace_id: str = ""
    thread_id: str = ""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def is_configured() -> bool:
    return bool(_base_url(required=False))


def _base_url(*, required: bool = True) -> str:
    value = (os.environ.get("DOOERS_SANDBOX_SERVICE_URL") or "").strip().rstrip("/")
    if not value and required:
        raise SandboxError("Managed Dooers sandbox is not configured for this runtime (DOOERS_SANDBOX_SERVICE_URL)")
    return value


def _core_base_url() -> str:
    return (
        (os.environ.get("AGENT_CORE_BASE_URL") or "").strip()
        or (os.environ.get("DOOERS_CORE_BASE_URL") or "").strip()
        or AGENT_CORE_BASE_URL
    ).rstrip("/")


def _service_token_client() -> ServiceTokenClient:
    global _token_client
    if _token_client is None:
        _token_client = ServiceTokenClient(core_base_url=_core_base_url())
    return _token_client


def _resolve_context(explicit: SandboxContext | None) -> SandboxContext:
    if explicit is not None and explicit.agent_id:
        return explicit
    runtime = current_execution_context()
    agent_id = runtime.agent_id if runtime and runtime.agent_id else require_agent_id()
    return SandboxContext(
        agent_id=agent_id,
        organization_id=(runtime.organization_id if runtime else "") or "",
        workspace_id=(runtime.workspace_id if runtime else "") or "",
        thread_id=(runtime.thread_id if runtime else "") or "",
    )


async def _bearer(context: SandboxContext) -> str:
    try:
        persistence = require_persistence()
    except Exception as exc:  # noqa: BLE001
        raise SandboxError("The sandbox requires an active handler turn to mint a Core service token") from exc
    runtime_api_key = await resolve_runtime_api_key(persistence, context.agent_id)
    if not runtime_api_key:
        raise SandboxError(
            f"Missing runtime API key — set AGENT_SEED_SECRET or wait for {RUNTIME_API_KEY_SECRET_NAME} via settings.seed"
        )
    token = await _service_token_client().get_token(
        agent_id=context.agent_id,
        workspace_id=context.workspace_id,
        runtime_api_key=runtime_api_key,
        audience=_AUDIENCE,
        scopes=_SCOPES,
    )
    if not token:
        raise SandboxError("Could not mint a Core service token for the sandbox (is the sandbox feature enabled?)")
    return token


def _session_id(context: SandboxContext, profile: str) -> str:
    raw = f"{context.agent_id}:{context.thread_id or 'no-thread'}:{profile}"
    return "thr_" + hashlib.sha256(raw.encode()).hexdigest()[:40]


class Sandbox:
    """One remote sandbox. Paths are relative to the sandbox workspace (``/workspace``)."""

    def __init__(self, sandbox_id: str, context: SandboxContext, *, timeout: float = 330.0) -> None:
        self.id = sandbox_id
        self.context = context
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await _bearer(self.context)}"}
        headers.update(kwargs.pop("headers", {}) or {})
        timeout = kwargs.pop("timeout", self._timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.request(method, f"{_base_url()}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SandboxError(f"sandbox request failed: {exc}") from exc

    async def exec(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> ExecResult:
        response = await self._request(
            "POST",
            f"/v1/sandboxes/{self.id}/exec",
            json={"command": command, "cwd": cwd, "env": env or {}, "timeout": int(timeout)},
            timeout=float(timeout) + 60.0,
        )
        if response.status_code == 413:
            raise SandboxError("command output exceeded the sandbox limit — write large output to a file instead")
        if not response.is_success:
            raise SandboxError(f"sandbox exec returned {response.status_code}: {response.text[:300]}")
        body = response.json()
        return ExecResult(
            exit_code=int(body.get("exit_code", 1)),
            stdout=str(body.get("stdout") or ""),
            stderr=str(body.get("stderr") or ""),
        )

    async def write_file(self, path: str, data: bytes) -> None:
        response = await self._request(
            "PUT",
            f"/v1/sandboxes/{self.id}/files/binary",
            params={"path": path},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if not response.is_success:
            raise SandboxError(f"sandbox write returned {response.status_code}: {response.text[:300]}")

    async def read_file(self, path: str) -> bytes:
        response = await self._request("GET", f"/v1/sandboxes/{self.id}/files", params={"path": path})
        if response.status_code == 404:
            raise SandboxFileMissing(f"{path} not found in sandbox")
        if not response.is_success:
            raise SandboxError(f"sandbox read returned {response.status_code}: {response.text[:300]}")
        return response.content

    async def close(self) -> None:
        response = await self._request("DELETE", f"/v1/sandboxes/{self.id}")
        if response.status_code not in (204, 404):
            raise SandboxError(f"sandbox delete returned {response.status_code}")
        for key, value in list(_sessions.items()):
            if value == self.id:
                _sessions.pop(key, None)


async def open(  # noqa: A001 — mirrors ``sandbox.open`` in docs
    *,
    profile: Profile = "documents",
    context: SandboxContext | None = None,
) -> Sandbox:
    """Get (or create) the sandbox of the current thread. Creation is idempotent server-side."""
    ctx = _resolve_context(context)
    key = (ctx.agent_id, ctx.thread_id, profile)
    cached = _sessions.get(key)
    if cached:
        return Sandbox(cached, ctx)
    box = Sandbox("", ctx)
    response = await box._request(
        "POST",
        "/v1/sandboxes",
        json={
            "session_id": _session_id(ctx, profile),
            "organization_id": ctx.organization_id or "legacy",
            "agent_id": ctx.agent_id,
            "thread_id": ctx.thread_id or None,
            "profile": profile,
        },
        timeout=300.0,
    )
    if not response.is_success:
        raise SandboxError(f"sandbox create returned {response.status_code}: {response.text[:300]}")
    sandbox_id = str(response.json().get("id") or "")
    if not sandbox_id:
        raise SandboxError("sandbox create returned no id")
    _sessions[key] = sandbox_id
    return Sandbox(sandbox_id, ctx)
