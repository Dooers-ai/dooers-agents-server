"""Leitura autenticada local, sem cache, redirecionamento ou retry implícito."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from dooers.agents.server.protocol.maestro import MaestroContractError, MaestroContracts

from .adapter import MaestroAdmission, MaestroCurrentLease, _epoch
from .config import MaestroConfig, MaestroCredentialRequest, MaestroInstanceCredential
from .diagnostics import diagnostic_stage
from .security import _decode, load_public_keys, renewed_capability, strict_json


class MaestroContextError(RuntimeError):
    """Código fechado; nunca contém URL, token, payload ou erro do transporte."""

    def __init__(self, code: str = "CONTEXT_UNAVAILABLE") -> None:
        allowed = {"CONTEXT_UNAVAILABLE", "ACCESS_REVOKED", "CONTEXT_INVALID", "CONTEXT_LIMIT", "CONTEXT_EXPIRED", "UNSUPPORTED_CAPABILITY"}
        self.code = code if code in allowed else "CONTEXT_UNAVAILABLE"
        super().__init__(self.code)


class MaestroContextClient:
    """Port de contexto para executor após o gate atual. Não é gate/heartbeat.

    O resolvedor é confiável e entrega token core próprio; o Maestro autentica sua
    assinatura e autorização atual. A inspeção local das claims apenas evita
    confusão de credenciais/instâncias, não substitui essa autenticação remota.
    """

    def __init__(
        self,
        config: MaestroConfig,
        admission: MaestroAdmission,
        lease: MaestroCurrentLease,
        *,
        contracts: MaestroContracts | None = None,
        max_page_bytes: int = 1048576,
    ) -> None:
        self.config, self.admission = config, admission
        self._lease = lease
        self._contracts = contracts or MaestroContracts()
        self._closed = False
        self._finished = False
        self._cancelled: Callable[[], bool] = lambda: False
        self._capability = admission.execution_capability
        self._claims = admission.capability_claims
        self._keys = load_public_keys(config.trusted_public_keys)
        if type(max_page_bytes) is not int or not 1024 <= max_page_bytes <= 4194304:
            raise MaestroContextError("CONTEXT_LIMIT")
        self.max_page_bytes = max_page_bytes
        self._envelope = admission.envelope
        try:
            self._contracts.validate("Delivery.DispatchEnvelope", self._envelope)
            for key, expected in (
                ("organization_id", config.organization_id),
                ("workspace_id", config.workspace_id),
                ("instance_agent_id", config.instance_agent_id),
                ("runtime_id", config.runtime_id),
                ("callback_base_url", config.callback_base_url),
            ):
                if self._envelope[key] != expected:
                    raise ValueError
            if "delivery.context" not in self._claims["operations"]:
                raise ValueError
        except Exception:
            raise MaestroContextError("CONTEXT_INVALID") from None
        if lease.execution_capability is not None:
            self.update_capability(lease.execution_capability)
        self._check_current()

    def close(self) -> None:
        """Finalização/cancelamento local impede qualquer nova leitura do contexto."""
        self._finished = True

    def bind_cancellation(self, cancelled: Callable[[], bool]) -> None:
        """Port local do executor, sem conceder autoridade remota."""
        self._check_current()
        self._cancelled = cancelled

    def update_capability(self, token: str) -> None:
        """Token renovado por heartbeat; assinatura/bindings verificados antes de troca."""
        try:
            claims = renewed_capability(token, self._claims, keys=self._keys, contracts=self._contracts)
            if self._closed or self._finished or self._cancelled() or self.admission.remaining_seconds() <= 0:
                raise ValueError
            self._capability, self._claims = token, claims
        except Exception:
            raise MaestroContextError("ACCESS_REVOKED") from None

    def update_lease(self, lease: MaestroCurrentLease) -> None:
        """Somente executor confiável após ACK autenticado de heartbeat."""
        previous = self._lease
        self._lease = lease
        try:
            self._check_current(reserve_completion=False)
            if _epoch(lease.lease_until) < _epoch(previous.lease_until):
                raise MaestroContextError("CONTEXT_EXPIRED")
        except Exception:
            self._lease = previous
            raise

    def _check_current(self, *, reserve_completion: bool = True) -> float:
        if self._finished or self._cancelled():
            raise MaestroContextError("CONTEXT_EXPIRED")
        if self._closed:
            raise MaestroContextError("ACCESS_REVOKED")
        try:
            self._contracts.validate("Public.HeartbeatResponse", {"lease_until": self._lease.lease_until})
            remaining = min(
                self.admission.remaining_seconds()
                - self.config.safety_margin_seconds
                - (self.config.completion_budget_seconds if reserve_completion else 0),
                _epoch(self._lease.lease_until) - time.time(),
                self._claims["exp"] - time.time(),
            )
            if (
                self._lease.execution_id != self._envelope["execution_id"]
                or self._lease.lease_fence != self._envelope["lease_fence"]
                or _epoch(self._lease.lease_until) > _epoch(self._envelope["deadline_at"])
                or remaining <= 0
            ):
                raise ValueError
            return remaining
        except Exception:
            raise MaestroContextError("CONTEXT_EXPIRED") from None

    async def _credential(self, scopes: tuple[str, ...] = ("deliveries:read", "context:read")) -> MaestroInstanceCredential:
        return await resolve_instance_credential(self.config, self.admission, self._contracts, scopes, capability=self._capability)

    async def page(self, cursor: str, *, limit: int = 100) -> dict[str, Any]:
        """Cada página renova autorização remota, inclusive chamadas repetidas."""
        try:
            with diagnostic_stage("context.page", self._envelope):
                self._contracts.validate("Public.PageQuery", {"cursor": cursor, "limit": limit})
                remaining = self._check_current()
                async with asyncio.timeout(min(remaining, self.config.network_timeout_seconds)):
                    with diagnostic_stage("auth.credential", self._envelope):
                        credential = await self._credential()
                    self._check_current()
                    headers = {
                        "Authorization": f"Bearer {credential.token}",
                        "Maestro-Execution-Capability": self._capability,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-store",
                    }
                    with diagnostic_stage("auth.workload_identity", self._envelope):
                        headers.update(await self.config.transport_headers(self.config.callback_base_url))
                    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                    async with httpx.AsyncClient(
                        transport=transport, trust_env=False, follow_redirects=False, timeout=self.config.network_timeout_seconds
                    ) as client:
                        url = f"{self.config.callback_base_url}/deliveries/{self._envelope['delivery_id']}/context"
                        async with client.stream("GET", url, params={"cursor": cursor, "limit": limit}, headers=headers) as response:
                            if response.status_code in {401, 403, 404, 409, 410}:
                                self._closed = True
                                raise MaestroContextError("ACCESS_REVOKED")
                            if response.status_code != 200:
                                raise MaestroContextError()
                            if (
                                response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
                                or response.headers.get("content-encoding", "identity") != "identity"
                            ):
                                raise MaestroContextError("CONTEXT_INVALID")
                            length = response.headers.get("content-length")
                            if length is not None and (not length.isdecimal() or int(length) > self.max_page_bytes):
                                raise MaestroContextError("CONTEXT_LIMIT")
                            raw = bytearray()
                            async for chunk in response.aiter_raw():
                                raw.extend(chunk)
                                if len(raw) > self.max_page_bytes:
                                    raise MaestroContextError("CONTEXT_LIMIT")
                            self._check_current()
                            if time.time() >= credential.expires_at:
                                raise MaestroContextError("ACCESS_REVOKED")
                            return self._contracts.validate("Public.ContextResponse", strict_json(bytes(raw)))
        except MaestroContextError as error:
            if error.code == "ACCESS_REVOKED":
                self._closed = True
            raise
        except (MaestroContractError, ValueError, TypeError, KeyError):
            raise MaestroContextError("CONTEXT_INVALID") from None
        except Exception:
            raise MaestroContextError() from None

    async def interaction_context(self, *, approval: bool = False) -> dict[str, Any]:
        """Private candidate read: current instance + execution capability, never public history."""
        try:
            with diagnostic_stage("context.interaction", self._envelope):
                if not self._envelope.get("resume"):
                    raise MaestroContextError("CONTEXT_INVALID")
                remaining = self._check_current()
                async with asyncio.timeout(min(remaining, self.config.network_timeout_seconds)):
                    with diagnostic_stage("auth.credential", self._envelope):
                        credential = await self._credential()
                    self._check_current()
                    headers = {
                        "Authorization": f"Bearer {credential.token}",
                        "Maestro-Execution-Capability": self._capability,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-store",
                    }
                    with diagnostic_stage("auth.workload_identity", self._envelope):
                        headers.update(await self.config.transport_headers(self.config.callback_base_url))
                    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                    async with httpx.AsyncClient(
                        transport=transport, trust_env=False, follow_redirects=False, timeout=self.config.network_timeout_seconds
                    ) as client:
                        suffix = "approval-context" if approval else "interaction-context"
                        url = f"{self.config.callback_base_url}/deliveries/{self._envelope['delivery_id']}/{suffix}"
                        async with client.stream("GET", url, headers=headers) as response:
                            if response.status_code in {401, 403, 404, 409, 410}:
                                self._closed = True
                                raise MaestroContextError("ACCESS_REVOKED")
                            if response.status_code != 200:
                                raise MaestroContextError()
                            if (
                                response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
                                or response.headers.get("content-encoding", "identity") != "identity"
                            ):
                                raise MaestroContextError("CONTEXT_INVALID")
                            length = response.headers.get("content-length")
                            if length is not None and (not length.isdecimal() or int(length) > self.max_page_bytes):
                                raise MaestroContextError("CONTEXT_LIMIT")
                            raw = bytearray()
                            async for chunk in response.aiter_raw():
                                raw.extend(chunk)
                                if len(raw) > self.max_page_bytes:
                                    raise MaestroContextError("CONTEXT_LIMIT")
                            self._check_current()
                            if time.time() >= credential.expires_at:
                                raise MaestroContextError("ACCESS_REVOKED")
                            from .interactions import validate_private_context

                            if approval:
                                from .approvals import validate_approval_context

                                value = validate_approval_context(strict_json(bytes(raw)), self._envelope)
                            else:
                                value = validate_private_context(strict_json(bytes(raw)), self._envelope)
                            # Validation/deepcopy are synchronous work and may outlast
                            # network timeout callbacks; expose nothing after that budget.
                            self._check_current()
                            if time.time() >= credential.expires_at:
                                raise MaestroContextError("ACCESS_REVOKED")
                            return value
        except MaestroContextError as error:
            if error.code == "ACCESS_REVOKED":
                self._closed = True
            raise
        except (MaestroContractError, ValueError, TypeError, KeyError):
            raise MaestroContextError("CONTEXT_INVALID") from None
        except Exception:
            raise MaestroContextError() from None

    async def private_resume_context(self):
        if (self._envelope.get("resume") or {}).get("kind") == "tool_approval":
            return await self.interaction_context(approval=True)
        # Preserve the established no-keyword form client/override contract.
        return await self.interaction_context()


async def resolve_instance_credential(
    config: MaestroConfig,
    admission: MaestroAdmission,
    contracts: MaestroContracts,
    scopes: tuple[str, ...],
    *,
    capability: str | None = None,
) -> MaestroInstanceCredential:
    """Credential projection only; Maestro remotely authenticates current authority."""
    request = MaestroCredentialRequest(
        config.organization_id,
        config.workspace_id,
        config.instance_agent_id,
        config.runtime_id,
        scopes,
    )
    try:
        credential = await config.credential_resolver(request)
        if (
            not isinstance(credential, MaestroInstanceCredential)
            or not isinstance(credential.token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", credential.token)
            or len(credential.token) > 16384
            or not math.isfinite(credential.expires_at)
        ):
            raise ValueError
        header, payload, _signature = credential.token.split(".")
        header = strict_json(_decode(header))
        claims = contracts.validate("Identity.AgentTokenClaims", strict_json(_decode(payload)))
        if (
            header.get("typ") != "maestro-access+jwt"
            or header.get("alg") != "RS256"
            or claims["agent_id"] != request.instance_agent_id
            or claims["runtime_id"] != request.runtime_id
            or claims["organization_id"] != request.organization_id
            or claims["workspace_id"] != request.workspace_id
            or not set(request.scopes) <= set(claims["scopes"])
            or not claims["iat"] <= time.time() < min(claims["exp"], credential.expires_at)
            or credential.token == (capability or admission.execution_capability)
        ):
            raise ValueError
        return MaestroInstanceCredential(credential.token, min(claims["exp"], credential.expires_at))
    except Exception:
        raise MaestroContextError("ACCESS_REVOKED") from None
