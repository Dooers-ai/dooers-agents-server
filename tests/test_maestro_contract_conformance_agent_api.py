"""Guard: what production lets a caller write through ``Repository`` and ``AgentServer.ingest`` is valid for the Maestro contract.

The sibling of ``tests/test_maestro_contract_conformance.py``. That one covers what a handler yields (``send.*``). This one covers
the production models a caller writes: ``Thread`` (``Repository.create_thread``/``update_thread``), ``ThreadEvent`` with its content
parts and ``User`` (``Repository.create_event``) and the message of a channel (``AgentServer.ingest``). In Maestro mode they go
through ``MaestroAgentPersistence``/``MaestroIngress`` and the agent API, which validates each body against
``Public.AgentThreadCreateRequest``, ``Public.AgentThreadPatchRequest``, ``Public.AgentEventCreateRequest`` and the participant and
actor schemas before it sends it.

The values come from production, as in the sibling guard (``tests/production_domain.py``): the signature of the production function,
the Pydantic models it builds (``Thread``, ``ThreadEvent``, ``User`` and the content parts), the queue rule. One parameter varies at
a time over the ordinary call. The call is the real one, over the loopback agent API (``tests/parity/fake_agent_api.py``), and the
contract is the real one wrapped to say which schema refused a value.

Declared by the project, so they are not a failure of the guard (each is pinned by a test below):

- ``agent_id``, ``organization_id`` and ``workspace_id``: the token of the agent covers one agent, one organization and one workspace
  (``MaestroAgentApi.check_scope``; Maestro README "Agente" and "Escopo"). The values of these three are the ones the token covers.
  Any other value is ``MaestroAgentScopeError``.
- ``MaestroAgentUnsupported``: ``Repository.create_event`` publishes only a ``message`` of the assistant or of a person, with text,
  contact and http(s) file parts; ``AgentServer.ingest`` only the channels Maestro has a contact for (Maestro README "Diferenças
  declaradas"). The SDK raises before it writes. The README says the decision to close them is open.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import typing
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

import pytest
from pydantic import TypeAdapter

import dooers.agents.server.maestro.agent_api as agent_api_module
import dooers.agents.server.observability.otel as otel
import dooers.agents.server.server as server_module
from dooers.agents.server.maestro.agent_api import MaestroAgentApi, MaestroAgentApiError, MaestroAgentScopeError
from dooers.agents.server.maestro.agent_persistence import MaestroAgentPersistence, MaestroAgentUnsupported
from dooers.agents.server.protocol import models
from dooers.agents.server.protocol.maestro import default_contracts
from dooers.agents.server.repository import Repository
from dooers.agents.server.server import AgentServer
from tests.maestro_conformance_support import (
    Divergence,
    Refusal,
    SpyContracts,
)
from tests.parity.fake_agent_api import AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, FakeAgentApi
from tests.parity.memory_persistence import MemoryPersistence
from tests.production_domain import (
    OMIT,
    Sample,
    _split_optional,
    omitted,
    queue_samples,
    samples_for,
    string_samples,
)
from tests.test_maestro_ingest import agent_config

ASSISTANT = "Assistente"
ANA = models.User(user_id="user-1", user_name="Ana Souza", user_email="ana@example.com", identity_ids=["idp-ana"])

#: The values the token of the fake agent covers (declared: ``MaestroAgentApi.check_scope``, Maestro README "Agente").
SCOPE_SAMPLES = {
    "agent_id": [Sample("plain", AGENT_ID), Sample("empty", ""), Sample("upper-case", AGENT_ID.upper())],
    "organization_id": [Sample("plain", ORGANIZATION_ID), Sample("empty", ""), Sample("upper-case", ORGANIZATION_ID.upper())],
    "workspace_id": [Sample("plain", WORKSPACE_ID)],
}


# --- production functions ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteFunction:
    """A production function that writes a model through the agent API in Maestro mode."""

    name: str
    function: Callable[..., Any]
    #: Where the type of a parameter comes from, when the function takes it as ``str`` and passes it to a model that restricts it.
    model: type[models.BaseModel] | None = None
    #: Parameters this guard does not vary (a precondition, not a value of the domain), and why.
    fixed: dict[str, str] = field(default_factory=dict)

    @property
    def signature(self) -> inspect.Signature:
        parameters = [p for p in inspect.signature(self.function).parameters.values() if p.name != "self"]
        return inspect.Signature(parameters)

    def hint(self, name: str) -> Any:
        """The production type of a parameter: the model's own when the function hands the value straight to the model.

        ``Repository.create_event(content: list[ContentPart])`` is typed with the handler parts, but ``ThreadEvent.content`` takes the
        wire parts (``WireS2C_ContentPart``) and rejects the handler parts: the model is what production accepts.
        """
        hints = typing.get_type_hints(self.function)
        if self.model is None or name not in self.model.model_fields:
            return hints[name]
        model_hint = typing.get_type_hints(self.model)[name]
        _, optional = _split_optional(hints[name])
        return typing.Optional[model_hint] if optional else model_hint  # noqa: UP045


WRITE_FUNCTIONS = [
    WriteFunction("Repository.create_thread", Repository.create_thread, models.Thread),
    WriteFunction(
        "Repository.update_thread",
        Repository.update_thread,
        fixed={"thread_id": "the thread must exist; its id is whatever create_thread returned"},
    ),
    WriteFunction(
        "Repository.create_event",
        Repository.create_event,
        models.ThreadEvent,
        fixed={"thread_id": "the thread must exist; its id is whatever create_thread returned"},
    ),
    WriteFunction("AgentServer.ingest", AgentServer.ingest),
]
UPDATE_FIELDS = {"title": models.Thread, "queue": models.Thread}  # ``Repository.update_thread`` reads only these two keys


def parameters(function: WriteFunction) -> list[tuple[str, Any, bool]]:
    """``(name, annotation, has_default)`` of each parameter the guard varies."""
    if function.name == "Repository.update_thread":
        hints = typing.get_type_hints(models.Thread)
        return [(name, hints[name], True) for name in UPDATE_FIELDS]
    return [
        (p.name, function.hint(p.name), p.default is not inspect.Parameter.empty)
        for p in function.signature.parameters.values()
        if p.kind is not inspect.Parameter.VAR_KEYWORD and p.name not in function.fixed
    ]


def domain(function: WriteFunction, name: str, annotation: Any, has_default: bool) -> list[Sample]:
    if name in SCOPE_SAMPLES:
        return SCOPE_SAMPLES[name]
    if name == "queue" and function.name == "Repository.update_thread":
        # ``Repository.update_thread`` stores any text (``thread.queue = updates["queue"]``, no ``resolve_thread_queue``).
        return [*queue_samples(), *[s for s in string_samples() if s.id != "plain"], OMIT]
    if name == "queue":
        return [*queue_samples(), *([OMIT] if has_default else [])]
    if function.name == "AgentServer.ingest" and name == "content":
        return ingest_content_samples()
    if function.name == "AgentServer.ingest" and name == "actor":
        # Production raises a ValidationError for ``actor="assistant"`` (``ThreadEvent.user`` is ``None``: Maestro README F13), so only
        # ``user`` is a value production accepts.
        return [Sample("literal:user", "user")]
    if function.name == "AgentServer.ingest" and name == "queues":
        return [Sample("plain", None)]  # only read by the handler context, never written
    if function.name == "AgentServer.ingest" and name == "channel":
        return [Sample("plain", "dooers-platform"), Sample("whatsapp", "whatsapp"), *[s for s in string_samples() if s.id != "plain"]]
    return samples_for(annotation, name=name, required=not has_default, has_default=has_default)


def ingest_content_samples() -> list[Sample]:
    """``ingest(content=...)``: the text and contact parts of the channel wire format (a file needs the upload pipeline)."""
    out = [Sample("plain", None)]
    for part in (models.WireC2S_TextPart, models.WireC2S_ContactPart):
        out += [Sample(f"[{part.model_fields['type'].default}:{s.id}]", [s.value]) for s in samples_for(part, name="content")]
    return out


# --- the stack: real SDK objects over the loopback agent API ---------------------------------------------------------------


@dataclass
class Stack:
    fake: FakeAgentApi
    spy: SpyContracts
    repository: Repository
    server: AgentServer


@contextmanager
def stack_patches() -> Iterator[SpyContracts]:
    """The agent API of the persistence and the one of ``AgentServer.ingest`` both validate with this contract."""
    spy = SpyContracts(default_contracts())
    with (
        mock.patch.object(agent_api_module, "default_contracts", lambda: spy),
        mock.patch.object(server_module, "_build_persistence", lambda _config: MemoryPersistence()),
        mock.patch.object(otel, "init_otel", lambda **_kwargs: None),
        mock.patch.object(otel, "_otel_enabled", False),
    ):
        yield spy


@dataclass
class Outcome:
    ok: bool
    refusals: list[Refusal] = field(default_factory=list)
    #: ``unsupported`` (``MaestroAgentUnsupported``), ``scope`` (``MaestroAgentScopeError``) or ``other``.
    kind: str = "ok"
    detail: str = ""

    def explain(self) -> str:
        if self.refusals:
            return f"Maestro refused it: {self.refusals[-1]}"
        return f"{self.kind}: {self.detail}"


def _coerce(annotation: Any, value: Any) -> Any:
    """The sample as the object the production function takes (a ``User``, a list of content parts...)."""
    if value is None or omitted(Sample("", value)):
        return value
    return TypeAdapter(annotation).validate_python(value)


@dataclass(frozen=True)
class Case:
    function: WriteFunction
    param: str | None
    sample: Sample
    kwargs: dict[str, Any]

    @property
    def id(self) -> str:
        return f"{self.function.name}({'ordinary' if self.param is None else f'{self.param}={self.sample.id}'})"

    def describe_value(self) -> str:
        text = repr(self.sample.value)
        return text if len(text) <= 90 else f"{text[:80]}... ({len(text)} chars)"

    def where(self) -> str:
        function = self.function.function
        return f"production {self.function.name}({self.param}), {inspect.getsourcefile(function)}:{inspect.getsourcelines(function)[1]}"


#: A parameter that needs another one set for its value to be written: ``Repository.create_event`` writes ``user`` only for
#: ``actor="user"``.
CONTEXT = {("Repository.create_event", "user"): {"actor": "user"}}
#: ``ThreadEvent`` takes these two as a ``Literal``: the ``Repository`` signature says ``str``, the model is the production rule.
BASELINE_OVERRIDES = {
    "Repository.create_event": {"type": "message", "actor": "assistant", "content": [{"type": "text", "text": "Olá"}]},
    "Repository.create_thread": {"agent_id": AGENT_ID, "organization_id": ORGANIZATION_ID, "workspace_id": WORKSPACE_ID},
    "AgentServer.ingest": {
        "agent_id": AGENT_ID,
        "organization_id": ORGANIZATION_ID,
        "workspace_id": WORKSPACE_ID,
        "message": "Oi",
        "user": {"user_id": "user-1"},
        "actor": "user",
    },
}


def ordinary_kwargs(function: WriteFunction) -> dict[str, Any]:
    """Each required parameter at its ordinary value; every other one left out. Values are plain data, coerced when the call is made."""
    out: dict[str, Any] = {}
    for name, annotation, has_default in parameters(function):
        if not has_default:
            out[name] = domain(function, name, annotation, has_default)[0].value
    return {**out, **BASELINE_OVERRIDES.get(function.name, {})}


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for function in WRITE_FUNCTIONS:
        ordinary = ordinary_kwargs(function)
        cases.append(Case(function, None, Sample("plain", None), ordinary))
        for name, annotation, has_default in parameters(function):
            for sample in domain(function, name, annotation, has_default):
                kwargs = {k: v for k, v in ordinary.items() if k != name}
                kwargs.update(CONTEXT.get((function.name, name), {}))
                if not omitted(sample):
                    kwargs[name] = sample.value
                if kwargs == ordinary:
                    continue
                cases.append(Case(function, name, sample, kwargs))
    return cases


def _typed(function: WriteFunction, kwargs: dict[str, Any]) -> dict[str, Any]:
    annotations = {name: annotation for name, annotation, _ in parameters(function)}
    return {name: _coerce(annotations[name], value) if name in annotations else value for name, value in kwargs.items()}


async def run_case(stack: Stack, case: Case) -> Outcome:
    function, kwargs = case.function, _typed(case.function, case.kwargs)
    stack.spy.refusals.clear()
    try:
        if function.name == "Repository.create_thread":
            await stack.repository.create_thread(**kwargs)
        elif function.name == "AgentServer.ingest":
            await stack.server.ingest(**kwargs)
        else:
            thread = await stack.repository.create_thread(AGENT_ID, ORGANIZATION_ID, WORKSPACE_ID, user=ANA)
            stack.spy.refusals.clear()
            if function.name == "Repository.update_thread":
                await stack.repository.update_thread(thread.id, **kwargs)
            else:
                await stack.repository.create_event(thread.id, **kwargs)
    except MaestroAgentUnsupported as error:
        return Outcome(False, list(stack.spy.refusals), "unsupported", str(error))
    except MaestroAgentScopeError:
        return Outcome(False, list(stack.spy.refusals), "scope", "MaestroAgentScopeError")
    except MaestroAgentApiError as error:
        return Outcome(False, list(stack.spy.refusals), "other", f"MaestroAgentApiError {error.code}")
    return Outcome(True)


async def _run_all(cases: list[Case]) -> dict[str, Outcome]:
    """Every case against one loopback agent API (a server per case would take minutes)."""
    logging.disable(logging.CRITICAL)
    fake = await FakeAgentApi(workspace_id=WORKSPACE_ID).start()
    try:
        with stack_patches() as spy:
            repository = Repository(
                MaestroAgentPersistence(MaestroAgentApi(fake.config()), assistant_name=ASSISTANT, local=MemoryPersistence())
            )
            server = AgentServer(agent_config(thread_management="maestro", maestro=fake.config()))
            stack = Stack(fake, spy, repository, server)
            try:
                return {case.id: await run_case(stack, case) for case in cases}
            finally:
                await server.close()
    finally:
        logging.disable(logging.NOTSET)
        await fake.stop()


@pytest.fixture(scope="module")
def outcomes() -> dict[str, Outcome]:
    return asyncio.run(_run_all(CASES))


# --- what this guard found and nobody fixed yet -------------------------------------------------------------------------------
#
# Every group the guard found (D01-D12) is fixed except one case of D03, stopped by a decision of the design: the person of a
# Maestro thread (the owner of Repository.create_thread and the user of AgentServer.ingest) is a principal the core authorizes,
# and a production user_id that cannot be a core principal (longer than 256 characters or with a control character) is a
# person the core does not know. The contract takes the value; Maestro answers what the core would (403 scope_mismatch). A
# fix needs a person in a Maestro thread that is not a core principal, which the design does not have. The user decides.
# Any entry runs as ``xfail(strict=True)`` and is never a permanent difference.

_NOT_CORE_PRINCIPAL = ("user_id=long", "user_id=long-unicode", "user_id=huge", "user_id=huge-unicode", "user_id=control-chars")

DIVERGENCES: list[Divergence] = [
    Divergence(
        frozenset({"Repository.create_thread", "AgentServer.ingest"}),
        "user",
        lambda sample_id: sample_id in _NOT_CORE_PRINCIPAL,
        None,
        "User.user_id is any text in production (models.py:391-401, JSONB); in Maestro the owner of the thread and the user of"
        " ingest are core principals (Public.AgentThreadCreateRequest takes the value; agent-api/threads.ts answers 403"
        " scope_mismatch, the core's answer for a person it does not know: opaquePrincipalSchema, common.ts:5-10)",
        blocked_by="the person of a Maestro thread is a principal the core authorizes; access rules equal to legacy, 22/10/2026",
    ),
]


CASES = build_cases()


def _marks(case: Case) -> list[Any]:
    for divergence in DIVERGENCES:
        if divergence.matches(case.function.name, case.param, case.sample.id, ""):
            return [pytest.mark.xfail(strict=True, reason=divergence.mark_reason())]
    return []


def _failure(case: Case, outcome: Outcome) -> str:
    return (
        f"\n  function: {case.function.name}\n  field: {case.param}\n  production value: {case.describe_value()} ({case.sample.id})"
        f"\n  {case.where()}\n  {outcome.explain()}"
    )


#: The inputs the Maestro README declares ``MaestroAgentUnsupported`` ("Diferenças declaradas", "ingest e chat_upload pela API do agente"):
#: a type other than ``message``; an actor without a person; a message without a part; a channel
#: without an external contact; a person without an id. Any other ``MaestroAgentUnsupported`` is a divergence, not a declared difference.
DECLARED_UNSUPPORTED = re.compile(
    r"create_event: type '.+' has no route in the agent API \(only 'message'\)"
    r"|create_event: actor '.+' without a person cannot be published"
    r"|create_event: a message needs at least one part"
    r"|ingest: channel '.*' has no external contact in the agent API"
    r"|ingest: a message of a person without user_id"
    r"|ingest: a channel message of a contact without an id",
    re.DOTALL,
)


def _declared_unsupported(case: Case, outcome: Outcome) -> bool:
    return outcome.kind == "unsupported" and not outcome.refusals and DECLARED_UNSUPPORTED.search(outcome.detail) is not None


#: ``Repository.update_thread`` takes any text as a queue and the server decides what is a valid queue (Maestro README, "Diferenças
#: declaradas": "fila inválida é decisão do servidor (produção não valida)"). The slug rule is the server's, not the contract schema's.
def _declared_server_queue(case: Case, outcome: Outcome) -> bool:
    free_text = {s.id for s in string_samples()} - {"plain"}
    return case.function.name == "Repository.update_thread" and case.param == "queue" and case.sample.id in free_text and not outcome.ok


@pytest.mark.parametrize("case", [pytest.param(case, id=case.id, marks=_marks(case)) for case in CASES])
def test_production_value_is_valid_for_the_maestro_contract(outcomes: dict[str, Outcome], case: Case) -> None:
    outcome = outcomes[case.id]
    if not outcome.ok:
        if _declared_unsupported(case, outcome):
            pytest.xfail(f"declared, MaestroAgentUnsupported (Maestro README, Diferenças declaradas): {outcome.detail}")
        if _declared_server_queue(case, outcome):
            pytest.xfail("declared: update_thread takes any queue text and the server decides (Maestro README, Diferenças declaradas)")
    assert outcome.ok, _failure(case, outcome)


# --- the guard guards itself ---------------------------------------------------------------------------------------------------


def test_every_parameter_of_every_write_function_is_exercised() -> None:
    exercised: dict[str, set[str | None]] = {}
    for case in CASES:
        exercised.setdefault(case.function.name, set()).add(case.param)
    for function in WRITE_FUNCTIONS:
        for name, annotation, has_default in parameters(function):
            if len(domain(function, name, annotation, has_default)) == 1:
                continue  # one value only (the token's scope, the only actor production accepts): the ordinary call is its case
            assert name in exercised[function.name], f"{function.name}({name}) has no case"


async def test_a_value_outside_the_token_scope_is_the_declared_scope_error() -> None:
    """The scope values are the token's: another agent, organization or workspace is ``MaestroAgentScopeError``, never a write."""
    fake = await FakeAgentApi(workspace_id=WORKSPACE_ID).start()
    try:
        repository = Repository(
            MaestroAgentPersistence(MaestroAgentApi(fake.config()), assistant_name=ASSISTANT, local=MemoryPersistence())
        )
        for name, value in (("agent_id", "dooers-assistant"), ("organization_id", "other-org"), ("workspace_id", "other-ws")):
            kwargs = {**ordinary_kwargs(WRITE_FUNCTIONS[0]), name: value}
            with pytest.raises(MaestroAgentScopeError):
                await repository.create_thread(**kwargs)
        assert not [r for r in fake.requests if r.method == "POST"]
    finally:
        await fake.stop()
