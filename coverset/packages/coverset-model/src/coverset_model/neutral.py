"""The in-memory neutral bundle: the facts one test shard leaves behind, in no particular language.

Everything here is a frozen, slotted dataclass. Paths are repository-relative POSIX strings, test
ids are the framework's own (pytest nodeids, Jest's ``file > describe > name``), and a *function
triple* is ``(path, qualname, first line)`` where the first line is the declaration's first line
(decorators included), as the shape tracer reports it. ``coverset.lang.base`` re-exports every
name, so code that imported them from there keeps working.

:mod:`coverset_model.io` reads and writes the on-disk form (``docs/neutral-bundle.md``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

if TYPE_CHECKING:
    from coverset_model.types import TypeDescriptor

__all__ = [
    "IMPORT_CONTEXT",
    "ChangeKind",
    "CollectedItem",
    "FrozenMap",
    "FuncSampling",
    "FuncTriple",
    "FunctionChange",
    "FunctionKind",
    "FunctionRegion",
    "InputCov",
    "NeutralBundle",
    "Outcome",
    "ShapeObs",
    "StaticFile",
    "TestRow",
    "func_key",
]

IMPORT_CONTEXT = "<import>"
"""Pseudo test id holding coverage recorded outside any test (coverage.py's ``''`` context)."""

type FuncTriple = tuple[str, str, int]
"""``(path, qualname, first line)`` identifying a function as the shape tracer saw it."""

type Outcome = Literal["passed", "failed", "error", "skipped", "xfailed", "xpassed"]

type FunctionKind = Literal["function", "method", "class", "module"]

type ChangeKind = Literal["added", "deleted", "modified", "trivial", "renamed", "moved"]

type SamplingCap = Literal["sample", "full"]


def func_key(path: str, qualname: str, ordinal: int) -> str:
    """Return the stable 16-hex-digit key of a function: ``blake2b(path\\0qualname\\0ordinal, 8)``."""
    raw = f"{path}\0{qualname}\0{ordinal}".encode()
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


class FrozenMap[K, V](dict[K, V]):
    """A read-only ``dict`` (so it JSON-encodes, compares and pickles like one, but cannot change)."""

    __slots__ = ()

    def _readonly(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("FrozenMap is read-only")

    __setitem__ = __delitem__ = _readonly  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = __ior__ = _readonly  # type: ignore[assignment]

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenMap, (dict(self),))

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({dict.__repr__(self)})"

    @classmethod
    def of(cls, items: Mapping[K, V] | Iterable[tuple[K, V]] = ()) -> FrozenMap[K, V]:
        return items if isinstance(items, FrozenMap) else cls(items)


def _empty_map() -> FrozenMap[Any, Any]:
    return FrozenMap()


@dataclass(frozen=True, slots=True)
class CollectedItem:
    """One collected test (``tests.jsonl``; ``collect.json`` plus ``invariant_links.jsonl`` for pytest)."""

    nodeid: str
    file: str
    line: int | None
    markers: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    hypothesis: bool
    """A property-based test (Hypothesis, fast-check): ``property`` in ``tests.jsonl``."""


@dataclass(frozen=True, slots=True)
class TestRow:
    """The final, authoritative result of one test in one shard."""

    __test__ = False  # not a pytest test class

    nodeid: str
    file: str
    outcome: Outcome
    duration_s: float
    failed_phase: str | None
    fail_sig: str | None
    longrepr: str | None
    """The failure text (``message`` in ``tests.jsonl``)."""
    start: float | None
    stop: float | None


@dataclass(frozen=True, slots=True)
class StaticFile:
    """Static facts about one measured source file at the run's commit (``files.jsonl``)."""

    path: str
    n_lines: int
    statements: tuple[int, ...]
    excluded: tuple[int, ...]
    arcs_possible: tuple[tuple[int, int], ...]
    """Possible branch arcs (``branches`` in ``files.jsonl``); negative targets are exits."""


@dataclass(frozen=True, slots=True)
class ShapeObs:
    """Aggregated observations of one parameter shape for one ``(function, caller)`` pair.

    ``caller`` is a function triple when the caller is in-repo code, else the calling test's id.
    """

    func: FuncTriple
    param: str
    shape_key: str
    caller: FuncTriple | str
    samples: int
    tests: tuple[str, ...]
    truncated: bool
    example: str | None
    example_test: str | None


@dataclass(frozen=True, slots=True)
class FuncSampling:
    """How often the shape tracer sampled one function, and whether (and which) cap stopped it."""

    func: FuncTriple
    calls: int
    tests: tuple[str, ...]
    truncated: bool
    cap: SamplingCap | None = None
    """``"sample"`` (the adaptive cap), ``"full"`` (the full-trace safety cap) or None (not capped,
    or a producer that does not say which cap)."""


@dataclass(frozen=True, slots=True)
class FunctionRegion:
    """One function (or class/module region) of a source file (``functions.jsonl``)."""

    func_key: str
    path: str
    qualname: str
    ordinal: int
    def_line: int
    body_start: int
    end_line: int
    body_hash: str
    sig_hash: str
    params: tuple[str, ...]
    kind: FunctionKind

    @property
    def triple(self) -> FuncTriple:
        """The ``(path, qualname, first line)`` triple the shape tracer reports for this region."""
        return (self.path, self.qualname, self.def_line)


@dataclass(frozen=True, slots=True)
class FunctionChange:
    """How one function changed between two commits (``region`` is the head side)."""

    region: FunctionRegion | None
    prev: FunctionRegion | None
    kind: ChangeKind


@dataclass(frozen=True, slots=True)
class InputCov:
    """Input-class coverage of one parameter: the classes expected and those observed."""

    expected: tuple[str, ...]
    observed: tuple[str, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        """Expected classes no observed value fell into, in catalogue order."""
        seen = set(self.observed)
        return tuple(c for c in self.expected if c not in seen)

    @property
    def observed_expected(self) -> int:
        """How many expected classes were observed (the numerator of input-class coverage)."""
        return len(set(self.expected) & set(self.observed))


@dataclass(frozen=True, slots=True)
class NeutralBundle:
    """The language-neutral facts of one shard: what ``coverset.facts.ingest`` consumes.

    ``coverage`` maps test id → path → executed lines; the key :data:`IMPORT_CONTEXT` holds lines
    executed outside any test. ``run_arcs`` maps path → executed branch arcs (union over tests).
    ``functions`` are the function regions the bundle ships (``functions.jsonl``; empty for a
    language whose adapter indexes the sources itself, as Python's does) and ``types`` the declared
    type of each parameter (and ``return``) by function triple, None when unannotated.
    """

    meta: dict
    collected: tuple[CollectedItem, ...]
    tests: tuple[TestRow, ...]
    coverage: dict[str, dict[str, tuple[int, ...]]]
    static: dict[str, StaticFile]
    run_arcs: dict[str, tuple[tuple[int, int], ...]]
    shapes: tuple[ShapeObs, ...]
    sampling: tuple[FuncSampling, ...]
    durations: dict[str, float]
    functions: tuple[FunctionRegion, ...] = ()
    types: Mapping[FuncTriple, Mapping[str, TypeDescriptor | None]] = field(default_factory=_empty_map)

    def regions_for(self, path: str) -> Iterator[FunctionRegion]:
        """The shipped function regions of one file, in the order the bundle lists them."""
        return (r for r in self.functions if r.path == path)
