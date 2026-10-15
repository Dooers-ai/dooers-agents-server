"""Configuração local explícita. Imports legados não carregam extras Maestro."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID


class MaestroConfigurationError(ValueError):
    """Erro de startup sem valores de configuração ou material de chave."""


@dataclass(frozen=True)
class MaestroCredentialRequest:
    """Identidade confiável para resolvedor futuro; nunca token recebido no dispatch."""

    organization_id: str
    workspace_id: str | None
    instance_agent_id: str
    runtime_id: str
    scopes: tuple[str, ...]
    audience: str = "dooers-service-maestro"


@dataclass(frozen=True)
class MaestroInstanceCredential:
    token: str = field(repr=False)
    expires_at: float


CredentialResolver = Callable[[MaestroCredentialRequest], Awaitable[MaestroInstanceCredential]]


def _uuid(value: object) -> None:
    if not isinstance(value, str):
        raise MaestroConfigurationError("MAESTRO_IDENTITY_INVALID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise MaestroConfigurationError("MAESTRO_IDENTITY_INVALID") from None


def _local_url(value: object, *, allow_origin: bool = False) -> str:
    # O primeiro adapter suporta somente o perfil local ADR0003. TLS/egress de
    # produção requer implementação própria, não apenas trocar o scheme.
    try:
        if not isinstance(value, str) or not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError
        url = urlsplit(value)
        if (
            value != url.geturl()
            or url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.netloc != f"127.0.0.1:{url.port}"
            or url.query
            or url.fragment
            or (not (allow_origin and url.path == "") and (not url.path.startswith("/") or url.path.endswith("/")))
            or not (allow_origin and url.path == "")
            and not re.fullmatch(r"/[A-Za-z0-9_/-]+", url.path)
            or "//" in url.path
            or any(part in {".", ".."} for part in url.path.split("/"))
        ):
            raise ValueError
        return url.path
    except (ValueError, TypeError):
        raise MaestroConfigurationError("MAESTRO_LOCAL_URL_INVALID") from None


@dataclass(frozen=True)
class MaestroConfig:
    """Perfil local opt-in; segundos e bytes explícitos, sem descoberta pelo request.

    Cada configuração representa um vínculo exato organização/workspace/instância/
    runtime. Réplicas compartilham esse vínculo. Outros vínculos usam adapters
    separados. O resolvedor é configuração privada e só será chamado pelo cliente
    autenticado; admissão jamais usa seu token de dispatch como credencial core.
    """

    runtime_endpoint: str
    callback_base_url: str
    core_base_url: str
    organization_id: str
    workspace_id: str | None
    instance_agent_id: str
    runtime_id: str
    trusted_public_keys: Mapping[str, bytes] = field(repr=False)
    credential_resolver: CredentialResolver = field(repr=False)
    allow_local_http: bool = False
    network_timeout_seconds: int = 5
    request_timeout_seconds: int = 340
    execution_deadline_seconds: int = 300
    completion_budget_seconds: int = 30
    safety_margin_seconds: int = 10
    admission_timeout_seconds: int = 20
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    max_concurrency: int = 2
    max_dispatch_bytes: int = 65536
    max_content_bytes: int = 65536
    max_output_bytes: int = 65536
    product_capabilities: tuple[str, ...] = ()
    # Empty keeps the existing text-only executor. No implicit MIME/provider policy.
    artifact_mime_types: tuple[str, ...] = ()
    max_artifact_bytes: int = 8388608
    max_hydrated_bytes: int = 16777216
    artifact_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        if type(self.product_capabilities) is not tuple or any(value != "stream.delta" for value in self.product_capabilities):
            raise MaestroConfigurationError("MAESTRO_CAPABILITY_INVALID")
        if (
            type(self.artifact_mime_types) is not tuple
            or len(set(self.artifact_mime_types)) != len(self.artifact_mime_types)
            or any(
                value not in {"text/plain", "application/pdf", "image/png", "image/jpeg", "audio/wav"} for value in self.artifact_mime_types
            )
        ):
            raise MaestroConfigurationError("MAESTRO_CAPABILITY_INVALID")
        if type(self.allow_local_http) is not bool or not self.allow_local_http:
            raise MaestroConfigurationError("MAESTRO_LOCAL_HTTP_OPT_IN_REQUIRED")
        for value in (self.organization_id, self.instance_agent_id, self.runtime_id):
            _uuid(value)
        if self.workspace_id is not None:
            _uuid(self.workspace_id)
        path = _local_url(self.runtime_endpoint)
        _local_url(self.callback_base_url)
        _local_url(self.core_base_url, allow_origin=True)
        if not path.endswith("/maestro/deliveries"):
            raise MaestroConfigurationError("MAESTRO_DELIVERY_PATH_INVALID")
        if not callable(self.credential_resolver):
            raise MaestroConfigurationError("MAESTRO_CREDENTIAL_RESOLVER_REQUIRED")
        limits = {
            "network_timeout_seconds": (1, 10),
            "request_timeout_seconds": (1, 340),
            "execution_deadline_seconds": (1, 300),
            "completion_budget_seconds": (1, 30),
            "safety_margin_seconds": (1, 10),
            "admission_timeout_seconds": (1, 20),
            "lease_seconds": (1, 60),
            "heartbeat_seconds": (1, 15),
            "max_concurrency": (1, 2),
            "max_dispatch_bytes": (1024, 65536),
            "max_content_bytes": (1, 65536),
            "max_output_bytes": (1, 65536),
            "max_artifact_bytes": (1, 26214400),
            "max_hydrated_bytes": (1, 33554432),
            "artifact_timeout_seconds": (1, 10),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise MaestroConfigurationError("MAESTRO_LIMIT_INVALID")
        if (
            self.request_timeout_seconds < self.execution_deadline_seconds + self.completion_budget_seconds
            or self.execution_deadline_seconds
            <= self.admission_timeout_seconds + self.completion_budget_seconds + self.safety_margin_seconds
            or self.heartbeat_seconds * 2 >= self.lease_seconds
            or self.lease_seconds <= 20
        ):
            raise MaestroConfigurationError("MAESTRO_BUDGET_INVALID")
        try:
            keys = dict(self.trusted_public_keys)
            if not 1 <= len(keys) <= 16:
                raise ValueError
            for kid, pem in keys.items():
                if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", kid):
                    raise ValueError
                if not isinstance(pem, bytes) or not 128 <= len(pem) <= 16384:
                    raise ValueError
        except (TypeError, ValueError):
            raise MaestroConfigurationError("MAESTRO_KEYSET_INVALID") from None
        object.__setattr__(self, "trusted_public_keys", MappingProxyType(keys))
        self.validate_startup()

    def validate_startup(self) -> None:
        from .security import load_public_keys

        load_public_keys(self.trusted_public_keys)
        from dooers.agents.server.protocol.maestro.contracts import _dependency_check, verify_contract_bundle

        # Validar o pin/dependências no startup; compilação dos mesmos schemas
        # acontece uma vez por adapter, não três vezes por configuração.
        _dependency_check()
        verify_contract_bundle()

    @property
    def delivery_path(self) -> str:
        return urlsplit(self.runtime_endpoint).path
