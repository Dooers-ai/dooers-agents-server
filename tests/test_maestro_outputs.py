from __future__ import annotations

import copy
from uuid import UUID

import pytest

from dooers.agents.server.handlers.send import AgentEvent, AgentSend
from dooers.agents.server.maestro.outputs import (
    MaestroOutput,
    RunTracker,
    UnsupportedAgentEvent,
    agent_event_from,
    output_for,
)
from dooers.agents.server.protocol.maestro.contracts import MaestroContracts

FENCE = "1"
S = AgentSend()
AGENT_UUID = "00000000-0000-4000-8000-000000000012"

ALL_FORM_ELEMENTS = [
    S.form_text("nome", label="Nome", order=0, required=True, input_type="email"),
    S.form_textarea("obs", label="Obs", order=1, rows=3),
    S.form_select("cor", label="Cor", options=[{"value": "r", "label": "Red"}], order=2, default="r"),
    S.form_radio("tam", label="Tam", options=[{"value": "p", "label": "P"}], order=3, variant="button"),
    S.form_checkbox("ext", label="Ext", options=[{"value": "a", "label": "A"}], order=4, default=["a"]),
    S.form_file("doc", label="Doc", upload_url="https://up.example.com/x", order=5, accept="image/*", multiple=True),
]

PRODUCTION_EVENTS: dict[str, AgentEvent] = {
    "text": S.text("olá"),
    "text_author": S.text("olá", author="Bot"),
    "contact": S.contact(display_name="Ana"),
    "contact_full": S.contact(display_name="Ana", vcard="BEGIN:VCARD", phones=[{"phone": "+5511999990000", "type": "CELL"}], author="Bot"),
    "template": S.template("boas_vindas", "pt_BR"),
    "template_components": S.template(
        "boas_vindas", "pt_BR", template_components=[{"type": "body", "parameters": [{"type": "text", "text": "x"}]}], author="Bot"
    ),
    "reasoning": S.reasoning("pensando"),
    "reasoning_author": S.reasoning("pensando", author="Bot"),
    "tool_call": S.tool_call("buscar", {"q": "x", "n": None}, id="call-1"),
    "tool_call_display": S.tool_call("buscar", {}, display_name="Buscar", id="call-2"),
    "tool_result": S.tool_result("buscar", {"ok": True}, id="call-1"),
    "tool_result_full": S.tool_result("buscar", {"ok": True}, args={"q": "x"}, display_name="Buscar", id="call-1"),
    "tool_transaction": S.tool_transaction("buscar", {"q": "x"}, {"ok": True}, display_name="Buscar", id="call-3"),
    "form": S.form("Preencha", ALL_FORM_ELEMENTS, submit_label="Ok", cancel_label="Não", size="large"),
    "chart": S.chart(chart_type="bar", data=[{"x": "a", "y": 1}], x_key="x", y_keys=["y"]),
    "chart_full": S.chart(
        chart_type="line",
        data=[{"x": "a", "y": 1, "z": None}],
        x_key="x",
        y_keys=["y"],
        title="T",
        message="M",
        size="small",
        series=[S.chart_series("y", label="Y", color="#fff"), S.chart_series("z")],
        x_label="X",
        y_label="Y",
    ),
    "run_start": S.run_start(),
    "run_start_other_agent": S.run_start(agent_id=AGENT_UUID),
    "run_end": S.run_end(),
    "run_end_failed": S.run_end(status="failed", error="boom"),
    "event_update": S.update_user_event("00000000-0000-4000-8000-000000000005", [{"type": "text", "text": "novo"}]),
    "thread_update_title": S.update_thread(title="Novo"),
    "thread_update_queue": S.update_thread(queue="fila-a"),
    "thread_update_both": S.update_thread(title="Novo", queue="fila-a"),
    "participant_add": S.add_participant("user-1"),
    "participant_add_full": S.add_participant("user-1", user_name="Ana", user_email="ana@example.com", identity_ids=["id-1"]),
}

# Mesmos eventos com o contrato vendorizado atual (run_start com agent_id exige o plano 2, tarefa 5).
CONTRACT_EVENTS = {k: v for k, v in PRODUCTION_EVENTS.items() if k != "run_start_other_agent"}

STORED_TYPE = {
    "message.create": "message.created",
    "reasoning.create": "reasoning.created",
    "tool_call.create": "tool_call.created",
    "tool_result.create": "tool_result.created",
    "tool_transaction.create": "tool_transaction.created",
    "chart.create": "chart.created",
    "form.create": "form.created",
    "message.update": "message.updated",
    "run.update": "run.updated",
    "thread.update": "thread.updated",
    "participant.add": "participant.added",
}


@pytest.fixture(scope="module")
def contracts():
    return MaestroContracts()


def strip_none(event: AgentEvent) -> AgentEvent:
    return AgentEvent(event.send_type, {k: v for k, v in event.data.items() if v is not None})


def stored_event_for(out: MaestroOutput) -> dict:
    """Evento gravado pelo Maestro para a saída: o payload, mais os campos que o Maestro acrescenta."""
    payload = copy.deepcopy(out.payload)
    if out.type == "thread.update":
        payload.update({"state": "active", "thread_id": "t-1", "version": 2})
    if out.type == "run.update":
        payload.update({"trigger_event_id": "e-1"})
    return {"type": STORED_TYPE[out.type], "payload": payload, "actor": {"kind": "agent"}}


@pytest.mark.parametrize("event", CONTRACT_EVENTS.values(), ids=CONTRACT_EVENTS.keys())
def test_output_validates_against_contract(event, contracts):
    out = output_for(event, 0)
    contracts.validate(
        "Public.OutputRequest",
        {"output_id": "output:0", "type": out.type, "payload": out.payload, "lease_fence": FENCE},
    )


@pytest.mark.xfail(strict=True, reason="plano 2, tarefa 5")
def test_run_start_other_agent_validates_against_contract(contracts):
    out = output_for(PRODUCTION_EVENTS["run_start_other_agent"], 0)
    contracts.validate(
        "Public.OutputRequest",
        {"output_id": "output:0", "type": out.type, "payload": out.payload, "lease_fence": FENCE},
    )


def test_run_start_other_agent_maps_agent_id():
    out = output_for(PRODUCTION_EVENTS["run_start_other_agent"], 0)
    assert out.type == "run.update"
    assert out.payload["agent_id"] == AGENT_UUID and out.payload["status"] == "running"


def test_payload_equals_production_data_without_none():
    ev = AgentSend().reasoning("pensando", author=None)
    assert output_for(ev, 0) == MaestroOutput("reasoning.create", {"text": "pensando"})


@pytest.mark.parametrize(
    "key,expected_type",
    [
        ("text", "message.create"),
        ("contact", "message.create"),
        ("template", "message.create"),
        ("reasoning", "reasoning.create"),
        ("tool_call", "tool_call.create"),
        ("tool_result", "tool_result.create"),
        ("tool_transaction", "tool_transaction.create"),
        ("form", "form.create"),
        ("chart", "chart.create"),
        ("run_start", "run.update"),
        ("run_end", "run.update"),
        ("event_update", "message.update"),
        ("thread_update_title", "thread.update"),
        ("participant_add", "participant.add"),
    ],
)
def test_output_types(key, expected_type):
    assert output_for(PRODUCTION_EVENTS[key], 0).type == expected_type


@pytest.mark.parametrize("key", ["tool_call", "tool_result_full", "tool_transaction", "form", "chart_full", "participant_add_full"])
def test_payload_keeps_production_values(key):
    event = PRODUCTION_EVENTS[key]
    expected = {k: v for k, v in event.data.items() if v is not None}
    assert output_for(event, 0).payload == expected


def test_text_payload():
    assert output_for(S.text("oi", author="Bot"), 0).payload == {
        "content": [{"type": "text", "text": "oi"}],
        "mentions": [],
        "author": "Bot",
    }


def test_contact_payload():
    out = output_for(PRODUCTION_EVENTS["contact_full"], 0)
    assert out.payload == {
        "content": [
            {
                "type": "contact",
                "display_name": "Ana",
                "vcard": "BEGIN:VCARD",
                "phones": [{"phone": "+5511999990000", "type": "CELL"}],
            }
        ],
        "mentions": [],
        "author": "Bot",
    }


def test_template_payload_uses_production_data_keys_and_display_text():
    out = output_for(PRODUCTION_EVENTS["template_components"], 0)
    assert out.payload == {
        "content": [{"type": "text", "text": "[template: boas_vindas (pt_BR)]"}],
        "mentions": [],
        "author": "Bot",
        "template": {
            "template_name": "boas_vindas",
            "template_language": "pt_BR",
            "template_components": [{"type": "body", "parameters": [{"type": "text", "text": "x"}]}],
        },
    }


def test_whatsapp_tag_passes_through_on_message_events(contracts):
    ev = S.whatsapp.text("oi", to_e164="+5511999990000", instance_id="inst-1")
    out = output_for(ev, 0)
    assert out.payload["whatsapp"] == {"to_e164": "+5511999990000", "instance_id": "inst-1"}
    contracts.validate("Public.OutputRequest", {"output_id": "output:0", "type": out.type, "payload": out.payload, "lease_fence": FENCE})
    assert agent_event_from(stored_event_for(out)) == strip_none(ev)


def test_event_update_payload():
    out = output_for(PRODUCTION_EVENTS["event_update"], 0)
    assert out == MaestroOutput(
        "message.update", {"event_id": "00000000-0000-4000-8000-000000000005", "content": [{"type": "text", "text": "novo"}]}
    )


def test_thread_update_and_participant_payloads():
    assert output_for(S.update_thread(title="T", queue="q"), 0) == MaestroOutput("thread.update", {"title": "T", "queue": "q"})
    assert output_for(S.add_participant("u"), 0).payload == {
        "user": {"user_id": "u", "user_name": None, "user_email": None, "identity_ids": []}
    }


def test_does_not_mutate_event_and_payload_is_independent():
    ev = PRODUCTION_EVENTS["chart_full"]
    before = copy.deepcopy(ev)
    out = output_for(ev, 0)
    assert ev == before
    out.payload["data"].append({"x": "b"})
    assert ev == before


@pytest.mark.parametrize("kind", ["image", "audio", "document"])
def test_media_events_are_not_mapped(kind):
    ev = {
        "image": S.image("https://x.example.com/a.png"),
        "audio": S.audio("https://x.example.com/a.ogg", "audio/ogg"),
        "document": S.document("https://x.example.com/a.pdf", "a.pdf", "application/pdf"),
    }[kind]
    with pytest.raises(UnsupportedAgentEvent):
        output_for(ev, 0)


def test_unknown_send_type_is_rejected():
    with pytest.raises(UnsupportedAgentEvent):
        output_for(AgentEvent("nope", {}), 0)


def test_run_start_creates_uuid4_and_running_status():
    a, b = output_for(S.run_start(), 0), output_for(S.run_start(), 1)
    assert a.payload["status"] == "running" and set(a.payload) == {"run_id", "status"}
    assert UUID(a.payload["run_id"]).version == 4
    assert a.payload["run_id"] != b.payload["run_id"]


def test_run_end_uses_most_recent_open_run_of_the_same_execution():
    runs = RunTracker()
    first = output_for(S.run_start(), 0, runs=runs).payload["run_id"]
    second = output_for(S.run_start(agent_id=AGENT_UUID), 1, runs=runs).payload["run_id"]
    end_second = output_for(S.run_end(), 2, runs=runs)
    end_first = output_for(S.run_end(status="failed", error="x"), 3, runs=runs)
    assert end_second.payload == {"run_id": second, "status": "succeeded"}
    assert end_first.payload == {"run_id": first, "status": "failed", "error": "x"}


def test_runs_of_different_executions_do_not_mix():
    r1, r2 = RunTracker(), RunTracker()
    id1 = output_for(S.run_start(), 0, runs=r1).payload["run_id"]
    id2 = output_for(S.run_start(), 0, runs=r2).payload["run_id"]
    assert output_for(S.run_end(), 1, runs=r1).payload["run_id"] == id1
    assert output_for(S.run_end(), 1, runs=r2).payload["run_id"] == id2


def test_run_end_without_open_run_still_yields_a_valid_run_id():
    out = output_for(S.run_end(), 0)
    assert UUID(out.payload["run_id"]).version == 4


def test_outputs_inside_a_run_carry_its_run_id(contracts):
    runs = RunTracker()
    run_id = output_for(S.run_start(), 0, runs=runs).payload["run_id"]
    for i, key in enumerate(
        [
            "text",
            "contact",
            "template",
            "reasoning",
            "tool_call",
            "form",
            "chart",
            "event_update",
            "thread_update_title",
            "participant_add",
        ],
        1,
    ):
        out = output_for(PRODUCTION_EVENTS[key], i, runs=runs)
        assert out.payload["run_id"] == run_id
        contracts.validate(
            "Public.OutputRequest", {"output_id": f"output:{i}", "type": out.type, "payload": out.payload, "lease_fence": FENCE}
        )
    output_for(S.run_end(), 20, runs=runs)
    assert "run_id" not in output_for(S.text("depois"), 21, runs=runs).payload


def test_no_run_id_without_tracker():
    assert "run_id" not in output_for(S.text("x"), 0).payload


@pytest.mark.parametrize("ordinal", [-1, "0", None])
def test_invalid_ordinal_is_rejected(ordinal):
    with pytest.raises(ValueError):
        output_for(S.text("x"), ordinal)


@pytest.mark.parametrize("event", PRODUCTION_EVENTS.values(), ids=PRODUCTION_EVENTS.keys())
def test_round_trip(event):
    assert agent_event_from(stored_event_for(output_for(event, 0))) == strip_none(event)


def test_round_trip_keeps_run_id_out_of_the_event():
    runs = RunTracker()
    output_for(S.run_start(), 0, runs=runs)
    out = output_for(S.text("x", author="Bot"), 1, runs=runs)
    assert agent_event_from(stored_event_for(out)) == strip_none(S.text("x", author="Bot"))


@pytest.mark.parametrize(
    "event",
    [
        {"type": "message.created", "payload": {"content": [{"type": "attachment", "artifact_id": AGENT_UUID}], "mentions": []}},
        {
            "type": "message.created",
            "payload": {"content": [{"type": "media_link", "kind": "image", "url": "https://x.example.com/a.png"}], "mentions": []},
        },
        {"type": "message.created", "payload": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "mentions": []}},
        {"type": "execution.completed", "payload": {}},
        {"type": "message.removed", "payload": {"event_id": "e"}},
        {"type": "message.created"},
        {"payload": {}},
        {},
    ],
)
def test_agent_event_from_returns_none_for_unmapped_events(event):
    assert agent_event_from(event) is None


def test_agent_event_from_reads_real_participant_added_shape():
    event = {
        "type": "participant.added",
        "payload": {
            "history_policy": "full",
            "participant": {"kind": "human", "principal_id": "auth0|u1", "display_name": "Ana", "participant_id": "p"},
        },
    }
    assert agent_event_from(event) == AgentEvent("participant_add", {"user": {"user_id": "auth0|u1", "user_name": "Ana"}})
    assert agent_event_from({"type": "participant.added", "payload": {"participant": {"kind": "agent", "principal_id": "a"}}}) is None


def test_agent_event_from_real_run_updated_shape():
    event = {
        "type": "run.updated",
        "payload": {"agent_id": AGENT_UUID, "run_id": "r", "status": "failed", "error": "x", "trigger_event_id": "e"},
    }
    assert agent_event_from(event) == AgentEvent("run_end", {"status": "failed", "error": "x"})
