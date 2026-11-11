"""Parity scenarios: one per public SDK function, plus AgentServer, channels, incoming and history.

The scenarios live in three files by area; this module joins them in ``SCENARIOS``.
"""

from __future__ import annotations

from tests.parity.model import Scenario, ScenarioInput, Turn
from tests.parity.scenarios_incoming import INCOMING_SCENARIOS
from tests.parity.scenarios_send import SEND_SCENARIOS
from tests.parity.scenarios_server import SERVER_SCENARIOS

__all__ = ["SCENARIOS", "Scenario", "ScenarioInput", "Turn"]


def _merge(*groups: dict[str, Scenario]) -> dict[str, Scenario]:
    merged: dict[str, Scenario] = {}
    for group in groups:
        duplicated = merged.keys() & group.keys()
        if duplicated:
            raise ValueError(f"duplicated parity scenarios: {sorted(duplicated)}")
        merged.update(group)
    return merged


SCENARIOS: dict[str, Scenario] = _merge(SEND_SCENARIOS, SERVER_SCENARIOS, INCOMING_SCENARIOS)
