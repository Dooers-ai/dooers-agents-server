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
    "Maestro events have no run_id (the v1 contract has no Run, design 4.2) and the person's event has no "
    "user_email (the Maestro actor has no e-mail)"
)
_THREAD_PEOPLE = "the thread owner and users have no user_email or identity_ids (Maestro participants have neither)"
_RESPONDED: tuple[Outcome, ...] = ((200, "responded", "responded"),)
_UNSUPPORTED: tuple[Outcome, ...] = ((200, "failed", "unsupported_capability"),)
_FORBIDDEN: tuple[Outcome, ...] = ((403, "running", None),)


def _sink_refuses(call: str) -> Gap:
    return Gap(
        f"the Maestro sink refuses {call} (UNSUPPORTED_CAPABILITY, delivery failed): no agent event; {_EVERY_WEB_CHAT_TURN}",
        deliveries=_UNSUPPORTED,
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
_FORM_REFUSED = "MaestroSend.form without suspension raises UNSUPPORTED_CAPABILITY: no form event; " + _EVERY_WEB_CHAT_TURN
_FORM_RESPONSE = "send.form is refused, so no form exists and the fake rejects the form.response turn (the contract has no legacy form); "

GAPS: dict[str, Gap] = {
    # send.* through a Maestro delivery
    "text": Gap("only the shared differences: " + _EVERY_WEB_CHAT_TURN, deliveries=_RESPONDED),
    "text_author": _sink_refuses("send.text(author=...)"),
    "image": _sink_refuses("send.image"),
    "document": _sink_refuses("send.document"),
    "audio": _sink_refuses("send.audio"),
    "contact": _sink_refuses("send.contact"),
    "template": _sink_refuses("send.template"),
    "reasoning": _sink_refuses("send.reasoning"),
    "tool_call": _sink_refuses("send.tool_call"),
    "tool_result": _sink_refuses("send.tool_result"),
    "tool_transaction": _sink_refuses("send.tool_transaction"),
    "chart": _sink_refuses("send.chart"),
    **{f"chart_{chart_type}": _sink_refuses(f"send.chart(chart_type={chart_type!r})") for chart_type in CHART_VARIANTS},
    "form": Gap(_FORM_REFUSED, deliveries=_UNSUPPORTED),
    **{
        f"form_text_{input_type}": Gap(f"form_text(input_type={input_type!r}): {_FORM_REFUSED}", deliveries=_UNSUPPORTED)
        for input_type in FORM_TEXT_INPUT_TYPES
    },
    **{f"form_{builder}": Gap(f"form_{builder}: {_FORM_REFUSED}", deliveries=_UNSUPPORTED) for builder in FORM_ELEMENT_BUILDERS},
    "form_response_submit": Gap(_FORM_RESPONSE + "form_data never reaches the handler", raises=FakeMaestroRejected, match="form.response"),
    "form_response_cancel": Gap(_FORM_RESPONSE + "the cancel never reaches the handler", raises=FakeMaestroRejected, match="form.response"),
    "run_failed": Gap(
        "the sink refuses run_end(status='failed') after the text (handler_contract_error) and Maestro records no Run "
        "(runs: []); " + _EVERY_WEB_CHAT_TURN,
        deliveries=((200, "failed", "handler_contract_error"),),
    ),
    "run_other_agent": Gap(
        "the sink refuses run_start(agent_id=<another agent>) with FORBIDDEN (dispatch answered 403, no complete): "
        "no agent event and no Run; " + _EVERY_WEB_CHAT_TURN,
        deliveries=_FORBIDDEN,
    ),
    "update_user_event": Gap(
        "MaestroSend.update_user_event raises UNSUPPORTED_CAPABILITY: the person's message keeps its text; " + _EVERY_WEB_CHAT_TURN,
        deliveries=_UNSUPPORTED,
    ),
    "update_thread_title": Gap(
        "MaestroSend.update_thread raises FORBIDDEN (dispatch answered 403, no complete): the title stays 'Nova conversa'; "
        f"{_THREAD_PEOPLE}; {_EVERY_WEB_CHAT_TURN}",
        deliveries=_FORBIDDEN,
    ),
    "update_thread_queue": Gap(
        "MaestroSend.update_thread has no queue parameter (TypeError, handler_error) and the Maestro thread has no queue; "
        f"{_THREAD_PEOPLE}; {_EVERY_WEB_CHAT_TURN}",
        deliveries=((200, "failed", "handler_error"),),
    ),
    "add_participant": Gap(
        "MaestroSend.add_participant raises FORBIDDEN (dispatch answered 403, no complete): Bia is not added; "
        f"{_THREAD_PEOPLE}; {_EVERY_WEB_CHAT_TURN}",
        deliveries=_FORBIDDEN,
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
        "publish is rejected; turn 1 hits update_thread(queue=...) TypeError; masked: channel 'maestro', no "
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
