"""Parity suite, legacy side: what thread_management="agent" produces for each SDK function.

The normalized results are the ground truth the Maestro side (task 8) is compared with.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.parity.harness import ParityResult, run_legacy
from tests.parity.normalize import DATE_FIELDS, ID_FIELDS, URL_FIELD, normalize
from tests.parity.scenarios import SCENARIOS
from tests.parity.subjects import SUBJECTS, assert_subject

HISTORY_FORMATS = (
    "openai",
    "anthropic",
    "google",
    "cohere",
    "voyage",
    "langchain",
    "openai_completions",
    "openai_responses",
)

EXPECTED_SCENARIOS = {
    "text",
    "text_author",
    "image",
    "document",
    "audio",
    "media_upload",
    "contact",
    "template",
    "reasoning",
    "tool_call",
    "big_integers",
    "tool_result",
    "tool_transaction",
    "form",
    "form_response_submit",
    "form_response_cancel",
    "chart",
    "chart_bar_horizontal",
    "chart_stacked_bar",
    "chart_line",
    "chart_area",
    "chart_pie",
    "chart_donut",
    "chart_scatter",
    "form_text_text",
    "form_text_password",
    "form_text_email",
    "form_text_number",
    "form_text_date",
    "form_textarea",
    "form_select",
    "form_radio",
    "form_checkbox",
    "form_file",
    "content_policy_denied",
    "content_policy_denied_message",
    "content_policy_allowed",
    "run_failed",
    "run_other_agent",
    "run_agent_label",
    "run_agent_label_failed",
    "update_user_event",
    "update_thread_title",
    "update_thread_queue",
    "update_thread_queue_unknown",
    "update_thread_queue_malformed",
    "update_thread_queue_normalized",
    "add_participant",
    "whatsapp_text",
    "whatsapp_image",
    "whatsapp_document",
    "whatsapp_audio",
    "whatsapp_contact",
    "whatsapp_template",
    "whatsapp_receipt",
    "whatsapp_history_raw",
    "public_chat_form_response",
    "server_dispatch",
    "thread_key_dispatch",
    "public_chat_thread_ids",
    "server_ingest",
    "repository_crud",
    "memory_by_thread_id",
    "chat_upload",
    "public_chat_guest",
    "public_chat_authenticated",
    "incoming_fields",
    *(f"history_{fmt}" for fmt in HISTORY_FORMATS),
}


def test_scenario_names_match_the_brief():
    assert set(SCENARIOS) == EXPECTED_SCENARIOS


def test_every_scenario_declares_its_subject():
    assert set(SUBJECTS) == set(SCENARIOS)
    assert all(subject.non_empty for subject in SUBJECTS.values())


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_legacy_scenario_is_stable(name):
    raw = await run_legacy(name)
    first = normalize(raw)
    second = normalize(await run_legacy(name))
    assert first == second
    # Replaces the plan's `assert first.events`: each scenario's subject (events, incoming,
    # histories or outputs) must hold what production produced, so an empty capture on both
    # sides can never count as parity.
    assert_subject(name, raw)


def test_normalize_keeps_author():
    a = ParityResult(events=[{"type": "message", "data": {"author": "A"}}], incoming={}, histories={})
    b = ParityResult(events=[{"type": "message", "data": {"author": "B"}}], incoming={}, histories={})
    assert normalize(a) != normalize(b)


def test_normalize_treats_ref_id_as_an_id():
    def result(ref_id, author="Ana Souza"):
        part = {"type": "image", "ref_id": ref_id, "url": None}
        return _result(
            _event(actor="user", author=author, content=[part]),
            outputs={"chat_upload": {"ref_id": ref_id, "filename": "foto.png"}},
        )

    assert normalize(result("ref-a")) == normalize(result("ref-b"))
    assert normalize(result("ref-a")) != normalize(result("ref-b", author="Outra Pessoa"))
    normalized = normalize(result("ref-a"))
    assert normalized.outputs["chat_upload"]["ref_id"] == normalized.events[0]["content"][0]["ref_id"]
    assert normalized.outputs["chat_upload"]["ref_id"].startswith("<id:")


def test_ignored_field_list_is_fixed():
    assert ID_FIELDS == frozenset({"id", "event_id", "thread_id", "run_id", "ref_id"})
    assert DATE_FIELDS == frozenset({"created_at", "updated_at", "last_event_at"})
    assert URL_FIELD == "url"


def _event(**overrides):
    event = {
        "id": "event-a",
        "thread_id": "thread-a",
        "run_id": None,
        "type": "message",
        "actor": "assistant",
        "author": "Assistente",
        "user": {"user_id": "", "user_name": None},
        "content": [{"type": "text", "text": "oi"}],
        "data": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    event.update(overrides)
    return event


def _result(*events, **kwargs):
    return ParityResult(
        events=list(events),
        incoming=kwargs.get("incoming", {}),
        histories={},
        outputs=kwargs.get("outputs", {}),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "reasoning"),
        ("actor", "user"),
        ("author", None),
        ("author", "Outro"),
        ("user", {"user_id": "user-1", "user_name": None}),
        ("content", [{"type": "text", "text": "olá"}]),
        ("content", None),
        ("data", {"delivery": {"status": "accepted"}}),
        ("run_id", "run-a"),
    ],
)
def test_normalize_detects_changes_outside_ignored_fields(field, value):
    assert normalize(_result(_event())) != normalize(_result(_event(**{field: value})))


def test_normalize_replaces_ids_dates_and_hosted_urls():
    a = _result(
        _event(
            id="event-a",
            thread_id="thread-a",
            content=[{"type": "image", "url": "https://cdn-a.example/x.png?sig=1"}],
            data={"form_event_id": "event-a"},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        incoming={"context": {"thread_created_at": datetime(2026, 1, 1, tzinfo=UTC), "thread_id": "thread-a"}},
    )
    b = _result(
        _event(
            id="event-b",
            thread_id="thread-b",
            content=[{"type": "image", "url": "https://cdn-b.example/y.png"}],
            data={"form_event_id": "event-b"},
            created_at=datetime(2027, 5, 5, tzinfo=UTC),
        ),
        incoming={"context": {"thread_created_at": datetime(2027, 5, 5, tzinfo=UTC), "thread_id": "thread-b"}},
    )
    normalized = normalize(a)
    assert normalized == normalize(b)
    event = normalized.events[0]
    assert event["id"].startswith("<id:")
    assert event["data"]["form_event_id"] == event["id"]
    assert event["thread_id"] == normalized.incoming["context"]["thread_id"]
    assert event["created_at"] == "<ts>"
    assert normalized.incoming["context"]["thread_created_at"] == "<ts>"
    assert event["content"][0]["url"].startswith("<url:")


def test_normalize_keeps_relative_urls_and_missing_values():
    a = _result(_event(content=[{"type": "image", "url": "/files/x.png"}], created_at=None))
    b = _result(_event(content=[{"type": "image", "url": "/files/y.png"}], created_at=None))
    assert normalize(a) != normalize(b)
    assert normalize(a).events[0]["created_at"] is None


def test_normalize_detects_a_broken_reference():
    good = _result(_event(id="event-a"), _event(id="event-b", data={"form_event_id": "event-a"}))
    broken = _result(_event(id="event-a"), _event(id="event-b", data={"form_event_id": "event-b"}))
    assert normalize(good) != normalize(broken)
