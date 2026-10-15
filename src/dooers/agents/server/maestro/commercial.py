"""Negativas comerciais candidatas: motivos públicos fechados, sem preço ou segredo."""

from __future__ import annotations

from .journal_store import JournalError, uuid

REASONS = frozenset(
    {
        "insufficient_balance",
        "org_limit_exceeded",
        "billing_policy_unavailable",
        "model_not_allowed",
        "gateway_correlation_unavailable",
        "operation_already_started",
    }
)


class MaestroCommercialBlocked(JournalError):
    """Resultado terminal do Core autenticado, nunca silêncio/retry financeiro automático."""

    def __init__(self, reason: str):
        if reason not in REASONS:
            raise JournalError("USAGE_COMMERCIAL_INVALID")
        self.reason_code = reason
        super().__init__(reason)


def commercial_denial(value: object) -> MaestroCommercialBlocked:
    required = {"allowed", "reason", "retryable", "policy_version", "retry_after", "sandbox"}
    if (
        type(value) is not dict
        or set(value) not in (required, required | {"decision_id"})
        or value["allowed"] is not False
        or value["retryable"] is not False
        or value["sandbox"] is not True
        or value["policy_version"] != "sandbox-v1"
        or value["retry_after"] is not None
        or type(value["reason"]) is not str
        or value["reason"] not in REASONS
    ):
        raise JournalError("USAGE_COMMERCIAL_INVALID")
    if "decision_id" in value:
        try:
            uuid(value["decision_id"])
        except (ValueError, TypeError, AttributeError, JournalError):
            raise JournalError("USAGE_COMMERCIAL_INVALID") from None
    return MaestroCommercialBlocked(value["reason"])
