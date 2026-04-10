from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from dooers.protocol.models import User

logger = logging.getLogger(__name__)


@dataclass
class AuthValidationResult:
    valid: bool
    user: User | None = None
    rate_limits: dict[str, Any] = field(default_factory=dict)
    thread_ttl_hours: int | None = None
    reason: str | None = None


class AuthValidationClient:
    def __init__(self, url: str, timeout: float = 5.0):
        self._url = url
        self._timeout = timeout

    async def validate(
        self,
        *,
        auth_token: str | None,
        agent_id: str,
        guest_user_id: str,
        organization_id: str = "",
        workspace_id: str = "",
    ) -> AuthValidationResult:
        payload = {
            "authToken": auth_token,
            "agentId": agent_id,
            "guestUserId": guest_user_id,
            "organizationId": organization_id,
            "workspaceId": workspace_id,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=payload)
        except httpx.HTTPError as e:
            logger.warning("[auth-validation] transport error: %s", e)
            return AuthValidationResult(valid=False, reason="transport_error")

        if response.status_code >= 500:
            logger.warning("[auth-validation] upstream %d", response.status_code)
            return AuthValidationResult(valid=False, reason=f"upstream_{response.status_code}")

        if response.status_code != 200:
            return AuthValidationResult(valid=False, reason=f"status_{response.status_code}")

        try:
            body = response.json()
        except ValueError:
            return AuthValidationResult(valid=False, reason="bad_json")

        if not body.get("valid"):
            return AuthValidationResult(valid=False, reason=body.get("reason"))

        user_data = body.get("user") or {}
        user = User(
            user_id=user_data.get("user_id", ""),
            user_name=user_data.get("user_name"),
            user_email=user_data.get("user_email"),
            identity_ids=user_data.get("identity_ids", []),
        )
        return AuthValidationResult(
            valid=True,
            user=user,
            rate_limits=body.get("rateLimits") or {},
            thread_ttl_hours=body.get("threadTtlHours"),
        )
