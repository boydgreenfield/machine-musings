"""What the pages read from one run's facts, loaded once per run and cached.

:func:`run_facts` returns a :class:`RunFacts` for ``(repo_id, run_id)``; the cache key includes a
stamp of the run directory's files, so a run that is still ingesting is re-read and a finished run
is read once. Everything here is synchronous (call it through ``asyncio.to_thread``) and reads the
store only through :class:`~coverset.facts.store.FactsStore` (``read_table``), never by globbing.

Input classes come from :class:`~coverset.analysis.shapes.Catalogue` over the run's own
``call_shapes`` (the same observations the verdict used). The declared types come from the
descriptors stored with the function at ingest (``functions.types_json``, what a neutral bundle
shipped) through :func:`~coverset_model.catalogue.function_catalogue_from_types` whenever the row
has them or the run's language is not Python, so a TypeScript function's page and cards count the
classes ``finalize_run`` stored; otherwise :func:`~coverset.analysis.shapes.function_catalogue`
reads them from the head commit's Python source, exactly as the verdict does.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import numpy as np

from coverset.analysis.functions import index_functions_detailed
from coverset.analysis.shapes import (
    Catalogue,
    FunctionCatalogue,
    FunctionInputCov,
    InputClass,
    ParamInfo,
    function_catalogue,
    function_catalogue_from_types,
)
from coverset.facts.cache import run_stamp
from coverset.facts.dictionary import RepoDictionary
from coverset.facts.queries import FunctionPoint, TreeNode, treemap
from coverset.facts.schema import CALLABLE_KINDS
from coverset.facts.store import FactsStore
from coverset.lang.base import IMPORT_CONTEXT, FuncSampling, FunctionRegion, ShapeObs, StaticFile
from coverset.web.render.ribbon import Flow
from coverset.web.render.shapegrid import GridCell, GridModel, GridRow
from coverset.web.render.source import LineFacts
from coverset_model.types import DescriptorError, TypeDescriptor, from_json

__all__ = [
    "FunctionView",
    "LostClass",
    "ParamReach",
    "RunFacts",
    "history_losses",
    "param_reach",
    "reached_classes",
    "run_facts",
    "run_point",
]

CACHE_SIZE = 24
SPECIFIC_FAMILIES = frozenset(
    {"email", "quantity", "number", "bool", "none", "elements", "order", "pairs", "literal", "enum"}
)
"""Families shown in inlay chips when a parameter has any (else every class is shown)."""
MAX_CHIPS = 8


_cache: OrderedDict[tuple[str, int, str, tuple[Any, ...]], RunFacts] = OrderedDict()
_lock = threading.Lock()


def run_facts(store: FactsStore, repo_id: int, run_id: str) -> RunFacts:
    """The (cached) facts of one run."""
    key = (str(store.root), repo_id, run_id, run_stamp(store.run_dir(repo_id, run_id)))
    with _lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
            return found
    facts = RunFacts(store, repo_id, run_id)
    with _lock:
        _cache[key] = facts
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return facts


@dataclass(frozen=True, slots=True)
class FunctionView:
    """A function's input-class picture in one run."""

    region: FunctionRegion
    cov: FunctionInputCov
    observations: tuple[ShapeObs, ...]
    samples: Mapping[str, int]
    annotations: Mapping[str, str | None]
    returns: str | None
    return_cells: tuple[GridCell, ...]
    line_pct: float | None
    branch_pct: float | None
    catalogue: Catalogue = dataclasses.field(default_factory=Catalogue)
    """The catalogue ``cov`` was computed with (enum-aware for the function's file)."""
    tests_called: int | None = None
    """Tests (not import time) that ran a line of the body, from line coverage; None when not
    counted. Tells "never called" apart from "called, but no argument shapes recorded" (a Rust
    bundle, a Python run without shape capture)."""

    @property
    def hollow(self) -> int:
        """Expected classes never observed."""
        return len(self.cov.missing)

    def cells(self, param: str | None = None) -> list[GridCell]:
        """Grid cells (every parameter, or one)."""
        out = []
        for cls in self.cov.expected:
            if param is not None and cls.param != param:
                continue
            out.append(_cell(cls, cls.name in self.cov.observed, self.samples.get(cls.name, 0)))
        return out

    def grid(self) -> GridModel:
        """The shape-grid model: one row per parameter in signature order, then ``return``."""
        rows: list[GridRow] = []
        params = [p.lstrip("*") for p in self.region.params]
        seen = set(params)
        extra = [c.param for c in self.cov.expected if c.param not in seen]
        for param in dict.fromkeys([*params, *extra]):
            if param in ("self", "cls") and not self.cells(param):
                continue
            cells = tuple(self.cells(param))
            note = ""
            if not cells:
                observed = any(o.param.lstrip("*") == param for o in self.observations)
                note = "no input classes expected" if observed else self.cov.missing_reason
                if not self.observations:
                    note = self.no_shapes_note
            rows.append(GridRow(param, self.annotations.get(param), cells, note))
        if self.return_cells or any(o.param == "return" for o in self.observations):
            rows.append(GridRow("return", self.returns, self.return_cells, "no return classes recorded"))
        observed = self.cov.observed_expected
        return GridModel(
            func_key=self.region.func_key,
            qualname=self.region.qualname,
            rows=tuple(rows),
            observed=observed,
            expected=len(self.cov.expected),
            line_pct=self.line_pct,
            branch_pct=self.branch_pct,
            missing_reason=self.cov.missing_reason,
            note=self.cov.note,
        )

    @property
    def no_shapes_note(self) -> str:
        """A row's note when no argument shape of the function was recorded: "never called by a
        test" only when line coverage agrees (or was not counted), else how many tests ran it."""
        if not self.tests_called:
            return "never called by a test"
        tests = "1 test" if self.tests_called == 1 else f"{self.tests_called} tests"
        return f"called by {tests}, no argument shapes recorded"

    def chips(self) -> list[tuple[str, str | None, list[GridCell], list[GridCell]]]:
        """Inlay chips per parameter: ``(param, annotation, shown cells, hidden cells)``.

        Specific families (email, quantity, ...) are shown when a parameter has any, at most
        :data:`MAX_CHIPS` (never-exercised classes first); every other class is returned as hidden,
        so the inlay can say "+K more" and name them.
        """
        out = []
        for param in dict.fromkeys(p.lstrip("*") for p in self.region.params):
            cells = self.cells(param)
            if not cells:
                continue
            specific = [c for c in cells if c.family in SPECIFIC_FAMILIES]
            shown = specific or cells
            if len(shown) > MAX_CHIPS:
                shown = sorted(shown, key=lambda c: c.observed)[:MAX_CHIPS]
            kept = {c.name for c in shown}
            hidden = sorted((c for c in cells if c.name not in kept), key=lambda c: c.observed)
            out.append((param, self.annotations.get(param), shown, hidden))
        return out

    def flows(self, file_of: Callable[[str], str]) -> list[Flow]:
        """Ribbon flows: test file → caller → parameter, weighted by samples."""
        catalogue = self.catalogue
        expected = {c.name for c in self.cov.expected}
        flows: list[Flow] = []
        for obs in self.observations:
            if obs.param in ("<receiver>", "return"):
                continue
            classes = frozenset(catalogue.observed_classes([obs]) & expected)
            if isinstance(obs.caller, tuple):
                caller = obs.caller[1]
                files: dict[str, int] = defaultdict(int)
                for test in obs.tests:
                    files[file_of(test)] += 1
                if not files:
                    files[file_of(obs.example_test or "")] = 1
            else:
                caller = "called from the test"
                files = {file_of(obs.caller): 1}
            total = sum(files.values())
            for file, weight in files.items():
                share = max(1, round(obs.samples * weight / total))
                flows.append(Flow(file, caller, obs.param.lstrip("*"), share, classes))
        return flows


def _cell(cls: InputClass, observed: bool, samples: int) -> GridCell:
    return GridCell(
        name=cls.name,
        family=cls.family,
        observed=observed,
        samples=samples,
        hint=cls.hypothesis_hint,
        description=cls.description,
    )


def test_file(nodeid: str) -> str:
    """The file part of a nodeid (``<import>`` and empty ids are labelled)."""
    if not nodeid:
        return "(unknown test)"
    if nodeid == IMPORT_CONTEXT:
        return "import time"
    return nodeid.split("::", 1)[0]


class RunFacts:
    """Lazy, cached views over one run's facts tables."""

    def __init__(self, store: FactsStore, repo_id: int, run_id: str) -> None:
        self.store = store
        self.repo_id = repo_id
        self.run_id = run_id

    def _read(self, table: str) -> Any:
        return self.store.read_table(self.repo_id, self.run_id, table)

    @property
    def exists(self) -> bool:
        """Whether the run has a facts directory."""
        return self.store.run_dir(self.repo_id, self.run_id).is_dir()

    @cached_property
    def ids(self) -> RepoDictionary:
        """The repo dictionary."""
        return self.store.dictionary(self.repo_id)

    @cached_property
    def paths(self) -> dict[str, int]:
        """Every path the run's bundles listed (sources and tests), path → file id."""
        out: dict[str, int] = {}
        for row in self._read("files").to_pylist():
            out.setdefault(row["path"], row["file_id"])
        return out

    @cached_property
    def statics(self) -> dict[str, StaticFile]:
        """Static facts per measured source path."""
        by_id = {fid: path for path, fid in self.paths.items()}
        arcs: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for row in self._read("file_arcs_possible").to_pylist():
            arcs[row["file_id"]].append((row["from_line"], row["to_line"]))
        out: dict[str, StaticFile] = {}
        for row in self._read("file_static").to_pylist():
            path = by_id.get(row["file_id"])
            if path is None or path in out:
                continue
            out[path] = StaticFile(
                path=path,
                n_lines=row["n_lines"] or 0,
                statements=tuple(row["statements"] or ()),
                excluded=tuple(row["excluded"] or ()),
                arcs_possible=tuple(sorted(set(arcs.get(row["file_id"], ())))),
            )
        return out

    @cached_property
    def branch_lines(self) -> dict[str, frozenset[int]]:
        """Lines with more than one possible exit, per path."""
        by_id = {fid: path for path, fid in self.paths.items()}
        out: dict[str, frozenset[int]] = {}
        for row in self._read("file_static").to_pylist():
            path = by_id.get(row["file_id"])
            if path is not None and path not in out:
                out[path] = frozenset(row["branch_lines"] or ())
        return out

    @cached_property
    def _lines(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        table = self._read("test_lines")
        file_id = table.column("file_id").to_numpy()
        line = table.column("line").to_numpy()
        test_id = table.column("test_id").to_numpy()
        order = np.lexsort((test_id, line, file_id))
        return file_id[order], line[order], test_id[order]

    def _slice(self, path: str) -> tuple[np.ndarray, np.ndarray]:
        file_id = self.paths.get(path)
        files, lines, tests = self._lines
        if file_id is None:
            return lines[:0], tests[:0]
        lo, hi = np.searchsorted(files, [file_id, file_id + 1])
        return lines[lo:hi], tests[lo:hi]

    @cached_property
    def run_arcs(self) -> dict[str, frozenset[tuple[int, int]]]:
        """Executed arcs per path."""
        by_id = {fid: path for path, fid in self.paths.items()}
        out: dict[str, set[tuple[int, int]]] = defaultdict(set)
        for row in self._read("run_arcs").to_pylist():
            path = by_id.get(row["file_id"])
            if path is not None:
                out[path].add((row["from_line"], row["to_line"]))
        return {k: frozenset(v) for k, v in out.items()}

    def union_lines(self) -> dict[str, set[int]]:
        """Executed lines per measured path (``<import>`` included)."""
        return {path: set(self._slice(path)[0].tolist()) for path in self.statics}

    def partial(self, path: str) -> dict[int, tuple[int, ...]]:
        """Branch lines whose possible exits were not all taken, with the missed destinations."""
        static = self.statics.get(path)
        if static is None:
            return {}
        taken = self.run_arcs.get(path, frozenset())
        possible: dict[int, set[int]] = defaultdict(set)
        for a, b in static.arcs_possible:
            possible[a].add(b)
        out: dict[int, tuple[int, ...]] = {}
        executed = set(self._slice(path)[0].tolist())
        for line in self.branch_lines.get(path, frozenset()):
            if line not in executed:
                continue
            missed = sorted(b for b in possible.get(line, ()) if (line, b) not in taken)
            if missed and len(possible.get(line, ())) > 1:
                out[line] = tuple(missed)
        return out

    def line_facts(self, path: str) -> LineFacts:
        """Line states and covering-test counts of ``path``."""
        static = self.statics.get(path)
        if static is None:
            return LineFacts(known=False)
        lines, tests = self._slice(path)
        counts: dict[int, int] = {}
        mask = tests != 0
        if mask.any():
            pairs = np.unique(np.stack([lines[mask], tests[mask]]), axis=1)
            values, freq = np.unique(pairs[0], return_counts=True)
            counts = dict(zip(values.tolist(), freq.tolist(), strict=True))
        return LineFacts(
            statements=frozenset(static.statements),
            excluded=frozenset(static.excluded),
            executed=frozenset(lines.tolist()),
            partial=self.partial(path),
            tests=counts,
        )

    def tests_in_range(self, path: str, start: int, end: int, *, exclude_import: bool = True) -> list[str]:
        """Nodeids whose lines touch ``[start, end]`` of ``path``, sorted."""
        lines, tests = self._slice(path)
        a, b = np.searchsorted(lines, [start, end + 1])
        ids = set(np.unique(tests[a:b]).tolist())
        if exclude_import:
            ids.discard(0)
        return sorted(self.ids.nodeid(t) for t in ids)

    def import_covers(self, path: str, line: int) -> bool:
        """Whether import time (``<import>``) executed ``path:line``."""
        lines, tests = self._slice(path)
        a, b = np.searchsorted(lines, [line, line + 1])
        return bool((tests[a:b] == 0).any())

    @cached_property
    def _function_rows(self) -> list[dict[str, Any]]:
        return self._read("functions").to_pylist()

    @cached_property
    def language(self) -> str:
        """The language of the run's functions (``python`` when the table names none)."""
        found = {row["language"] for row in self._function_rows if row["language"]}
        return found.pop() if len(found) == 1 else "python"

    @cached_property
    def declared_types(self) -> dict[str, dict[str, TypeDescriptor | None]]:
        """The descriptors stored at ingest (``functions.types_json``) by func_key.

        A value that does not parse as a descriptor reads as no declared type for that parameter.
        """
        out: dict[str, dict[str, TypeDescriptor | None]] = {}
        for row in self._function_rows:
            raw = row.get("types_json")
            if not raw:
                continue
            try:
                decoded = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(decoded, dict):
                continue
            found: dict[str, TypeDescriptor | None] = {}
            for param, value in decoded.items():
                try:
                    found[str(param)] = from_json(value) if value is not None else None
                except DescriptorError:
                    found[str(param)] = None
            out[row["func_key"]] = found
        return out

    def catalogue_for(self, region: FunctionRegion, source: str | None) -> FunctionCatalogue:
        """The catalogue ``finalize_run`` used for ``region``: the stored descriptors when the row has
        them or the run is not Python, else the annotations of the head ``source``."""
        declared = self.declared_types.get(region.func_key)
        if declared is not None or self.language != "python":
            return function_catalogue_from_types(region, declared)
        return function_catalogue(
            region, source, indexed=_indexed(region.path, source) if source is not None else None
        )

    @cached_property
    def regions(self) -> list[FunctionRegion]:
        """Every region of the head commit (functions, methods, classes, modules)."""
        out = []
        for row in self._function_rows:
            out.append(
                FunctionRegion(
                    func_key=row["func_key"],
                    path=row["path"],
                    qualname=row["qualname"],
                    ordinal=row["ordinal"],
                    def_line=row["def_line"],
                    body_start=row["body_start"],
                    end_line=row["end_line"],
                    body_hash=row["body_hash"],
                    sig_hash=row["sig_hash"],
                    params=tuple(json.loads(row["params_json"] or "[]")),
                    kind=row["kind"],
                )
            )
        return out

    def region(self, func_key: str) -> FunctionRegion | None:
        """The region with ``func_key``."""
        return next((r for r in self.regions if r.func_key == func_key), None)

    def callables(self, path: str | None = None) -> list[FunctionRegion]:
        """Functions and methods (of ``path``), in source order."""
        found = [r for r in self.regions if r.kind in CALLABLE_KINDS and (path is None or r.path == path)]
        return sorted(found, key=lambda r: (r.path, r.def_line))

    @cached_property
    def input_cov_rows(self) -> dict[str, dict[str, Any]]:
        """``func_input_cov`` rows by func_key."""
        return {row["func_key"]: row for row in self._read("func_input_cov").to_pylist()}

    @cached_property
    def _observations(self) -> dict[str, list[ShapeObs]]:
        triples = {r.func_key: r.triple for r in self.regions}
        for row in self._read("func_sampling").to_pylist():
            triples.setdefault(row["func_key"], (row["path"], row["qualname"], row["firstlineno"]))
        out: dict[str, list[ShapeObs]] = defaultdict(list)
        for row in self._read("call_shapes").to_pylist():
            triple = triples.get(row["func_key"])
            if triple is None:
                continue
            caller: tuple[str, str, int] | str
            if row["caller_kind"] == "test":
                caller = row["caller_key"] or ""
            else:
                caller = (row["caller_path"] or "", row["caller_qualname"] or "?", row["caller_line"] or 0)
            example_test = row["example_test_id"]
            out[row["func_key"]].append(
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
        return out

    def observations(self, func_key: str) -> list[ShapeObs]:
        """Shape observations of one function in this run."""
        return list(self._observations.get(func_key, ()))

    def sampling(self, region: FunctionRegion) -> FuncSampling | None:
        """The function's sampling summary (how many tests hit the sample cap)."""
        rows = [r for r in self._read("func_sampling").to_pylist() if r["func_key"] == region.func_key]
        if not rows:
            return None
        tests = sorted({self.ids.nodeid(t) for r in rows for t in (r["test_ids"] or ())})
        return FuncSampling(
            func=region.triple,
            calls=sum(int(r["calls"] or 0) for r in rows),
            tests=tuple(tests),
            truncated=any(bool(r["truncated"]) for r in rows),
        )

    def body_coverage(self, region: FunctionRegion) -> tuple[float | None, float | None]:
        """Line and branch percentages of the function body."""
        static = self.statics.get(region.path)
        if static is None:
            return None, None
        executed = set(self._slice(region.path)[0].tolist())
        body = {
            n
            for n in set(static.statements) - set(static.excluded)
            if region.body_start <= n <= region.end_line
        }
        if region.body_start <= region.def_line and not self.tests_in_range(
            region.path, region.def_line, region.def_line
        ):
            executed.discard(region.def_line)
        line_pct = round(100.0 * len(body & executed) / len(body), 1) if body else None
        taken = self.run_arcs.get(region.path, frozenset())
        possible = [
            (a, b)
            for a, b in static.arcs_possible
            if a in self.branch_lines.get(region.path, frozenset())
            and region.body_start <= a <= region.end_line
        ]
        branch_pct = (
            round(100.0 * sum(1 for arc in possible if arc in taken) / len(possible), 1) if possible else None
        )
        return line_pct, branch_pct

    def treemap_nodes(self) -> list[TreeNode]:
        """Treemap aggregates (one DuckDB query)."""
        return treemap(self.store, self.repo_id, self.run_id)

    def function_view(self, region: FunctionRegion, source: str | None) -> FunctionView:
        """Input-class coverage, per-class samples and the return row of ``region``.

        The catalogue is :meth:`catalogue_for`'s: the descriptors stored at ingest for a function
        that shipped them (any non-Python run), else
        :func:`~coverset.analysis.shapes.function_catalogue` over the head source, the helper the
        verdict uses too, so a card counts the classes the stored ``func_input_cov`` and the sticky
        comment count.
        """
        found = self.catalogue_for(region, source)
        catalogue, annotations, returns = found.catalogue, found.annotations, found.returns
        observations = self.observations(region.func_key)
        sampling = self.sampling(region)
        capped = (sampling is not None and sampling.truncated) or any(o.truncated for o in observations)
        # The body's covering tests, read only when needed: for the sample-cap wording, and to tell
        # a function no test ran from one whose runs recorded no argument shape.
        covering = (
            self.tests_in_range(region.path, region.body_start, region.end_line)
            if capped or not observations
            else None
        )
        cov = found.input_cov(region, observations, sampling, tests_called=covering if capped else None)
        expected = {c.name for c in cov.expected}
        samples: dict[str, int] = defaultdict(int)
        for obs in observations:
            for name in catalogue.observed_classes([obs]) & expected:
                samples[name] += obs.samples
        returned = [dataclasses.replace(o, param="returns") for o in observations if o.param == "return"]
        return_cells: tuple[GridCell, ...] = ()
        if returned:
            info = ParamInfo("returns", returns)
            seen = catalogue.observed_classes(returned)
            ret_samples: dict[str, int] = defaultdict(int)
            for obs in returned:
                for name in catalogue.observed_classes([obs]):
                    ret_samples[name] += obs.samples
            # Only what the function was seen returning: a return value is an output, so a class
            # it never returned is not a gap and gets no hollow square or strategy.
            return_cells = tuple(
                dataclasses.replace(
                    _cell(cls, True, ret_samples.get(cls.name, 0)),
                    name="return:" + cls.name.split(":", 1)[1],
                    role="return",
                )
                for cls in catalogue.expected_classes(info, returned)
                if cls.name in seen
            )
        line_pct, branch_pct = self.body_coverage(region)
        return FunctionView(
            region=region,
            cov=cov,
            observations=tuple(observations),
            samples=dict(samples),
            annotations=annotations,
            returns=returns,
            return_cells=return_cells,
            line_pct=line_pct,
            branch_pct=branch_pct,
            catalogue=catalogue,
            tests_called=len(covering) if covering is not None else None,
        )


_indexed_cache: OrderedDict[tuple[str, int], list[Any]] = OrderedDict()


def _indexed(path: str, source: str) -> list[Any]:
    key = (path, hash(source))
    with _lock:
        found = _indexed_cache.get(key)
    if found is None:
        try:
            found = index_functions_detailed(path, source)
        except SyntaxError:
            found = []
        with _lock:
            _indexed_cache[key] = found
            while len(_indexed_cache) > 64:
                _indexed_cache.popitem(last=False)
    return found


# -- one test's view of a function (the test page) -------------------------------------------------


def reached_classes(view: FunctionView, observations: Sequence[ShapeObs]) -> dict[str, frozenset[str]]:
    """The function's expected input classes that ``observations`` (one test's) carried, by
    parameter (receiver and return rows left out, as in :meth:`FunctionView.flows`)."""
    expected = {c.name for c in view.cov.expected}
    out: dict[str, set[str]] = defaultdict(set)
    for obs in observations:
        if obs.param in ("<receiver>", "return"):
            continue
        out[obs.param.lstrip("*")] |= view.catalogue.observed_classes([obs]) & expected
    return {param: frozenset(found) for param, found in out.items()}


@dataclass(frozen=True, slots=True)
class ParamReach:
    """One parameter on the test page: the classes this test passed in, the function's classes."""

    param: str
    this_test: tuple[GridCell, ...]
    """Classes this test reached (filled) and the function's never-exercised classes (hollow)."""
    any_test: tuple[GridCell, ...]
    """Every expected class of the parameter: filled when any test reached it."""
    reached: tuple[str, ...]
    others_only: int
    """Classes other tests reached but this one did not (left out of ``this_test``)."""


def param_reach(view: FunctionView, reached: Mapping[str, frozenset[str]]) -> list[ParamReach]:
    """Per parameter (signature order, then any other parameter with expected classes) the strips
    the test page draws: "this test" and "any test"."""
    missing = {c.name for c in view.cov.missing}
    params = [p.lstrip("*") for p in view.region.params]
    params += [c.param for c in view.cov.expected if c.param not in params]
    out = []
    for param in dict.fromkeys(params):
        cells = view.cells(param)
        if not cells:
            continue
        mine = reached.get(param, frozenset())
        this_test = tuple(
            dataclasses.replace(cell, observed=cell.name in mine)
            for cell in cells
            if cell.name in mine or cell.name in missing
        )
        out.append(
            ParamReach(
                param=param,
                this_test=this_test,
                any_test=tuple(cells),
                reached=tuple(sorted(c.short for c in cells if c.name in mine)),
                others_only=sum(1 for c in cells if c.observed and c.name not in mine),
            )
        )
    return out


# -- a function over the baselines (the function page's history strip) -----------------------------


def run_point(facts: RunFacts, region: FunctionRegion, created_at: Any, head_sha: str) -> FunctionPoint:
    """``region`` in ``facts``' run as a :class:`~coverset.facts.queries.FunctionPoint`, for a run
    the baseline history does not hold (a PR run): the stored ``func_input_cov`` row and the body
    line percentage by the same rule as :func:`~coverset.facts.queries.function_history`."""
    row = facts.input_cov_rows.get(region.func_key) or {}
    missing: dict[str, Any] = {}
    try:
        loaded = json.loads(row.get("missing_json") or "{}")
        missing = loaded if isinstance(loaded, dict) else {}
    except ValueError:
        missing = {}
    line_pct, _branch_pct = facts.body_coverage(region)
    return FunctionPoint(
        run_id=facts.run_id,
        created_at=created_at,
        head_sha=head_sha,
        present=True,
        expected=row.get("blocks_expected"),
        observed=row.get("blocks_observed"),
        line_pct=line_pct,
        tests_called=int(row.get("tests_called") or 0),
        missing=tuple(
            sorted({str(n) for names in missing.values() if isinstance(names, list) for n in names})
        ),
        tests_truncated=int(row.get("tests_truncated") or 0),
        sig_hash=region.sig_hash or None,
    )


@dataclass(frozen=True, slots=True)
class LostClass:
    """An input class missing at the selected point that a comparable earlier point did not miss."""

    name: str
    last_seen: FunctionPoint
    """The newest comparable earlier point that did not list it as missing."""


def _comparable(a: FunctionPoint, b: FunctionPoint) -> bool:
    """Whether the stored gaps of two points can be compared by name: the same expected class
    count and, when both are known, the same signature digest."""
    if not (a.present and b.present) or a.expected is None or a.expected != b.expected:
        return False
    return not (a.sig_hash and b.sig_hash and a.sig_hash != b.sig_hash)


def history_losses(points: Sequence[FunctionPoint], index: int) -> list[LostClass]:
    """Classes missing at ``points[index]`` that the nearest comparable earlier point did not list
    as missing, each with the newest comparable earlier point that did not.

    Only never-observed classes are stored, so this is inferred from the stored gaps: a point is
    comparable only when it expects the same number of classes with the same signature digest
    (``sig_hash``); others are skipped. Even then the expected set may differ at the same count,
    which is why the page says "missing now, not missing in" rather than "observed"."""
    if not 0 <= index < len(points):
        return []
    here = points[index]
    if here.expected is None or not here.present:
        return []
    earlier = [p for p in points[:index] if _comparable(p, here)]
    if not earlier:
        return []
    previous = earlier[-1]
    lost = sorted(set(here.missing) - set(previous.missing))
    out = []
    for name in lost:
        seen = next(p for p in reversed(earlier) if name not in p.missing)
        out.append(LostClass(name, seen))
    return out
