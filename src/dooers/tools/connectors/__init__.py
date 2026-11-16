"""Dooers connector tools for agent handlers.

    from dooers.tools import connectors

    tools = await connectors.tools()            # ToolSet: .openai(), .anthropic(), ..., await .run(call)
    body = await connectors.call("agenda", "create_event", {...})
    event = body["content"]                     # call() returns the service body; the tool's result is in ["content"]
    await connectors.list()                     # what this agent may use in this turn, and why the rest is missing

``dooers.agents.server`` imports ``runtime`` from here, so this module must stay import-light:
``tools``, ``call``, ``list`` and ``ToolSet`` load lazily through ``__getattr__`` (same pattern as
``dooers.tools.whatsapp``). ``list`` is ``toolset.list_connectors``; defining it here would shadow the builtin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dooers.tools.connectors.errors import (
    ApprovalDeclined,
    ConnectionRequired,
    ConnectorError,
    InteractionTimeout,
    InvalidArguments,
    PermissionDenied,
)

if TYPE_CHECKING:
    from dooers.tools.connectors.formats import ToolSpec
    from dooers.tools.connectors.toolset import ToolSet, call, tools
    from dooers.tools.connectors.toolset import list_connectors as list  # noqa: F401 (typed connectors.list, kept out of __all__)

# "list" is left out on purpose: ``from dooers.tools.connectors import *`` would shadow the builtin.
__all__ = [
    "ApprovalDeclined",
    "ConnectionRequired",
    "ConnectorError",
    "InteractionTimeout",
    "InvalidArguments",
    "PermissionDenied",
    "ToolSet",
    "ToolSpec",
    "call",
    "tools",
]

_LAZY = {"ToolSet": "ToolSet", "tools": "tools", "call": "call", "list": "list_connectors"}


def __getattr__(name: str) -> object:
    if name in _LAZY:
        from dooers.tools.connectors import toolset

        return getattr(toolset, _LAZY[name])
    if name == "ToolSpec":
        from dooers.tools.connectors.formats import ToolSpec

        return ToolSpec
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
