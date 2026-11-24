"""Parity suite, Dooers Threads side: the legacy scenarios through thread_management="dooers_threads".

``run_dooers_threads`` runs each scenario's handler and inputs through the SDK's Dooers Threads code
against ``FakeDooersThreads`` (or through the Dooers Threads-mode ``AgentServer`` call that is the
scenario's ingress). The result must equal the legacy ground truth after ``normalize``.
``KNOWN_GAPS`` lists every scenario that still differs. Those run as strict xfail that
accepts only the failure the gap states (``Gap.raises``): a fixed gap, or a gap that
starts to fail for another reason, fails here until a plan updates the list. A gap that
names declared differences (``Gap.masked``) must match once they are masked.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dooers.agents.server.dooers_threads.agent_api import DooersThreadsAgentApiError
from dooers.agents.server.exceptions import ThreadManagementModeError
from tests.parity import harness
from tests.parity.fake_dooers_threads import FakeDooersThreads, FakeDooersThreadsRejected
from tests.parity.harness import ContractViolation, run_dooers_threads, run_legacy
from tests.parity.known_gaps import DECLARED_DIFFERENCES, GAP_REASONS, GAPS, KNOWN_GAPS, masked
from tests.parity.normalize import normalize
from tests.parity.scenarios import SCENARIOS

HEALTHY_DELIVERY_STATES = {"responded", "silent"}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_dooers_threads_matches_legacy(name, request):
    if name in KNOWN_GAPS:
        request.applymarker(pytest.mark.xfail(strict=True, raises=GAPS[name].raises, reason=GAP_REASONS[name]))
    dooers_threads = await run_dooers_threads(name)
    assert normalize(dooers_threads) == normalize(await run_legacy(name))
    # Equal events are not enough: every Dooers Threads delivery must also end well.
    assert all(d["status"] == 200 and d["state"] in HEALTHY_DELIVERY_STATES for d in dooers_threads.deliveries), dooers_threads.deliveries


def test_known_gaps_only_lists_real_scenarios():
    assert KNOWN_GAPS <= set(SCENARIOS)


def test_every_known_gap_states_what_differs():
    assert set(GAP_REASONS) == KNOWN_GAPS
    assert all(isinstance(reason, str) and reason.strip() for reason in GAP_REASONS.values())
    for gap in GAPS.values():
        assert gap.raises in {AssertionError, ThreadManagementModeError, FakeDooersThreadsRejected, DooersThreadsAgentApiError}
        assert (gap.match is None) == (gap.raises is AssertionError)
        # Only the AgentServer ingress and WhatsApp (plan 5), the public chat (plan 6) and the scenarios that differ
        # by a permanent declared difference alone are left.
        assert set(gap.masked) <= set(DECLARED_DIFFERENCES)
        if gap.masked:
            assert gap.raises is AssertionError and all("permanent" in DECLARED_DIFFERENCES[m][0] for m in gap.masked)
        else:
            assert "plan 5" in gap.reason or "plan 6" in gap.reason, gap.reason


@pytest.mark.parametrize("name", sorted(KNOWN_GAPS))
async def test_known_gap_fails_as_stated(name):
    gap = GAPS[name]
    if gap.raises is AssertionError:
        result = await run_dooers_threads(name)
        assert [(d["status"], d["state"], d["reason_code"]) for d in result.deliveries] == list(gap.deliveries)
        if gap.masked:
            # The declared differences are the whole gap: masked on both sides, the results are equal.
            assert masked(normalize(result), gap.masked) == masked(normalize(await run_legacy(name)), gap.masked)
    else:
        with pytest.raises(gap.raises, match=gap.match):
            await run_dooers_threads(name)


# -- A refused SDK request is never a gap --


def _refuse_heartbeats(monkeypatch):
    """Simulate an SDK request outside the contract: FakeDooersThreads records a refused heartbeat."""
    handle = FakeDooersThreads.handle

    def refusing(self, request):
        if request.url.path.endswith("/heartbeat"):
            self.rejections.append(("heartbeat", "INVALID_REQUEST"))
        return handle(self, request)

    monkeypatch.setattr(FakeDooersThreads, "handle", refusing)


async def test_refused_sdk_request_fails_the_run(monkeypatch):
    _refuse_heartbeats(monkeypatch)
    with pytest.raises(ContractViolation, match="heartbeat"):
        await run_dooers_threads("text")


async def test_refused_sdk_request_outranks_the_scenario_failure(monkeypatch):
    _refuse_heartbeats(monkeypatch)

    def refused_form_response(self, *args, **kwargs):
        raise FakeDooersThreadsRejected("form.response", "scenario failure after the refused heartbeats")

    monkeypatch.setattr(FakeDooersThreads, "respond_form", refused_form_response)
    with pytest.raises(ContractViolation) as error:
        await run_dooers_threads("form_response_submit")
    assert isinstance(error.value.__context__, FakeDooersThreadsRejected)
    assert not issubclass(ContractViolation, tuple(gap.raises for gap in GAPS.values()))


async def test_expected_rejection_is_declared_per_scenario(monkeypatch):
    _refuse_heartbeats(monkeypatch)
    declared = (("heartbeat", "INVALID_REQUEST", "probe"),) * 3
    monkeypatch.setitem(harness.EXPECTED_REJECTIONS, "text", declared)
    result = await run_dooers_threads("text")
    assert result.deliveries == [{"status": 200, "state": "responded", "reason_code": "responded"}]


# -- run_dooers_threads reaches the SDK path each flow stands for --


async def test_dashboard_turn_runs_the_handler_through_a_dooers_threads_delivery():
    result = await run_dooers_threads("text")
    assert [(event["actor"], event["content"]) for event in result.events] == [
        ("user", [{"type": "text", "text": "Olá"}]),
        ("assistant", [{"type": "text", "text": "Olá! Como posso ajudar?"}]),
    ]
    assert result.events[1]["author"] == "Assistente"
    assert result.deliveries == [{"status": 200, "state": "responded", "reason_code": "responded"}]


async def test_send_whatsapp_publishes_then_fails_without_the_agent_api_route(monkeypatch):
    """F6/Task 7 correction: ``send.whatsapp.*`` is no longer refused by the sink; the message publishes.

    Plan 5, task 8 gives ``FakeDooersThreads``'s credential the ``threads:manage`` scope too (every
    ``DooersThreadsAgentApi`` call, inside or outside a delivery, shares it), so ``DooersThreadsAgentApi.record_channel_delivery``
    (the initial delivery status) no longer fails on the credential. The ``text`` scenario still has no agent-api
    backend (it runs through ``_run_dooers_threads_deliveries``, not ``_run_dooers_threads_server``'s combined route), so the call
    now fails on the route instead: the delivery still ends ``failed``, caught by the executor the same way any
    handler exception is (``handler_error``), not raised past it.
    """

    async def handler(on, send, memory, analytics, settings):
        yield send.whatsapp.text("Oi", to_e164="+5511999990000", instance_id="instancia-1")

    monkeypatch.setitem(SCENARIOS, "text", replace(SCENARIOS["text"], handler=handler))
    monkeypatch.setitem(
        harness.EXPECTED_REJECTIONS,
        "text",
        (("POST /agent/channel-deliveries", "NOT_FOUND", "no agent-api backend outside _run_dooers_threads_server"),),
    )
    result = await run_dooers_threads("text")
    assert [event["actor"] for event in result.events] == ["user", "assistant"]
    assert result.deliveries == [{"status": 200, "state": "failed", "reason_code": "handler_error"}]


async def test_public_chat_visitor_never_reaches_the_dooers_threads_mode_router(monkeypatch):
    """Task 6a's own fix: the visitor talks to Dooers Threads directly (design 7.3), never through the Router.

    ``public_chat_guest``/``public_chat_authenticated`` used to ack every ``event.create`` with
    ``THREAD_MANAGEMENT_MISMATCH`` (``_run_dooers_threads_server`` sent them to the Router). They are plain
    ``KNOWN_GAPS`` entries now (a permanent declared difference, not a routing failure): this proves the
    new path actually runs — two deliveries, both responded, no FakeDooersThreads rejection — the same quantity
    ``test_dooers_threads_matches_legacy``/``test_known_gap_fails_as_stated`` already check, stated here on its
    own so a future regression back to the Router is obvious without reading the masked diff.
    """
    monkeypatch.setattr(harness, "FakeDooersThreads", _Recorded)
    result = await run_dooers_threads("public_chat_guest")
    assert result.deliveries == [{"status": 200, "state": "responded", "reason_code": "responded"}] * 2
    fake = _Recorded.last
    assert fake.rejections == []
    (thread_id,) = fake.threads
    assert [p["kind"] for p in fake.threads[thread_id]["participants"]] == ["agent", "external"]
    assert [event["actor"]["kind"] for event in fake.events[thread_id] if event["type"] == "message.created"] == [
        "external",
        "agent",
        "external",
        "agent",
    ]


async def test_web_chat_person_cannot_send_a_url_part(monkeypatch):
    """A URL part reaches Dooers Threads only from the agent (``media_link``); the web chat sends uploads."""
    scenario = SCENARIOS["form_response_submit"]
    photo = {"type": "image", "url": "https://cdn.example.com/foto.png", "mime_type": "image/png"}
    turns = (replace(scenario.incoming.turns[0], content=({"type": "text", "text": "Veja"}, photo)), *scenario.incoming.turns[1:])
    monkeypatch.setitem(SCENARIOS, "form_response_submit", replace(scenario, incoming=replace(scenario.incoming, turns=turns)))
    with pytest.raises(FakeDooersThreadsRejected) as error:
        await run_dooers_threads("form_response_submit")
    assert error.value.action == "message.create"


class _Recorded(FakeDooersThreads):
    """The FakeDooersThreads of the last ``run_dooers_threads``, to look at what Dooers Threads stored."""

    last: FakeDooersThreads | None = None

    def __init__(self, **options):
        super().__init__(**options)
        _Recorded.last = self


async def test_dispatch_turn_is_the_agents_publication_of_the_person(monkeypatch):
    monkeypatch.setattr(harness, "FakeDooersThreads", _Recorded)
    await run_dooers_threads("history_openai")
    fake = _Recorded.last
    (thread_id,) = fake.threads
    photo = next(event for event in fake.events[thread_id] if event["type"] == "message.created")
    assert photo["actor"]["display_name"] == "Ana Souza" and "email" not in photo["actor"]
    assert photo["payload"]["content"][1] == {
        "type": "media_link",
        "kind": "image",
        "url": "https://cdn.example.com/foto.png",
        "mime_type": "image/png",
        "filename": "foto.png",
    }
    # The declared e-mail and identity IDs go back only to the agent, in its context.
    actor = fake.agent_view(thread_id)["events"][1]["actor"]
    assert (actor["email"], actor["identity_ids"]) == ("ana@example.com", ["idp-ana"])


async def test_media_upload_scenario_uploads_the_files(monkeypatch):
    """``media_upload`` runs the upload path: each served URL becomes an attachment of the agent."""
    monkeypatch.setattr(harness, "FakeDooersThreads", _Recorded)
    await run_dooers_threads("media_upload")
    fake = _Recorded.last
    parts = [
        event["payload"]["content"][0]
        for event in fake.all_events()
        if event["type"] == "message.created" and event["actor"]["kind"] == "agent" and "text" not in event["payload"]["content"][0]
    ]
    assert [(part["type"], part["mime_type"], part["filename"]) for part in parts] == [
        ("attachment", "image/png", "grafico.png"),
        ("attachment", "audio/ogg", "resposta.ogg"),
        ("attachment", "application/pdf", "relatorio.pdf"),
    ]
    assert {fake.artifacts[part["artifact_id"]]["state"] for part in parts} == {"linked"}
