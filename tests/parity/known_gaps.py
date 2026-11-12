"""Scenarios whose Maestro result still differs from the legacy result.

``test_parity_maestro`` runs these as ``xfail(strict=True, raises=Gap.raises)``: only the
failure the gap states counts as the gap, and any other error fails the test. It also
checks that each gap still fails as stated (``Gap.raises`` and ``Gap.deliveries``). Each
plan removes the scenarios it fixes, or updates a gap whose failure changed. At the end
of plan 6 the set is empty.

Measured on 2026-11-11 (task 8 of plan 1). No scenario matches yet, ``text`` included:
every web chat scenario shares the two differences in ``_EVERY_WEB_CHAT_TURN``. Each
reason names the first difference the scenario hits. A difference that a reason calls
"masked" exists too, but an earlier failure hides it from the comparison.

The variant scenarios (``chart_<type>``, ``form_text_<input_type>``, ``form_<builder>``,
``whatsapp_<send type>``) were added and measured on 2026-11-11 (final review of plan 1,
item I2). All 22 fail on the Maestro side, each as its base scenario does.

Measured again on 2026-11-11 (plan 3, task 3): the sink publishes every production
``send.*`` except ``send.whatsapp.*``, and ``FakeMaestro`` records every output type of
index 4.4. No gap closed. The send gaps now complete ``responded`` and differ because the
legacy view maps only ``message.created`` and the fake does not apply thread changes
(plan 3, tasks 5 and 7).
"""

from __future__ import annotations

from dataclasses import dataclass

from dooers.agents.server.exceptions import ThreadManagementModeError
from tests.parity.fake_maestro import FakeMaestroRejected
from tests.parity.scenarios_send import CHART_VARIANTS, FORM_ELEMENT_BUILDERS, FORM_TEXT_INPUT_TYPES
from tests.parity.scenarios_server import WHATSAPP_SENDS

Outcome = tuple[int, str, str | None]


@dataclass(frozen=True)
class Gap:
    #: What differs today (one line, for the plan that fixes it).
    reason: str
    #: How ``run_maestro`` fails today: ``AssertionError`` when it returns a result that differs
    #: from legacy, or the exception it raises (an SDK Maestro-mode guard or a contract refusal).
    raises: type[Exception] = AssertionError
    #: Text the raised exception contains (``raises`` other than ``AssertionError``).
    match: str | None = None
    #: (HTTP status, state, reason code) of each Maestro delivery, in order (``AssertionError`` gaps).
    deliveries: tuple[Outcome, ...] = ()


_EVERY_WEB_CHAT_TURN = (
    "the legacy view of Maestro events has no run_id (run.updated is recorded but not mapped yet, plan 3 tasks 5 and 7) "
    "and the person's event has no user_email (the Maestro actor has no e-mail)"
)
_THREAD_PEOPLE = "the thread owner and users have no user_email or identity_ids (Maestro participants have neither)"
_RESPONDED: tuple[Outcome, ...] = ((200, "responded", "responded"),)


def _not_mapped(event: str) -> Gap:
    """The sink publishes the output (plan 3, task 3), but the legacy view maps only ``message.created``."""
    return Gap(
        f"{event} is recorded, but the legacy view maps only message.created, so the agent event is missing "
        f"(plan 3, tasks 5 and 7); {_EVERY_WEB_CHAT_TURN}",
        deliveries=_RESPONDED,
    )


def _server_blocked(call: str, detail: str = "") -> Gap:
    reason = f"AgentServer.{call} raises THREAD_MANAGEMENT_MISMATCH in Maestro mode{detail}"
    return Gap(reason, raises=ThreadManagementModeError, match="THREAD_MANAGEMENT_MISMATCH")


_PUBLIC_CHAT = Gap("the Maestro-mode router acks event.create with THREAD_MANAGEMENT_MISMATCH: no events and no thread")
_HISTORY = Gap(
    "turn 1 has an image part with a URL, which the Maestro message contract does not have (only text and attachment), "
    "so the publish is rejected; masked: the [maestro source ...] prefix on every history message (design 5.4)",
    raises=FakeMaestroRejected,
    match="message.create",
)
_FORM_RESPONSE = "the fake has no form.responded delivery yet (plan 3, task 7), so it rejects the form.response turn; "
_OFFLINE_MEDIA = (
    "the media URL is offline, so the SDK publishes a media_link with the agent's URL (the production part keeps it too); "
    "only the shared differences remain: "
)

GAPS: dict[str, Gap] = {
    # send.* through a Maestro delivery
    "text": Gap("only the shared differences: " + _EVERY_WEB_CHAT_TURN, deliveries=_RESPONDED),
    "text_author": Gap(
        "the agent message author is the participant name ('Assistente'), not send.text(author=...): the legacy view "
        "does not read payload.author yet (plan 3, task 7); " + _EVERY_WEB_CHAT_TURN,
        deliveries=_RESPONDED,
    ),
    "image": Gap(_OFFLINE_MEDIA + _EVERY_WEB_CHAT_TURN, deliveries=_RESPONDED),
    "document": Gap(_OFFLINE_MEDIA + _EVERY_WEB_CHAT_TURN, deliveries=_RESPONDED),
    "audio": Gap(_OFFLINE_MEDIA + _EVERY_WEB_CHAT_TURN, deliveries=_RESPONDED),
    "contact": Gap("only the shared differences: " + _EVERY_WEB_CHAT_TURN, deliveries=_RESPONDED),
    "template": Gap(
        "the agent message has no data.template: the legacy view does not map payload.template yet (plan 3, task 7); "
        + _EVERY_WEB_CHAT_TURN,
        deliveries=_RESPONDED,
    ),
    "reasoning": _not_mapped("reasoning.created"),
    "tool_call": _not_mapped("tool_call.created"),
    "tool_result": _not_mapped("tool_result.created"),
    "tool_transaction": _not_mapped("tool_transaction.created"),
    "chart": _not_mapped("chart.created"),
    **{f"chart_{chart_type}": _not_mapped(f"chart.created (chart_type={chart_type!r})") for chart_type in CHART_VARIANTS},
    "form": _not_mapped("form.created"),
    **{
        f"form_text_{input_type}": _not_mapped(f"form.created (form_text input_type={input_type!r})")
        for input_type in FORM_TEXT_INPUT_TYPES
    },
    **{f"form_{builder}": _not_mapped(f"form.created (form_{builder})") for builder in FORM_ELEMENT_BUILDERS},
    "form_response_submit": Gap(_FORM_RESPONSE + "form_data never reaches the handler", raises=FakeMaestroRejected, match="form.response"),
    "form_response_cancel": Gap(_FORM_RESPONSE + "the cancel never reaches the handler", raises=FakeMaestroRejected, match="form.response"),
    "run_failed": Gap(
        "the run.updated events (running, failed) are recorded but not mapped: Maestro outputs.runs is empty "
        "(plan 3, tasks 5 and 7); " + _EVERY_WEB_CHAT_TURN,
        deliveries=_RESPONDED,
    ),
    "run_other_agent": Gap(
        "the run.updated events (running, succeeded) of run_start(agent_id=<another agent>) are recorded, with that "
        "agent_id, but not mapped: Maestro outputs.runs is empty (plan 3, tasks 5 and 7); " + _EVERY_WEB_CHAT_TURN,
        deliveries=_RESPONDED,
    ),
    "update_user_event": Gap(
        "message.updated is recorded but not applied: the person's message keeps its text in the legacy view "
        "(plan 3, tasks 5 and 7); " + _EVERY_WEB_CHAT_TURN,
        deliveries=_RESPONDED,
    ),
    "update_thread_title": Gap(
        "thread.updated is recorded, but the fake does not apply it: the title stays 'Nova conversa' (plan 3, task 7); "
        f"{_THREAD_PEOPLE}; {_EVERY_WEB_CHAT_TURN}",
        deliveries=_RESPONDED,
    ),
    "update_thread_queue": Gap(
        "thread.updated (queue) is recorded, but the fake does not apply it and the legacy view has no thread.update event "
        f"(plan 3, task 7); {_THREAD_PEOPLE}; {_EVERY_WEB_CHAT_TURN}",
        deliveries=_RESPONDED,
    ),
    "add_participant": Gap(
        "participant.added is recorded, but the fake does not add Bia to the thread (plan 3, task 7); "
        f"{_THREAD_PEOPLE}; {_EVERY_WEB_CHAT_TURN}",
        deliveries=_RESPONDED,
    ),
    # AgentServer, channels and public chat
    "server_dispatch": _server_blocked("dispatch"),
    "server_ingest": _server_blocked("ingest"),
    "repository_crud": _server_blocked("repository"),
    "memory_by_thread_id": _server_blocked("dispatch", " (first call of the script; memory is never reached)"),
    "chat_upload": _server_blocked("chat_upload"),
    **{
        f"whatsapp_{send_type}": _server_blocked("dispatch", f" (the WhatsApp ingress); send.whatsapp.{send_type} is never reached")
        for send_type in WHATSAPP_SENDS
    },
    "whatsapp_receipt": _server_blocked("dispatch", " (first call of the script); no receipt is applied"),
    "public_chat_guest": _PUBLIC_CHAT,
    "public_chat_authenticated": _PUBLIC_CHAT,
    # AgentIncoming and AgentMemory
    "incoming_fields": Gap(
        "turn 2 has image, document, audio and contact parts, which the Maestro message contract does not have, so the "
        "publish is rejected; masked: channel 'maestro', no "
        "thread_title/queue/queues, rag on_behalf False (design 5.3)",
        raises=FakeMaestroRejected,
        match="message.create",
    ),
    **{
        f"history_{fmt}": _HISTORY
        for fmt in ("openai", "anthropic", "google", "cohere", "voyage", "langchain", "openai_completions", "openai_responses")
    },
}

GAP_REASONS: dict[str, str] = {name: gap.reason for name, gap in GAPS.items()}

KNOWN_GAPS: frozenset[str] = frozenset(GAPS)
