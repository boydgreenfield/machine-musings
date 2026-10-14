"""Database and mirror lookups shared by the UI blueprints, and the per-process caches behind them.

Source text is read only with :func:`coverset.orchestrator.repos.read_blobs` from the repo's bare
mirror at a run's head commit, and only for paths listed in that run's facts ``files`` table: a URL
path is never joined to the filesystem.

Three caches keep warm pages fast (docs/data-model.md, "Caches"):

* :func:`facts_for` returns an :class:`IndexedRunFacts` per run, kept in an LRU bounded by count
  and by an estimate of everything it holds (:meth:`IndexedRunFacts.memory_bytes`: arrays, Arrow
  tables and the Python object graphs of its views, checked on every lookup), and keyed by the
  run's stamp
  (:func:`coverset.facts.cache.run_stamp`), so an ingest or a finalize is seen by the next request.
  It answers the per-file and per-function questions of the pages from indexes built once
  (file boundaries in ``test_lines``, sampling rows by function, executed lines per file) and
  memoises the function views and line facts it computes; a run's facts never change under a
  stamp, so neither do these.
* :func:`read_source` keeps the text of blobs it read, by ``(mirror, commit, path)`` (immutable),
  in an LRU bounded by bytes; the first miss for a commit reads every measured source of the run in
  one ``git cat-file --batch`` instead of one process per file.
* :func:`source_cache` is the process's :class:`~coverset.web.render.source.SourceCache`: a file is
  syntax-highlighted once per text, not on every render.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import Iterable
from functools import cached_property
from itertools import islice
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from werkzeug.exceptions import NotFound

from coverset.db.models import (
    Invariant,
    InvariantResult,
    PullRequest,
    Repo,
    ReviewFinding,
    Run,
    TestCase,
    TestResult,
)
from coverset.facts.cache import override, run_stamp
from coverset.facts.queries import summary_is_fresh
from coverset.facts.store import FactsStore
from coverset.lang.base import FuncSampling, FunctionRegion, ShapeObs
from coverset.orchestrator.repos import RepoError, mirror_path, read_blobs
from coverset.web.api import RUN_ID_PARAM
from coverset.web.context import app_settings
from coverset.web.facts_view import FunctionView, RunFacts
from coverset.web.render.source import LineFacts, SourceCache

__all__ = [
    "IndexedRunFacts",
    "cached_run_facts",
    "clear_caches",
    "default_run",
    "facts_for",
    "findings_for_run",
    "invariant_rows",
    "pr_run",
    "read_source",
    "require_run",
    "result_rows",
    "run_counts",
    "source_cache",
    "warm_run",
]

BASELINE_TRIGGERS = ("baseline", "push")


def store() -> FactsStore:
    """The app's facts store."""
    return FactsStore(app_settings().facts_dir)


async def facts_for(run: Run) -> RunFacts:
    """The (cached) facts of ``run``: an :class:`IndexedRunFacts` (see the module docstring)."""
    return await asyncio.to_thread(cached_run_facts, store(), run.repo_id, run.id)


# -- run facts ---------------------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _configured_int(name: str) -> int | None:
    """``name`` as configured by the app (``facts.cache.configure``: Settings, ``.env`` included),
    or None, in which case the module constant read from the environment at import applies."""
    raw = override(name)
    try:
        return max(1, int(raw)) if raw else None
    except ValueError:
        return None


RUN_FACTS_ENTRIES = _env_int("COVERSET_RUN_FACTS_ENTRIES", 8)
"""Runs whose :class:`IndexedRunFacts` stay cached (``COVERSET_RUN_FACTS_ENTRIES``)."""
RUN_FACTS_BUDGET = _env_int("COVERSET_RUN_FACTS_MB", 384) * 1024 * 1024
"""Estimated bytes the cached runs may hold together (``COVERSET_RUN_FACTS_MB``; see
:meth:`IndexedRunFacts.memory_bytes`). The process's resident set grows by about 1.5 times this
(allocator overhead, measured on the real bundle). The newest run is always kept, so one run larger
than the budget still works."""


def run_facts_entries() -> int:
    """The run-facts cache's run count: the app's ``COVERSET_RUN_FACTS_ENTRIES``, else
    :data:`RUN_FACTS_ENTRIES`."""
    return _configured_int("COVERSET_RUN_FACTS_ENTRIES") or RUN_FACTS_ENTRIES


def run_facts_budget() -> int:
    """The run-facts cache's byte budget: the app's ``COVERSET_RUN_FACTS_MB``, else
    :data:`RUN_FACTS_BUDGET`."""
    mb = _configured_int("COVERSET_RUN_FACTS_MB")
    return mb * 1024 * 1024 if mb else RUN_FACTS_BUDGET


_SAMPLE = 32
"""Elements of a long container :func:`estimate_bytes` weighs before extrapolating to all of them."""
_EXACT = 64
"""Containers up to this length are weighed element by element."""
_ATOMS = (str, bytes, int, float, bool, complex, type(None))
_ARROW = (pa.Table, pa.RecordBatch, pa.ChunkedArray, pa.Array)
_MEMO_ATTRS = ("_views", "_line_facts", "_executed", "_body", "_partial", "_obs")
"""The memo dictionaries of :class:`IndexedRunFacts`: they grow, so they are weighed per entry."""
_UNSIZED = frozenset({"store", "_sized", "_memo_weights", *_MEMO_ATTRS})
_REFERENCES = frozenset({"_callables_by_path", "_region_index"})
"""Indexes holding only references to :attr:`RunFacts.regions` objects: weighed shallowly."""


def estimate_bytes(obj: object, depth: int = 8, seen: set[int] | None = None) -> int:
    """Approximate bytes ``obj`` holds, its referents included: numpy arrays and Arrow data by their
    buffers, Python objects by :func:`sys.getsizeof` recursively, each object once per call.

    Containers of up to :data:`_EXACT` elements are walked whole; longer ones are weighed from
    :data:`_SAMPLE` evenly spaced elements and extrapolated, so a run's whole object graph (hundreds
    of thousands of objects) costs milliseconds instead of the 170 ms an exact walk takes. On the
    real bundle the estimate of a warmed run is within 20 % of an exact walk.
    """
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return 0
    if isinstance(obj, np.ndarray):
        seen.add(id(obj))
        return int(obj.nbytes) + 112
    if isinstance(obj, _ARROW):
        seen.add(id(obj))
        return int(obj.nbytes)
    size = sys.getsizeof(obj)
    if isinstance(obj, _ATOMS):
        if not isinstance(obj, str | bytes):
            return size  # small ints and floats: often shared, never worth a set entry
        seen.add(id(obj))
        return size
    seen.add(id(obj))
    if depth <= 0:
        return size
    depth -= 1
    if isinstance(obj, dict):
        return (
            size
            + _estimate_items(list(obj.keys()), depth, seen)
            + _estimate_items(list(obj.values()), depth, seen)
        )
    if isinstance(obj, list | tuple | set | frozenset):
        return size + _estimate_items(obj if isinstance(obj, list | tuple) else list(obj), depth, seen)
    held = getattr(obj, "__dict__", None)
    if isinstance(held, dict):
        size += estimate_bytes(held, depth, seen)
    for cls in type(obj).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            if slot not in ("__dict__", "__weakref__"):
                size += estimate_bytes(getattr(obj, slot, None), depth, seen)
    return size


def _estimate_items(items: list[Any] | tuple[Any, ...], depth: int, seen: set[int]) -> int:
    """The weight of a container's elements: all of them, or a spaced sample scaled up."""
    if len(items) <= _EXACT:
        return sum(estimate_bytes(item, depth, seen) for item in items)
    step = len(items) / _SAMPLE
    picked = [items[int(i * step)] for i in range(_SAMPLE)]
    return sum(estimate_bytes(item, depth, seen) for item in picked) * len(items) // _SAMPLE


def _lexsorted(file_id: np.ndarray, line: np.ndarray, test_id: np.ndarray) -> bool:
    """Whether the rows are already in ``(file_id, line, test_id)`` order (a one-shard run's
    ``test_lines`` is written that way, so its load skips the sort)."""
    if file_id.size < 2:
        return True
    df, dl, dt = np.diff(file_id), np.diff(line), np.diff(test_id)
    return bool(np.all((df > 0) | ((df == 0) & ((dl > 0) | ((dl == 0) & (dt >= 0))))))


class IndexedRunFacts(RunFacts):
    """:class:`~coverset.web.facts_view.RunFacts` with the per-file and per-function lookups of the
    pages answered from indexes built once per run, and the views it builds memoised.

    Every override returns what the base class returns for the same arguments (the web tests run
    against it); only the cost changes: a file's slice of ``test_lines`` is a dictionary lookup
    instead of a binary search over every row (with a dtype conversion of the whole array), a
    function's sampling rows are an index lookup instead of a read of ``func_sampling``, and a
    function view, a file's line facts and a function's body coverage are computed once.
    """

    def __init__(self, store: FactsStore, repo_id: int, run_id: str) -> None:
        super().__init__(store, repo_id, run_id)
        self._views: dict[tuple[str, int, int, int | None], FunctionView] = {}
        self._line_facts: dict[str, LineFacts] = {}
        self._executed: dict[str, frozenset[int]] = {}
        self._body: dict[str, tuple[float | None, float | None]] = {}
        self._partial: dict[str, dict[int, tuple[int, ...]]] = {}
        self._obs: dict[str, list[ShapeObs]] = {}
        self._sized: dict[str, tuple[int, int]] = {}
        self._memo_weights: dict[str, tuple[int, int]] = {}

    @cached_property
    def _lines(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        table = self._read("test_lines")
        file_id = table.column("file_id").to_numpy()
        line = table.column("line").to_numpy()
        test_id = table.column("test_id").to_numpy()
        if _lexsorted(file_id, line, test_id):
            return file_id, line, test_id
        order = np.lexsort((test_id, line, file_id))
        return file_id[order], line[order], test_id[order]

    @cached_property
    def _bounds(self) -> dict[int, tuple[int, int]]:
        """``file_id`` → ``(start, stop)`` of its rows in :attr:`_lines`."""
        files = self._lines[0]
        if files.size == 0:
            return {}
        starts = np.concatenate(([0], np.flatnonzero(np.diff(files)) + 1))
        stops = np.concatenate((starts[1:], [files.size]))
        return {
            int(f): (int(a), int(b))
            for f, a, b in zip(files[starts].tolist(), starts.tolist(), stops.tolist(), strict=True)
        }

    def _slice(self, path: str) -> tuple[np.ndarray, np.ndarray]:
        _files, lines, tests = self._lines
        file_id = self.paths.get(path)
        bounds = self._bounds.get(file_id) if file_id is not None else None
        if bounds is None:
            return lines[:0], tests[:0]
        return lines[bounds[0] : bounds[1]], tests[bounds[0] : bounds[1]]

    @cached_property
    def _hits(self) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] | None:
        """The run's ``line_hits`` summary by file id (``lines``, ``tests``, ``by_import``), or
        None when the run has no fresh summary (then ``test_lines`` answers instead)."""
        if not summary_is_fresh(self.store, self.repo_id, self.run_id, "line_hits"):
            return None
        table = self._read("line_hits")
        file_id = table.column("file_id").to_numpy()
        line = table.column("line").to_numpy()
        tests = table.column("tests").to_numpy()
        by_import = table.column("by_import").to_numpy(zero_copy_only=False)
        order = np.lexsort((line, file_id))
        file_id, line, tests, by_import = file_id[order], line[order], tests[order], by_import[order]
        out: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        if file_id.size:
            starts = np.concatenate(([0], np.flatnonzero(np.diff(file_id)) + 1))
            stops = np.concatenate((starts[1:], [file_id.size]))
            for f, a, b in zip(file_id[starts].tolist(), starts.tolist(), stops.tolist(), strict=True):
                out[int(f)] = (line[a:b], tests[a:b], by_import[a:b])
        return out

    def _file_hits(self, path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """``path``'s rows of :attr:`_hits` (empty arrays for a file nothing ran), or None
        without the summary."""
        hits = self._hits
        if hits is None:
            return None
        file_id = self.paths.get(path)
        found = hits.get(file_id) if file_id is not None else None
        if found is None:
            empty = np.empty(0, dtype=np.int32)
            return empty, empty, np.empty(0, dtype=bool)
        return found

    def executed(self, path: str) -> frozenset[int]:
        """Executed lines of ``path`` (``<import>`` included), memoised."""
        found = self._executed.get(path)
        if found is None:
            hits = self._file_hits(path)
            lines = hits[0] if hits is not None else np.unique(self._slice(path)[0])
            found = self._executed[path] = frozenset(lines.tolist())
        return found

    def _test_ran(self, path: str, line: int) -> bool:
        """Whether a test other than ``<import>`` ran ``path:line``."""
        hits = self._file_hits(path)
        if hits is None:
            return bool(self.tests_in_range(path, line, line))
        index = int(np.searchsorted(hits[0], line))
        return index < hits[0].size and int(hits[0][index]) == line and int(hits[1][index]) > 0

    def import_covers(self, path: str, line: int) -> bool:
        """Whether import time (``<import>``) executed ``path:line``."""
        hits = self._file_hits(path)
        if hits is None:
            return super().import_covers(path, line)
        index = int(np.searchsorted(hits[0], line))
        return index < hits[0].size and int(hits[0][index]) == line and bool(hits[2][index])

    def union_lines(self) -> dict[str, set[int]]:
        """Executed lines per measured path (``<import>`` included)."""
        return {path: set(self.executed(path)) for path in self.statics}

    def partial(self, path: str) -> dict[int, tuple[int, ...]]:
        """Branch lines whose possible exits were not all taken, with the missed destinations
        (the base class's rule over the memoised executed lines; memoised per file)."""
        found = self._partial.get(path)
        if found is not None:
            return dict(found)
        static = self.statics.get(path)
        if static is None:
            return {}
        taken = self.run_arcs.get(path, frozenset())
        possible: dict[int, set[int]] = {}
        for a, b in static.arcs_possible:
            possible.setdefault(a, set()).add(b)
        executed = self.executed(path)
        found = {}
        for line in self.branch_lines.get(path, frozenset()):
            if line not in executed:
                continue
            missed = sorted(b for b in possible.get(line, ()) if (line, b) not in taken)
            if missed and len(possible.get(line, ())) > 1:
                found[line] = tuple(missed)
        self._partial[path] = found
        return dict(found)

    def line_facts(self, path: str) -> LineFacts:
        """Line states and covering-test counts of ``path``, from the run's ``line_hits`` summary
        when it has one (memoised per file)."""
        found = self._line_facts.get(path)
        if found is not None:
            return found
        static = self.statics.get(path)
        hits = self._file_hits(path)
        if static is None or hits is None:
            found = super().line_facts(path)
        else:
            lines, tests, _by_import = hits
            counted = tests > 0
            found = LineFacts(
                statements=frozenset(static.statements),
                excluded=frozenset(static.excluded),
                executed=self.executed(path),
                partial=self.partial(path),
                tests=dict(zip(lines[counted].tolist(), tests[counted].tolist(), strict=True)),
            )
        self._line_facts[path] = found
        return found

    @cached_property
    def _region_index(self) -> dict[str, FunctionRegion]:
        out: dict[str, FunctionRegion] = {}
        for region in self.regions:
            out.setdefault(region.func_key, region)
        return out

    def region(self, func_key: str) -> FunctionRegion | None:
        """The region with ``func_key`` (an index lookup)."""
        return self._region_index.get(func_key)

    @cached_property
    def _callables_by_path(self) -> dict[str | None, list[FunctionRegion]]:
        every = super().callables()
        out: dict[str | None, list[FunctionRegion]] = {None: every}
        for region in every:
            out.setdefault(region.path, []).append(region)
        return out

    def callables(self, path: str | None = None) -> list[FunctionRegion]:
        """Functions and methods (of ``path``), in source order."""
        return list(self._callables_by_path.get(path, ()))

    @cached_property
    def _sampling_rows(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for row in self._read("func_sampling").to_pylist():
            out.setdefault(row["func_key"], []).append(row)
        return out

    def sampling(self, region: FunctionRegion) -> FuncSampling | None:
        """The function's sampling summary, from rows indexed once by ``func_key``."""
        rows = self._sampling_rows.get(region.func_key)
        if not rows:
            return None
        tests = sorted({self.ids.nodeid(t) for r in rows for t in (r["test_ids"] or ())})
        return FuncSampling(
            func=region.triple,
            calls=sum(int(r["calls"] or 0) for r in rows),
            tests=tuple(tests),
            truncated=any(bool(r["truncated"]) for r in rows),
        )

    @cached_property
    def _measured_lines(self) -> dict[str, frozenset[int]]:
        """Statements minus excluded lines, per measured path."""
        return {
            path: frozenset(static.statements) - frozenset(static.excluded)
            for path, static in self.statics.items()
        }

    @cached_property
    def _shape_rows(self) -> tuple[pa.Table, dict[str, tuple[int, int]], dict[str, tuple[str, str, int]]]:
        """``call_shapes`` stably sorted by ``func_key`` with each function's ``(start, stop)`` rows,
        and the function triples, so one function's observations are built without building every
        function's (89,000 rows on a real repository took 0.4 s per cold page). Spans instead of
        row-number lists keep the index to one tuple per function (a list per function cost 5 MB
        per run)."""
        table = self._read("call_shapes")
        if table.num_rows:
            table = table.take(pc.sort_indices(table.column("func_key")))  # stable: keeps row order
        keys = table.column("func_key").to_pylist()
        spans: dict[str, tuple[int, int]] = {}
        start = 0
        for index in range(1, len(keys) + 1):
            if index == len(keys) or keys[index] != keys[start]:
                spans.setdefault(keys[start], (start, index))
                start = index
        triples = {r.func_key: r.triple for r in self.regions}
        for row in (
            self._read("func_sampling").select(["func_key", "path", "qualname", "firstlineno"]).to_pylist()
        ):
            triples.setdefault(row["func_key"], (row["path"], row["qualname"], row["firstlineno"]))
        return table, spans, triples

    def observations(self, func_key: str) -> list[ShapeObs]:
        """Shape observations of one function in this run (the base class's objects, built for
        this function only and memoised)."""
        found = self._obs.get(func_key)
        if found is None:
            table, spans, triples = self._shape_rows
            triple = triples.get(func_key)
            span = spans.get(func_key)
            found = []
            if triple is not None and span is not None:
                for row in table.slice(span[0], span[1] - span[0]).to_pylist():
                    caller: tuple[str, str, int] | str
                    if row["caller_kind"] == "test":
                        caller = row["caller_key"] or ""
                    else:
                        caller = (
                            row["caller_path"] or "",
                            row["caller_qualname"] or "?",
                            row["caller_line"] or 0,
                        )
                    example_test = row["example_test_id"]
                    found.append(
                        ShapeObs(
                            func=triple,
                            param=row["param"],
                            shape_key=self.ids.shape_key(row["shape_id"]),
                            caller=caller,
                            samples=int(row["samples"] or 0),
                            tests=tuple(self.ids.nodeid(t) for t in row["test_ids"] or ()),
                            truncated=bool(row["truncated"]),
                            example=row["example_repr"],
                            example_test=self.ids.nodeid(example_test) if example_test is not None else None,
                        )
                    )
            self._obs[func_key] = found
        return list(found)

    def body_coverage(self, region: FunctionRegion) -> tuple[float | None, float | None]:
        """Line and branch percentages of the function body, by the base class's rule (a body
        statement ran when any test or ``<import>`` ran it, except a def line that is its own
        body, which needs a real test), from the file's memoised executed lines."""
        key = f"{region.func_key}:{region.path}:{region.body_start}:{region.end_line}:{region.def_line}"
        found = self._body.get(key)
        if found is not None:
            return found
        static = self.statics.get(region.path)
        if static is None:
            return None, None
        executed = self.executed(region.path)
        start, end = region.body_start, region.end_line
        body = {n for n in self._measured_lines[region.path] if start <= n <= end}
        if start <= region.def_line and not self._test_ran(region.path, region.def_line):
            executed = executed - {region.def_line}
        line_pct = round(100.0 * len(body & executed) / len(body), 1) if body else None
        taken = self.run_arcs.get(region.path, frozenset())
        branch_lines = self.branch_lines.get(region.path, frozenset())
        possible = [(a, b) for a, b in static.arcs_possible if a in branch_lines and start <= a <= end]
        branch_pct = (
            round(100.0 * sum(1 for arc in possible if arc in taken) / len(possible), 1) if possible else None
        )
        found = self._body[key] = (line_pct, branch_pct)
        return found

    def function_view(self, region: FunctionRegion, source: str | None) -> FunctionView:
        """Input-class coverage of ``region`` (memoised per function and source text: the view
        depends on nothing else of the run, which never changes under this object)."""
        key = (region.func_key, region.def_line, region.end_line, None if source is None else hash(source))
        found = self._views.get(key)
        if found is None:
            found = self._views[key] = super().function_view(region, source)
        return found

    def memory_bytes(self) -> int:
        """Approximate bytes this object holds: every loaded view (arrays, the ``call_shapes``
        Arrow table, the Python graphs of statics, regions, rows and the repo dictionary) and the
        memoised entries, by :func:`estimate_bytes`.

        Cheap enough to call on every cache lookup: a loaded view never changes, so it is weighed
        once (by identity), and a memo dictionary is weighed as its length times an entry weight
        re-sampled from its newest entries whenever it has doubled. On the real bundle a fully
        warmed run weighs about 80 MB (an exact walk agrees within 10 %) and adds about 120 MB of
        resident set.
        """
        total = 0
        for name, value in list(self.__dict__.items()):
            if name in _UNSIZED:
                continue
            found = self._sized.get(name)
            if found is None or found[0] != id(value):
                weight = estimate_bytes(value, depth=1 if name in _REFERENCES else 8)
                found = self._sized[name] = (id(value), weight)
            total += found[1]
        for name in _MEMO_ATTRS:
            memo: dict[Any, Any] = self.__dict__[name]
            count = len(memo)
            weight = self._memo_weights.get(name)
            if count and (weight is None or count >= 2 * weight[0]):
                try:
                    sample = list(islice(reversed(memo.items()), _SAMPLE))
                except RuntimeError:  # another thread added an entry meanwhile: weigh it next time
                    sample = []
                if sample:
                    each = estimate_bytes(sample) // len(sample)
                    weight = self._memo_weights[name] = (count, each)
            total += sys.getsizeof(memo) + (count * weight[1] if weight is not None else 0)
        return total


_run_facts: OrderedDict[tuple[str, int, str], tuple[Any, IndexedRunFacts]] = OrderedDict()
_run_facts_lock = threading.Lock()


def cached_run_facts(facts_store: FactsStore, repo_id: int, run_id: str) -> IndexedRunFacts:
    """The cached :class:`IndexedRunFacts` of one run (blocking; see the module docstring).

    The key is the run's stamp: a run that is still ingesting gets a new object after each commit
    and a finished run is loaded once. Older entries are evicted beyond :func:`run_facts_entries`
    runs or :func:`run_facts_budget` estimated bytes, least recently used first. The budget is
    checked on every lookup, hits included: an entry loads lazily and weighs almost nothing when it
    is inserted, so what the previous requests loaded is counted by the next one.
    """
    key = (str(facts_store.root), repo_id, run_id)
    stamp = run_stamp(facts_store.run_dir(repo_id, run_id))
    with _run_facts_lock:
        found = _run_facts.get(key)
        facts = found[1] if found is not None and found[0] == stamp else None
        if facts is not None:
            _run_facts.move_to_end(key)
    if facts is None:
        built = IndexedRunFacts(facts_store, repo_id, run_id)
        with _run_facts_lock:
            raced = _run_facts.get(key)
            if raced is not None and raced[0] == stamp:
                facts = raced[1]
            else:
                facts = built
                _run_facts[key] = (stamp, built)
            _run_facts.move_to_end(key)
    _enforce_run_facts_budget()
    return facts


def _enforce_run_facts_budget() -> None:
    """Evict least recently used runs beyond :func:`run_facts_entries` or :func:`run_facts_budget`.

    The entries are weighed outside the lock (each weight is memoised, so this is cheap after the
    first weighing of a view); an entry added meanwhile counts as empty until the next lookup. The
    most recently used entry always stays.
    """
    with _run_facts_lock:
        entries = list(_run_facts.items())
    weights = {key: entry.memory_bytes() for key, (_stamp, entry) in entries}
    entries_cap, budget = run_facts_entries(), run_facts_budget()
    with _run_facts_lock:
        total = sum(weights.get(key, 0) for key in _run_facts)
        while len(_run_facts) > 1 and (len(_run_facts) > entries_cap or total > budget):
            old_key, _old = _run_facts.popitem(last=False)
            total -= weights.get(old_key, 0)


def run_facts_weight() -> tuple[int, int]:
    """The number of cached runs and their estimated bytes together (for tests and benchmarks)."""
    with _run_facts_lock:
        entries = [entry for _stamp, entry in _run_facts.values()]
    return len(entries), sum(entry.memory_bytes() for entry in entries)


# -- source text ---------------------------------------------------------------------------------------

BLOB_BUDGET = _env_int("COVERSET_SOURCE_CACHE_MB", 64) * 1024 * 1024
"""Bytes of source text :func:`read_source` keeps (``COVERSET_SOURCE_CACHE_MB``)."""


def blob_budget() -> int:
    """The source-text cache's byte budget: the app's ``COVERSET_SOURCE_CACHE_MB``, else
    :data:`BLOB_BUDGET`."""
    mb = _configured_int("COVERSET_SOURCE_CACHE_MB")
    return mb * 1024 * 1024 if mb else BLOB_BUDGET


PREFETCH_MAX_PATHS = 2_000
"""A run with more measured sources than this reads them one by one instead of all at once."""

_blobs: OrderedDict[tuple[str, str, str], str] = OrderedDict()
_blob_bytes = 0
_blobs_lock = threading.Lock()
_prefetch_failed: set[tuple[str, str]] = set()
"""``(mirror, commit)`` pairs whose all-paths prefetch failed (bounded; cleared with the caches)."""


def _remember_blobs(mirror: Path, sha: str, found: dict[str, bytes]) -> None:
    global _blob_bytes
    with _blobs_lock:
        for path, data in found.items():
            key = (str(mirror), sha, path)
            if key in _blobs:
                continue
            text = data.decode("utf-8", "replace")
            _blobs[key] = text
            _blob_bytes += len(text)
        budget = blob_budget()
        while _blobs and _blob_bytes > budget:
            _old, text = _blobs.popitem(last=False)
            _blob_bytes -= len(text)


def _cached_blob(mirror: Path, sha: str, path: str) -> str | None:
    with _blobs_lock:
        key = (str(mirror), sha, path)
        found = _blobs.get(key)
        if found is not None:
            _blobs.move_to_end(key)
        return found


def _read_text(mirror: Path, sha: str, path: str, prefetch: Iterable[str]) -> str | None:
    """``path`` at ``sha`` from the cache, else from the mirror together with ``prefetch``.

    A prefetch that fails (any :class:`RepoError`, or a ``ValueError`` from a batch reply git
    formats unexpectedly, such as a missing path containing a space) falls back to reading
    ``path`` alone, so one odd path among the run's sources cannot fail every other file's page;
    the commit is then remembered and later misses read one path at a time.
    """
    found = _cached_blob(mirror, sha, path)
    if found is not None:
        return found
    commit = (str(mirror), sha)
    with _blobs_lock:
        batch = commit not in _prefetch_failed
    wanted = [path]
    if batch:
        wanted += [p for p in prefetch if p != path and _cached_blob(mirror, sha, p) is None]
    if len(wanted) > 1:
        try:
            _remember_blobs(mirror, sha, read_blobs(mirror, sha, wanted))
        except (RepoError, ValueError):
            with _blobs_lock:
                if len(_prefetch_failed) >= 256:
                    _prefetch_failed.clear()
                _prefetch_failed.add(commit)
        found = _cached_blob(mirror, sha, path)
    if found is None:  # absent at the commit, a failed prefetch, or larger than the whole budget
        data = read_blobs(mirror, sha, [path]).get(path)
        if data is None:
            return None
        _remember_blobs(mirror, sha, {path: data})
        return data.decode("utf-8", "replace")
    return found


def warm_run(
    facts_store: FactsStore,
    repo_id: int,
    run_id: str,
    mirror: Path,
    head_sha: str,
    base_sha: str | None = None,
) -> None:
    """Load a just-ingested run into the web's caches (blocking).

    The orchestrator calls it in a background thread after ``finalize_run``
    (``orchestrator.runner.schedule_warm_up``): the run's :class:`IndexedRunFacts` is cached with
    its paths, static facts, executed lines and the per-file index of ``test_lines`` built (what the
    PR, source and test pages read first); its measured sources are read from the mirror at
    ``head_sha`` in one batch, as a page's first miss would; and with ``base_sha`` (a PR run) the PR
    page's diff analysis is computed and cached (:func:`coverset.web.pr_view.analyse_diff`). A mirror
    without the commit, or a run with more than :data:`PREFETCH_MAX_PATHS` measured sources, just
    leaves the source cache cold.
    """
    facts = cached_run_facts(facts_store, repo_id, run_id)
    if not facts.exists:
        return
    facts.union_lines()  # paths, statics and the per-file executed-line indexes
    facts._bounds  # noqa: B018 - builds the sorted test_lines arrays and each file's row span
    if not (mirror / "HEAD").is_file():
        return
    if base_sha:
        from coverset.web.pr_view import analyse_diff

        analyse_diff(mirror, base_sha, head_sha, facts)
    if len(facts.statics) > PREFETCH_MAX_PATHS:
        return
    missing = [path for path in facts.statics if _cached_blob(mirror, head_sha, path) is None]
    if missing:
        try:
            _remember_blobs(mirror, head_sha, read_blobs(mirror, head_sha, missing))
        except (RepoError, ValueError):  # the page will read path by path, as it would anyway
            return


def source_cache() -> SourceCache:
    """The process's memo of syntax-highlighted sources (pass it to ``render_source``)."""
    return _SOURCE_CACHE


_SOURCE_CACHE = SourceCache(maxsize=32)


def clear_caches() -> None:
    """Forget every cached run, source text and highlighted file (tests)."""
    global _blob_bytes, _SOURCE_CACHE
    with _run_facts_lock:
        _run_facts.clear()
    with _blobs_lock:
        _blobs.clear()
        _prefetch_failed.clear()
        _blob_bytes = 0
    _SOURCE_CACHE = SourceCache(maxsize=32)


async def require_run(session: AsyncSession, run_id: str | None, repo_id: int | None = None) -> Run:
    """The run with ``run_id`` (of ``repo_id`` when given) or 404."""
    if not run_id or not RUN_ID_PARAM.fullmatch(run_id):
        raise NotFound("That is not a run id.")
    run = await session.get(Run, run_id)
    if run is None or (repo_id is not None and run.repo_id != repo_id):
        raise NotFound(f"There is no run {run_id} in this repository.")
    return run


async def default_run(session: AsyncSession, repo: Repo) -> Run | None:
    """The newest completed default-branch baseline, else the newest completed run."""
    run = await session.scalar(
        select(Run)
        .where(
            Run.repo_id == repo.id,
            Run.status == "completed",
            Run.branch == repo.default_branch,
            Run.trigger.in_(BASELINE_TRIGGERS),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = await session.scalar(
            select(Run)
            .where(Run.repo_id == repo.id, Run.status == "completed")
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    return run


async def pr_run(session: AsyncSession, pr: PullRequest) -> Run | None:
    """The latest run for the PR's head commit, else the PR's latest run."""
    run = await session.scalar(
        select(Run)
        .where(Run.pr_id == pr.id, Run.head_sha == pr.head_sha)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = await session.scalar(
            select(Run).where(Run.pr_id == pr.id).order_by(Run.created_at.desc()).limit(1)
        )
    return run


async def read_source(run: Run, path: str, facts: RunFacts) -> str | None:
    """``path`` at the run's head commit, when the run's facts list it (else None)."""
    if path not in facts.paths:
        return None
    mirror = mirror_path(app_settings(), run.repo_id)
    text = None
    if (mirror / "HEAD").is_file():
        measured = list(facts.statics) if len(facts.statics) <= PREFETCH_MAX_PATHS else []
        try:
            text = await asyncio.to_thread(_read_text, mirror, run.head_sha, path, measured)
        except (RepoError, ValueError):  # ValueError: a batch reply read_blobs cannot parse
            text = None
    if text is None:  # upload mode: sources sent with `coverset-ci upload --with-sources`
        from coverset.facts.sources import read_source as read_uploaded

        blob = await asyncio.to_thread(
            read_uploaded, app_settings().data_dir, run.repo_id, run.head_sha, path
        )
        text = blob.decode("utf-8", "replace") if blob is not None else None
    return text


async def run_counts(session: AsyncSession, run_ids: list[str]) -> dict[str, dict[str, int]]:
    """Outcome counts per run."""
    if not run_ids:
        return {}
    rows = await session.execute(
        select(TestResult.run_id, TestResult.outcome, func.count())
        .where(TestResult.run_id.in_(run_ids))
        .group_by(TestResult.run_id, TestResult.outcome)
    )
    out: dict[str, dict[str, int]] = {}
    for run_id, outcome, count in rows.all():
        out.setdefault(run_id, {})[outcome] = int(count)
    return out


async def result_rows(session: AsyncSession, run: Run, nodeids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Results of only ``nodeids`` in ``run`` by nodeid, with the keys of
    :func:`coverset.web.run_view.order_rows`' rows that a short test list shows (``nodeid``,
    ``test_id``, ``outcome``, ``rank``, ``duration_ms``, ``flaky``, ``quarantined``).

    The line popover lists the tests of one line: reading every result of the run (5,000 ORM rows
    on a real repository) to show a few dozen of them was most of its time.
    """
    from coverset.orchestrator.history import nodeid_hash

    wanted = list(dict.fromkeys(nodeids))
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), 500):
        chunk = [nodeid_hash(n) for n in wanted[start : start + 500]]
        rows = await session.execute(
            select(
                TestCase.nodeid,
                TestResult.test_id,
                TestResult.outcome,
                TestResult.rank,
                TestResult.duration_ms,
                TestResult.flaky,
                TestResult.quarantined,
                TestCase.quarantined,
            )
            .join(TestCase, TestCase.id == TestResult.test_id)
            .where(
                TestResult.run_id == run.id,
                TestCase.repo_id == run.repo_id,
                TestCase.nodeid_hash.in_(chunk),
            )
        )
        for nodeid, test_id, outcome, rank, duration_ms, flaky, quarantined, case_quarantined in rows.all():
            out[nodeid] = {
                "nodeid": nodeid,
                "test_id": test_id,
                "outcome": outcome,
                "rank": rank,
                "duration_ms": duration_ms,
                "flaky": flaky,
                "quarantined": bool(case_quarantined or quarantined),
            }
    return out


async def findings_for_run(
    session: AsyncSession, run: Run, func_keys: list[str] | None = None
) -> dict[str, ReviewFinding]:
    """The newest review finding per func_key recorded by ``run`` (or reused for its bodies)."""
    query = select(ReviewFinding).where(ReviewFinding.run_id == run.id)
    if func_keys:
        query = select(ReviewFinding).where(
            (ReviewFinding.run_id == run.id) | ReviewFinding.func_key.in_(func_keys)
        )
    found: dict[str, ReviewFinding] = {}
    for finding in await session.scalars(query.order_by(ReviewFinding.created_at.desc())):
        found.setdefault(finding.func_key, finding)
    return found


async def invariant_rows(session: AsyncSession, run: Run) -> list[dict[str, Any]]:
    """Invariant results of ``run`` with their spec, newest spec per key."""
    rows = (
        await session.execute(
            select(InvariantResult, Invariant)
            .join(Invariant, Invariant.id == InvariantResult.invariant_id)
            .where(InvariantResult.run_id == run.id)
            .order_by(Invariant.inv_key)
        )
    ).all()
    out = []
    for result, inv in rows:
        spec = inv.spec_json or {}
        evidence = result.evidence or {}
        out.append(
            {
                "key": inv.inv_key,
                "invariant_id": inv.id,
                "status": result.status,
                "statement": spec.get("statement", ""),
                "severity": spec.get("severity", ""),
                "target": (spec.get("scope") or {}).get("target", ""),
                "verify": [v.get("kind") for v in spec.get("verify", []) if isinstance(v, dict)],
                "lens": evidence.get("lens", ""),
                "removed_in_head": bool(evidence.get("removed_in_head")),
                "entries": evidence.get("entries", []),
                "target_func_key": result.target_func_key,
                "covering": len(result.covering_test_ids or []),
                "covering_ids": [t for t in result.covering_test_ids or [] if isinstance(t, int)],
            }
        )
    names = await nodeid_map(session, sorted({t for row in out for t in row["covering_ids"]}))
    for row in out:
        row["linked"] = [names[t] for t in row["covering_ids"] if t in names]
    return out


async def nodeid_map(session: AsyncSession, test_ids: list[int]) -> dict[int, str]:
    """Nodeids of control-DB test ids."""
    if not test_ids:
        return {}
    rows = await session.execute(select(TestCase.id, TestCase.nodeid).where(TestCase.id.in_(test_ids)))
    return {tid: nodeid for tid, nodeid in rows.all()}
