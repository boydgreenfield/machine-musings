"""Merge the outputs of pytest-xdist workers into one session's worth of bundle files.

Under ``pytest -n N`` every worker runs the whole plugin into ``<out>/workers/<gw>/`` (see
:mod:`pytest_coverset.parallel`) and the controller merges those directories into ``<out>``. This
module holds the merge itself, as functions of directories and rows, so it is testable without
starting workers:

* coverage: :func:`pytest_coverset.coverage_ctx.merge_data_files` (``CoverageData.update`` keeps
  every context, so a test's lines stay under its node id);
* ``results.jsonl``: the workers' rows, the authoritative ones, plus the controller's own row for
  a test no worker recorded (a worker that crashed mid-test: xdist reports that test as failed);
* ``collect.json``: every worker collects the whole suite (xdist checks the collections agree),
  so the first readable one is taken and any item or collection error only another worker saw
  is appended;
* ``shapes.jsonl.gz``: each worker's header-indexed rows are re-indexed into one header. An
  observation's samples add up and its tests are the union; its example is the one a serial run
  would keep, the one recorded during the earliest test in collection order (import time first).
  A function's sampled calls add up, its tests are the union, and it is truncated when any worker
  capped it (``cap: "full"`` wins over ``"sample"``). Each worker samples on its own, so a hot
  function can be sampled up to N times the per-session cap; ``calls`` stays "sampled calls".
  Import time is the exception: every worker imports the same modules (each collects the whole
  suite), so an observation made at import time (caller ``<import>``) keeps the largest count of
  any worker instead of the sum, and a function's ``calls`` counts its import-time calls once.

Every reader is lenient: a missing or unreadable file of one worker is reported as a problem and
the other workers' data is still merged.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from pytest_coverset.bundle import COLLECT_FILE, META_FILE, RESULTS_FILE, SHAPES_FILE, CollectedItem
from pytest_coverset.outcomes import TestResult
from pytest_coverset.shapes import IMPORT_CALLER, SHAPES_FORMAT_VERSION

_RESULT_FIELDS = frozenset(f.name for f in fields(TestResult))
_NATURAL = re.compile(r"(\d+)")
_UNKNOWN_RANK = 1 << 60


@dataclass(slots=True)
class WorkerOutput:
    """What one worker left in its directory (anything unreadable is ``None`` or empty)."""

    id: str
    path: Path
    meta: dict[str, Any] | None = None
    results: list[TestResult] = field(default_factory=list)
    collect: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def coverage_file(self) -> Path:
        """The worker's ``coverage.sqlite`` (names still relative to the worker's working directory)."""
        return self.path / "coverage.sqlite"

    @property
    def shapes_file(self) -> Path:
        """The worker's ``shapes.jsonl.gz``."""
        return self.path / SHAPES_FILE


def natural_key(name: str) -> tuple[Any, ...]:
    """Sort key that puts ``gw2`` before ``gw10``."""
    return tuple(int(part) if part.isdigit() else part for part in _NATURAL.split(name))


def read_workers(workers_dir: Path) -> list[WorkerOutput]:
    """Every worker directory under ``workers_dir``, in natural order of their ids."""
    if not workers_dir.is_dir():
        return []
    found = [p for p in workers_dir.iterdir() if p.is_dir() and not p.is_symlink()]
    return [read_worker(p) for p in sorted(found, key=lambda p: natural_key(p.name))]


def read_worker(path: Path) -> WorkerOutput:
    """Read one worker directory; what cannot be read is listed in ``problems``."""
    out = WorkerOutput(id=path.name, path=path)
    meta = _read_json(path / META_FILE, out.problems)
    out.meta = meta if isinstance(meta, dict) else None
    if out.meta is None:
        out.problems.append(f"{META_FILE} missing: the worker did not finish (crashed or killed)")
    collect = _read_json(path / COLLECT_FILE, out.problems)
    out.collect = collect if isinstance(collect, dict) else None
    out.results = read_results(path / RESULTS_FILE, out.problems)
    return out


def _read_json(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return None


def read_results(path: Path, problems: list[str] | None = None) -> list[TestResult]:
    """The rows of a ``results.jsonl`` as :class:`TestResult`; a malformed line is skipped (and noted)."""
    rows: list[TestResult] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows
    except OSError as exc:
        if problems is not None:
            problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return rows
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            rows.append(TestResult(**{k: v for k, v in row.items() if k in _RESULT_FIELDS}))
        except (ValueError, TypeError, AttributeError) as exc:
            if problems is not None:
                problems.append(f"{path.name} line {number}: {type(exc).__name__}")
    return rows


def collected_item(row: Mapping[str, Any]) -> CollectedItem:
    """A ``collect.json`` item as a :class:`CollectedItem`."""
    line = row.get("line")
    return CollectedItem(
        nodeid=str(row["nodeid"]),
        file=str(row.get("file") or ""),
        line=line if isinstance(line, int) else None,
        markers=tuple(str(m) for m in row.get("markers") or ()),
        invariant_ids=tuple(str(i) for i in row.get("invariant_ids") or ()),
        hypothesis=row.get("hypothesis") is True,
    )


def merge_collect(workers: Sequence[WorkerOutput]) -> tuple[list[CollectedItem], list[dict[str, Any]]]:
    """Collected items (the first worker's order, then items only later workers saw) and errors."""
    items: dict[str, CollectedItem] = {}
    errors: dict[str, dict[str, Any]] = {}
    for worker in workers:
        collect = worker.collect or {}
        for row in collect.get("items") or ():
            if isinstance(row, dict) and isinstance(row.get("nodeid"), str) and row["nodeid"] not in items:
                items[row["nodeid"]] = collected_item(row)
        for error in collect.get("errors") or ():
            if isinstance(error, dict):
                errors.setdefault(str(error.get("nodeid")), error)
    return list(items.values()), list(errors.values())


def merge_results(
    workers: Sequence[WorkerOutput],
    fallback: Mapping[str, TestResult],
    order: Sequence[str],
) -> list[TestResult]:
    """Every worker row (the last one per node id wins, as in a serial ``results.jsonl``), plus a
    ``fallback`` row for each test no worker recorded; sorted by collection ``order``."""
    final: dict[str, TestResult] = {}
    for worker in workers:
        for result in worker.results:
            final[result.nodeid] = result
    for nodeid, result in fallback.items():
        final.setdefault(nodeid, result)
    rank = {nodeid: index for index, nodeid in enumerate(order)}
    return sorted(final.values(), key=lambda r: (rank.get(r.nodeid, _UNKNOWN_RANK), r.nodeid))


@dataclass(slots=True)
class _Obs:
    samples: int
    tests: set[int]
    example: str | None
    example_test: int | None
    rank: int


@dataclass(slots=True)
class _Sampling:
    calls: int = 0
    tests: set[int] = field(default_factory=set)
    truncated: bool = False
    cap: str | None = None


@dataclass(slots=True)
class ShapesMerge:
    """Accumulates worker ``shapes.jsonl.gz`` files into one header and one set of rows."""

    order: Mapping[str, int]
    """Collection index per node id: decides which example a serial run would have kept."""
    funcs: list[list[Any]] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    callers: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    _func_index: dict[tuple[Any, ...], int] = field(default_factory=dict)
    _shape_index: dict[str, int] = field(default_factory=dict)
    _test_index: dict[str, int] = field(default_factory=dict)
    _caller_index: dict[tuple[Any, ...], int] = field(default_factory=dict)
    _obs: dict[tuple[int, str, int, int], _Obs] = field(default_factory=dict)
    _sampling: dict[int, _Sampling] = field(default_factory=dict)
    _import_max: dict[int, int] = field(default_factory=dict)
    """Per function: the most import-time calls one worker made (counted once in ``calls``)."""
    _import_sum: dict[int, int] = field(default_factory=dict)

    def add_file(self, path: Path, label: str) -> None:
        """Merge one worker's file; a missing file adds nothing, a broken one is a problem."""
        if not path.is_file():
            return
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                header = json.loads(fh.readline() or "null")
                if not isinstance(header, dict) or header.get("kind") != "header":
                    self.problems.append(f"{label}: {SHAPES_FILE} has no header")
                    return
                maps = self._maps(header)
                imports: dict[int, dict[str, int]] = {}
                for line in fh:
                    self._row(json.loads(line), maps, imports)
            for f, labels in imports.items():
                calls = max(labels.values(), default=0)
                self._import_max[f] = max(self._import_max.get(f, 0), calls)
                self._import_sum[f] = self._import_sum.get(f, 0) + calls
        except (OSError, ValueError, EOFError, KeyError, IndexError, TypeError) as exc:
            self.problems.append(f"{label}: {SHAPES_FILE}: {type(exc).__name__}: {exc}")

    def _maps(self, header: Mapping[str, Any]) -> tuple[list[int], list[int], list[int], list[int]]:
        def lst(key: str) -> list[Any]:
            value = header.get(key)
            return value if isinstance(value, list) else []

        funcs = [self._intern_func(f) for f in lst("funcs")]
        shapes = [self._intern(self._shape_index, self.shapes, s, s) for s in lst("shapes")]
        tests = [self._intern(self._test_index, self.tests, t, t) for t in lst("tests")]
        callers = [self._intern_caller(c) for c in lst("callers")]
        return funcs, shapes, tests, callers

    @staticmethod
    def _intern(index: dict[Any, int], table: list[Any], key: Any, value: Any) -> int:
        found = index.get(key)
        if found is None:
            found = index[key] = len(table)
            table.append(value)
        return found

    def _intern_func(self, func: Any) -> int:
        key = tuple(func) if isinstance(func, list) else (repr(func),)
        return self._intern(self._func_index, self.funcs, key, func)

    def _intern_caller(self, caller: Any) -> int:
        if isinstance(caller, dict):
            key: tuple[Any, ...] = tuple(sorted((str(k), repr(v)) for k, v in caller.items()))
        else:
            key = (repr(caller),)
        return self._intern(self._caller_index, self.callers, key, caller)

    def _rank(self, test: int | None) -> int:
        if test is None:
            return -1  # import time: a serial run sees it before any test
        return self.order.get(self.tests[test], _UNKNOWN_RANK)

    def _is_import(self, caller: int) -> bool:
        entry = self.callers[caller]
        return (
            isinstance(entry, dict) and entry.get("kind") == "test" and entry.get("nodeid") == IMPORT_CALLER
        )

    def _row(
        self,
        row: Any,
        maps: tuple[list[int], list[int], list[int], list[int]],
        imports: dict[int, dict[str, int]],
    ) -> None:
        funcs, shapes, tests, callers = maps
        if not isinstance(row, dict):
            return
        kind = row.get("kind")
        if kind == "obs":
            key = (funcs[row["f"]], str(row["p"]), shapes[row["s"]], callers[row["c"]])
            samples = int(row.get("n") or 0)
            at_import = self._is_import(key[3])
            if at_import:
                labels = imports.setdefault(key[0], {})
                labels[key[1]] = labels.get(key[1], 0) + samples
            row_tests = {tests[t] for t in row.get("t") or ()}
            ext = row.get("ext")
            example_test = tests[ext] if isinstance(ext, int) else None
            rank = self._rank(example_test)
            found = self._obs.get(key)
            if found is None:
                self._obs[key] = _Obs(samples, row_tests, row.get("ex"), example_test, rank)
                return
            found.samples = max(found.samples, samples) if at_import else found.samples + samples
            found.tests |= row_tests
            if rank < found.rank:
                found.example, found.example_test, found.rank = row.get("ex"), example_test, rank
        elif kind == "func_sampling":
            state = self._sampling.setdefault(funcs[row["f"]], _Sampling())
            state.calls += int(row.get("calls") or 0)
            state.tests |= {tests[t] for t in row.get("tests") or ()}
            if row.get("truncated") is True:
                state.truncated = True
                cap = row.get("cap")
                if cap == "full" or (cap == "sample" and state.cap is None):
                    state.cap = cap

    @property
    def functions_traced(self) -> int:
        """Functions in the merged header."""
        return len(self.funcs)

    @property
    def functions_truncated(self) -> int:
        """Functions some worker capped."""
        return sum(1 for state in self._sampling.values() if state.truncated)

    def write(self, path: Path) -> dict[str, int]:
        """Write the merged ``shapes.jsonl.gz`` (the plugin's format); return row counts."""
        header = {
            "kind": "header",
            "version": SHAPES_FORMAT_VERSION,
            "funcs": self.funcs,
            "shapes": self.shapes,
            "tests": self.tests,
            "callers": self.callers,
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as out:
                out.write(json.dumps(header, separators=(",", ":")) + "\n")
                for (f, param, shape, caller), obs in self._obs.items():
                    state = self._sampling.get(f)
                    row = {
                        "kind": "obs",
                        "f": f,
                        "p": param,
                        "s": shape,
                        "c": caller,
                        "n": obs.samples,
                        "t": sorted(obs.tests),
                        "trunc": bool(state and state.truncated),
                        "ex": obs.example,
                        "ext": obs.example_test,
                    }
                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
                for f in sorted(self._sampling):
                    state = self._sampling[f]
                    calls = state.calls - self._import_sum.get(f, 0) + self._import_max.get(f, 0)
                    row = {
                        "kind": "func_sampling",
                        "f": f,
                        "calls": max(calls, 0),
                        "tests": sorted(state.tests),
                        "truncated": state.truncated,
                        "cap": state.cap if state.truncated else None,
                    }
                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {"funcs": len(self.funcs), "shapes": len(self.shapes), "obs": len(self._obs)}


def merge_shapes(files: Iterable[tuple[str, Path]], out: Path, order: Mapping[str, int]) -> ShapesMerge:
    """Merge ``(label, path)`` shape files into ``out``; returns the merge (counts and problems)."""
    merged = ShapesMerge(order=order)
    for label, path in files:
        merged.add_file(path, label)
    merged.write(out)
    return merged
