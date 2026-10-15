"""Contratos Maestro opt-in. Não monta rotas, conecta DB ou ativa capabilities."""

from .contracts import (
    BUNDLE_MANIFEST_SHA256,
    SOURCE_MANIFEST_SHA256,
    SOURCE_REVISION,
    MaestroContractError,
    MaestroContracts,
    verify_contract_bundle,
)
from .identity import StableIdentity, callback_idempotency_key, stable_identity

__all__ = [
    "BUNDLE_MANIFEST_SHA256",
    "SOURCE_MANIFEST_SHA256",
    "SOURCE_REVISION",
    "MaestroContractError",
    "MaestroContracts",
    "StableIdentity",
    "callback_idempotency_key",
    "stable_identity",
    "verify_contract_bundle",
]
