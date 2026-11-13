"""What each scenario is about, checked on the raw ``run_legacy`` result.

The stability test alone passes when a capture is empty on both runs. Each entry here
names the parts of ``ParityResult`` that must be non-empty (``non_empty``) and checks
that they hold what production produces (``check``). Every scenario persists events,
so ``"events"`` is in every entry; ``repository_crud`` writes them with
``Repository.create_event`` and ``server_ingest`` with ``AgentServer.ingest``.

Paths: ``events``, ``incoming.<key>``, ``histories.<format>``, ``outputs.<key>``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tests.parity.model import (
    AGENT_ID,
    OTHER_AGENT_ID,
    USER,
    WHATSAPP_OUTBOUND_ID,
    WORKSPACE_ID,
    ParityResult,
)
from tests.parity.scenarios_incoming import HISTORY_FORMATS
from tests.parity.scenarios_send import CHART_VARIANTS, FORM_TEXT_INPUT_TYPES
from tests.parity.scenarios_server import PNG_BYTES, WHATSAPP_SENDS

Check = Callable[[ParityResult], None]


@dataclass(frozen=True)
class Subject:
    non_empty: tuple[str, ...]
    check: Check | None = None


def _value(result: ParityResult, path: str) -> Any:
    head, _, rest = path.partition(".")
    value: Any = getattr(result, head)
    for key in rest.split(".") if rest else ():
        assert isinstance(value, dict) and key in value, f"{path}: missing {key!r}"
        value = value[key]
    return value


def assert_subject(name: str, result: ParityResult) -> None:
    subject = SUBJECTS[name]
    for path in subject.non_empty:
        value = _value(result, path)
        assert value not in (None, "", [], {}), f"{name}: {path} is empty"
    if subject.check is not None:
        subject.check(result)


# -- Reusable checks --


def _all(*checks: Check) -> Check:
    def check(result: ParityResult) -> None:
        for one in checks:
            one(result)

    return check


def _types(*expected: str) -> Check:
    def check(result: ParityResult) -> None:
        assert [e["type"] for e in result.events] == list(expected)

    return check


def _last_part(part_type: str, **fields: Any) -> Check:
    def check(result: ParityResult) -> None:
        last = result.events[-1]
        assert last["actor"] == "assistant"
        part = last["content"][0]
        assert part["type"] == part_type
        for key, value in fields.items():
            assert part[key] == value, f"{key}: {part[key]!r} != {value!r}"

    return check


def _last_data(**fields: Any) -> Check:
    def check(result: ParityResult) -> None:
        data = result.events[-1]["data"]
        for key, value in fields.items():
            assert data[key] == value, f"{key}: {data[key]!r} != {value!r}"

    return check


def _last_author(author: str | None) -> Check:
    def check(result: ParityResult) -> None:
        assert result.events[-1]["author"] == author

    return check


def _run(status: str, agent_id: str | None, error: str | None) -> Check:
    def check(result: ParityResult) -> None:
        (run,) = result.outputs["runs"]
        assert (run["status"], run["agent_id"], run["error"]) == (status, agent_id, error)
        assert all(e["run_id"] == run["id"] for e in result.events)

    return check


def _thread(**fields: Any) -> Check:
    def check(result: ParityResult) -> None:
        (thread,) = result.outputs["threads"]
        for key, value in fields.items():
            assert thread[key] == value, f"{key}: {thread[key]!r} != {value!r}"

    return check


def _participant(user_id: str) -> Check:
    def check(result: ParityResult) -> None:
        (thread,) = result.outputs["threads"]
        assert user_id in [u["user_id"] for u in thread["users"]]

    return check


def _form_elements(result: ParityResult) -> None:
    data = result.events[-1]["data"]
    assert [element["type"] for element in data["elements"]] == [
        "text_input",
        "textarea_input",
        "select_input",
        "radio_input",
        "checkbox_input",
        "file_input",
    ]
    assert (data["submit_label"], data["cancel_label"], data["size"]) == ("Confirmar", "Agora não", "large")


def _single_form(*elements: dict[str, Any]) -> Check:
    """The form event holds exactly these elements (type plus the variant field)."""

    def check(result: ParityResult) -> None:
        data = result.events[-1]["data"]
        assert [{key: element[key] for key in expected} for element, expected in zip(data["elements"], elements, strict=True)] == list(
            elements
        )

    return check


_FORM_BUILDER_ELEMENTS: dict[str, tuple[dict[str, Any], ...]] = {
    "textarea": ({"type": "textarea_input", "rows": 4},),
    "select": ({"type": "select_input", "default": "pro"},),
    "radio": ({"type": "radio_input", "variant": "native"}, {"type": "radio_input", "variant": "button"}),
    "checkbox": ({"type": "checkbox_input", "variant": "native"}, {"type": "checkbox_input", "variant": "button"}),
    "file": ({"type": "file_input", "multiple": True},),
}


def _revised_user_event(result: ParityResult) -> None:
    (event,) = result.events
    assert event["actor"] == "user"
    assert event["content"] == [{"type": "text", "text": "Olá (mensagem revisada)"}]


def _form_response(*, cancelled: bool, values: dict[str, Any]) -> Check:
    """The second invocation got the ``form.response`` fields the way production sets them."""

    def check(result: ParityResult) -> None:
        forms = [e["id"] for e in result.events if e["type"] == "form"]
        assert len(forms) == 1
        assert [e["type"] for e in result.events].count("form.response") == 1
        assert result.incoming["form_event_id"] == forms[0]
        assert result.incoming["form_cancelled"] is cancelled
        assert result.incoming["form_data"] == values
        assert all(ack["ok"] for ack in result.outputs["acks"])

    return check


def _incoming_fields(result: ParityResult) -> None:
    context = result.incoming["context"]
    assert context["channel"] == "dooers-platform"
    assert context["user"]["user_id"] == USER.user_id
    assert context["workspace_id"] == WORKSPACE_ID
    assert context["thread_title"] == "Pedido 42"
    assert isinstance(context["thread_created_at"], datetime)
    assert context["queue"] == "suporte"
    assert [queue["slug"] for queue in context["queues"]] == ["suporte", "vendas"]
    assert [part["type"] for part in result.incoming["content"]] == ["text", "image", "document", "audio", "contact"]


_IMAGE_TEXT = "[image: foto.png]"


def _has_image_entry(fmt: str, message: dict[str, Any]) -> bool:
    if fmt in ("openai", "anthropic", "voyage"):
        return _IMAGE_TEXT in message["content"]
    if fmt == "google":
        return any(_IMAGE_TEXT in part["text"] for part in message["parts"])
    if fmt == "cohere":
        return _IMAGE_TEXT in message["message"]
    image_type = {"langchain": "image", "openai_completions": "image_url", "openai_responses": "input_image"}[fmt]
    return any(part.get("type") == image_type for part in message["content"])


def _history(fmt: str) -> Check:
    def check(result: ParityResult) -> None:
        history = result.histories[fmt]
        assert len(history) == 3, history
        assert _has_image_entry(fmt, history[0]), history[0]
        assert "Recebi a foto." in repr(history[1])

    return check


def _media_upload(result: ParityResult) -> None:
    """Production stores each media URL with the send's metadata; the next turn reads them back."""
    assert [(e["actor"], e["content"][0]["type"]) for e in result.events[1:4]] == [
        ("assistant", "image"),
        ("assistant", "audio"),
        ("assistant", "document"),
    ]
    image, audio, document = (e["content"][0] for e in result.events[1:4])
    assert (image["url"], image["filename"], image["ref_id"], image["alt"]) == (
        "https://cdn.example.com/online/grafico.png",
        None,
        None,
        "Gráfico de vendas",
    )
    assert (audio["filename"], audio["duration"], document["filename"], document["size_bytes"]) == (None, 3.5, "relatorio.pdf", None)
    for fmt in ("openai", "anthropic"):
        texts = [message["content"] for message in result.histories[fmt]]
        assert texts[1:4] == ["[image: image]", "[audio: audio, audio/ogg, 3.5s]", "[document: relatorio.pdf]"], texts


def _server_dispatch(result: ParityResult) -> None:
    calls = result.outputs["dispatch"]
    assert [call["is_new_thread"] for call in calls] == [True, False, True]
    assert calls[0]["thread_id"] == calls[1]["thread_id"] != calls[2]["thread_id"]
    assert all([e["send_type"] for e in call["collected"]] == ["run_start", "text", "run_end"] for call in calls)


def _server_ingest(result: ParityResult) -> None:
    calls = result.outputs["ingest"]
    assert [call["call"] for call in calls] == ["actor_user_new_thread", "default_actor", "actor_user_existing_thread"]
    assert calls[0]["thread_id"] == calls[2]["thread_id"]
    assert calls[1] == {"call": "default_actor", "raised": "ValidationError"}
    assert len(result.events) == 2


_REPOSITORY_KEYS = {
    "create_thread",
    "get_thread",
    "update_thread",
    "update_missing_thread",
    "create_event",
    "list_events_default",
    "list_events_user_asc",
    "get_event",
    "list_events_without_thread",
    "get_removed_thread",
    "list_threads",
    "list_runs",
    "get_missing_run",
    "get_settings",
}


def _repository(result: ParityResult) -> None:
    repo = result.outputs["repository"]
    assert set(repo) == _REPOSITORY_KEYS
    assert repo["update_thread"]["title"] == "Título novo"
    assert len(repo["list_events_default"]) == 2
    assert len(repo["list_threads"]) == 1
    assert repo["get_settings"] == {"tom": "formal"}


def _memory(result: ParityResult) -> None:
    memory = result.outputs["memory"]
    assert memory["get_thread"]["agent_id"] == AGENT_ID
    assert len(memory["get_history_raw"]) == 3
    assert len(memory["get_history"]) == 3
    assert [a["direction"] for a in memory["list_artifacts"]] == ["out", "in"]
    assert memory["missing_get_thread"] is None


def _chat_upload(result: ParityResult) -> None:
    upload = result.outputs["chat_upload"]
    assert upload["size_bytes"] == len(PNG_BYTES)
    assert result.events[0]["content"][1]["ref_id"] == upload["ref_id"]
    assert result.incoming["content"][1]["data"] == PNG_BYTES
    assert result.outputs["chat_upload_errors"] == {"empty_file": "empty file", "blank_agent_id": "agent_id is required"}


def _whatsapp_sent(send_type: str) -> Check:
    def check(result: ParityResult) -> None:
        (request,) = result.outputs["whatsapp_requests"]
        assert request["signed"] is True
        assert request["body"]["send_type"] == send_type
        delivery = result.events[-1]["data"]["delivery"]
        assert delivery["wa_message_id"] == WHATSAPP_OUTBOUND_ID

    return check


def _whatsapp_receipt(result: ParityResult) -> None:
    receipts = result.outputs["receipts"]
    assert [r["applied"] for r in receipts] == [True, True, True, False, True]
    assert [r["delivery"][0]["status"] for r in receipts] == ["delivered", "read", "read", "read", "failed"]


def _public_chat(connection_type: str, owner_id: str) -> Check:
    def check(result: ParityResult) -> None:
        assert result.outputs["core_validations"] == 1
        assert all(ack["ok"] for ack in result.outputs["acks"])
        (thread,) = result.outputs["threads"]
        assert (thread["owner"]["user_id"], thread["owner"]["connection_type"]) == (owner_id, connection_type)
        assert [e["user"]["user_id"] for e in result.events if e["actor"] == "user"] == [owner_id, owner_id]
        assert all(e["data"]["delivery"]["channel"] == "dooers-public-chat" for e in result.events if e["actor"] == "assistant")

    return check


_TWO_MESSAGES = _types("message", "message")

SUBJECTS: dict[str, Subject] = {
    "text": Subject(("events",), _all(_TWO_MESSAGES, _last_part("text"), _last_author("Assistente"))),
    "text_author": Subject(("events",), _all(_TWO_MESSAGES, _last_author("Agente Financeiro"))),
    "image": Subject(("events",), _all(_TWO_MESSAGES, _last_part("image", mime_type="image/png"))),
    "document": Subject(("events",), _all(_TWO_MESSAGES, _last_part("document", filename="relatorio.pdf"))),
    "audio": Subject(("events",), _all(_TWO_MESSAGES, _last_part("audio", duration=3.5))),
    "media_upload": Subject(("events", "histories.openai", "histories.anthropic"), _media_upload),
    "contact": Subject(("events",), _all(_TWO_MESSAGES, _last_part("contact", display_name="Suporte Dooers"))),
    "template": Subject(("events",), _all(_TWO_MESSAGES, _last_part("text", text="[template: order_update (pt_BR)]"))),
    "reasoning": Subject(("events",), _types("message", "reasoning")),
    "tool_call": Subject(("events",), _all(_types("message", "tool.call"), _last_data(name="buscar_pedido"))),
    "tool_result": Subject(("events",), _all(_types("message", "tool.result"), _last_data(result={"status": "enviado"}))),
    "tool_transaction": Subject(("events",), _types("message", "tool.transaction")),
    "form": Subject(("events",), _all(_types("message", "form"), _form_elements)),
    "form_response_submit": Subject(
        ("events", "incoming.form_data", "incoming.form_event_id", "outputs.acks"),
        _form_response(cancelled=False, values={"nome": "Ana", "plano": "pro"}),
    ),
    "form_response_cancel": Subject(
        ("events", "incoming.form_event_id", "incoming.form_cancelled", "outputs.acks"),
        _form_response(cancelled=True, values={}),
    ),
    "chart": Subject(("events",), _all(_types("message", "chart"), _last_data(chart_type="bar"))),
    **{
        f"chart_{chart_type}": Subject(("events",), _all(_types("message", "chart"), _last_data(chart_type=chart_type)))
        for chart_type in CHART_VARIANTS
    },
    **{
        f"form_text_{input_type}": Subject(
            ("events",), _all(_types("message", "form"), _single_form({"type": "text_input", "input_type": input_type}))
        )
        for input_type in FORM_TEXT_INPUT_TYPES
    },
    **{
        f"form_{builder}": Subject(("events",), _all(_types("message", "form"), _single_form(*elements)))
        for builder, elements in _FORM_BUILDER_ELEMENTS.items()
    },
    "content_policy_denied": Subject(
        ("events",),
        _all(
            _TWO_MESSAGES,
            _last_author("Assistente"),
            _last_part("text", text="This agent does not accept this attachment type in chat (text). Allowed: image."),
        ),
    ),
    "content_policy_denied_message": Subject(
        ("events",), _all(_TWO_MESSAGES, _last_part("text", text="Só aceito document, image, mas chegou text."))
    ),
    "content_policy_allowed": Subject(("events",), _all(_TWO_MESSAGES, _last_part("text", text="Mensagem de texto aceita."))),
    "run_failed": Subject(("events", "outputs.runs"), _run("failed", None, "Serviço de pedidos indisponível")),
    "run_other_agent": Subject(("events", "outputs.runs"), _run("succeeded", OTHER_AGENT_ID, None)),
    "update_user_event": Subject(("events",), _revised_user_event),
    "update_thread_title": Subject(("events", "outputs.threads"), _thread(title="Pedido 42")),
    "update_thread_queue": Subject(("events", "outputs.threads"), _all(_types("message", "thread.update"), _thread(queue="suporte"))),
    # A refused queue changes nothing but logs a warning; the title still applies (pipeline.py thread_update).
    "update_thread_queue_unknown": Subject(("events", "outputs.threads"), _all(_types("message"), _thread(title="Pedido 7", queue=None))),
    "update_thread_queue_malformed": Subject(("events", "outputs.threads"), _all(_types("message"), _thread(title="Pedido 7", queue=None))),
    "update_thread_queue_normalized": Subject(
        ("events", "outputs.threads"), _all(_types("message", "thread.update"), _thread(title="Pedido 7", queue="suporte"))
    ),
    "add_participant": Subject(("events", "outputs.threads"), _participant("user-2")),
    **{
        f"whatsapp_{send_type}": Subject(("events", "outputs.whatsapp_requests"), _whatsapp_sent(send_type)) for send_type in WHATSAPP_SENDS
    },
    "whatsapp_receipt": Subject(("events", "outputs.receipts", "outputs.whatsapp_requests"), _whatsapp_receipt),
    "server_dispatch": Subject(("events", "outputs.dispatch"), _server_dispatch),
    "server_ingest": Subject(("events", "outputs.ingest"), _server_ingest),
    "repository_crud": Subject(("events", "outputs.repository"), _repository),
    "memory_by_thread_id": Subject(("events", "outputs.memory"), _memory),
    "chat_upload": Subject(("events", "outputs.chat_upload", "outputs.chat_upload_errors", "incoming.content"), _chat_upload),
    "public_chat_guest": Subject(("events", "outputs.acks", "outputs.threads"), _public_chat("guest", "guest:abc123")),
    "public_chat_authenticated": Subject(("events", "outputs.acks", "outputs.threads"), _public_chat("public_authenticated", "user-42")),
    "incoming_fields": Subject(("events", "incoming.context", "incoming.content"), _incoming_fields),
    **{f"history_{fmt}": Subject(("events", f"histories.{fmt}"), _history(fmt)) for fmt in HISTORY_FORMATS},
}
