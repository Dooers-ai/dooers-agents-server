"""Scenarios whose Maestro result still differs from the legacy result.

``test_parity_maestro`` runs these as ``xfail(strict=True, raises=Gap.raises)``: only the
failure the gap states counts as the gap, and any other error fails the test. It also
checks that each gap still fails as stated (``Gap.raises`` and ``Gap.deliveries``) and, for
a gap with ``Gap.masked``, that the results are equal once those declared differences are
masked on both sides: the gap is exactly what it states. Each plan removes the scenarios it
fixes, or updates a gap whose failure changed. At the end of plan 6 the set is empty.

History: measured on 2026-11-11 (task 8 of plan 1: no scenario matched); the variant
scenarios (``chart_<type>``, ``form_text_<input_type>``, ``form_<builder>``,
``whatsapp_<send type>``) were added on 2026-11-11 (final review of plan 1); plan 3 tasks 3
to 6 restated the gaps without closing one.

Measured again on 2026-11-12 (plan 3, task 7). The harness reads the Maestro thread back
through the SDK's own conversion (``MaestroExecutionPersistence``, the persistence of
``MaestroAgentMemory``), and FakeMaestro applies thread changes, issues links and delivers
``form.responded``. A ``dispatch`` turn is the agent's own publication of the person's
message (the declared name, e-mail and identity IDs; URL parts as ``media_link``). Every
execution scenario matches, except the ones below that need Core's connection context
(plan 5). The other gaps are the ``AgentServer`` ingress (plan 5: WhatsApp, ``Repository``,
``dispatch`` outside a delivery) and the public chat (plan 6).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace

from dooers.agents.server.exceptions import ThreadManagementModeError
from tests.parity.model import AGENT_ID, ParityResult
from tests.parity.scenarios_server import WHATSAPP_SENDS

Outcome = tuple[int, str, str | None]

#: The value a declared difference gets on both sides before the comparison.
MASK = "<declared>"
_ROLES = ("system_role", "organization_role", "workspace_role")


def _thread_people(result: ParityResult) -> None:
    for thread in result.outputs.get("threads", []):
        for user in [thread["owner"], *thread["users"]]:
            if user["user_id"] != AGENT_ID:
                user.update(dict.fromkeys(("user_name", "user_email", "identity_ids", *_ROLES), MASK))


def _web_chat_person(result: ParityResult) -> None:
    for event in result.events:
        if event["actor"] == "user":
            event["author"] = MASK
            event["user"].update(user_name=MASK, user_email=MASK)


def _core_roles(result: ParityResult) -> None:
    user = result.incoming.get("context", {}).get("user")
    if user is not None:
        user.update(dict.fromkeys(_ROLES, MASK))


def _media_link_size(result: ParityResult) -> None:
    for parts in [result.incoming.get("content", []), *(event["content"] or [] for event in result.events)]:
        for part in parts:
            if part.get("type") == "document" and part.get("ref_id") is None:
                part["size_bytes"] = MASK


def _agent_websocket(result: ParityResult) -> None:
    result.outputs.pop("acks", None)
    result.outputs.pop("core_validations", None)


#: Declared differences a gap may mask (name -> what differs, and the mask applied to both sides).
DECLARED_DIFFERENCES: dict[str, tuple[str, Callable[[ParityResult], None]]] = {
    "thread_people": (
        "plan 5: the people of Thread.owner/users. A Maestro participant has no name (Maestro shows 'Humano'), e-mail, "
        "identity IDs or roles; production has the ones Core (or send.add_participant) gave",
        _thread_people,
    ),
    "web_chat_person": (
        "plan 5: a web chat person's event. The agent's context has no name ('Humano') or e-mail of a web chat person; "
        "production has the ones of Core's connection context",
        _web_chat_person,
    ),
    "core_roles": (
        "plan 5: on.context.user has the User default roles; production takes them from Core's connection context",
        _core_roles,
    ),
    "media_link_size": (
        "declared, permanent: a URL document reaches Maestro as a media_link, which has no size "
        "(stored size_bytes None, handler size_bytes 0)",
        _media_link_size,
    ),
    "agent_websocket": (
        "declared, permanent: the legacy WebSocket acks and Core validation count; in Maestro mode the browser talks to "
        "Maestro (plan 4), not to the agent",
        _agent_websocket,
    ),
}


def masked(result: ParityResult, names: tuple[str, ...]) -> ParityResult:
    """A copy of a normalized result with the declared differences ``names`` masked."""
    copy = replace(
        result,
        events=deepcopy(result.events),
        incoming=deepcopy(result.incoming),
        histories=deepcopy(result.histories),
        outputs=deepcopy(result.outputs),
    )
    for name in names:
        DECLARED_DIFFERENCES[name][1](copy)
    return copy


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
    #: ``DECLARED_DIFFERENCES`` that are the whole difference (``AssertionError`` gaps).
    masked: tuple[str, ...] = ()


def _declared(*names: str, deliveries: tuple[Outcome, ...]) -> Gap:
    reason = "; ".join(DECLARED_DIFFERENCES[name][0] for name in names)
    return Gap(reason, deliveries=deliveries, masked=names)


def _server_blocked(call: str, detail: str = "") -> Gap:
    reason = f"plan 5: AgentServer.{call} raises THREAD_MANAGEMENT_MISMATCH in Maestro mode{detail}"
    return Gap(reason, raises=ThreadManagementModeError, match="THREAD_MANAGEMENT_MISMATCH")


_RESPONDED: tuple[Outcome, ...] = ((200, "responded", "responded"),)
#: A phase without a visible reply (message, form or chart) completes silent: Maestro refuses responded for it.
_SILENT: tuple[Outcome, ...] = ((200, "silent", "implicit_empty"),)
_PUBLIC_CHAT = Gap("plan 6: the Maestro-mode router acks event.create with THREAD_MANAGEMENT_MISMATCH: no events and no thread")

GAPS: dict[str, Gap] = {
    # send.* through a Maestro delivery: equal except Core's identity of the people (plan 5)
    "update_thread_title": _declared("thread_people", deliveries=_SILENT),
    "update_thread_queue": _declared("thread_people", deliveries=_SILENT),
    "add_participant": _declared("thread_people", deliveries=_SILENT),
    "form_response_submit": _declared("web_chat_person", "agent_websocket", deliveries=_RESPONDED * 2),
    "form_response_cancel": _declared("web_chat_person", "agent_websocket", deliveries=_RESPONDED * 2),
    "incoming_fields": _declared("core_roles", "media_link_size", deliveries=_RESPONDED * 2),
    # AgentServer ingress (plan 5) and public chat (plan 6)
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
}

GAP_REASONS: dict[str, str] = {name: gap.reason for name, gap in GAPS.items()}

KNOWN_GAPS: frozenset[str] = frozenset(GAPS)
