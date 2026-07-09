"""Acquisition/cache/renewal of dooers-service-core service tokens (audience ``otel-service``).

One dooers-agents-server process can serve many concurrent agents (``ConnectionRegistry`` is
keyed by ``agent_id``), each with its own ``runtimeApiKey`` — tokens are cached per agent_id,
never shared across agents.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_AUDIENCE = "otel-service"
_SCOPES = ["otel:write"]
# Renew proactively before expiry (tokens last 300s) instead of waiting for a 401 — a
# long-lived worker renews many times per hour, treat it as routine, not as an error path.
_REFRESH_MARGIN_SECONDS = 60


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # time.monotonic() timestamp


class ServiceTokenClient:
    def __init__(self, *, core_base_url: str, http_client: httpx.AsyncClient | None = None):
        self._core_base_url = core_base_url.rstrip("/")
        self.http_client = http_client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = http_client is None
        self._cache: dict[str, _CachedToken] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    async def get_token(self, *, agent_id: str, workspace_id: str, runtime_api_key: str) -> str | None:
        cached = self._cache.get(agent_id)
        if cached and cached.expires_at - time.monotonic() > _REFRESH_MARGIN_SECONDS:
            return cached.access_token

        fresh = await self._fetch_token(agent_id=agent_id, workspace_id=workspace_id, runtime_api_key=runtime_api_key)
        if fresh is not None:
            self._cache[agent_id] = fresh
            return fresh.access_token

        # Renewal failed (core unreachable, key revoked) — reuse the stale token rather than
        # dropping the export outright. Worst case the receiving service rejects it with 401;
        # that's no worse than not exporting at all, and covers transient core outages.
        if cached is not None:
            logger.warning("service-token: renewal failed for agent_id=%s, reusing stale token", agent_id)
            return cached.access_token

        return None

    async def _fetch_token(self, *, agent_id: str, workspace_id: str, runtime_api_key: str) -> _CachedToken | None:
        url = f"{self._core_base_url}/api/v2/identity/service-token/worker"
        try:
            response = await self.http_client.post(
                url,
                json={
                    "audience": _AUDIENCE,
                    "workerId": agent_id,
                    "workspaceId": workspace_id,
                    "runtimeApiKey": runtime_api_key,
                    "scopes": _SCOPES,
                },
            )
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