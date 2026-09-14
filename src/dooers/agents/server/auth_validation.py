from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from dooers.agents.server.protocol.models import ConnectionContext, User
from dooers.agents.server.settings import AGENT_CORE_BASE_URL

logger = logging.getLogger("agents")

CORE_ISSUER = "dooers-service-core"
DASHBOARD_VALIDATION_PATH = "/api/v2/identity/validate-agent-session"
PUBLIC_CHAT_VALIDATION_PATH = "/api/v2/public-chats/validate-session"


def _normalize_core_base_url(value: str) -> str | None:
    """Return the core base URL without a trailing slash, or None when unusable."""
    base = (value or "").strip().rstrip("/")
    if not base:
        return None
    try:
        parsed = urlparse(base)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return base


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload WITHOUT verifying it.

    The result is untrusted. It is only used to pick which fixed core endpoint
    verifies the token; it must never decide where the token is sent.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class AuthValidationResult:
    valid: bool
    user: User | None = None
    rate_limits: dict[str, Any] = field(default_factory=dict)
    thread_ttl_hours: int | None = None
    organization_id: str | None = None
    workspace_id: str | None = None
    reason: str | None = None
    # New fields from ConnectionContext
    connection_type: str = ""
    agent_id: str = ""
    agent_owner_user_id: str | None = None
    can_configure_settings: bool = False
    organization_plan: str = "free"


class AuthValidationClient:
    def __init__(self, url: str, timeout: float = 5.0, core_base_url: str = AGENT_CORE_BASE_URL):
        """
        Args:
            url: operator-configured validation URL for opaque (non-core) tokens.
            timeout: HTTP timeout in seconds.
            core_base_url: base URL of dooers-service-core. Core-issued JWTs are
                always verified here, whatever ``validation_url`` the token claims.
        """
        self._url = url
        self._timeout = timeout
        self._core_base_url = _normalize_core_base_url(core_base_url)
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def core_base_url(self) -> str | None:
        return self._core_base_url

    async def close(self) -> None:
        await self._client.aclose()

    async def validate(
        self,
        *,
        auth_token: str | None,
        agent_id: str,
        guest_user_id: str,
        organization_id: str = "",
        workspace_id: str = "",
        user_id: str | None = None,
    ) -> AuthValidationResult:
        # Core-issued JWTs are verified by the configured core only. The token's
        # own validation_url claim is unsigned from the SDK's point of view, so it
        # must never choose the destination.
        if auth_token:
            payload = _decode_jwt_payload(auth_token)
            if payload is not None and payload.get("iss") == CORE_ISSUER:
                if self._core_base_url is None:
                    logger.error("[auth-validation] core base URL is not configured; rejecting core token")
                    return AuthValidationResult(valid=False, reason="core_base_url_not_configured")
                path = PUBLIC_CHAT_VALIDATION_PATH if "session_token" in payload else DASHBOARD_VALIDATION_PATH
                validation_url = f"{self._core_base_url}{path}"
                claimed = payload.get("validation_url")
                if isinstance(claimed, str) and claimed != validation_url:
                    logger.warning("[auth-validation] token validation_url differs from configured core; using configured core")
                return await self._validate_jwt(auth_token=auth_token, validation_url=validation_url)

        # Fallback: opaque session tokens use the configured auth_validation_url.
        return await self._validate_legacy(
            auth_token=auth_token,
            agent_id=agent_id,
            guest_user_id=guest_user_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )

    async def _validate_jwt(self, *, auth_token: str, validation_url: str) -> AuthValidationResult:
        """Validate a core-issued JWT by POSTing it to a fixed endpoint on the configured core."""
        try:
            response = await self._client.post(validation_url, json={"token": auth_token})
        except httpx.HTTPError as e:
            logger.warning("[auth-validation] JWT transport error: %s", e)
            return AuthValidationResult(valid=False, reason="transport_error")

        if response.status_code >= 500:
            logger.warning("[auth-validation] JWT upstream %d", response.status_code)
            return AuthValidationResult(valid=False, reason=f"upstream_{response.status_code}")

        if response.status_code != 200:
            return AuthValidationResult(valid=False, reason=f"status_{response.status_code}")

        try:
            body = response.json()
        except ValueError:
            return AuthValidationResult(valid=False, reason="bad_json")

        if not body.get("valid"):
            return AuthValidationResult(valid=False, reason=body.get("reason"))

        # Parse the nested ConnectionContext response.
        try:
            ctx = ConnectionContext(**body)
        except Exception:
            logger.warning("[auth-validation] failed to parse ConnectionContext from JWT response")
            return AuthValidationResult(valid=False, reason="bad_context")

        user = User(
            user_id=ctx.user.id,
            user_name=ctx.user.name or None,
            user_email=ctx.user.email or None,
            identity_ids=ctx.user.identity_ids,
            system_role=ctx.user.system_role or "user",
            organization_role=ctx.organization.role or "member",
            workspace_role=ctx.workspace.role or "member",
            connection_type=ctx.connection_type or "dashboard",
        )

        rate_limits: dict[str, Any] = {}
        if ctx.policies.rate_limit_msgs_per_min is not None:
            rate_limits["messagesPerMinute"] = ctx.policies.rate_limit_msgs_per_min

        return AuthValidationResult(
            valid=True,
            user=user,
            rate_limits=rate_limits,
            thread_ttl_hours=ctx.policies.thread_ttl_hours,
            organization_id=ctx.organization.id,
            workspace_id=ctx.workspace.id,
            connection_type=ctx.connection_type,
            agent_id=ctx.agent.id,
            agent_owner_user_id=ctx.agent.owner_user_id,
            can_configure_settings=ctx.agent.can_configure_settings,
            organization_plan=ctx.organization.plan,
        )

    async def _validate_legacy(
        self,
        *,
        auth_token: str | None,
        agent_id: str,
        guest_user_id: str,
        organization_id: str = "",
        workspace_id: str = "",
        user_id: str | None = None,
    ) -> AuthValidationResult:
        """Validate an opaque session token against the configured auth_validation_url."""
        payload = {
            "authToken": auth_token,
            "agentId": agent_id,
            "guestUserId": guest_user_id,
            "organizationId": organization_id,
            "workspaceId": workspace_id,
            # When set, the backend treats this as a dashboard-authenticated
            # connect and resolves the user's workspace role server-side
            # instead of trusting client-supplied role claims.
            "userId": user_id,
        }
        try:
            response = await self._client.post(self._url, json=payload)
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

        # Check whether the response uses the new nested ConnectionContext shape
        # (has nested "organization" object) or the legacy flat shape.
        if isinstance(body.get("organization"), dict):
            try:
                ctx = ConnectionContext(**body)
            except Exception:
                logger.warning("[auth-validation] failed to parse ConnectionContext from legacy response")
                return AuthValidationResult(valid=False, reason="bad_context")

            user = User(
                user_id=ctx.user.id,
                user_name=ctx.user.name or None,
                user_email=ctx.user.email or None,
                identity_ids=ctx.user.identity_ids,
                system_role=ctx.user.system_role or "user",
                organization_role=ctx.organization.role or "member",
                workspace_role=ctx.workspace.role or "member",
            )

            rate_limits: dict[str, Any] = {}
            if ctx.policies.rate_limit_msgs_per_min is not None:
                rate_limits["messagesPerMinute"] = ctx.policies.rate_limit_msgs_per_min

            return AuthValidationResult(
                valid=True,
                user=user,
                rate_limits=rate_limits,
                thread_ttl_hours=ctx.policies.thread_ttl_hours,
                organization_id=ctx.organization.id,
                workspace_id=ctx.workspace.id,
                connection_type=ctx.connection_type,
                agent_id=ctx.agent.id,
                agent_owner_user_id=ctx.agent.owner_user_id,
                can_configure_settings=ctx.agent.can_configure_settings,
                organization_plan=ctx.organization.plan,
            )

        # Legacy flat response shape (backwards compatibility).
        user_data = body.get("user") or {}
        user = User(
            user_id=user_data.get("user_id", ""),
            user_name=user_data.get("user_name"),
            user_email=user_data.get("user_email"),
            identity_ids=user_data.get("identity_ids", []),
            system_role=user_data.get("system_role") or "user",
            organization_role=user_data.get("organization_role") or "member",
            workspace_role=user_data.get("workspace_role") or "member",
        )
        return AuthValidationResult(
            valid=True,
            user=user,
            rate_limits=body.get("rateLimits") or {},
            thread_ttl_hours=body.get("threadTtlHours"),
            organization_id=body.get("organizationId"),
            workspace_id=body.get("workspaceId"),
        )
