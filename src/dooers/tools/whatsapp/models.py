"""Public models for ``dooers.tools.whatsapp``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WhatsAppInstance:
    instance_id: str
    phone_number: str | None = None
    status: str | None = None
