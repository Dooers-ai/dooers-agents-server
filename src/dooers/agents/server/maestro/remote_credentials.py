"""Core credential exchange over the explicit sandbox transport; no credential cache."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable

import httpx

from .config import MaestroCredentialRequest, MaestroInstanceCredential
from .remote import MaestroRemoteError, MaestroRemoteTransport


def remote_core_credentials(
    core_origin: str,
    transport: MaestroRemoteTransport,
    identity: MaestroCredentialRequest,
    credential: Callable[[], str],
):
    """Bind one instance/runtime. Caller supplies a secret provider, never a dispatch token."""
    if transport.validate_url(core_origin) != core_origin or not callable(credential) or identity.audience != "dooers-service-maestro":
        raise MaestroRemoteError()

    from dooers.agents.server.protocol.maestro import MaestroContracts

    contracts = MaestroContracts()

    async def resolve(request: MaestroCredentialRequest) -> MaestroInstanceCredential:
        if (
            request.organization_id != identity.organization_id
            or request.workspace_id != identity.workspace_id
            or request.instance_agent_id != identity.instance_agent_id
            or request.runtime_id != identity.runtime_id
            or request.audience != identity.audience
            or not request.scopes
            or not set(request.scopes) <= set(identity.scopes)
        ):
            raise MaestroRemoteError()
        started = time.time()
        try:
            async with asyncio.timeout(5):
                secret = credential()
                if not isinstance(secret, str) or not re.fullmatch(r"[\x21-\x7e]{16,512}", secret):
                    raise MaestroRemoteError()
                endpoint = core_origin + "/api/v2/maestro/agent-token"
                headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json", "Accept-Encoding": "identity"}
                headers.update(await transport.headers(endpoint))
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
                    async with client.stream(
                        "POST",
                        endpoint,
                        headers=headers,
                        json={
                            "organization_id": request.organization_id,
                            "workspace_id": request.workspace_id,
                            "agent_id": request.instance_agent_id,
                            "runtime_id": request.runtime_id,
                            "scopes": list(request.scopes),
                        },
                    ) as response:
                        if (
                            response.status_code != 200
                            or response.headers.get("content-type", "").split(";")[0] != "application/json"
                            or response.headers.get("content-encoding", "identity") != "identity"
                        ):
                            raise MaestroRemoteError()
                        data = bytearray()
                        async for chunk in response.aiter_raw():
                            data.extend(chunk)
                            if len(data) > 32768:
                                raise MaestroRemoteError()
                        body = json.loads(data)
                contracts.validate("Identity.AgentTokenResponse", body)
                if (
                    not set(request.scopes) <= set(body["scopes"])
                    or started + body["expires_in"] <= time.time()
                    or body["scope"]
                    != {
                        "organization_id": request.organization_id,
                        "workspace_id": request.workspace_id,
                        "scope_kind": "personal" if request.workspace_id is None else "workspace",
                    }
                ):
                    raise MaestroRemoteError()
                return MaestroInstanceCredential(body["access_token"], started + body["expires_in"])
        except Exception:
            raise MaestroRemoteError() from None

    return resolve
