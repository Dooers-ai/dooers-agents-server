"""Deprecated: use `dooers.agents.server` instead."""

import warnings

warnings.warn(
    "dooers_agents is deprecated; import from dooers.agents.server instead",
    DeprecationWarning,
    stacklevel=2,
)
from dooers.agents.server import *  # noqa: F403
