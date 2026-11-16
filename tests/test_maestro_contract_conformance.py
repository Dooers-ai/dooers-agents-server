"""Guard: everything production lets a handler send is valid for the Maestro contract.

The rule of the project: a handler written for the production SDK runs in Maestro mode with no change and with the same
result; the Maestro contract only grows. The contract once gave ``agent_id`` of a run the type "UUID", while production takes
any text (``Run.agent_id: str | None``, a ``TEXT`` column). A real agent (``send.run_start(agent_id="dooers-assistant")``)
failed in Maestro mode, and no test saw it, because the scenarios chose each value by hand (always a UUID).

This guard takes the values from production, not from a person:

1. ``tests/production_domain.py`` reads the signature and the annotations of every public function of production ``AgentSend``
   and ``send.whatsapp`` (and the Pydantic models and the queue rule that production applies) and builds the values production
   accepts: free text that is not a UUID, ``None``, an empty string, Unicode, a long text, numbers at the extremes of the type,
   every ``Literal`` value, every optional parameter left out. A new function or parameter in production enters alone.
2. Each value goes through the same path the SDK uses in Maestro mode, ``MaestroEventSink.accept`` (``output_for``, the
   cleaning of NUL and lone surrogates, the media upload or ``media_link``, the thread queue rule, the run tracker), and the
   ``Public.OutputRequest`` it builds is validated against the vendored Maestro contract (``bundle.zip``), the same validator
   that ``FakeMaestro`` and the SDK use.
3. One parameter varies at a time, the others stay at an ordinary production value, so a failure names one field and one value.
4. Production models written through the agent API (``Repository`` -> ``MaestroAgentPersistence``, ``AgentServer.ingest`` ->
   ``MaestroIngress``: ``Thread``, ``ThreadEvent``, ``User`` and the content parts) go through the same conversion functions of
   the SDK and are validated against ``Public.AgentThreadCreateRequest``, ``Public.AgentEventCreateRequest`` and the related
   schemas.

A failing case says the function, the field, the production value and the Maestro schema that refused it.

``DIVERGENCES`` lists what this guard found and nobody fixed yet. Each one runs as ``xfail(strict=True)`` with the reason "divergência
real, a corrigir": when the contract or the SDK is fixed, the case passes, the strict ``xfail`` fails, and the entry must leave the
list. It is never a permanent difference. The only differences stated as permanent are the length ceilings that the Maestro contract
declares on purpose (``maestro_conformance_support.DECLARED_CEILINGS``, each with the line of the contract that says it); they are
tried only with the "huge" samples (100 000 characters). Nothing in ``tests/parity/known_gaps.py::DECLARED_DIFFERENCES`` is about a value
a handler sends through ``send.*`` (they are about the legacy WebSocket, the size of a URL document read back, the agent that is always a
participant of an agent-API thread and the link of a read-back attachment).

The guard also guards itself: the ordinary call of every function conforms, every production function and parameter has cases, and
``MaestroSend`` keeps every production signature.
"""

from __future__ import annotations

import inspect
import typing
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from dooers.agents.server.handlers.send import AgentEvent
from dooers.agents.server.maestro import artifacts as artifacts_module
from dooers.agents.server.maestro import sink as sink_module
from dooers.agents.server.maestro.callbacks import MaestroCallbackError
from dooers.agents.server.maestro.media_fetch import FetchedMedia, MediaFetchError
from dooers.agents.server.maestro.sink import MaestroEventSink, MaestroSend
from dooers.agents.server.protocol import models
from dooers.agents.server.protocol.maestro import default_contracts
from tests.maestro_conformance_support import (
    EMPTY_AND_LONG_TEXT,
    LONG_TEXT,
    NOT_ORDINARY,
    Divergence,
    Refusal,
    SpyContracts,
    ids,
    is_declared_ceiling,
    prefixed,
)
from tests.production_domain import (
    ProductionFunction,
    Sample,
    element_builders,
    omitted,
    parameter_domain,
    production_functions,
)

WORKSPACE = "11111111-1111-4111-8111-111111111111"
THREAD = "22222222-2222-4222-8222-222222222222"
INSTANCE = "33333333-3333-4333-8333-333333333333"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
MEDIA_SENDS = ("image", "audio", "document")


# --- the Maestro side: the SDK's own sink over a client that records instead of calling the network ------------------


class RecordingClient:
    """The part of ``MaestroCallbackClient`` the sink uses. ``output`` records the body; the contract is the real one."""

    def __init__(self, contracts: Any) -> None:
        self._contracts = SpyContracts(contracts)
        self.config = SimpleNamespace(
            instance_agent_id=INSTANCE,
            max_output_bytes=1048576,
            max_artifact_bytes=26214400,
            artifact_mime_types=(),
            product_capabilities=(),
        )
        self._envelope = {
            "lease_fence": "1",
            "workspace_id": WORKSPACE,
            "thread_id": THREAD,
            "delivery_id": "44444444-4444-4444-8444-444444444444",
            "phase_id": "55555555-5555-4555-8555-555555555555",
        }
        self._claims = {"operations": []}
        self.sent: list[dict[str, Any]] = []

    def _check_current(self) -> float:
        return 30.0

    async def heartbeat(self, _state: str) -> None:
        return None

    async def output(self, output: dict[str, Any], _ordinal: int) -> dict[str, Any]:
        self.sent.append(output)
        return {"event": {"event_id": str(uuid.uuid4()), "sequence": str(len(self.sent))}}


@dataclass
class Outcome:
    ok: bool
    code: str | None = None
    refusals: list[Refusal] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)

    def explain(self) -> str:
        if self.ok:
            return "accepted"
        if self.refusals:
            return f"Maestro refused it (sink code {self.code}): {self.refusals[-1]}"
        return f"the SDK refused it before the contract (sink code {self.code})"


@contextmanager
def media_mode(mode: str) -> Iterator[None]:
    """``link``: the URL cannot be fetched, so the sink publishes a ``media_link`` with the production values.
    ``upload``: the file is fetched and uploaded, so the sink publishes an ``attachment`` and adds ``alt`` and ``duration``."""

    async def unreachable(*_args: Any, **_kwargs: Any) -> FetchedMedia:
        raise MediaFetchError("network")

    async def fetched(*_args: Any, **_kwargs: Any) -> FetchedMedia:
        return FetchedMedia(PNG, "image/png", "arquivo.png")

    async def upload(_self: Any, data: bytes, *, filename: str, mime_type: str, ordinal: int) -> dict[str, Any]:
        return {
            "type": "attachment",
            "artifact_id": str(uuid.uuid4()),
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(data),
        }

    with (
        mock.patch.object(sink_module, "fetch_media", unreachable if mode == "link" else fetched),
        mock.patch.object(artifacts_module.MaestroArtifactsClient, "upload", upload),
    ):
        yield


async def publish(events: list[AgentEvent], *, mode: str = "link") -> Outcome:
    """Every event through ``MaestroEventSink.accept``, as the executor does for each event a handler yields."""
    client = RecordingClient(default_contracts())
    sink = MaestroEventSink(client)  # type: ignore[arg-type]
    sink.bind_queues(())
    try:
        with media_mode(mode):
            for event in events:
                await sink.accept(event)
    except MaestroCallbackError as error:
        return Outcome(False, error.code, list(client._contracts.refusals), client.sent)
    return Outcome(True, sent=client.sent)


# --- the production side: the cases --------------------------------------------------------------------------------------


def _target(send: Any, function: ProductionFunction) -> Any:
    for step in function.path:
        send = getattr(send, step)
    return send


def event_functions() -> list[ProductionFunction]:
    """The production functions that make an ``AgentEvent`` (the ``form_*`` and ``chart_series`` builders are composed into them)."""
    return [f for f in production_functions() if f.builds_event]


def _ordinary_kwargs(function: ProductionFunction) -> dict[str, Any]:
    """The ordinary call: each required parameter at its ordinary value, every other parameter left out."""
    return {
        p.name: parameter_domain(function, p)[0].value
        for p in function.signature.parameters.values()
        if p.default is inspect.Parameter.empty
    }


@dataclass(frozen=True)
class Case:
    function: ProductionFunction
    #: ``None`` is the ordinary call.
    param: str | None
    sample: Sample
    kwargs: dict[str, Any]
    mode: str

    @property
    def id(self) -> str:
        suffix = f"[{self.mode}]" if self.function.path[-1] in MEDIA_SENDS else ""
        field_part = "ordinary" if self.param is None else f"{self.param}={self.sample.id}"
        return f"{self.function.name}({field_part}){suffix}"

    def call(self) -> AgentEvent:
        return _target(MaestroSend(), self.function)(**self.kwargs)

    def describe_value(self) -> str:
        value = self.sample.value
        text = repr(value)
        return text if len(text) <= 90 else f"{text[:80]}... ({len(text)} chars)"

    def where(self) -> str:
        """The production definition the value was taken from (file and line)."""
        parameter = self.function.signature.parameters.get(self.param or "")
        annotation = inspect.formatannotation(parameter.annotation) if parameter is not None else "-"
        line = inspect.getsourcelines(self.function.function)[1]
        return f"production {self.function.name}({self.param}: {annotation}), src/dooers/agents/server/handlers/send.py:{line}"


def accepted_by_production(event: AgentEvent) -> bool:
    """What ``HandlerPipeline`` validates before it stores an event (``pipeline.py``): other values are not production values."""
    from pydantic import ValidationError

    from tests.production_domain import form_element_models

    try:
        if event.send_type == "chart":
            models.WireS2C_ChartEventData(**event.data)
        elif event.send_type == "form":
            by_type = form_element_models()
            for element in event.data["elements"]:
                by_type[element["type"]](**element)
        elif event.send_type == "event_update":
            for part in event.data["content"]:
                models.deserialize_s2c_part(part)
    except (ValidationError, ValueError, KeyError):
        return False
    return True


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for function in event_functions():
        ordinary = _ordinary_kwargs(function)
        modes = ("link", "upload") if function.path[-1] in MEDIA_SENDS else ("link",)
        for mode in modes:
            cases.append(Case(function, None, Sample("plain", None), ordinary, mode))
            for parameter in function.signature.parameters.values():
                for sample in parameter_domain(function, parameter):
                    kwargs = {k: v for k, v in ordinary.items() if k != parameter.name}
                    if not omitted(sample):
                        kwargs[parameter.name] = sample.value
                    if kwargs == ordinary:
                        continue
                    case = Case(function, parameter.name, sample, kwargs, mode)
                    if accepted_by_production(case.call()):
                        cases.append(case)
    return cases


async def run_case(case: Case) -> Outcome:
    send = MaestroSend()
    events = [case.call()]
    if case.function.name != "send.run_start":
        events.insert(0, send.run_start())  # every other output of a handler can sit inside a run: it carries the run_id
    return await publish(events, mode=case.mode)


# --- what this guard found and nobody fixed yet ---------------------------------------------------------------------------
#
# Each entry is a real divergence: production accepts the value, the Maestro contract (``docs/contracts/v1/public.ts`` of
# dooers-service-maestro, vendored in ``bundle.zip``) or the SDK conversion refuses it. Production lines are in
# ``src/dooers/agents/server/handlers/send.py``; every production parameter below is a plain ``str``/``float``/``int``/``list``, stored
# in a ``TEXT`` or ``JSONB`` column (``persistence/postgres.py:152-191``) with no length or range rule.


def _both(*names: str) -> frozenset[str]:
    """``send.<name>`` and ``send.whatsapp.<name>`` (the same send types, plus ``data["whatsapp"]``)."""
    return frozenset({f"send.{n}" for n in names} | {f"send.whatsapp.{n}" for n in names})


def _only(*names: str) -> frozenset[str]:
    return frozenset(f"send.{n}" for n in names)


_MESSAGES = ("text", "image", "document", "audio", "contact", "template")
_MEDIA = ("image", "audio", "document")
_LINK = frozenset({"link"})
_BOTH_MODES = None
_BUILDERS = [b.name.removeprefix("send.") for b in element_builders()]


def _any_of(*patterns: Any) -> Any:
    return lambda sample_id: any(p(sample_id) for p in patterns)


DIVERGENCES: list[Divergence] = [
    Divergence(
        _both(*_MESSAGES),
        "author",
        LONG_TEXT,
        _BOTH_MODES,
        "author: production str | None (send.py:128, TEXT column events.author), Maestro agentMessageFields.author max 200 (public.ts:189)",
    ),
    Divergence(
        _only("add_participant"),
        "user_id",
        LONG_TEXT,
        _BOTH_MODES,
        "user_id: production str (send.py:301), Maestro participantAddPayload.user.user_id max 256 (public.ts:395)",
    ),
    Divergence(
        _only("add_participant"),
        "user_name",
        LONG_TEXT,
        _BOTH_MODES,
        "user_name: production str | None (send.py:301), Maestro participantAddPayload.user.user_name max 200 (public.ts:396)",
    ),
    Divergence(
        _only("add_participant"),
        "user_email",
        LONG_TEXT,
        _BOTH_MODES,
        "user_email: production str | None (send.py:301), Maestro participantAddPayload.user.user_email max 320 (public.ts:397)",
    ),
    Divergence(
        _only("add_participant"),
        "identity_ids",
        ids("empty-item", "long-item", "huge-item", "many"),
        _BOTH_MODES,
        "identity_ids: production list[str] | None (send.py:301), Maestro items min 1 max 256 and at most 64 items (public.ts:398)",
    ),
    Divergence(
        _both("contact"),
        "display_name",
        LONG_TEXT,
        _BOTH_MODES,
        "display_name: production str (send.py:170), Maestro contactPart.display_name max 512 (public.ts:131)",
    ),
    Divergence(
        _both(*_MEDIA),
        "mime_type",
        NOT_ORDINARY,
        _LINK,
        "mime_type: production free str (send.py:134,146,158), Maestro media_link.mime_type must look like type/subtype, 3 to 128 "
        "characters"
        " (public.ts:62-66,141); the SDK sends the production value in the media_link when it cannot upload the file (sink.py:694-706)",
    ),
    Divergence(
        _both(*_MEDIA),
        "url",
        NOT_ORDINARY,
        _LINK,
        "url: production free str (send.py:134,146,158), Maestro media_link.url must be an http(s) URL (public.ts:140) and the SDK"
        " fails the run with INVALID_REQUEST for another text when it cannot upload the file (sink.py:701)",
    ),
    Divergence(
        _both("document"),
        "filename",
        ids("empty", "path-chars", "control-chars", "markup", "long", "long-unicode", "huge", "huge-unicode"),
        _LINK,
        "filename: production str (send.py:146), Maestro media_link.filename 1 to 255 characters without / \\ or control characters "
        "(public.ts:56-61,142)",
    ),
    Divergence(
        _both("image"),
        "alt",
        ids("long-unicode", "huge", "huge-unicode"),
        _BOTH_MODES,
        "alt: production str | None (send.py:134), Maestro alt max 1024 UTF-16 units (public.ts:93,143)",
    ),
    Divergence(
        _both("audio"),
        "duration",
        ids("minus-one", "float-lowest", "float-max"),
        _BOTH_MODES,
        "duration: production float | None (send.py:158), Maestro duration from 0 to 9007199254740991 (public.ts:94,144)",
    ),
    Divergence(
        _both("template"),
        "template_name",
        EMPTY_AND_LONG_TEXT,
        _BOTH_MODES,
        "template_name: production str (send.py:188, pipeline.py:716 stores it as sent), Maestro template_name 1 to 512 characters "
        "(public.ts:172)",
    ),
    Divergence(
        _both("template"),
        "template_language",
        EMPTY_AND_LONG_TEXT,
        _BOTH_MODES,
        "template_language: production str (send.py:188), Maestro template_language 1 to 64 characters (public.ts:174)",
    ),
    Divergence(
        frozenset(f"send.whatsapp.{n}" for n in _MESSAGES),
        "to_e164",
        EMPTY_AND_LONG_TEXT,
        _BOTH_MODES,
        "to_e164: production str (send.py:28-33), Maestro whatsapp.to_e164 1 to 64 characters (public.ts:177-178)",
    ),
    Divergence(
        frozenset(f"send.whatsapp.{n}" for n in _MESSAGES),
        "instance_id",
        EMPTY_AND_LONG_TEXT,
        _BOTH_MODES,
        "instance_id: production str (send.py:28-33), Maestro whatsapp.instance_id 1 to 256 characters (public.ts:177-178)",
    ),
    Divergence(
        _only("form"),
        "elements",
        _any_of(
            *(ids(f"[{b}.order={x}]") for b in _BUILDERS for x in ("int64-max", "int64-min")),
            *(ids(f"[form_textarea.rows={x}]") for x in ("int64-max", "int64-min")),
        ),
        _BOTH_MODES,
        "order and rows: production int (send.py:325-466, WireS2C_Form*Element, JSONB), Maestro z.number().int() is a JavaScript safe "
        "integer,"
        " at most 2^53-1 in magnitude (public.ts:278,298)",
    ),
    Divergence(
        _only("update_user_event"),
        "event_id",
        NOT_ORDINARY,
        _BOTH_MODES,
        "event_id: production str (send.py:284); pipeline.py:956-960 looks the id up and only logs a warning when it finds no event;"
        " Maestro messageUpdatePayload.event_id must be a UUID (public.ts:384), so the run fails on any other text",
    ),
    Divergence(
        _only("update_user_event"),
        "content",
        ids("[text:text=empty]", "[text:text=blank]"),
        _BOTH_MODES,
        "an edited message with an empty text: production stores it (pipeline.py:962-963); Maestro message.update keeps the "
        "nonblank-content"
        " rule (public.ts:155-160,385; docs/contracts/README.md:17 says message.update keeps it on purpose)",
    ),
    Divergence(
        _only("update_user_event"),
        "content",
        prefixed("[audio:", "[image:", "[document:"),
        _BOTH_MODES,
        "an edited message with an audio, image or document part: production takes the wire part (WireS2C_*Part: type "
        "audio/image/document with"
        " a url, deserialize_s2c_part, pipeline.py:962-963); the SDK sends content as it came (outputs.py:194 _passthrough) and the Maestro"
        " message.update content accepts only text, attachment, contact and media_link parts (public.ts:129-145,147-163)",
    ),
    Divergence(
        _only("update_user_event"),
        "content",
        _any_of(
            ids("[contact:vcard=none]", "[contact:display_name=omitted]"),
            prefixed("[contact:display_name=long", "[contact:display_name=huge"),
        ),
        _BOTH_MODES,
        "an edited message with a contact part: production ContactPart has display_name default '' and vcard str | None (models.py:69-73);"
        " Maestro contactPart.display_name is required and max 512, vcard cannot be null (public.ts:129-134); the SDK passes the part "
        "through",
    ),
]


def _marks(case: Case) -> list[Any]:
    for divergence in DIVERGENCES:
        if divergence.matches(case.function.name, case.param, case.sample.id, case.mode):
            return [pytest.mark.xfail(strict=True, reason=f"divergência real, a corrigir: {divergence.reason}")]
    return []


CASES = build_cases()


def _failure(case: Case, outcome: Outcome) -> str:
    return (
        f"\n  function: {case.function.name}\n  field: {case.param}\n  production value: {case.describe_value()} ({case.sample.id})"
        f"\n  {case.where()}\n  {outcome.explain()}"
    )


@pytest.mark.parametrize("case", [pytest.param(case, id=case.id, marks=_marks(case)) for case in CASES])
async def test_production_value_is_valid_for_the_maestro_contract(case: Case) -> None:
    outcome = await run_case(case)
    if not outcome.ok:
        ceiling = is_declared_ceiling(case.sample.id, outcome.refusals[-1] if outcome.refusals else None)
        if ceiling is not None:
            pytest.xfail(f"declared ceiling, permanent: {ceiling.why}")
    assert outcome.ok, _failure(case, outcome)


# --- the guard guards itself ---------------------------------------------------------------------------------------------


def test_every_production_function_and_parameter_is_exercised() -> None:
    """A function or a parameter that production gains is in the cases, with no change here."""
    exercised: dict[str, set[str | None]] = {}
    for case in CASES:
        exercised.setdefault(case.function.name, set()).add(case.param)
    for function in event_functions():
        assert function.name in exercised, f"{function.name} has no case"
        for name in function.signature.parameters:
            assert name in exercised[function.name], f"{function.name}({name}) has no case: its value domain is empty"


@pytest.mark.parametrize("function", event_functions(), ids=lambda f: f.name)
async def test_the_ordinary_call_of_every_production_function_conforms(function: ProductionFunction) -> None:
    """The baseline of the other cases: if it failed, the failures of one field would hide behind it."""
    for case in (c for c in CASES if c.function is function and c.param is None):
        outcome = await run_case(case)
        assert outcome.ok, _failure(case, outcome)


def _same_or_broader(production: Any, maestro: Any) -> bool:
    """The Maestro annotation takes every value the production one takes (``Literal["a", "b"]`` -> ``str`` is fine)."""
    if production == maestro:
        return True
    return (
        typing.get_origin(production) is typing.Literal and maestro is str and all(isinstance(v, str) for v in typing.get_args(production))
    )


def test_maestro_send_keeps_every_production_signature() -> None:
    """A handler written for ``AgentSend`` calls ``MaestroSend`` the same way: same names, kinds, defaults and types."""
    for function in production_functions():
        target = _target(MaestroSend(), function)
        maestro = inspect.signature(target)
        hints = typing.get_type_hints(target)
        for name, parameter in function.signature.parameters.items():
            other = maestro.parameters.get(name)
            assert other is not None, f"{function.name}: MaestroSend lost the parameter {name!r}"
            assert (other.kind, other.default) == (parameter.kind, parameter.default), f"{function.name}({name}): kind or default changed"
            assert _same_or_broader(function.hints[name], hints[name]), f"{function.name}({name}): the type is narrower in MaestroSend"
        production_order = [n for n, p in function.signature.parameters.items() if p.kind is not inspect.Parameter.KEYWORD_ONLY]
        maestro_order = [n for n, p in maestro.parameters.items() if n in production_order and p.kind is not inspect.Parameter.KEYWORD_ONLY]
        assert maestro_order == production_order, f"{function.name}: the positional order changed in MaestroSend"
