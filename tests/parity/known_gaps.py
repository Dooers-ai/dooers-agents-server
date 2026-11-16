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

Task 6b (2026-11-13): ``AgentServer.dispatch`` no longer raises in Maestro mode: it publishes over the agent API and follows the thread
(``tests/test_maestro_dispatch.py``, ``tests/test_maestro_dispatch_run.py``). The scripts that call it end at the first agent API call.

Task 5c (2026-11-13): ``AgentServer.repository()`` and ``memory(thread_id)`` no longer raise in Maestro mode; ``repository_crud``
ends at the first agent API call (see its entry).

Measured again on 2026-11-13 (plan 5, task 5a). The delivery context carries Core's person data (name,
e-mail, identity IDs and the three roles), the workspace queue catalog and the thread view, and the SDK
reads them. The three ``send.*`` scenarios that waited for them match (``update_thread_title``,
``update_thread_queue``, ``add_participant``). ``form_response_submit``, ``form_response_cancel`` and
``incoming_fields`` still differ, only by a permanent declared difference (the legacy WebSocket acks; the
size of a URL document).

Measured again on 2026-11-13 (plan 5, task 8). ``tests/parity/harness.py``'s ``_run_maestro_server`` now wires
``tests/parity/fake_agent_api.py`` into the same ``FakeMaestro`` instance the scenario uses (one combined
respx route: ``/agent/*`` to ``FakeAgentApi``, everything else to ``FakeMaestro``'s existing delivery
callback handling), so every ``AgentServer`` ingress scenario's ``MaestroAgentApi`` calls now succeed. A
``trigger: true`` publication (``AgentServer.dispatch``, the WhatsApp inbound webhook) is delivered for real
through ``create_text_delivery_adapter``: the harness mirrors the thread and the trigger forward into
``FakeMaestro`` so the delivery protocol can run, and mirrors the handler's own outputs (plus the delivery's
own lifecycle, which ``FakeMaestro`` has no notion of) back into ``FakeAgentApi`` as they happen, so a later
agent-api read (``AgentServer.memory()``/``.repository()``, the dispatch stream's own polling) sees them too.
``server_dispatch``, ``server_ingest`` and ``memory_by_thread_id`` now match exactly. Three scenarios differ
only by a permanent declared difference the investigation surfaced (not one the brief anticipated, each
backed by production's own code or test suite, never by a change to it): ``repository_crud`` (a fresh
agent-api thread always has the agent as a participant), ``chat_upload`` (an attachment's ``url`` resolves to
a real temporary link outside a delivery), and every ``whatsapp_<send type>``/``whatsapp_receipt`` (no
``data.delivery`` outside a delivery — ``MaestroExecutionPersistence._channel_state``'s own docstring states
it has no thread view over the agent API).

Final review of plan 5 (fix 1, 2026-11-14): that last gap was never the extra read the docstring blamed —
``MaestroAgentPersistence._reads`` already pays for ``get_thread`` once per call and was discarding its
result. ``_reads`` now passes that ``view`` into ``_AgentWindow``'s head (the ``thread`` key
``_channel_state`` looks for), so ``data.delivery`` is populated outside a delivery too. The 7
``whatsapp_<send type>``/``whatsapp_receipt`` scenarios match exactly now and are gone from ``GAPS``.

Measured again on 2026-11-16 (plan 6, task 6a, Passo 0/1). ``public_chat_guest``/``public_chat_authenticated``
no longer go through the Maestro-mode Router (``THREAD_MANAGEMENT_MISMATCH`` is gone): the harness now plays
the visitor's own path (design section 7.3) — ``FakeMaestro.ensure_external_thread``/``.publish_external``
(a new ``kind: "external"`` actor/participant, idempotent by ``external_key``, routed to a delivery the same
way a web chat person's message is) through a new ``harness._run_maestro_public_chat``, a sibling of
``_run_maestro_deliveries``/``_run_maestro_server`` picked by ``_is_public_chat_scenario`` (``ScenarioInput.connect``
present with ``connection_type`` ``guest``/``public_authenticated``) rather than by ``_delivered_by_maestro``
(unchanged). What was left of both scenarios then was two declared differences, not the old gap: the
legacy WebSocket acks and Core validation count (``agent_websocket``, already used by ``form_response_submit``/
``form_response_cancel``), and the visitor's own declared profile — name, e-mail, identity IDs, roles,
``connection_type`` — plus ``data.source``, which reach the live handler context (plan 6 task 5's own
``test_maestro_public_chat.py``) but not a Maestro-mode read-back outside it, because ``legacy_model.py``'s
``to_thread``/``_message`` call ``production_user`` with no participant for an external actor
(``public_chat_visitor_profile``, new; closed by the final review of plan 6, below).

Fix 1 of task 6a (2026-11-16, review): ``_external_user`` (``legacy_model.py``) now falls back to ``person``
itself the same way ``_core_person`` already does for a human, so ``Thread.owner``/``.users`` carry the
visitor's own profile.

Final review of plan 6 (2026-11-16): the visitor's masks were hiding differences that could be fixed, so
``public_chat_visitor_profile``, ``public_chat_thread_metadata`` and ``public_chat_guest_role`` are gone. The
``guest`` role is the fixture's (``GUEST_CONNECT.core_user``: Core returns it, ``publicChatSession/services.ts``);
the message's author, name, e-mail and ``data.source`` come from the message actor's participant
(``legacy_model._message``) and the channel; ``Thread.metadata`` is built from the thread's channel and the
owner's ``guest_profile`` (``legacy_model._thread_metadata``); ``Thread.users`` lists the visitor before the agent
(``_in_join_order``); the live handler context is compared too (``_public_chat_reply`` calls ``capture_incoming``).
What is left of both public chat scenarios is the legacy WebSocket acks and the Core validation count
(``agent_websocket``).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace

from tests.parity.model import AGENT_ID, ParityResult

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


def _agent_api_thread_has_agent(result: ParityResult) -> None:
    def strip(value: object) -> None:
        if isinstance(value, dict):
            users = value.get("users")
            if isinstance(users, list):
                value["users"] = [user for user in users if not (isinstance(user, dict) and user.get("user_id") == AGENT_ID)]
            for child in value.values():
                strip(child)
        elif isinstance(value, list):
            for child in value:
                strip(child)

    strip(result.outputs)


def _agent_api_attachment_url(result: ParityResult) -> None:
    for parts in [result.incoming.get("content", []), *(event["content"] or [] for event in result.events)]:
        for part in parts:
            # The wire event keeps ``ref_id`` (a staged upload); the handler's own view has no such field, but
            # carries the file's raw ``data`` instead (``capture_incoming``) — either marks the same attachment.
            if part.get("type") in {"image", "audio", "document"} and (
                part.get("ref_id") is not None or isinstance(part.get("data"), bytes)
            ):
                part["url"] = MASK
                if "ref_id" in part:
                    # Once dispatched, the stored event's ``ref_id`` is Maestro's own artifact id, no longer the
                    # staged upload's; legacy keeps the one ``chat_upload`` returned (``normalize``'s id markers
                    # see that as one shared id in legacy and two unrelated ones in Maestro).
                    part["ref_id"] = MASK
    upload = result.outputs.get("chat_upload")
    if isinstance(upload, dict) and "ref_id" in upload:
        upload["ref_id"] = MASK


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
    "agent_api_thread_has_agent": (
        "declared, permanent: an agent-api-created thread always has the agent as an active participant "
        "(Public.AgentThreadCreateRequest/FakeAgentApi, test_maestro_agent_persistence.py's own "
        "'the same users except the agent'); Repository.create_thread only ever adds the explicit owner/users",
        _agent_api_thread_has_agent,
    ),
    "agent_api_attachment_url": (
        "declared, permanent: an attachment read back through the agent API outside a delivery resolves to a "
        "real temporary link (MaestroAgentApi.link_resolver), and its stored ref_id is Maestro's own artifact id, "
        "not the staged upload's; legacy's local ref_id keeps url=None and stays the same id throughout",
        _agent_api_attachment_url,
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


_RESPONDED: tuple[Outcome, ...] = ((200, "responded", "responded"),)

GAPS: dict[str, Gap] = {
    # Core's person, workspace and thread data reach the agent's context (plan 5, task 5a): the scenarios that
    # waited for them match. What is left differs only by a permanent declared difference.
    "form_response_submit": _declared("agent_websocket", deliveries=_RESPONDED * 2),
    "form_response_cancel": _declared("agent_websocket", deliveries=_RESPONDED * 2),
    "incoming_fields": _declared("media_link_size", deliveries=_RESPONDED * 2),
    # Plan 5, task 8: the harness wires ``tests/parity/fake_agent_api.py`` into ``_run_maestro_server`` (an
    # AgentServer ingress scenario's ``MaestroAgentApi`` calls now reach it, and a ``trigger: true`` publication
    # is delivered for real through ``create_text_delivery_adapter``). ``server_dispatch``, ``server_ingest`` and
    # ``memory_by_thread_id`` now match exactly. ``repository_crud`` differs only by a permanent declared
    # difference: a fresh agent-api thread always has the agent as a participant (real Maestro behavior,
    # already accepted as a divergence in ``test_maestro_agent_persistence.py``'s own
    # ``test_create_thread_owner_is_the_user``), which ``Repository.create_thread`` never adds without a run.
    "repository_crud": _declared("agent_api_thread_has_agent", deliveries=()),
    # ``chat_upload`` matches except for a permanent declared difference: the staged file's ``url`` resolves to a
    # real temporary link once read back through the agent API outside a delivery (``MaestroAgentApi.link_resolver``);
    # the local ``ref_id`` legacy stores keeps ``url=None``.
    "chat_upload": _declared("agent_api_attachment_url", deliveries=_RESPONDED),
    # Final review fix 1: ``_reads()`` now passes the thread ``view`` it already read into ``_AgentWindow``'s head,
    # so ``_channel_state`` has a thread view outside a delivery too; every ``send.whatsapp.*`` and the receipt
    # scenario now match exactly (``agent_api_no_delivery_data`` is gone — see git history for the closed gap).
    # Task 6a (2026-11-16): the visitor talks to Maestro directly (``FakeMaestro.ensure_external_thread``/
    # ``.publish_external``, ``_run_maestro_public_chat``), never through the Maestro-mode Router — the old
    # THREAD_MANAGEMENT_MISMATCH gap is gone.
    # Final review of plan 6 (2026-11-16): both scenarios match the legacy result, the live handler context included,
    # except for the legacy WebSocket acks and the Core validation count (a permanent declared difference).
    "public_chat_guest": _declared("agent_websocket", deliveries=_RESPONDED * 2),
    "public_chat_authenticated": _declared("agent_websocket", deliveries=_RESPONDED * 2),
}

GAP_REASONS: dict[str, str] = {name: gap.reason for name, gap in GAPS.items()}

KNOWN_GAPS: frozenset[str] = frozenset(GAPS)
