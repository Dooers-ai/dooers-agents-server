"""Scenarios whose Maestro result still differs from the legacy result.

``test_parity_maestro`` runs these as ``xfail(strict=True, raises=Gap.raises)``: only the
failure the gap states counts as the gap, and any other error fails the test. It also
checks that each gap still fails as stated (``Gap.raises`` and ``Gap.deliveries``) and, for
a gap with ``Gap.masked``, that the results are equal once those declared differences are
masked on both sides: the gap is exactly what it states. Each plan removes the scenarios it
fixes, or updates a gap whose failure changed. At the end of plan 6 the only gaps left are the ones
that state a permanent declared difference and nothing else.

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

Task 6a (2026-11-13): ``AgentServer.ingest`` and ``chat_upload`` no longer raise in Maestro mode (``server_ingest`` ends at
the first agent API call; ``chat_upload`` ends at its ``dispatch``).

Task 5c (2026-11-13): ``AgentServer.repository()`` and ``memory(thread_id)`` no longer raise in Maestro mode; ``repository_crud``
ends at the first agent API call (see its entry).

Measured again on 2026-11-13 (plan 5, task 5a). The delivery context carries Core's person data (name,
e-mail, identity IDs and the three roles), the workspace queue catalog and the thread view, and the SDK
reads them. The three ``send.*`` scenarios that waited for them match (``update_thread_title``,
``update_thread_queue``, ``add_participant``). ``form_response_submit``, ``form_response_cancel`` and
``incoming_fields`` still differ, only by a permanent declared difference (the legacy WebSocket acks; the
size of a URL document).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace

from dooers.agents.server.exceptions import ThreadManagementModeError
from dooers.agents.server.maestro.agent_api import MaestroAgentApiError
from tests.parity.model import ParityResult
from tests.parity.scenarios_server import WHATSAPP_SENDS

Outcome = tuple[int, str, str | None]

#: The value a declared difference gets on both sides before the comparison.
MASK = "<declared>"


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
_PUBLIC_CHAT = Gap("plan 6: the Maestro-mode router acks event.create with THREAD_MANAGEMENT_MISMATCH: no events and no thread")

GAPS: dict[str, Gap] = {
    # Core's person, workspace and thread data reach the agent's context (plan 5, task 5a): the scenarios that
    # waited for them match. What is left differs only by a permanent declared difference.
    "form_response_submit": _declared("agent_websocket", deliveries=_RESPONDED * 2),
    "form_response_cancel": _declared("agent_websocket", deliveries=_RESPONDED * 2),
    "incoming_fields": _declared("media_link_size", deliveries=_RESPONDED * 2),
    # AgentServer ingress (plan 5) and public chat (plan 6)
    "server_dispatch": _server_blocked("dispatch"),
    # ``AgentServer.ingest`` runs over the agent API (plan 5, task 6a): ``tests/test_maestro_ingest.py`` runs the same calls on
    # production and on Maestro and compares the thread and the events. This harness serves only the delivery routes
    # (FakeMaestro), so the run ends at the first agent API call with CREDENTIAL_UNAVAILABLE (as ``repository_crud``): the
    # scenario joins the harness with the agent API server when the WhatsApp tasks need it (plan 5, task 8).
    "server_ingest": Gap(
        "plan 5: ingest runs over the agent API (compared in test_maestro_ingest.py); this harness has no agent API server, "
        "so the run ends with CREDENTIAL_UNAVAILABLE at the first call",
        raises=MaestroAgentApiError,
        match="CREDENTIAL_UNAVAILABLE",
    ),
    # ``AgentServer.repository()`` runs over the agent API (plan 5, task 5c): ``tests/test_maestro_agent_persistence.py`` runs this
    # scenario's calls on production and on Maestro and compares them. This harness serves only the delivery routes (FakeMaestro),
    # so the Maestro run ends at the first agent API call, which has no Core credential: the scenario joins the harness
    # with the agent API server (``tests/parity/fake_agent_api.py``) when the WhatsApp tasks need it (plan 5, task 8).
    "repository_crud": Gap(
        "plan 5: Repository runs over the agent API (compared in test_maestro_agent_persistence.py); this harness has no agent API "
        "server, so the run ends with CREDENTIAL_UNAVAILABLE at the first call",
        raises=MaestroAgentApiError,
        match="CREDENTIAL_UNAVAILABLE",
    ),
    "memory_by_thread_id": _server_blocked("dispatch", " (first call of the script; memory is never reached)"),
    # ``chat_upload`` works in Maestro mode (plan 5, task 6a: local store, production fields and errors); the scenario ends with a
    # ``dispatch`` of the staged file (task 6b).
    "chat_upload": _server_blocked("dispatch", " (the last call of the script, after chat_upload)"),
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
