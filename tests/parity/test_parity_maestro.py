"""Parity suite, Maestro side: the legacy scenarios through thread_management="maestro".

``run_maestro`` runs each scenario's handler and inputs through the SDK's Maestro code
against ``FakeMaestro`` (or through the Maestro-mode ``AgentServer`` call that is the
scenario's ingress). The result must equal the legacy ground truth after ``normalize``.
``KNOWN_GAPS`` lists every scenario that still differs. Those run as strict xfail that
accepts only the failure the gap states (``Gap.raises``): a fixed gap, or a gap that
starts to fail for another reason, fails here until a plan updates the list.
"""

from __future__ import annotations

import pytest

from dooers.agents.server.exceptions import ThreadManagementModeError
from tests.parity import harness
from tests.parity.fake_maestro import FakeMaestro, FakeMaestroRejected
from tests.parity.harness import ContractViolation, run_legacy, run_maestro
from tests.parity.known_gaps import GAP_REASONS, GAPS, KNOWN_GAPS
from tests.parity.normalize import normalize
from tests.parity.scenarios import SCENARIOS

HEALTHY_DELIVERY_STATES = {"responded", "silent"}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_maestro_matches_legacy(name, request):
    if name in KNOWN_GAPS:
        request.applymarker(pytest.mark.xfail(strict=True, raises=GAPS[name].raises, reason=GAP_REASONS[name]))
    maestro = await run_maestro(name)
    assert normalize(maestro) == normalize(await run_legacy(name))
    # Equal events are not enough: every Maestro delivery must also end well.
    assert all(d["status"] == 200 and d["state"] in HEALTHY_DELIVERY_STATES for d in maestro.deliveries), maestro.deliveries


def test_known_gaps_only_lists_real_scenarios():
    assert KNOWN_GAPS <= set(SCENARIOS)


def test_every_known_gap_states_what_differs():
    assert set(GAP_REASONS) == KNOWN_GAPS
    assert all(isinstance(reason, str) and reason.strip() for reason in GAP_REASONS.values())
    for gap in GAPS.values():
        assert gap.raises in {AssertionError, ThreadManagementModeError, FakeMaestroRejected}
        assert (gap.match is None) == (gap.raises is AssertionError)


@pytest.mark.parametrize("name", sorted(KNOWN_GAPS))
async def test_known_gap_fails_as_stated(name):
    gap = GAPS[name]
    if gap.raises is AssertionError:
        result = await run_maestro(name)
        assert [(d["status"], d["state"], d["reason_code"]) for d in result.deliveries] == list(gap.deliveries)
    else:
        with pytest.raises(gap.raises, match=gap.match):
            await run_maestro(name)


# -- A refused SDK request is never a gap --


def _refuse_heartbeats(monkeypatch):
    """Simulate an SDK request outside the contract: FakeMaestro records a refused heartbeat."""
    handle = FakeMaestro.handle

    def refusing(self, request):
        if request.url.path.endswith("/heartbeat"):
            self.rejections.append(("heartbeat", "INVALID_REQUEST"))
        return handle(self, request)

    monkeypatch.setattr(FakeMaestro, "handle", refusing)


async def test_refused_sdk_request_fails_the_run(monkeypatch):
    _refuse_heartbeats(monkeypatch)
    with pytest.raises(ContractViolation, match="heartbeat"):
        await run_maestro("text")


async def test_refused_sdk_request_outranks_the_scenario_failure(monkeypatch):
    _refuse_heartbeats(monkeypatch)
    with pytest.raises(ContractViolation) as error:
        await run_maestro("form_response_submit")
    assert isinstance(error.value.__context__, FakeMaestroRejected)
    assert not issubclass(ContractViolation, tuple(gap.raises for gap in GAPS.values()))


async def test_expected_rejection_is_declared_per_scenario(monkeypatch):
    _refuse_heartbeats(monkeypatch)
    declared = (("heartbeat", "INVALID_REQUEST", "probe"),) * 3
    monkeypatch.setitem(harness.EXPECTED_REJECTIONS, "text", declared)
    result = await run_maestro("text")
    assert result.deliveries == [{"status": 200, "state": "responded", "reason_code": "responded"}]


# -- run_maestro reaches the SDK path each flow stands for --


async def test_dashboard_turn_runs_the_handler_through_a_maestro_delivery():
    result = await run_maestro("text")
    assert [(event["actor"], event["content"]) for event in result.events] == [
        ("user", [{"type": "text", "text": "Olá"}]),
        ("assistant", [{"type": "text", "text": "Olá! Como posso ajudar?"}]),
    ]
    assert result.events[1]["author"] == "Assistente"
    assert result.deliveries == [{"status": 200, "state": "responded", "reason_code": "responded"}]


async def test_send_function_refused_by_the_sink_leaves_only_the_user_message():
    result = await run_maestro("image")
    assert [event["actor"] for event in result.events] == ["user"]
    assert result.deliveries == [{"status": 200, "state": "failed", "reason_code": "unsupported_capability"}]


@pytest.mark.parametrize("name", ["server_dispatch", "repository_crud", "chat_upload", "whatsapp_text"])
async def test_agent_server_ingress_reaches_the_maestro_mode_guard(name):
    with pytest.raises(ThreadManagementModeError, match="THREAD_MANAGEMENT_MISMATCH"):
        await run_maestro(name)


async def test_public_chat_reaches_the_maestro_mode_router():
    result = await run_maestro("public_chat_guest")
    errors = [ack["error"]["code"] for ack in result.outputs["acks"] if not ack["ok"]]
    assert errors == ["THREAD_MANAGEMENT_MISMATCH", "THREAD_MANAGEMENT_MISMATCH"]
    assert result.events == [] and result.outputs["threads"] == [] and result.deliveries == []


@pytest.mark.parametrize(
    "name,action",
    [("history_openai", "message.create"), ("incoming_fields", "message.create"), ("form_response_submit", "form.response")],
)
async def test_input_outside_the_maestro_contract_is_rejected_by_the_fake(name, action):
    with pytest.raises(FakeMaestroRejected) as error:
        await run_maestro(name)
    assert error.value.action == action
