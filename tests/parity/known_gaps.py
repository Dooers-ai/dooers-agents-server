"""Scenarios whose Maestro result still differs from the legacy result.

``test_parity_maestro`` runs these as ``xfail(strict=True)``. Each plan removes the
scenarios it fixes. At the end of plan 6 the set is empty.

Measured on 2026-11-11 (task 8 of plan 1). No scenario matches yet, ``text`` included:
every web chat scenario shares the two differences in ``_EVERY_WEB_CHAT_TURN``. Each
reason names the first difference the scenario hits. A difference that a reason says
is "masked" exists too, but an earlier failure hides it from the comparison.
"""

from __future__ import annotations

_EVERY_WEB_CHAT_TURN = (
    "Maestro events have no run_id (the v1 contract has no Run, design 4.2) and the person's event has no "
    "user_email (the Maestro actor has no e-mail)"
)
_SINK_REFUSES = "the Maestro sink refuses {call} (UNSUPPORTED_CAPABILITY, delivery failed): no agent event; " + _EVERY_WEB_CHAT_TURN
_SERVER_BLOCKED = "AgentServer.{call} raises THREAD_MANAGEMENT_MISMATCH in Maestro mode"
_PUBLIC_CHAT = "the Maestro-mode router acks event.create with THREAD_MANAGEMENT_MISMATCH: no events and no thread"
_HISTORY = (
    "turn 1 has an image part with a URL, which the Maestro message contract does not have (only text and attachment), "
    "so the publish is rejected; masked: the [maestro source ...] prefix on every history message (design 5.4)"
)

#: Scenario -> what differs today (one line, for the plan that fixes it).
GAP_REASONS: dict[str, str] = {
    # send.* through a Maestro delivery
    "text": "only the shared differences: " + _EVERY_WEB_CHAT_TURN,
    "text_author": _SINK_REFUSES.format(call="send.text(author=...)"),
    "image": _SINK_REFUSES.format(call="send.image"),
    "document": _SINK_REFUSES.format(call="send.document"),
    "audio": _SINK_REFUSES.format(call="send.audio"),
    "contact": _SINK_REFUSES.format(call="send.contact"),
    "template": _SINK_REFUSES.format(call="send.template"),
    "reasoning": _SINK_REFUSES.format(call="send.reasoning"),
    "tool_call": _SINK_REFUSES.format(call="send.tool_call"),
    "tool_result": _SINK_REFUSES.format(call="send.tool_result"),
    "tool_transaction": _SINK_REFUSES.format(call="send.tool_transaction"),
    "chart": _SINK_REFUSES.format(call="send.chart"),
    "form": "MaestroSend.form without suspension raises UNSUPPORTED_CAPABILITY: no form event; " + _EVERY_WEB_CHAT_TURN,
    "form_response_submit": "send.form is refused, so no form exists and the fake rejects the form.response turn "
    "(the contract has no legacy form); form_data never reaches the handler",
    "form_response_cancel": "send.form is refused, so no form exists and the fake rejects the form.response turn "
    "(the contract has no legacy form); the cancel never reaches the handler",
    "run_failed": "the sink refuses run_end(status='failed') after the text (handler_contract_error) and Maestro records "
    "no Run (runs: []); " + _EVERY_WEB_CHAT_TURN,
    "run_other_agent": "the sink refuses run_start(agent_id='agent-2') with FORBIDDEN (dispatch answered 403, no complete): "
    "no agent event and no Run; " + _EVERY_WEB_CHAT_TURN,
    "update_user_event": "MaestroSend.update_user_event raises UNSUPPORTED_CAPABILITY: the person's message keeps its text; "
    + _EVERY_WEB_CHAT_TURN,
    "update_thread_title": "MaestroSend.update_thread raises FORBIDDEN (dispatch answered 403, no complete): the title "
    "stays 'Nova conversa'; " + _EVERY_WEB_CHAT_TURN,
    "update_thread_queue": "MaestroSend.update_thread has no queue parameter (TypeError, handler_error) and the Maestro "
    "thread has no queue; " + _EVERY_WEB_CHAT_TURN,
    "add_participant": "MaestroSend.add_participant raises FORBIDDEN (dispatch answered 403, no complete): Bia is not "
    "added; " + _EVERY_WEB_CHAT_TURN,
    # AgentServer, channels and public chat
    "server_dispatch": _SERVER_BLOCKED.format(call="dispatch"),
    "server_ingest": _SERVER_BLOCKED.format(call="ingest"),
    "repository_crud": _SERVER_BLOCKED.format(call="repository"),
    "memory_by_thread_id": _SERVER_BLOCKED.format(call="dispatch") + " (first call of the script; memory is never reached)",
    "chat_upload": _SERVER_BLOCKED.format(call="chat_upload"),
    "whatsapp_text": _SERVER_BLOCKED.format(call="dispatch") + " (the WhatsApp ingress); send.whatsapp.* is never reached",
    "whatsapp_receipt": _SERVER_BLOCKED.format(call="dispatch") + " (first call of the script); no receipt is applied",
    "public_chat_guest": _PUBLIC_CHAT,
    "public_chat_authenticated": _PUBLIC_CHAT,
    # AgentIncoming and AgentMemory
    "incoming_fields": "turn 2 has image, document, audio and contact parts, which the Maestro message contract does not "
    "have, so the publish is rejected; turn 1 hits update_thread(queue=...) TypeError; masked: channel 'maestro', no "
    "thread_title/queue/queues, rag on_behalf False (design 5.3)",
    **{
        f"history_{fmt}": _HISTORY
        for fmt in ("openai", "anthropic", "google", "cohere", "voyage", "langchain", "openai_completions", "openai_responses")
    },
}

KNOWN_GAPS: frozenset[str] = frozenset(GAP_REASONS)
