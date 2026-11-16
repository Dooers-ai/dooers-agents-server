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
(unchanged). What is left of both scenarios is two permanent declared differences, not the old gap: the
legacy WebSocket acks and Core validation count (``agent_websocket``, already used by ``form_response_submit``/
``form_response_cancel``), and the visitor's own declared profile — name, e-mail, identity IDs, roles,
``connection_type`` — plus ``data.source``, which reach the live handler context (plan 6 task 5's own
``test_maestro_public_chat.py``) but not a Maestro-mode read-back outside it, because ``legacy_model.py``'s
``to_thread``/``_message`` call ``production_user`` with no participant for an external actor
(``public_chat_visitor_profile``, new).

Fix 1 of task 6a (2026-11-16, review): that single gap conflated two causes. ``_message``'s loss is
genuinely structural — a message's actor is the real contract's own ``external`` actor variant, which never
carries e-mail/``guest_profile``/roles at all (confirmed against the compiled ``Public.AgentEvent`` schema),
and ``_stored`` keeps only ``user_id``/``user_name``/``user_email`` of any message's ``User`` regardless —
left alone, and ``public_chat_visitor_profile`` now covers only it (plus ``data.source``). ``Thread.owner``/
``.users``'s loss was a bug: ``production_user(participant)`` (``to_thread``, no separate ``core``) reached
``_external_user`` with ``core=None``, which returned a bare ``User(user_id, user_name)`` even though
``participant`` (``to_thread``'s already-enriched ``view["participants"]``, Task 3b) already carried
everything else — ``_external_user`` now falls back to ``person`` itself the same way ``_core_person``
already does for a human. ``public_chat_authenticated``'s ``Thread.owner``/``.users`` match exactly now, with
no mask needed for them (the owner identity, the match only differing where the thread lacks an equivalent
field at all: ``Thread.metadata``, never set by ``to_thread`` for any thread, and the join order, a
pre-existing ranking rule — both new, ``public_chat_thread_metadata``). ``public_chat_guest``'s differ by one
more thing the fix does not touch: the ``guest`` role literal in the three roles is a Maestro-only concept
(``visitorContextSchema``, plan 6), confirmed absent anywhere in the old chat widget's own code
(``public_chat_guest_role``, new).
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


def _public_chat_visitor_profile(result: ParityResult) -> None:
    """A public chat visitor's declared profile never reaches a *message*, read back in Maestro mode.

    ``legacy_model.py``'s ``_message`` calls ``production_user(actor)`` with no participant for every
    event (the message actor is a snapshot of that instant, in the real contract's own shape: it never
    carries e-mail/``guest_profile`` at all — ``docs/contracts/v1/common.ts``'s ``actorSchema``, confirmed
    reading the compiled ``Public.AgentEvent`` schema). Worse: even what the actor DOES carry
    (``connection_type``) never reaches the stored row either, because ``_stored`` only ever keeps
    ``user_id``/``user_name``/``user_email`` (production's own events table) — the same reason
    ``identity_ids``/the three roles are always empty there too, on both sides, with nothing to mask.
    Legacy's WS path has no such limitation: it stamps the ``User`` straight from the connect handshake
    onto every message. The same gap drops ``data.source`` (the channel a person's own message arrived
    on): Maestro's conversion only ever stamps ``data.delivery``, on an assistant's own message.
    ``Thread.owner``/``.users`` (a participant, not a message actor) do not have this gap — fix 1 taught
    ``_external_user`` to read a participant's own profile the same way ``_core_person`` already does
    for a human (see ``public_chat_thread_metadata``/``public_chat_guest_role`` for what is left there).
    """
    for event in result.events:
        if event.get("actor") == "user":
            event["author"] = MASK
            event["data"] = MASK
            user = event.get("user")
            if isinstance(user, dict):
                for key in ("user_name", "user_email"):
                    if key in user:
                        user[key] = MASK


def _public_chat_thread_metadata(result: ParityResult) -> None:
    """``Thread.metadata`` and the join order of ``Thread.users``, for a public chat visitor's thread.

    ``to_thread`` never sets ``metadata`` for any thread (nothing public-chat-specific): legacy's own
    ``Thread.metadata`` of a public chat thread carries the pre-chat form and the channel/label the
    visitor's browser sent at ``thread.create.public_chat`` time; Maestro's conversion has no such
    field to read from a ``Public.Thread`` view. The join order differs too: ``to_thread``'s
    ``_in_join_order`` ranks the agent ahead of a non-owner external participant (``rank``'s own
    ``else 2``); legacy's own WS-path thread builder lists the visitor (the channel owner) first.
    """
    for thread in result.outputs.get("threads", []):
        if "metadata" in thread:
            thread["metadata"] = MASK
        if isinstance(thread.get("users"), list):
            thread["users"] = sorted(thread["users"], key=lambda user: user.get("user_id", ""))


def _public_chat_guest_role(result: ParityResult) -> None:
    """The ``guest`` role in all three roles — a Maestro-only concept, absent from the legacy WS path.

    Plan 6's ``visitorContextSchema`` (``identity.ts``) gives an anonymous visitor the literal role
    ``"guest"`` in ``system_role``/``organization_role``/``workspace_role`` (confirmed in
    ``test_maestro_public_chat.py::test_guest_visitor_user_and_on_behalf``, Task 5's own test, which
    ``FakeMaestro._with_external_profile`` mirrors). The old, non-Maestro chat widget never had this
    concept: its connect handshake carries only the default roles (``user``/``member``/``member``,
    confirmed grepping the SDK for ``"guest"`` next to a role — there is none), regardless of
    ``connection_type``. ``public_authenticated`` is unaffected: there Maestro reads the person's own
    roles from the core, same as legacy's.
    """

    def strip(value: object) -> None:
        if isinstance(value, dict):
            if value.get("connection_type") == "guest":
                for key in ("system_role", "organization_role", "workspace_role"):
                    if key in value:
                        value[key] = MASK
            for child in value.values():
                strip(child)
        elif isinstance(value, list):
            for child in value:
                strip(child)

    strip(result.outputs)
    strip(result.incoming)


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
    "public_chat_visitor_profile": (
        "declared, permanent: a public chat visitor's declared name/e-mail and data.source never reach a "
        "message (legacy_model.py's _message calls production_user with the bare message actor, which the "
        "real contract never gives e-mail/guest_profile, and _stored keeps only user_id/user_name/user_email "
        "of any User read back from a message either way); legacy's WS path stamps every message from the "
        "connect handshake instead",
        _public_chat_visitor_profile,
    ),
    "public_chat_thread_metadata": (
        "declared, permanent: Thread.metadata (the pre-chat form and channel/label) and the join order of "
        "Thread.users, for a public chat visitor's thread; to_thread never sets metadata for any thread, and "
        "ranks the agent ahead of a non-owner external participant where legacy's own WS-path builder lists "
        "the visitor first",
        _public_chat_thread_metadata,
    ),
    "public_chat_guest_role": (
        "declared, permanent: the guest role literal in system_role/organization_role/workspace_role "
        "(visitorContextSchema, plan 6) is a Maestro-only concept; the old, non-Maestro chat widget's connect "
        "handshake never carries anything but the default roles for any connection_type",
        _public_chat_guest_role,
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
    # Task 6a, fix 1 (2026-11-16): ``_external_user`` (legacy_model.py) read a public chat visitor's own
    # e-mail/identity_ids/roles/connection_type/guest_profile only from a separate ``core`` argument no
    # caller ever passes, discarding them even when ``person`` (``Thread.owner``/``.users``'s own,
    # already-enriched participant) already had them; it now falls back to ``person`` itself, the same
    # way ``_core_person`` already does for a human. ``Thread.owner``/``.users`` match exactly now for
    # ``public_chat_authenticated``, and for ``public_chat_guest`` except the ``guest`` role literal
    # (``public_chat_guest_role``, Maestro-only, confirmed absent anywhere in the old chat widget). What
    # is left for both: the legacy WS acks/Core validation count, the per-message profile loss (genuinely
    # structural — the message actor itself never carries it, confirmed against the real contract), and
    # ``Thread.metadata``/the join order of ``Thread.users`` (``to_thread`` never sets the former for any
    # thread; the latter is a pre-existing ranking rule, unrelated to this fix).
    "public_chat_guest": _declared(
        "agent_websocket", "public_chat_visitor_profile", "public_chat_thread_metadata", "public_chat_guest_role", deliveries=_RESPONDED * 2
    ),
    "public_chat_authenticated": _declared(
        "agent_websocket", "public_chat_visitor_profile", "public_chat_thread_metadata", deliveries=_RESPONDED * 2
    ),
}

GAP_REASONS: dict[str, str] = {name: gap.reason for name, gap in GAPS.items()}

KNOWN_GAPS: frozenset[str] = frozenset(GAPS)
