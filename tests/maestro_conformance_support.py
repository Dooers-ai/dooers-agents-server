"""Shared by the Maestro contract conformance guards (``tests/test_maestro_contract_conformance*.py``).

``SpyContracts`` is the real ``MaestroContracts`` (the vendored ``bundle.zip``, the validator that ``FakeMaestro`` and the SDK use)
that also remembers which schema refused a value, where and why: the real one only says ``CONTRACT_INVALID``.
``Divergence`` and ``DeclaredCeiling`` are how a guard states what it found.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dooers.agents.server.protocol.maestro import MaestroContractError
from tests.production_domain import string_samples


@dataclass
class Refusal:
    """A schema of the Maestro contract that refused a value."""

    schema: str
    path: str
    message: str
    #: The ``max`` of a string-length rule that refused it (``None`` for another rule).
    max_length: int | None = None

    def __str__(self) -> str:
        return f"{self.schema} at {self.path}: {self.message}"


class SpyContracts:
    """The real ``MaestroContracts``; a refusal also keeps which schema, where and why (the real one only says INVALID)."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.refusals: list[Refusal] = []

    def validate(self, schema: str, data: Any) -> Any:
        try:
            return self._real.validate(schema, data)
        except MaestroContractError:
            self.refusals.append(_explain(self._real, schema, data))
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


_DISCRIMINATORS = ("type", "kind", "scope_kind")


def _leaves(error: Any) -> list[Any]:
    """The keywords that failed, down the ``oneOf``/``anyOf`` branches; a branch of another ``type`` or ``kind`` is not the one."""
    if not error.context:
        return [error]
    branches: dict[Any, list[Any]] = {}
    for sub in error.context:
        branches.setdefault(sub.relative_schema_path[0], []).extend(_leaves(sub))

    def other_variant(leaf: Any) -> bool:
        return leaf.validator in ("const", "enum") and bool(leaf.absolute_path) and leaf.absolute_path[-1] in _DISCRIMINATORS

    candidates = [leaves for leaves in branches.values() if not any(other_variant(leaf) for leaf in leaves)]
    return [leaf for leaves in (candidates or list(branches.values())) for leaf in leaves]


def _explain(contracts: Any, schema: str, data: Any) -> Refusal:
    """The keyword that refused ``data`` (the real validator says only that it is invalid)."""
    errors = [leaf for top in contracts._compiled[schema].iter_errors(data) for leaf in _leaves(top)]
    if not errors:
        return Refusal(schema, "$", "refused by a semantic rule of the contract")
    error = max(errors, key=lambda e: len(e.absolute_path))
    path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
    message = re.sub(r"\s+", " ", str(error.message))
    limit = error.validator_value.get("max") if error.validator == "x-maestro-string-length" else None
    return Refusal(schema, path, message if len(message) <= 220 else message[:200] + f"... ({len(message)} chars)", limit)


@dataclass(frozen=True)
class Divergence:
    """A real divergence: production accepts the value and the Maestro contract (or the SDK conversion) refuses it.

    It runs as ``xfail(strict=True)``: when the contract or the SDK is fixed the case passes, the strict ``xfail`` fails and the
    entry must leave the list. Never a permanent difference.
    """

    #: Function names it applies to (``send.text``, ``Repository.create_event``).
    functions: frozenset[str]
    param: str
    #: Decides from the sample id (``long``, ``[form_text.order=int64-max]``...).
    samples: Callable[[str], bool]
    #: Where the guard is run (``link``/``upload`` for the media sends); ``None`` is every mode.
    modes: frozenset[str] | None
    #: What differs, with the production and the Maestro definition (file:line).
    reason: str

    def matches(self, function: str, param: str | None, sample_id: str, mode: str) -> bool:
        return param == self.param and function in self.functions and self.samples(sample_id) and (self.modes is None or mode in self.modes)


def ids(*names: str) -> Callable[[str], bool]:
    return lambda sample_id: sample_id in names


def prefixed(*prefixes: str) -> Callable[[str], bool]:
    return lambda sample_id: sample_id.startswith(prefixes)


#: Every free-text sample except the ordinary one (the value a handler normally passes).
_FREE_TEXT = frozenset(sample.id for sample in string_samples()) - {"plain"}
NOT_ORDINARY = lambda sample_id: sample_id in _FREE_TEXT  # noqa: E731
LONG_TEXT = ids("long", "long-unicode", "huge", "huge-unicode")
EMPTY_AND_LONG_TEXT = ids("empty", "long", "long-unicode", "huge", "huge-unicode")


@dataclass(frozen=True)
class DeclaredCeiling:
    """A length limit the project declares on purpose, not as a bug (``docs/contracts/README.md``, ``common.ts``, ``public.ts``)."""

    max_length: int
    why: str


#: Only the "huge" samples (100 000 characters) can reach these: they are the v1 ceilings that the Maestro contract states in its
#: source. ``public.ts:23`` "These are v1 contract ceilings, not production quotas"; ``public.ts:26`` ``text`` 64 Ki;
#: ``public.ts:200-201`` ``agentLabel`` 64 Ki ("far above any tool name, form label or chart title"); ``public.ts:205-206``
#: ``threadTitle`` 4096; ``docs/contracts/README.md:17`` "O teto é o de 64 Ki dos outros ids que o agente escreve".
DECLARED_CEILINGS = {
    65536: DeclaredCeiling(65536, "64 Ki characters: the declared ceiling of ids, names, labels and free text the agent writes"),
    4096: DeclaredCeiling(4096, "4096 characters: the declared ceiling of a thread title"),
}


def is_declared_ceiling(sample_id: str, refusal: Refusal | None) -> DeclaredCeiling | None:
    """The ceiling that explains a refusal of a "huge" sample, if it is one the project declares."""
    if refusal is None or refusal.max_length not in DECLARED_CEILINGS or "huge" not in sample_id:
        return None
    return DECLARED_CEILINGS[refusal.max_length]
