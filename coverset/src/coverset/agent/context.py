"""One run as the agent tools see it, whichever backend it came from.

A :class:`RunContext` wraps the pages' :class:`~coverset.web.facts_view.RunFacts`: the store-backed
one for a facts-store run, or the report's in-memory
:class:`~coverset.report.facts.BundleFacts` for a local ``.coverset`` directory or bundle. Every
number the tools return (line state, partial branches, covering tests, input classes, ``cap_hits``)
therefore comes from the same code as the pages and the static report; the context only adds what
the tools need on top:

* the run's identity (:class:`RunInfo`) and its tests with outcomes and durations;
* lazy, cached source text (the mirror at the head commit, or the local work tree), which the
  Python catalogue reads annotations from;
* cached function views, the per-test line index (a stable sort of the facts' line table by test),
  and the coverage facts each test reaches (:mod:`coverset.agent.cover`);
* hooks the backend fills in: test history, failure messages and the diff of a change.

Contexts are cached by their backend (keyed by the run directory's stamp or the bundle's mtime), so
an agent's tenth call on a run costs a dictionary lookup, not a reload. Everything here is
synchronous; the service calls it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

import numpy as np

from coverset.analysis.patch import PatchReport
from coverset.lang.base import IMPORT_CONTEXT, FunctionChange, FunctionRegion, ShapeObs
from coverset.report.facts import TestInfo
from coverset.web.facts_view import FunctionView, RunFacts

__all__ = ["Changes", "RunContext", "RunInfo"]


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Who a run is: the fields ``get_run_summary`` and ``list_runs`` report."""

    run_id: str
    repo: str
    repo_id: int | None = None
    kind: str = "local"
    """``pr``, ``baseline``, ``push``, ``manual``, ``rerun``, ``upload`` or ``local``."""
    branch: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    created_at: str | None = None
    status: str | None = None
    verdict: str | None = None
    reason: str | None = None
    executor: str | None = None
    coverage_complete: bool | None = None
    origin: str = ""
    """Where the facts came from (a bundle path or ``facts store``)."""

    def to_json(self) -> dict[str, Any]:
        """The ``run`` object of a tool result."""
        return {
            "id": self.run_id,
            "repo": self.repo,
            "kind": self.kind,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "created_at": self.created_at,
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "executor": self.executor,
            "coverage_complete": self.coverage_complete,
            "origin": self.origin,
        }


@dataclass(slots=True)
class Changes:
    """A change measured against one run: the diff's changed lines and functions, and the patch
    report over the run's facts (the PR page's :class:`~coverset.web.pr_view.DiffAnalysis`, or the
    local work tree against a base ref)."""

    base: str | None
    head: str | None
    changed_lines: dict[str, set[int]] = field(default_factory=dict)
    changes: list[FunctionChange] = field(default_factory=list)
    patch: PatchReport | None = None
    complete: bool = True
    """False while a facts-store run is missing shards (uncovered lines are then unknown)."""
    error: str | None = None
    stale: list[str] = field(default_factory=list)
    """Changed files modified after the tests ran (local only): the coverage no longer matches."""
    notes: list[str] = field(default_factory=list)


ChangesLoader = Callable[[str | None], Changes | None]
"""``base`` (a git ref, local only) → the change, or None when the run has no diff to measure."""


class RunContext:
    """One run's facts plus the caches and hooks the agent tools need (see the module docstring)."""

    def __init__(
        self,
        info: RunInfo,
        facts: RunFacts,
        tests: Iterable[TestInfo],
        *,
        source_loader: Callable[[Iterable[str]], Mapping[str, str | None]],
        language: str | None = None,
        history_loader: Callable[[str, int], list[dict[str, Any]]] | None = None,
        message_loader: Callable[[str], tuple[str | None, str | None]] | None = None,
        changes_loader: ChangesLoader | None = None,
        cache_changes: bool = True,
        stamp: object = None,
    ) -> None:
        self.info = info
        self.facts = facts
        self.tests: dict[str, TestInfo] = {t.nodeid: t for t in tests}
        self._source_loader = source_loader
        self._language = language
        self.history_loader = history_loader
        self.message_loader = message_loader
        self._changes_loader = changes_loader
        self._cache_changes = cache_changes
        """False for a work tree: it can change between calls without the bundle changing."""
        self.stamp = stamp
        self._lock = threading.Lock()
        self._sources: dict[str, str | None] = {}
        self._views: dict[str, FunctionView] = {}
        self._changes: dict[str | None, Changes | None] = {}
        self.cache: dict[str, Any] = {}
        """Derived structures other modules build once per run (the cover index, gap rows)."""

    # -- identity and language -------------------------------------------------------------------

    @cached_property
    def language(self) -> str:
        """``python``, ``typescript``, ... (the run's function rows, else the measured files)."""
        if self._language:
            return self._language
        found = getattr(self.facts, "language", None)
        if isinstance(found, str) and found and found != "python":
            return found
        paths = list(self.facts.statics)
        if paths and all(p.endswith((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx")) for p in paths):
            return "typescript"
        return "python"

    # -- sources -----------------------------------------------------------------------------------

    def sources(self, paths: Iterable[str]) -> dict[str, str | None]:
        """Source text of ``paths`` (None: not available), loaded once per path."""
        wanted = [p for p in dict.fromkeys(paths) if p not in self._sources]
        if wanted:
            loaded = self._source_loader(wanted)
            with self._lock:
                for path in wanted:
                    self._sources[path] = loaded.get(path)
        return {p: self._sources.get(p) for p in paths}

    def source(self, path: str) -> str | None:
        """Source text of one path (None: not available)."""
        return self.sources([path])[path]

    # -- functions ---------------------------------------------------------------------------------

    @cached_property
    def callables(self) -> list[FunctionRegion]:
        """Every function and method of the run, by path and line."""
        return self.facts.callables()

    @cached_property
    def by_key(self) -> dict[str, FunctionRegion]:
        """Every region by func_key."""
        return {r.func_key: r for r in self.facts.regions}

    def view(self, region: FunctionRegion) -> FunctionView:
        """The pages' function view of ``region`` (cached)."""
        found = self._views.get(region.func_key)
        if found is None:
            found = self.facts.function_view(region, self.source(region.path))
            with self._lock:
                self._views[region.func_key] = found
        return found

    def views(self, regions: Iterable[FunctionRegion]) -> list[FunctionView]:
        """Views of many regions, with every needed source loaded in one batch first."""
        regions = list(regions)
        self.sources(r.path for r in regions)
        return [self.view(r) for r in regions]

    def innermost(self, path: str, line: int) -> FunctionRegion | None:
        """The innermost function or method of ``path`` whose ``[def_line, end_line]`` holds ``line``."""
        found = [r for r in self.callables if r.path == path and r.def_line <= line <= r.end_line]
        return max(found, key=lambda r: (r.def_line, -r.end_line)) if found else None

    def observations(self) -> dict[str, list[ShapeObs]]:
        """Every function's shape observations, by func_key."""
        out: dict[str, list[ShapeObs]] = {}
        for region in self.callables:
            found = self.facts.observations(region.func_key)
            if found:
                out[region.func_key] = found
        return out

    # -- tests and lines ---------------------------------------------------------------------------

    @cached_property
    def line_table(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The facts' ``(file_id, line, test_id)`` table, sorted by file, line and test.

        Both :class:`RunFacts` (from ``test_lines``) and ``BundleFacts`` (from the bundle) keep this
        table as ``_lines``; it is the one place the per-test structures below read."""
        return self.facts._lines

    @cached_property
    def _by_test(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The line table re-sorted by test (stable), for one-test slices."""
        files, lines, tests = self.line_table
        order = np.argsort(tests, kind="stable")
        return tests[order], files[order], lines[order]

    @cached_property
    def test_ids(self) -> dict[str, int]:
        """Nodeid → the facts' test id, for every test that ran measured lines (not ``<import>``)."""
        ids = np.unique(self.line_table[2])
        out: dict[str, int] = {}
        for tid in ids.tolist():
            if tid == 0:
                continue
            nodeid = self.facts.ids.nodeid(int(tid))
            if nodeid and nodeid != IMPORT_CONTEXT:
                out[nodeid] = int(tid)
        return out

    @cached_property
    def path_of(self) -> dict[int, str]:
        """File id → path."""
        return {fid: path for path, fid in self.facts.paths.items()}

    def test_lines(self, nodeid: str) -> dict[str, list[int]]:
        """Path → sorted lines ``nodeid`` executed (empty when it ran no measured line)."""
        tid = self.test_ids.get(nodeid)
        if tid is None:
            return {}
        tests, files, lines = self._by_test
        lo, hi = np.searchsorted(tests, [tid, tid + 1])
        out: dict[str, set[int]] = defaultdict(set)
        for fid, line in zip(files[lo:hi].tolist(), lines[lo:hi].tolist(), strict=True):
            path = self.path_of.get(fid)
            if path is not None:
                out[path].add(line)
        return {path: sorted(found) for path, found in sorted(out.items())}

    def nodeid_of(self, test_id: int) -> str:
        """The nodeid of a facts test id."""
        return self.facts.ids.nodeid(int(test_id))

    def duration(self, nodeid: str) -> float | None:
        """The test's duration in seconds, when recorded."""
        info = self.tests.get(nodeid)
        return info.duration_s if info is not None else None

    @cached_property
    def median_duration(self) -> float:
        """The median recorded duration (0.05 s when no test has one): what unknowns weigh."""
        known = sorted(t.duration_s for t in self.tests.values() if t.duration_s)
        return known[len(known) // 2] if known else 0.05

    # -- the change ----------------------------------------------------------------------------------

    def changes(self, base: str | None = None) -> Changes | None:
        """The run's change (a PR's diff, or the local work tree against ``base``); a PR's diff is
        cached per base, a work tree's is read again on every call."""
        if self._cache_changes and base in self._changes:
            return self._changes[base]
        found = self._changes_loader(base) if self._changes_loader is not None else None
        if self._cache_changes:
            with self._lock:
                self._changes[base] = found
        return found
