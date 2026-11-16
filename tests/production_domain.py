"""The values production accepts for each ``send.*`` parameter and each production model field.

This module is the *ruler* of ``tests/test_maestro_contract_conformance.py``. It knows production only: it reads the
signature and the type annotations of ``AgentSend`` / ``send.whatsapp`` and the fields of the production Pydantic models,
and it builds the values that production accepts for each of them. It knows nothing about Maestro.

What production accepts comes from its code:

- ``AgentSend`` annotations (``str``, ``Optional``, ``Literal``, defaults): ``inspect.signature`` and ``get_type_hints``.
- The Pydantic models that ``HandlerPipeline`` applies to a form element, a chart and an edited message part
  (``pipeline.py``: ``WireS2C_Form*Element``, ``WireS2C_ChartEventData``, ``deserialize_s2c_part``).
- The queue rule of ``thread_queue.QUEUE_SLUG_RE`` / ``resolve_thread_queue``.
- The column types of ``persistence/postgres.py``: every text column is ``TEXT`` and every structured column is ``JSONB``,
  so production has no length limit below the size of a database value. The "long" and "huge" samples use ``LONG`` and
  ``HUGE`` characters, sizes a handler can reasonably send (a long label, an LLM answer, a table), not limits of production.
- ``persistence.sanitize.without_nul``: production strips NUL characters before it stores a value, so a value with NUL is a
  value production accepts.

A sample is a ``Sample(id, value)``. ``OMIT`` is the sample that leaves the argument out (production uses its default).
"""

from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ValidationError

from dooers.agents.server.handlers.send import AgentEvent, AgentSend, _WhatsappSend
from dooers.agents.server.protocol import models
from dooers.agents.server.thread_queue import QUEUE_SLUG_RE, QueueValidationError, resolve_thread_queue

#: Characters of a "long" text (a long label, name or title) and of a "huge" one (an LLM answer, a table). Production has no
#: limit (``TEXT``/``JSONB``); these are large sizes a handler can send, not limits of production.
LONG = 1_000
HUGE = 100_000


@dataclass(frozen=True)
class Sample:
    id: str
    value: Any


class _Omit:
    def __repr__(self) -> str:
        return "<omitted>"


OMIT = Sample("omitted", _Omit())


def omitted(sample: Sample) -> bool:
    return isinstance(sample.value, _Omit)


# --- scalar domains -------------------------------------------------------------------------------------------------


def string_samples(ordinary: str = "valor") -> list[Sample]:
    """Free text: what a ``str`` parameter takes. The first sample is the ordinary one."""
    return [
        Sample("plain", ordinary),
        Sample("empty", ""),
        Sample("blank", "   "),
        Sample("unicode", "Ação — ünïcödé 日本語 😀 العربية"),
        Sample("long", "x" * LONG),
        Sample("long-unicode", "😀" * LONG),
        Sample("huge", "x" * HUGE),
        Sample("huge-unicode", "😀" * (HUGE // 4)),
        Sample("slug", "dooers-assistant"),
        Sample("path-chars", "pasta/arquivo\\nome.txt"),
        Sample("control-chars", "linha 1\nlinha 2\t\x01\x7f nul\x00 fim"),
        Sample("markup", "<b>\"aspas\" & 'apóstrofo'</b> {{x}} \\u0000"),
    ]


INT_SAMPLES = [
    Sample("zero", 0),
    Sample("one", 1),
    Sample("minus-one", -1),
    Sample("int32-max", 2**31 - 1),
    Sample("int32-min", -(2**31)),
    Sample("safe-int-max", 2**53 - 1),
    Sample("int64-max", 2**63 - 1),
    Sample("int64-min", -(2**63)),
]

FLOAT_SAMPLES = [
    Sample("zero", 0.0),
    Sample("fraction", 12.5),
    Sample("minus-one", -1.0),
    Sample("float-max", 1.7976931348623157e308),
    Sample("float-min-subnormal", 5e-324),
    Sample("float-lowest", -1.7976931348623157e308),
]

BOOL_SAMPLES = [Sample("true", True), Sample("false", False)]


def _deep(levels: int) -> Any:
    value: Any = "folha"
    for _ in range(levels):
        value = {"n": value}
    return value


def json_object_samples() -> list[Sample]:
    """Any JSON object: what a ``dict`` parameter takes (``JSONB`` columns)."""
    return [
        Sample("plain", {"consulta": "valor"}),
        Sample("empty", {}),
        Sample("nested-null", {"a": None, "b": [None, {"c": None}]}),
        Sample("unicode-keys", {"ação": "日本語", "😀": "x"}),
        Sample("empty-key", {"": "v"}),
        Sample("int-extremes", {"min": -(2**63), "max": 2**63 - 1, "safe": 2**53 - 1}),
        Sample("float-extremes", {"big": 1.7976931348623157e308, "tiny": 5e-324, "neg": -1.5, "zero": 0.0}),
        Sample("long-string", {"k": "x" * LONG}),
        Sample("huge-string", {"k": "x" * HUGE}),
        Sample("deep", _deep(30)),
        Sample("wide", {f"k{i}": i for i in range(2000)}),
        Sample("mixed-list", {"l": [True, False, 1, "1", None, 1.5]}),
    ]


# --- helpers over annotations ---------------------------------------------------------------------------------------


def _split_optional(annotation: Any) -> tuple[Any, bool]:
    """``X | None`` -> ``(X, True)``."""
    if get_origin(annotation) in (Union, types.UnionType):
        members = [m for m in get_args(annotation) if m is not type(None)]
        if len(members) != len(get_args(annotation)):
            return (members[0] if len(members) == 1 else Union[tuple(members)]), True  # noqa: UP007
    return annotation, False


def _discriminated_members(annotation: Any) -> list[type[BaseModel]] | None:
    """The member models of an ``Annotated[A | B, Field(discriminator=...)]``."""
    if get_origin(annotation) is typing.Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, types.UnionType):
        members = list(get_args(annotation))
        if members and all(isinstance(m, type) and issubclass(m, BaseModel) for m in members):
            return members
    return None


#: Ordinary values (the baseline of a call) for parameters whose type is free text but whose use is not: a handler passes a
#: URL, a MIME type or a file name. They are the ordinary production calls (``tests/parity/scenarios_send.py``,
#: ``docs/sdk-handler-reference.md``), never a domain limit: every other sample of the same parameter is also a production value.
ORDINARY: dict[str, Any] = {
    "url": "https://exemplo.com/arquivo.png",
    "mime_type": "image/png",
    "filename": "arquivo.pdf",
    "to_e164": "+5511999990000",
    "display_name": "Ana Souza",
    "instance_id": "inst-1",
    "template_name": "boas_vindas",
    "template_language": "pt_BR",
    "event_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "upload_url": "/uploads",
    "queue": "suporte",
}


#: Valid URLs of the kind a handler passes (a CDN file, a signed storage URL, a URL with Unicode); production only types ``url`` as ``str``.
URL_SAMPLES = [
    Sample("signed-url", "https://storage.exemplo.com/bucket/arquivo.png?X-Signature=" + "a" * 1_500),
    Sample("unicode-url", "https://exemplo.com/ação/日本語.png"),
    Sample("http-url", "http://exemplo.com/arquivo.png"),
]


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def samples_for(annotation: Any, *, name: str = "", required: bool = False, has_default: bool = False) -> list[Sample]:
    """Every sample production accepts for a value of type ``annotation``; the first one is the ordinary one.

    ``required``: no default (``None`` and an omitted argument are not production values). ``has_default``: the argument may
    be left out.
    """
    base, optional = _split_optional(annotation)
    samples = _samples_of(base, name)
    if name == "url":
        samples = [*samples, *URL_SAMPLES]
    if isinstance(ORDINARY.get(name), str):
        samples = [Sample("plain", ORDINARY[name]), *[s for s in samples if s.id != "plain"]]
    if optional:
        samples = [*samples, Sample("none", None)]
    if has_default and not required:
        samples = [*samples, OMIT]
    return samples


def _samples_of(base: Any, name: str) -> list[Sample]:  # noqa: C901
    origin = get_origin(base)
    if base is str:
        return string_samples()
    if base is bytes:
        return [Sample("plain", b"conteudo"), Sample("empty", b""), Sample("binary", bytes(range(256)))]
    if base is bool:
        return list(BOOL_SAMPLES)
    if base is int:
        return list(INT_SAMPLES)
    if base is float:
        return list(FLOAT_SAMPLES)
    if origin is Literal:
        return [Sample(f"literal:{value}", value) for value in get_args(base)]
    if base is dict or origin is dict:
        return json_object_samples()
    if base is Any:
        return [Sample("plain", "valor"), Sample("none", None), Sample("int", 1), Sample("object", {"k": "v"})]
    if origin is list or base is list:
        args = get_args(base)
        item = args[0] if args else Any
        return _list_samples(item, name)
    if _is_model(base):
        return model_samples(base)
    raise NotImplementedError(f"no value domain for the annotation {base!r} of {name!r}: add it to tests/production_domain.py")


def _list_samples(item: Any, name: str) -> list[Sample]:
    item_base, _ = _split_optional(item)
    if item_base is str:
        ordinary = ["valor"]
        return [
            Sample("plain", ordinary),
            Sample("empty", []),
            Sample("many", [f"item-{i}" for i in range(500)]),
            Sample("unicode", ["Ação", "日本語", "😀"]),
            Sample("empty-item", [""]),
            Sample("duplicates", ["a", "a"]),
            Sample("long-item", ["x" * LONG]),
            Sample("huge-item", ["x" * HUGE]),
        ]
    if item_base is dict or get_origin(item_base) is dict:
        objects = json_object_samples()
        plain = [objects[0].value]
        return [
            Sample("plain", plain),
            Sample("empty", []),
            *[Sample(f"item:{s.id}", [s.value]) for s in objects if s.id != "plain"],
            Sample("many", [{"i": i, "v": "x"} for i in range(2000)]),
        ]
    members = _discriminated_members(item_base)
    if members:
        return [
            Sample("plain", [model_samples(members[0])[0].value]),
            Sample("empty", []),
            *[
                Sample(f"[{m.model_fields['type'].default}:{s.id}]", [s.value])
                for m in members
                for s in model_samples(m)
                if not (m is members[0] and s.id == "plain")
            ],
        ]
    raise NotImplementedError(f"no value domain for list of {item!r} ({name!r})")


# --- Pydantic models (what the production pipeline validates) --------------------------------------------------------


def model_samples(model: type[BaseModel]) -> list[Sample]:
    """The model's values as dicts: one ordinary value, then each field varied alone over its own domain.

    Only values the model itself accepts (production builds the model from them) are returned.
    """
    hints = get_type_hints(model)
    baseline: dict[str, Any] = {}
    domains: dict[str, list[Sample]] = {}
    for field_name, field in model.model_fields.items():
        required = field.is_required()
        domains[field_name] = samples_for(hints[field_name], name=field_name, required=required, has_default=not required)
        if field_name == "type":  # the discriminator of a union: it names the model, it is not a value to vary
            domains[field_name] = domains[field_name][:1]
        first = next((s for s in domains[field_name] if not omitted(s)), None)
        baseline[field_name] = ORDINARY.get(field_name, first.value if first else None)
        if not required and field.default is None:
            baseline.pop(field_name)  # an optional field with no default: the ordinary value leaves it out
    out = [Sample("plain", dict(baseline))]
    for field_name, samples in domains.items():
        for sample in samples:
            candidate = {k: v for k, v in baseline.items() if k != field_name}
            if not omitted(sample):
                candidate[field_name] = sample.value
            if candidate == baseline:
                continue
            out.append(Sample(f"{field_name}={sample.id}", candidate))
    return [s for s in out if _accepts(model, s.value)]


def _accepts(model: type[BaseModel], value: dict[str, Any]) -> bool:
    try:
        model(**value)
    except ValidationError:
        return False
    return True


# --- production functions -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionFunction:
    #: ``send.text``, ``send.whatsapp.image`` ...
    name: str
    #: The attribute path on a ``send`` object (``("text",)``, ``("whatsapp", "image")``).
    path: tuple[str, ...]
    signature: inspect.Signature
    hints: dict[str, Any]
    #: ``True`` for a function that returns an ``AgentEvent``; the ``dict`` builders return a part of an event.
    builds_event: bool
    #: The function object of the production class (for the source line).
    function: Any


def _public_functions(owner: type) -> dict[str, Any]:
    return {n: f for n, f in inspect.getmembers(owner, predicate=inspect.isfunction) if not n.startswith("_")}


def _describe(name: str, path: tuple[str, ...], function: Any) -> ProductionFunction:
    signature = inspect.signature(function)
    params = [p for p in signature.parameters.values() if p.name != "self"]
    hints = get_type_hints(function)
    return ProductionFunction(name, path, signature.replace(parameters=params), hints, hints.get("return") is AgentEvent, function)


def production_functions() -> list[ProductionFunction]:
    """Every public function of production ``AgentSend`` and ``send.whatsapp`` (read from the classes, so a new one enters alone)."""
    functions = [_describe(f"send.{n}", (n,), f) for n, f in _public_functions(AgentSend).items()]
    functions += [_describe(f"send.whatsapp.{n}", ("whatsapp", n), f) for n, f in _public_functions(_WhatsappSend).items()]
    return functions


def parameter_domain(function: ProductionFunction, parameter: inspect.Parameter) -> list[Sample]:
    """Samples of one parameter of a production function, from its annotation and default.

    A ``list[dict]`` that production validates with a Pydantic model before it stores it (form elements, form options, chart
    series, the parts of an edited message) takes its samples from that model or from the builder that makes the dict.
    """
    has_default = parameter.default is not inspect.Parameter.empty
    name = parameter.name
    if name == "queue" and function.name == "send.update_thread":
        samples = queue_samples()
    elif name == "elements" and function.name == "send.form":
        samples = element_samples()
    elif name == "series" and function.name == "send.chart":
        samples = series_samples()
    elif name == "options":
        samples = option_samples()
    elif name == "content" and function.name == "send.update_user_event":
        samples = part_samples()
    else:
        return samples_for(function.hints[name], name=name, required=not has_default, has_default=has_default)
    _, optional = _split_optional(function.hints[name])
    if optional and not any(s.value is None for s in samples):
        samples = [*samples, Sample("none", None)]
    return [*samples, OMIT] if has_default else samples


def _call_builder(function: ProductionFunction, send: Any, kwargs: dict[str, Any]) -> Any:
    target: Any = send
    for step in function.path:
        target = getattr(target, step)
    return target(**kwargs)


def builder_samples(function: ProductionFunction) -> list[Sample]:
    """The dicts a production builder (``form_text``, ``chart_series``...) makes: the ordinary call, then each parameter alone."""
    send = AgentSend()
    parameters = list(function.signature.parameters.values())
    ordinary = {p.name: parameter_domain(function, p)[0].value for p in parameters if p.default is inspect.Parameter.empty}
    out = [Sample("plain", _call_builder(function, send, ordinary))]
    for parameter in parameters:
        for sample in parameter_domain(function, parameter):
            kwargs = {k: v for k, v in ordinary.items() if k != parameter.name}
            if not omitted(sample):
                kwargs[parameter.name] = sample.value
            if kwargs == ordinary:
                continue
            out.append(Sample(f"{function.name.removeprefix('send.')}.{parameter.name}={sample.id}", _call_builder(function, send, kwargs)))
    return out


def element_builders() -> list[ProductionFunction]:
    """The ``AgentSend.form_*`` builders (they return a dict that ``send.form`` takes in ``elements``)."""
    return [
        _describe(f"send.{n}", (n,), f)
        for n, f in _public_functions(AgentSend).items()
        if n.startswith("form_") and get_type_hints(f).get("return") is dict
    ]


def element_samples() -> list[Sample]:
    """``send.form(elements=...)``: one list with every element, then one element of each builder with each parameter varied alone."""
    models_by_type = form_element_models()
    builders = element_builders()
    assert builders, "AgentSend has no form_* builder"
    every: list[Sample] = []
    singles: list[Sample] = []
    for builder in builders:
        accepted = [s for s in builder_samples(builder) if _accepts(models_by_type[s.value["type"]], s.value)]
        every.append(accepted[0])
        singles += [Sample(f"[{s.id}]", [s.value]) for s in accepted]
    return [Sample("[every-builder]", [s.value for s in every]), *singles, Sample("[]", [])]


def series_samples() -> list[Sample]:
    builder = next(f for f in production_functions() if f.name == "send.chart_series")
    samples = [s for s in builder_samples(builder) if _accepts(models.WireS2C_ChartSeries, s.value)]
    return [Sample(f"[{s.id}]", [s.value]) for s in samples] + [Sample("[]", [])]


def option_samples() -> list[Sample]:
    """``options`` of select, radio and checkbox: ``FormOption`` items."""
    items = model_samples(models.FormOption)
    return [Sample(f"[{s.id}]", [s.value]) for s in items] + [
        Sample("[]", []),
        Sample("[many]", [{"value": f"v{i}", "label": f"Opção {i}"} for i in range(300)]),
    ]


def part_samples() -> list[Sample]:
    """``send.update_user_event(content=...)``: each S2C part type, each field varied alone (``deserialize_s2c_part``)."""
    out: list[Sample] = []
    for part_type, model in content_part_models().items():
        out += [Sample(f"[{part_type}:{s.id}]", [s.value]) for s in model_samples(model)]
    return out


# --- production acceptance filters ---------------------------------------------------------------------------------


def queue_accepted(value: Any) -> bool:
    """``HandlerPipeline`` ``thread_update``: ``resolve_thread_queue`` on a workspace thread without a catalog."""
    try:
        resolve_thread_queue(value, workspace_id="ws", allowed=None)
    except QueueValidationError:
        return False
    return True


def queue_samples() -> list[Sample]:
    """Slugs ``QUEUE_SLUG_RE`` accepts at its limits, plus what ``normalize_queue_slug`` normalises (strip, lower, empty clears)."""
    assert QUEUE_SLUG_RE.pattern == r"^[a-z][a-z0-9-]{0,31}$", "the queue rule of production changed: update queue_samples"
    candidates = [
        Sample("plain", "suporte"),
        Sample("one-char", "a"),
        Sample("max-length", "a" + "b" * 31),
        Sample("digits-hyphen", "fila-2-b"),
        Sample("padded-upper", "  Suporte  "),
        Sample("empty-clears", ""),
        Sample("none", None),
    ]
    return [s for s in candidates if queue_accepted(s.value)]


def is_discriminated(annotation: Any) -> list[type[BaseModel]] | None:
    return _discriminated_members(annotation)


def form_element_models() -> dict[str, type[BaseModel]]:
    """``type`` -> model of every form element the pipeline validates (``WireS2C_FormElement``)."""
    members = _discriminated_members(models.WireS2C_FormElement) or []
    return {m.model_fields["type"].default: m for m in members}


def content_part_models() -> dict[str, type[BaseModel]]:
    """``type`` -> model of every S2C content part (``deserialize_s2c_part``)."""
    members = _discriminated_members(models.WireS2C_ContentPart) or []
    return {m.model_fields["type"].default: m for m in members}
