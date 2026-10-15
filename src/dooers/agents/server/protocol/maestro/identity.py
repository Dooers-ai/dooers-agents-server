"""Identidades lógicas de output/efeito, independentes da tentativa de transporte."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import MaestroContracts


@dataclass(frozen=True, slots=True)
class StableIdentity:
    delivery_id: str
    phase_id: str
    ordinal: int
    output_id: str
    operation_id: str
    effect_key: str


def stable_identity(contracts: MaestroContracts, delivery_id: str, phase_id: str, ordinal: int) -> StableIdentity:
    """Ordinal semântico deve ser fixado antes da operação, nunca após um ACK."""
    value = {
        "delivery_id": delivery_id,
        "phase_id": phase_id,
        "ordinal": ordinal,
        "output_id": f"output:{ordinal}",
        "operation_id": f"effect:{ordinal}",
        "effect_key": f"maestro:v1:effect:{delivery_id}:{phase_id}:{ordinal}",
    }
    contracts.validate("Delivery.OutputIdentity", value)
    return StableIdentity(**value)


def callback_idempotency_key(
    contracts: MaestroContracts,
    operation: Literal["output", "complete", "suspend"],
    delivery_id: str,
    phase_id: str,
    ordinal: int | None = None,
) -> str:
    prefix = f"{operation}:{delivery_id}:{phase_id}"
    key = prefix if operation == "complete" else f"{prefix}:{ordinal}"
    contracts.validate(
        "Delivery.CallbackIdempotency",
        {"operation": operation, "delivery_id": delivery_id, "phase_id": phase_id, "ordinal": ordinal, "key": key},
    )
    return key
