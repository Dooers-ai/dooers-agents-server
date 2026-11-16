"""Dooers connector tools for agent handlers.

``dooers.agents.server`` imports ``runtime`` from here, so this module must stay import-light:
heavier submodules are exposed lazily through ``__getattr__`` (same pattern as ``dooers.tools.whatsapp``).
"""

from __future__ import annotations

from dooers.tools.connectors.errors import (
    ApprovalDeclined,
    ConnectionRequired,
    ConnectorError,
    InteractionTimeout,
    InvalidArguments,
    PermissionDenied,
)
from dooers.tools.connectors.runtime import TurnContext, current_turn

__all__ = [
    "ApprovalDeclined",
    "ConnectionRequired",
    "ConnectorError",
    "InteractionTimeout",
    "InvalidArguments",
    "PermissionDenied",
    "TurnContext",
    "current_turn",
]
