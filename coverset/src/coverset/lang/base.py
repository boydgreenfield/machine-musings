"""Language-neutral contracts shared by ``facts/``, ``analysis/`` and the language adapters.

The neutral bundle and its rows live in the dependency-free ``coverset_model`` package
(``coverset_model.neutral``) and are re-exported here unchanged, so ``facts/`` keeps importing only
this module (never ``lang.python`` or ``analysis``). What is coverset-specific stays here:
:class:`EnvSpec`, the :class:`LanguageAdapter` and :class:`PriorRun` protocols and the
:data:`Catalogue` callable type.

Paths are git-root-relative POSIX strings, test ids are the framework's own (pytest nodeids), and
a *function triple* is ``(path, qualname, firstlineno)`` where ``firstlineno`` is
``co_firstlineno`` (the first decorator line, else the ``def`` line).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from coverset_model.neutral import (
    IMPORT_CONTEXT,
    ChangeKind,
    CollectedItem,
    FrozenMap,
    FuncSampling,
    FunctionChange,
    FunctionKind,
    FunctionRegion,
    FuncTriple,
    InputCov,
    NeutralBundle,
    Outcome,
    ShapeObs,
    StaticFile,
    TestRow,
    func_key,
)
from coverset_model.types import TypeDescriptor

__all__ = [
    "IMPORT_CONTEXT",
    "Catalogue",
    "ChangeKind",
    "CollectedItem",
    "EnvSpec",
    "FrozenMap",
    "FuncSampling",
    "FuncTriple",
    "FunctionChange",
    "FunctionKind",
    "FunctionRegion",
    "InputCov",
    "LanguageAdapter",
    "NeutralBundle",
    "Outcome",
    "PriorRun",
    "ShapeObs",
    "StaticFile",
    "TestRow",
    "TypeDescriptor",
    "func_key",
]


@dataclass(frozen=True, slots=True)
class EnvSpec:
    """Everything an executor needs to build (or find) the environment a repo's tests run in."""

    python: str
    lock_hash: str
    requirements_txt: str
    plugin_wheel: Path | None
    source_root: str
    env_hash: str


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
