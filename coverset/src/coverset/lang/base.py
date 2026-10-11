"""Language-neutral contracts shared by ``facts/``, ``analysis/`` and the language adapters.

Everything here is a frozen, slotted dataclass or a Protocol. Paths are git-root-relative POSIX
strings, test ids are pytest nodeids, and a *function triple* is ``(path, qualname, firstlineno)``
where ``firstlineno`` is ``co_firstlineno`` (the first decorator line, else the ``def`` line).

``facts/`` imports only this module (never ``lang.python`` or ``analysis``), so the function-region
types that both sides need (:class:`FunctionRegion`, :class:`FunctionChange`) live here and
``coverset.analysis.functions`` re-exports them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "IMPORT_CONTEXT",
    "Catalogue",
    "CollectedItem",
    "EnvSpec",
    "FuncSampling",
    "FuncTriple",
    "FunctionChange",
    "FunctionRegion",
    "InputCov",
    "LanguageAdapter",
    "NeutralBundle",
    "Outcome",
    "PriorRun",
    "ShapeObs",
    "StaticFile",
    "TestRow",
    "func_key",
]

IMPORT_CONTEXT = "<import>"
"""Pseudo test id holding coverage recorded outside any test (coverage.py's ``''`` context)."""

type FuncTriple = tuple[str, str, int]
"""``(path, qualname, firstlineno)`` identifying a code object as the shape tracer saw it."""

type Outcome = Literal["passed", "failed", "error", "skipped", "xfailed", "xpassed"]

type FunctionKind = Literal["function", "method", "class", "module"]

type ChangeKind = Literal["added", "deleted", "modified", "trivial", "renamed", "moved"]


def func_key(path: str, qualname: str, ordinal: int) -> str:
    """Return the stable 16-hex-digit key of a function: ``blake2b(path\\0qualname\\0ordinal, 8)``."""
    raw = f"{path}\0{qualname}\0{ordinal}".encode()
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


@dataclass(frozen=True, slots=True)
class CollectedItem:
    """One collected test item, from ``collect.json`` (plus ``invariant_links.jsonl``)."""

    nodeid: str
    file: str
    line: int | None
    markers: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    hypothesis: bool


@dataclass(frozen=True, slots=True)
class TestRow:
    """The final, authoritative result of one test in one shard (``results.jsonl``)."""

    __test__ = False  # not a pytest test class

    nodeid: str
    file: str
    outcome: Outcome
    duration_s: float
    failed_phase: str | None
    fail_sig: str | None
    longrepr: str | None
    start: float | None
    stop: float | None


@dataclass(frozen=True, slots=True)
class StaticFile:
    """Static facts about one measured source file at the run's commit."""

    path: str
    n_lines: int
    statements: tuple[int, ...]
    excluded: tuple[int, ...]
    arcs_possible: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class ShapeObs:
    """Aggregated observations of one parameter shape for one ``(function, caller)`` pair.

    ``caller`` is a function triple when the caller is in-repo code, else the calling test's nodeid.
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
    """How often the shape tracer sampled one function, and whether it hit the adaptive cap."""

    func: FuncTriple
    calls: int
    tests: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class NeutralBundle:
    """The language-neutral facts of one shard: what ``facts.ingest`` consumes.

    ``coverage`` maps nodeid → path → executed lines; the key :data:`IMPORT_CONTEXT` holds lines
    executed outside any test. ``run_arcs`` maps path → executed branch arcs (union over tests).
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


@dataclass(frozen=True, slots=True)
class EnvSpec:
    """Everything an executor needs to build (or find) the environment a repo's tests run in."""

    python: str
    lock_hash: str
    requirements_txt: str
    plugin_wheel: Path | None
    source_root: str
    env_hash: str


@dataclass(frozen=True, slots=True)
class FunctionRegion:
    """One function (or class/module region) of a source file, as indexed from its AST."""

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
        """The ``(path, qualname, firstlineno)`` triple the shape tracer reports for this region."""
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


type Catalogue = Callable[[list[ShapeObs]], Mapping[str, InputCov]]
"""An input-class catalogue: called once per function with all of that function's observations in
a run; returns parameter name → :class:`InputCov`."""


@runtime_checkable
class LanguageAdapter(Protocol):
    """Everything coverset needs from a language: env, test command, bundle, function index."""

    name: str

    def detect(self, repo_root: Path) -> bool:
        """Return True when this adapter handles the repository at ``repo_root``."""
        ...

    def env_spec(self, repo_root: Path, cfg: Mapping) -> EnvSpec:
        """Describe the test environment for the repository at ``repo_root``."""
        ...

    def test_command(
        self, args_file: str, scores_file: str | None, out_dir: str, extra: Mapping
    ) -> list[str]:
        """Return the argv that runs one shard's tests and writes a bundle into ``out_dir``."""
        ...

    def read_bundle(self, bundle_dir: Path) -> NeutralBundle:
        """Convert an extracted shard bundle into a :class:`NeutralBundle`."""
        ...

    def changed_functions(
        self, path: str, old_src: str | None, new_src: str | None, added: set[int], removed: set[int]
    ) -> list[FunctionChange]:
        """Classify the functions of ``path`` touched by a diff."""
        ...

    def index_functions(self, path: str, source: str) -> list[FunctionRegion]:
        """Index every function region of one source file."""
        ...

    def shape_catalogue(self) -> Catalogue:
        """The input-class catalogue that turns this language's shape keys into input classes."""
        ...


@runtime_checkable
class PriorRun(Protocol):
    """Read access to one completed run's facts, used for scoring and wave-1 selection."""

    run_id: str

    def functions(self) -> list[FunctionRegion]:
        """Every function region recorded for the run's head commit."""
        ...

    def tests_touching(self, path: str, start: int, end: int) -> set[str]:
        """Nodeids whose run-phase lines intersect ``[start, end]`` of ``path`` (no ``<import>``)."""
        ...

    def test_lines(self, nodeid: str) -> dict[str, set[int]]:
        """The lines one test executed, by path."""
        ...

    def module_level_lines(self, path: str) -> set[int]:
        """Statement lines of ``path`` outside every function and method ``[def_line, end_line]``."""
        ...

    def durations(self) -> dict[str, float]:
        """Duration in seconds by nodeid (a p50 when the implementation aggregates several runs)."""
        ...
