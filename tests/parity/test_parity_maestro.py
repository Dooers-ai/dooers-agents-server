"""Parity suite, Maestro side: the legacy scenarios through thread_management="maestro".

``run_maestro`` runs each scenario's handler and inputs through the SDK's Maestro code
against ``FakeMaestro`` (or through the Maestro-mode ``AgentServer`` call that is the
scenario's ingress). The result must equal the legacy ground truth after ``normalize``.
``KNOWN_GAPS`` lists every scenario that still differs. Those run as strict xfail: a
gap that gets fixed fails here until a later plan removes it from the list.
"""

from __future__ import annotations

import pytest

from dooers.agents.server.exceptions import ThreadManagementModeError
from tests.parity.fake_maestro import FakeMaestroRejected
from tests.parity.harness import run_legacy, run_maestro
from tests.parity.known_gaps import GAP_REASONS, KNOWN_GAPS
from tests.parity.normalize import normalize
from tests.parity.scenarios import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_maestro_matches_legacy(name, request):
    if name in KNOWN_GAPS:
        request.applymarker(pytest.mark.xfail(strict=True, reason="lacuna de paridade"))
    assert normalize(await run_maestro(name)) == normalize(await run_legacy(name))


def test_known_gaps_only_lists_real_scenarios():
    assert KNOWN_GAPS <= set(SCENARIOS)


def test_every_known_gap_states_what_differs():
    assert set(GAP_REASONS) == KNOWN_GAPS
    assert all(isinstance(reason, str) and reason.strip() for reason in GAP_REASONS.values())


# -- run_maestro reaches the SDK path each flow stands for (so an xfail is a real gap) --


async def test_dashboard_turn_runs_the_handler_through_a_maestro_delivery():
    result = await run_maestro("text")
    assert [(event["actor"], event["content"]) for event in result.events] == [
        ("user", [{"type": "text", "text": "Olá"}]),
        ("assistant", [{"type": "text", "text": "Olá! Como posso ajudar?"}]),
    ]
    assert result.events[1]["author"] == "Assistente"


async def test_send_function_refused_by_the_sink_leaves_only_the_user_message():
    result = await run_maestro("image")
    assert [event["actor"] for event in result.events] == ["user"]


@pytest.mark.parametrize("name", ["server_dispatch", "repository_crud", "chat_upload", "whatsapp_text"])
async def test_agent_server_ingress_reaches_the_maestro_mode_guard(name):
    with pytest.raises(ThreadManagementModeError, match="THREAD_MANAGEMENT_MISMATCH"):
        await run_maestro(name)


async def test_public_chat_reaches_the_maestro_mode_router():
    result = await run_maestro("public_chat_guest")
    errors = [ack["error"]["code"] for ack in result.outputs["acks"] if not ack["ok"]]
    assert errors == ["THREAD_MANAGEMENT_MISMATCH", "THREAD_MANAGEMENT_MISMATCH"]
    assert result.events == [] and result.outputs["threads"] == []


@pytest.mark.parametrize(
    "name,action",
    [("history_openai", "message.create"), ("incoming_fields", "message.create"), ("form_response_submit", "form.response")],
)
async def test_input_outside_the_maestro_contract_is_rejected_by_the_fake(name, action):
    with pytest.raises(FakeMaestroRejected) as error:
        await run_maestro(name)
    assert error.value.action == action
