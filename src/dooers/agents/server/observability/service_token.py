"""Acquisition/cache/renewal of dooers-service-core service tokens.

One dooers-agents-server process can serve many concurrent agents (``ConnectionRegistry`` is
keyed by ``agent_id``), each with its own ``runtimeApiKey`` — tokens are cached per
``agent_id`` + workspace + audience + scopes, never shared across agents.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# Persisted under service_secrets when core sends ``settings.seed`` (the plaintext runtime API
# key). OTEL / RAG read it back to mint scoped tokens — same credential core verifies via
# ``verifyAgentRuntimeCredential``.
#
# For template-as-agent hosts, Core's createAgent contract is that the one-time plaintext key
# equals ``AGENT_SEED_SECRET`` on the process. When service_secrets has no key yet (no worker
# seed), fall back to that env value instead of requiring settings.seed.
RUNTIME_API_KEY_SECRET_NAME = "dooers_runtime_api_key"

_DEFAULT_AUDIENCE = "otel-service"
_DEFAULT_SCOPES = ("otel:write",)
# Renew proactively before expiry (tokens last 300s) instead of waiting for a 401 — a
# long-lived agent renews many times per hour, treat it as routine, not as an error path.
_REFRESH_MARGIN_SECONDS = 60


class _ServiceSecretsReader(Protocol):
    async def get_service_secrets(self, agent_id: str) -> dict[str, Any]: ...


_process_seed_secret: str = ""


def configure_process_seed_secret(value: str | None) -> None:
    """Register the host ``agent_seed_secret`` (from AgentConfig / .env via pydantic).

    RAG/OTEL resolve credentials from ``os.environ``; template hosts often load
    ``AGENT_SEED_SECRET`` only into Settings. Call this at AgentServer init so the
    env fallback still works without requiring the var in the process environment.
    """
    global _process_seed_secret
    _process_seed_secret = (value or "").strip()


def agent_seed_secret_from_env() -> str:
    return (os.environ.get("AGENT_SEED_SECRET") or "").strip() or _process_seed_secret


def runtime_api_key_from_secrets(secrets: dict[str, Any] | None) -> str | None:
    if not isinstance(secrets, dict):
        return None
    value = secrets.get(RUNTIME_API_KEY_SECRET_NAME)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def resolve_runtime_api_key(persistence: _ServiceSecretsReader, agent_id: str) -> str | None:
    """Return the Core runtime API key for minting service tokens.

    Prefer per-agent ``service_secrets.dooers_runtime_api_key`` (seed / hire path).
    Fall back to process ``AGENT_SEED_SECRET`` for template-as-agent hosts that never
    received settings.seed but already match createAgent's documented env contract.
    """
    aid = (agent_id or "").strip()
    if not aid:
        return None
    try:
        secrets = await persistence.get_service_secrets(aid)
    except Exception:
        logger.warning("Failed to read service_secrets for agent_id=%s", aid, exc_info=True)
        secrets = None
    from_db = runtime_api_key_from_secrets(secrets if isinstance(secrets, dict) else None)
    if from_db:
        return from_db
    return agent_seed_secret_from_env() or None


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # time.monotonic() timestamp


def _cache_key(agent_id: str, workspace_id: str, audience: str, scopes: tuple[str, ...]) -> str:
    return f"{agent_id}:{workspace_id.strip()}:{audience}:{','.join(scopes)}"


class ServiceTokenClient:
    def __init__(self, *, core_base_url: str, http_client: httpx.AsyncClient | None = None):
        self._core_base_url = core_base_url.rstrip("/")
        self.http_client = http_client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = http_client is None
        self._cache: dict[str, _CachedToken] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    async def get_token(
        self,
        *,
        agent_id: str,
        workspace_id: str = "",
        runtime_api_key: str,
        audience: str = _DEFAULT_AUDIENCE,
        scopes: list[str] | tuple[str, ...] | None = None,
    ) -> str | None:
        scope_tuple = tuple(scopes) if scopes is not None else _DEFAULT_SCOPES
        key = _cache_key(agent_id, workspace_id, audience, scope_tuple)
        cached = self._cache.get(key)
        if cached and cached.expires_at - time.monotonic() > _REFRESH_MARGIN_SECONDS:
            return cached.access_token

        fresh = await self._fetch_token(
            agent_id=agent_id,
            workspace_id=workspace_id,
            runtime_api_key=runtime_api_key,
            audience=audience,
            scopes=scope_tuple,
        )
        if fresh is not None:
            self._cache[key] = fresh
            return fresh.access_token

        # Renewal failed (core unreachable, key revoked) — reuse the stale token rather than
        # dropping the export outright. Worst case the receiving service rejects it with 401;
        # that's no worse than not exporting at all, and covers transient core outages.
        if cached is not None:
            logger.warning("service-token: renewal failed for agent_id=%s, reusing stale token", agent_id)
            return cached.access_token

        return None

    async def _fetch_token(
        self,
        *,
        agent_id: str,
        workspace_id: str,
        runtime_api_key: str,
        audience: str,
        scopes: tuple[str, ...],
    ) -> _CachedToken | None:
        url = f"{self._core_base_url}/api/v2/identity/service-token/agent"
        payload: dict[str, object] = {
            "audience": audience,
            "workerId": agent_id,
            "runtimeApiKey": runtime_api_key,
            "scopes": list(scopes),
        }
        if workspace_id.strip():
            payload["workspaceId"] = workspace_id.strip()
        try:
            response = await self.http_client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("service-token: transport error fetching token for agent_id=%s: %s", agent_id, exc)
            return None

        if response.status_code != 200:
            logger.warning(
                "service-token: core returned %d fetching token for agent_id=%s", response.status_code, agent_id
            )
            return None

        try:
            data = response.json()["data"]
            access_token = data["accessToken"]
            expires_in = float(data["expiresIn"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("service-token: malformed response fetching token for agent_id=%s: %s", agent_id, exc)
            return None

        return _CachedToken(access_token=access_token, expires_at=time.monotonic() + expires_in)
