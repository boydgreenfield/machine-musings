"""Read-side queries over a :class:`~coverset.facts.store.FactsStore`.

Each public query opens its own short-lived in-memory DuckDB connection scoped to the run(s) it
reads and returns plain dataclasses. Rules (from the plan's "Facts store" section): patch coverage
counts ``<import>`` lines as covered, and while a run is missing shards its uncovered changed lines
are "unknown", not "missing"; covering-test lists exclude ``<import>`` by default; "test T calls
function F" means T's lines intersect F's body ``[body_start, end_line]``. A test recorded by more
than one shard of a run resolves to the result of the most recently committed shard.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any, Literal

import duckdb
import numpy as np
import pyarrow as pa

from coverset.analysis.shapes import example_sort_key
from coverset.facts.dictionary import RepoDictionary
from coverset.facts.schema import CALLABLE_KINDS
from coverset.facts.store import FactsStore
from coverset.lang.base import IMPORT_CONTEXT, FunctionRegion, ShapeObs

__all__ = [
    "MODULE_LEAF",
    "BaselinePoint",
    "ClassChange",
    "FuncInputCov",
    "FunctionPoint",
    "GapRow",
    "IngestRecord",
    "ParquetPriorRun",
    "PatchCoverage",
    "RunSummary",
    "ShapeRow",
    "TableInventory",
    "TestOutcomeRow",
    "TouchedFile",
    "TouchedFunction",
    "TreeNode",
    "baseline_trend",
    "class_changes",
    "function_history",
    "function_input_cov",
    "ingest_record",
    "line_tests",
    "patch_coverage",
    "run_inventory",
    "run_summary",
    "shapes_for_function",
    "test_history",
    "test_shapes",
    "test_touched",
    "tests_covering",
    "top_gaps",
    "treemap",
]

MODULE_LEAF = "<module>"


def _query(
    store: FactsStore,
    sql: str,
    params: Sequence[object] | Mapping[str, object] = (),
    *,
    repo_id: int,
    run_id: str | None = None,
    run_ids: Sequence[str] | None = None,
    tables: Sequence[str],
    register: Mapping[str, pa.Table] | None = None,
) -> pa.Table:
    """Run one statement on a fresh connection; retried once if a shard was replaced mid-query."""

    def execute() -> pa.Table:
        con = store.connect(repo_id=repo_id, run_id=run_id, run_ids=run_ids, tables=tables)
        try:
            for name, table in (register or {}).items():
                con.register(name, table)
            return con.execute(sql, params).to_arrow_table()
        finally:
            con.close()

    try:
        return execute()
    except duckdb.IOException:
        return execute()


# -- patch coverage ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PatchCoverage:
    """Changed-line coverage: |changed ∩ statements ∩ executed| / |changed ∩ statements|.

    ``per_file`` maps path → (covered, total). Changed statements no test (and no import) executed
    are ``missing`` when the run's coverage is complete and ``unknown`` while shards are missing
    (``complete`` is false: the run was never finalized or fewer than ``expected_shards`` shards
    are in), when ``percent`` is a lower bound. ``unmeasured`` names changed paths with no static
    facts in the run.
    """

    covered: int
    total: int
    per_file: dict[str, tuple[int, int]]
    missing: dict[str, list[int]]
    unknown: dict[str, list[int]]
    unmeasured: tuple[str, ...]
    complete: bool
    expected_shards: int | None
    ingested_shards: int

    @property
    def percent(self) -> float | None:
        """Patch coverage in percent, or None when no changed line is a statement."""
        return 100.0 * self.covered / self.total if self.total else None


_PATCH_SQL = """
WITH paths AS (
    SELECT file_id, any_value(path) AS path FROM files GROUP BY file_id
), st AS (
    SELECT p.path, s.file_id, any_value(s.statements) AS statements, any_value(s.excluded) AS excluded
    FROM file_static s JOIN paths p USING (file_id)
    WHERE p.path IN (SELECT path FROM changed)
    GROUP BY p.path, s.file_id
), hit AS (
    SELECT DISTINCT tl.file_id, tl.line FROM test_lines tl
    WHERE tl.file_id IN (SELECT file_id FROM st)
), run AS (
    SELECT coalesce(bool_or(coverage_complete), false) AS complete,
           any_value(expected_shards) AS expected_shards,
           coalesce(any_value(ingested_shards), 0) AS ingested_shards
    FROM runs_dim
), lines AS (
    SELECT c.path, c.line, st.file_id IS NOT NULL AS measured,
           coalesce(list_contains(st.statements, c.line) AND NOT list_contains(st.excluded, c.line), false)
               AS is_statement,
           hit.line IS NOT NULL AS is_covered
    FROM changed c
    LEFT JOIN st ON st.path = c.path
    LEFT JOIN hit ON hit.file_id = st.file_id AND hit.line = c.line
)
SELECT lines.*, run.* FROM run LEFT JOIN lines ON true
ORDER BY lines.path, lines.line
"""


def patch_coverage(
    store: FactsStore, repo_id: int, run_id: str, changed: Mapping[str, set[int]]
) -> PatchCoverage:
    """Coverage of ``changed`` (path → head-side changed lines) by the run's union of tests."""
    pairs = sorted((path, line) for path, lines in changed.items() for line in lines)
    changed_table = pa.table(
        {
            "path": pa.array([p for p, _ in pairs], pa.string()),
            "line": pa.array([n for _, n in pairs], pa.int32()),
        }
    )
    rows = _query(
        store,
        _PATCH_SQL,
        repo_id=repo_id,
        run_id=run_id,
        tables=("files", "file_static", "test_lines", "runs_dim"),
        register={"changed": changed_table},
    ).to_pylist()
    per_file: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    uncovered: dict[str, list[int]] = defaultdict(list)
    unmeasured: set[str] = set()
    complete, expected, ingested = False, None, 0
    for row in rows:
        complete, expected, ingested = row["complete"], row["expected_shards"], row["ingested_shards"]
        if row["path"] is None:
            continue
        if not row["measured"]:
            unmeasured.add(row["path"])
        elif row["is_statement"]:
            counts = per_file[row["path"]]
            counts[1] += 1
            if row["is_covered"]:
                counts[0] += 1
            else:
                uncovered[row["path"]].append(row["line"])
    gaps = dict(sorted(uncovered.items()))
    return PatchCoverage(
        covered=sum(c for c, _ in per_file.values()),
        total=sum(t for _, t in per_file.values()),
        per_file={path: (c, t) for path, (c, t) in sorted(per_file.items())},
        missing=gaps if complete else {},
        unknown={} if complete else gaps,
        unmeasured=tuple(sorted(unmeasured)),
        complete=complete,
        expected_shards=expected,
        ingested_shards=ingested,
    )


# -- line → tests --------------------------------------------------------------------------------

_TESTS_IN_RANGE_SQL = """
SELECT td.nodeid
FROM (
    SELECT DISTINCT test_id FROM test_lines
    WHERE file_id = (SELECT any_value(file_id) FROM files WHERE path = $path)
      AND line BETWEEN $start AND $end
      AND (NOT $exclude_import OR test_id <> 0)
) hit
JOIN (SELECT test_id, any_value(nodeid) AS nodeid FROM tests_dim GROUP BY test_id) td USING (test_id)
ORDER BY td.nodeid
"""


def tests_covering(
    store: FactsStore,
    repo_id: int,
    run_id: str,
    path: str,
    start: int,
    end: int,
    *,
    exclude_import: bool = True,
) -> list[str]:
    """Nodeids of the run's tests that executed any line of ``path`` in ``[start, end]``."""
    rows = _query(
        store,
        _TESTS_IN_RANGE_SQL,
        {"path": path, "start": start, "end": end, "exclude_import": exclude_import},
        repo_id=repo_id,
        run_id=run_id,
        tables=("files", "test_lines", "tests_dim"),
    )
    return rows.column("nodeid").to_pylist()


def line_tests(
    store: FactsStore, repo_id: int, run_id: str, path: str, line: int, *, exclude_import: bool = False
) -> list[str]:
    """Nodeids that executed ``path:line`` in the run (``<import>`` included unless excluded)."""
    return tests_covering(store, repo_id, run_id, path, line, line, exclude_import=exclude_import)


# -- treemap -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TreeNode:
    """One treemap node. Function leaves have path ``<file>::<qualname>``; module-level code of a
    file is the leaf ``<file>::<module>`` (kind ``function``, no ``func_key``)."""

    path: str
    kind: Literal["dir", "file", "function"]
    statements: int
    covered: int
    input_cov: float | None
    func_key: str | None = None
    def_line: int | None = None


_TREEMAP_SQL = f"""
WITH paths AS (
    SELECT file_id, any_value(path) AS path FROM files GROUP BY file_id
), st AS (
    SELECT file_id, any_value(statements) AS statements FROM file_static GROUP BY file_id
), stl AS (
    SELECT file_id, unnest(statements) AS line FROM st
), hit AS (
    SELECT DISTINCT file_id, line FROM test_lines
), fn AS (
    SELECT DISTINCT func_key, file_id, qualname, def_line, body_start, end_line FROM functions
    WHERE kind IN {CALLABLE_KINDS!r}
), owner AS (
    SELECT stl.file_id, stl.line, arg_max(fn.func_key, fn.body_start) AS func_key
    FROM stl LEFT JOIN fn ON fn.file_id = stl.file_id AND stl.line BETWEEN fn.body_start AND fn.end_line
    GROUP BY stl.file_id, stl.line
), leaf AS (
    SELECT o.file_id, o.func_key, count(*) AS statements, count(hit.line) AS covered
    FROM owner o LEFT JOIN hit ON hit.file_id = o.file_id AND hit.line = o.line
    GROUP BY o.file_id, o.func_key
), all_leaves AS (
    SELECT coalesce(leaf.file_id, fn.file_id) AS file_id, coalesce(leaf.func_key, fn.func_key) AS func_key,
           coalesce(leaf.statements, 0) AS statements, coalesce(leaf.covered, 0) AS covered
    FROM leaf FULL JOIN fn ON fn.func_key = leaf.func_key
    UNION ALL
    SELECT st.file_id, NULL, 0, 0 FROM st
    WHERE st.file_id NOT IN (SELECT file_id FROM leaf)
)
SELECT p.path, l.func_key, fn.qualname, fn.def_line, l.statements, l.covered,
       fic.blocks_expected, fic.blocks_observed
FROM all_leaves l
JOIN paths p USING (file_id)
LEFT JOIN fn ON fn.func_key = l.func_key
LEFT JOIN func_input_cov fic ON fic.func_key = l.func_key
ORDER BY p.path, fn.def_line NULLS FIRST, fn.qualname
"""


@dataclass(slots=True)
class _Acc:
    statements: int = 0
    covered: int = 0
    expected: int = 0
    observed: int = 0

    def add(self, statements: int, covered: int, expected: int | None, observed: int | None) -> None:
        self.statements += statements
        self.covered += covered
        if expected:
            self.expected += expected
            self.observed += observed or 0

    @property
    def input_cov(self) -> float | None:
        return self.observed / self.expected if self.expected else None


def treemap(store: FactsStore, repo_id: int, run_id: str) -> list[TreeNode]:
    """Treemap nodes in pre-order (each directory, then its files, each file then its leaves).

    Area is statements: every statement belongs to the innermost function or method whose body
    ``[body_start, end_line]`` contains it, else to the file's ``<module>`` leaf. Def and
    decorator lines run at import time, so like coverage.py's own function regions they belong to
    the enclosing scope, and a function no test calls shows no covered statement. ``input_cov`` is
    observed ÷ expected input classes (None when nothing is expected, e.g. never called).
    """
    rows = _query(
        store,
        _TREEMAP_SQL,
        repo_id=repo_id,
        run_id=run_id,
        tables=("files", "file_static", "test_lines", "functions", "func_input_cov"),
    ).to_pylist()
    files: dict[str, _Acc] = defaultdict(_Acc)
    dirs: dict[str, _Acc] = defaultdict(_Acc)
    leaves: dict[str, list[TreeNode]] = defaultdict(list)
    for row in rows:
        path, expected, observed = row["path"], row["blocks_expected"], row["blocks_observed"]
        files[path].add(row["statements"], row["covered"], expected, observed)
        parts = path.split("/")
        for depth in range(1, len(parts)):
            dirs["/".join(parts[:depth])].add(row["statements"], row["covered"], expected, observed)
        if row["func_key"] is None and row["statements"] == 0:
            continue
        leaf_acc = _Acc()
        leaf_acc.add(row["statements"], row["covered"], expected, observed)
        leaves[path].append(
            TreeNode(
                path=f"{path}::{row['qualname'] or MODULE_LEAF}",
                kind="function",
                statements=row["statements"],
                covered=row["covered"],
                input_cov=leaf_acc.input_cov,
                func_key=row["func_key"],
                def_line=row["def_line"],
            )
        )
    nodes: list[TreeNode] = []
    emitted: set[str] = set()
    for path in sorted(files):
        parts = path.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            if prefix not in emitted:
                emitted.add(prefix)
                acc = dirs[prefix]
                nodes.append(TreeNode(prefix, "dir", acc.statements, acc.covered, acc.input_cov))
        acc = files[path]
        nodes.append(TreeNode(path, "file", acc.statements, acc.covered, acc.input_cov))
        nodes.extend(leaves[path])
    return nodes


# -- shapes --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShapeRow:
    """Observations of one (parameter, shape key) of a function, summed over the last N runs."""

    param: str
    shape_key: str
    samples: int
    runs: int
    n_tests: int
    tests: tuple[str, ...]
    example: str | None
    example_test: str | None


_RECENT_RUNS_SQL = """
SELECT run_id FROM runs_dim
WHERE $branch IS NULL OR branch = $branch
ORDER BY created_at DESC, run_id DESC
LIMIT $last_n
"""

_SHAPES_SQL = """
WITH recent AS (
    SELECT DISTINCT run_id FROM runs_dim
), cs AS (
    SELECT cs.* FROM call_shapes cs JOIN recent r USING (run_id)
    WHERE cs.func_key = $func_key
)
SELECT param, shape_id, sum(samples)::BIGINT AS samples, count(DISTINCT run_id) AS runs,
       list_sort(list_distinct(flatten(list(test_ids)))) AS test_ids,
       list(DISTINCT {'example': example_repr, 'test_id': example_test_id})
           FILTER (WHERE example_repr IS NOT NULL) AS examples
FROM cs GROUP BY param, shape_id
ORDER BY param, samples DESC, shape_id
"""


def shapes_for_function(
    store: FactsStore,
    repo_id: int,
    func_key: str,
    last_n_runs: int = 5,
    *,
    max_tests: int = 20,
    branch: str | None = None,
) -> list[ShapeRow]:
    """Every shape recorded for ``func_key`` over the repo's ``last_n_runs`` most recent runs.

    Recent runs are picked from ``runs_dim`` first (only runs whose ``runs_dim.branch`` is
    ``branch`` when given, e.g. the default branch for baseline-only history), so only those runs'
    ``call_shapes`` files are read. ``tests`` holds at most ``max_tests`` nodeids (sorted);
    ``n_tests`` is the full count. The example is chosen among every non-null redacted example of
    the (parameter, shape) by :func:`~coverset.analysis.shapes.example_sort_key` (shortest repr,
    then smallest repr, then smallest test nodeid), so it does not depend on run or test order.
    """
    recent = _query(
        store,
        _RECENT_RUNS_SQL,
        {"last_n": last_n_runs, "branch": branch},
        repo_id=repo_id,
        tables=("runs_dim",),
    ).column("run_id")
    if not len(recent):
        return []
    rows = _query(
        store,
        _SHAPES_SQL,
        {"func_key": func_key},
        repo_id=repo_id,
        run_ids=recent.to_pylist(),
        tables=("runs_dim", "call_shapes"),
    ).to_pylist()
    if not rows:
        return []
    ids = store.dictionary(repo_id)
    result = []
    for row in rows:
        nodeids = sorted(ids.nodeid(t) for t in row["test_ids"])
        example, example_test = _stable_example(row["examples"] or [], ids)
        result.append(
            ShapeRow(
                param=row["param"],
                shape_key=ids.shape_key(row["shape_id"]),
                samples=row["samples"],
                runs=row["runs"],
                n_tests=len(nodeids),
                tests=tuple(nodeids[:max_tests]),
                example=example,
                example_test=example_test,
            )
        )
    return result


def _stable_example(
    candidates: Sequence[Mapping[str, object]], ids: RepoDictionary
) -> tuple[str | None, str | None]:
    """The (example, nodeid) that sorts first under :func:`example_sort_key`."""
    best: tuple[tuple[int, str, str], str, str | None] | None = None
    for candidate in candidates:
        example = candidate.get("example")
        if not isinstance(example, str):
            continue
        test_id = candidate.get("test_id")
        nodeid = ids.nodeid(test_id) if isinstance(test_id, int) else None
        key = example_sort_key(example, nodeid)
        if best is None or key < best[0]:
            best = (key, example, nodeid)
    return (best[1], best[2]) if best is not None else (None, None)


@dataclass(frozen=True, slots=True)
class FuncInputCov:
    """Input-class coverage of one function in one run (see ``func_input_cov``)."""

    func_key: str
    tests_called: int
    tests_sampled: int
    tests_truncated: int
    blocks_expected: int | None
    blocks_observed: int | None
    missing: dict[str, list[str]]

    @property
    def ratio(self) -> float | None:
        """Observed ÷ expected input classes, or None when nothing is expected."""
        if not self.blocks_expected:
            return None
        return (self.blocks_observed or 0) / self.blocks_expected


def function_input_cov(store: FactsStore, repo_id: int, run_id: str, func_key: str) -> FuncInputCov | None:
    """The finalized input-class coverage of ``func_key`` in a run, or None if not computed."""
    rows = _query(
        store,
        "SELECT * EXCLUDE (repo_id, run_id) FROM func_input_cov WHERE func_key = ?",
        [func_key],
        repo_id=repo_id,
        run_id=run_id,
        tables=("func_input_cov",),
    ).to_pylist()
    if not rows:
        return None
    row = rows[0]
    return FuncInputCov(
        func_key=row["func_key"],
        tests_called=row["tests_called"],
        tests_sampled=row["tests_sampled"],
        tests_truncated=row["tests_truncated"],
        blocks_expected=row["blocks_expected"],
        blocks_observed=row["blocks_observed"],
        missing=json.loads(row["missing_json"]) if row["missing_json"] else {},
    )


# -- run summary ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Headline facts of one run; coverage totals are None until :func:`finalize_run` ran."""

    run_id: str
    head_sha: str
    base_sha: str | None
    branch: str | None
    pr_number: int | None
    created_at: datetime
    expected_shards: int | None
    ingested_shards: int
    coverage_complete: bool
    finalized: bool
    tests: int
    outcomes: dict[str, int]
    files: int | None
    statements: int | None
    covered: int | None
    branches: int | None
    branches_covered: int | None
    partial_branches: int | None
    percent: float | None


_SUMMARY_SQL = """
WITH shard_at AS (
    SELECT shard_id, max(ingested_at) FILTER (WHERE status = 'ok') AS at FROM ingest_log GROUP BY shard_id
), t AS (
    SELECT td.test_id, arg_max(td.outcome, s.at) AS outcome
    FROM tests_dim td JOIN shard_at s USING (shard_id)
    WHERE td.test_id <> 0
    GROUP BY td.test_id
)
SELECT r.*,
    (SELECT count(*) FROM t) AS tests,
    (SELECT map(list(outcome), list(n)) FROM (
        SELECT outcome, count(*) AS n FROM t WHERE outcome IS NOT NULL GROUP BY outcome)) AS outcomes,
    (SELECT count(*) FROM file_cov) AS files,
    (SELECT sum(statements)::BIGINT FROM file_cov) AS statements,
    (SELECT sum(covered)::BIGINT FROM file_cov) AS covered,
    (SELECT sum(branches)::BIGINT FROM file_cov) AS branches,
    (SELECT sum(branches_covered)::BIGINT FROM file_cov) AS branches_covered,
    (SELECT sum(partial_branches)::BIGINT FROM file_cov) AS partial_branches
FROM runs_dim r
"""


_TOTALS = ("files", "statements", "covered", "branches", "branches_covered", "partial_branches")


def run_summary(store: FactsStore, repo_id: int, run_id: str) -> RunSummary | None:
    """Headline facts of a run, or None when the run has no facts."""
    rows = _query(
        store,
        _SUMMARY_SQL,
        repo_id=repo_id,
        run_id=run_id,
        tables=("runs_dim", "tests_dim", "ingest_log", "file_cov"),
    ).to_pylist()
    if not rows:
        return None
    row = rows[0]
    finalized = row["finalized_at"] is not None
    totals = {k: row[k] if finalized else None for k in _TOTALS}
    lines = (totals["statements"] or 0) + (totals["branches"] or 0)
    hits = (totals["covered"] or 0) + (totals["branches_covered"] or 0)
    return RunSummary(
        run_id=run_id,
        head_sha=row["head_sha"],
        base_sha=row["base_sha"],
        branch=row["branch"],
        pr_number=row["pr_number"],
        created_at=row["created_at"],
        expected_shards=row["expected_shards"],
        ingested_shards=row["ingested_shards"],
        coverage_complete=bool(row["coverage_complete"]),
        finalized=finalized,
        tests=row["tests"],
        outcomes=dict(row["outcomes"] or {}),
        percent=100.0 * hits / lines if finalized and lines else None,
        **totals,
    )


# -- prior run -----------------------------------------------------------------------------------


class ParquetPriorRun:
    """:class:`~coverset.lang.base.PriorRun` over one stored run; loads lazily, caches per instance."""

    def __init__(self, store: FactsStore, repo_id: int, run_id: str) -> None:
        self.store = store
        self.repo_id = repo_id
        self.run_id = run_id

    def _read(self, table: str) -> pa.Table:
        return self.store.read_table(self.repo_id, self.run_id, table)

    @cached_property
    def _ids(self) -> RepoDictionary:
        return self.store.dictionary(self.repo_id)

    @cached_property
    def _functions(self) -> list[FunctionRegion]:
        return [
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
            for row in self._read("functions").to_pylist()
        ]

    @cached_property
    def _by_line(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``test_lines`` as (file_id, line, test_id) arrays sorted by file, line."""
        table = self._read("test_lines")
        file_id = table.column("file_id").to_numpy()
        line = table.column("line").to_numpy()
        test_id = table.column("test_id").to_numpy()
        order = np.lexsort((test_id, line, file_id))
        return file_id[order], line[order], test_id[order]

    @cached_property
    def _by_test(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``test_lines`` as (test_id, file_id, line) arrays sorted by test."""
        file_id, line, test_id = self._by_line
        order = np.argsort(test_id, kind="stable")
        return test_id[order], file_id[order], line[order]

    @cached_property
    def _statements(self) -> dict[int, tuple[int, ...]]:
        table = self._read("file_static")
        return {
            row["file_id"]: tuple(row["statements"] or ())
            for row in table.select(["file_id", "statements"]).to_pylist()
        }

    @cached_property
    def _durations(self) -> dict[str, float]:
        """Duration by nodeid; a test recorded by several shards takes the latest committed one."""
        shard_at: dict[int, datetime] = {}
        for row in self._read("ingest_log").to_pylist():
            if row["status"] == "ok":
                at = row["ingested_at"]
                shard_at[row["shard_id"]] = max(at, shard_at.get(row["shard_id"], at))
        rows = self._read("tests_dim").select(["nodeid", "duration_s", "shard_id"]).to_pylist()
        rows = [r for r in rows if r["duration_s"] is not None and r["nodeid"] != IMPORT_CONTEXT]
        rows.sort(key=lambda r: shard_at.get(r["shard_id"], datetime.min.replace(tzinfo=UTC)))
        return {r["nodeid"]: r["duration_s"] for r in rows}

    def functions(self) -> list[FunctionRegion]:
        """Every function region recorded for the run's head commit."""
        return list(self._functions)

    def tests_touching(self, path: str, start: int, end: int) -> set[str]:
        """Nodeids (never ``<import>``) whose lines intersect ``[start, end]`` of ``path``."""
        file_id = self._ids.find_file_id(path)
        if file_id is None:
            return set()
        files, lines, tests = self._by_line
        lo, hi = np.searchsorted(files, [file_id, file_id + 1])
        a, b = np.searchsorted(lines[lo:hi], [start, end + 1])
        return {self._ids.nodeid(t) for t in np.unique(tests[lo + a : lo + b]).tolist() if t != 0}

    def test_lines(self, nodeid: str) -> dict[str, set[int]]:
        """The lines ``nodeid`` executed in this run, by path."""
        test_id = self._ids.find_test_id(nodeid)
        if test_id is None:
            return {}
        tests, files, lines = self._by_test
        lo, hi = np.searchsorted(tests, [test_id, test_id + 1])
        result: dict[str, set[int]] = defaultdict(set)
        for file_id, line in zip(files[lo:hi].tolist(), lines[lo:hi].tolist(), strict=True):
            result[self._ids.path(file_id)].add(line)
        return dict(result)

    def module_level_lines(self, path: str) -> set[int]:
        """Statements of ``path`` outside every function/method ``[def_line, end_line]``.

        Unlike the treemap (which follows coverage.py and gives def and decorator lines to the
        enclosing scope), def and decorator lines count as part of the function here: impact
        analysis attributes a changed decorator or signature to the function (the plan's rule),
        so they are not module-level changes.
        """
        file_id = self._ids.find_file_id(path)
        if file_id is None:
            return set()
        statements = set(self._statements.get(file_id, ()))
        for region in self._functions:
            if region.path == path and region.kind in CALLABLE_KINDS:
                statements -= set(range(region.def_line, region.end_line + 1))
        return statements

    def durations(self) -> dict[str, float]:
        """Recorded duration in seconds by nodeid (one run, so the p50 is the value itself)."""
        return dict(self._durations)


# -- one test's data flow ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TouchedFunction:
    """One function (or method) a test called, with the lines of its body the test ran.

    A line belongs to the innermost callable whose body ``[body_start, end_line]`` contains it (the
    treemap's rule), so running a nested function does not make its enclosing function "called".
    """

    func_key: str
    qualname: str
    kind: str
    def_line: int
    body_start: int
    end_line: int
    lines: tuple[int, ...]
    body_statements: int
    """Statements of the body (the innermost-owner rule applies here too)."""


@dataclass(frozen=True, slots=True)
class TouchedFile:
    """What one test ran in one file: every line, how many were statements, and the functions."""

    path: str
    lines: tuple[int, ...]
    statements: int
    functions: tuple[TouchedFunction, ...]
    module_lines: tuple[int, ...]
    """Lines outside every callable (module- or class-level code the test ran)."""


_TEST_ID_CTE = """
t AS (SELECT DISTINCT test_id FROM tests_dim WHERE nodeid = $nodeid AND test_id <> 0)
"""

_TOUCHED_SQL = f"""
WITH {_TEST_ID_CTE}, paths AS (
    SELECT file_id, any_value(path) AS path FROM files GROUP BY file_id
), st AS (
    SELECT file_id, any_value(statements) AS statements, any_value(excluded) AS excluded
    FROM file_static GROUP BY file_id
), hit AS (
    SELECT DISTINCT tl.file_id, tl.line FROM test_lines tl JOIN t USING (test_id)
), fn AS (
    SELECT DISTINCT func_key, file_id, qualname, kind, def_line, body_start, end_line FROM functions
    WHERE kind IN {CALLABLE_KINDS!r}
), owner AS (
    SELECT h.file_id, h.line, arg_max(fn.func_key, fn.body_start) AS func_key
    FROM hit h LEFT JOIN fn ON fn.file_id = h.file_id AND h.line BETWEEN fn.body_start AND fn.end_line
    GROUP BY h.file_id, h.line
), stl AS (
    SELECT file_id, unnest(statements) AS line FROM st WHERE file_id IN (SELECT file_id FROM hit)
), stl_owner AS (
    SELECT stl.file_id, stl.line, arg_max(fn.func_key, fn.body_start) AS func_key
    FROM stl JOIN fn ON fn.file_id = stl.file_id AND stl.line BETWEEN fn.body_start AND fn.end_line
    WHERE fn.func_key IN (SELECT func_key FROM owner)
    GROUP BY stl.file_id, stl.line
), body AS (
    SELECT func_key, count(*) AS body_statements FROM stl_owner GROUP BY func_key
)
SELECT p.path, o.line, o.func_key, fn.qualname, fn.kind, fn.def_line, fn.body_start, fn.end_line,
       coalesce(body.body_statements, 0) AS body_statements,
       coalesce(list_contains(st.statements, o.line)
                AND NOT list_contains(coalesce(st.excluded, []), o.line), false) AS is_statement
FROM owner o
JOIN paths p USING (file_id)
LEFT JOIN fn ON fn.func_key = o.func_key
LEFT JOIN body ON body.func_key = o.func_key
LEFT JOIN st ON st.file_id = o.file_id
ORDER BY p.path, fn.def_line NULLS FIRST, o.line
"""


def test_touched(store: FactsStore, repo_id: int, run_id: str, nodeid: str) -> list[TouchedFile]:
    """What ``nodeid`` ran in a run: per file (sorted by path) the lines, the statement count and
    the functions it called (in ``def_line`` order) with the body lines it ran in each.

    Lines come from ``test_lines`` (the test's own contexts, never ``<import>``), joined to
    ``files`` for paths, ``file_static`` for statements and ``functions`` for bodies. A test whose
    coverage another shard recorded first (``tests_dim.has_lines = false``) still resolves: the
    lines are stored under the same ``test_id``.
    """
    rows = _query(
        store,
        _TOUCHED_SQL,
        {"nodeid": nodeid},
        repo_id=repo_id,
        run_id=run_id,
        tables=("tests_dim", "files", "file_static", "test_lines", "functions"),
    ).to_pylist()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_path[row["path"]].append(row)
    out: list[TouchedFile] = []
    for path, items in by_path.items():
        functions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        module: list[int] = []
        for row in items:
            if row["func_key"] is None:
                module.append(int(row["line"]))
            else:
                functions[str(row["func_key"])].append(row)
        touched = [
            TouchedFunction(
                func_key=key,
                qualname=str(first["qualname"]),
                kind=str(first["kind"]),
                def_line=int(first["def_line"]),
                body_start=int(first["body_start"]),
                end_line=int(first["end_line"]),
                lines=tuple(sorted(int(r["line"]) for r in group)),
                body_statements=int(first["body_statements"]),
            )
            for key, group in functions.items()
            for first in (group[0],)
        ]
        touched.sort(key=lambda f: (f.def_line, f.qualname))
        out.append(
            TouchedFile(
                path=path,
                lines=tuple(sorted(int(r["line"]) for r in items)),
                statements=sum(1 for r in items if r["is_statement"]),
                functions=tuple(touched),
                module_lines=tuple(sorted(module)),
            )
        )
    return out


_TEST_SHAPES_SQL = f"""
WITH {_TEST_ID_CTE}, sh AS (
    SELECT shape_id, any_value(shape_key) AS shape_key FROM shapes GROUP BY shape_id
), fn AS (
    SELECT func_key, any_value(path) AS path, any_value(qualname) AS qualname, any_value(def_line) AS line
    FROM functions GROUP BY func_key
), fs AS (
    SELECT func_key, any_value(path) AS path, any_value(qualname) AS qualname,
           any_value(firstlineno) AS line
    FROM func_sampling GROUP BY func_key
)
SELECT cs.func_key, coalesce(fn.path, fs.path) AS path, coalesce(fn.qualname, fs.qualname) AS qualname,
       coalesce(fn.line, fs.line) AS line, cs.param, sh.shape_key, cs.caller_kind, cs.caller_key,
       cs.caller_path, cs.caller_qualname, cs.caller_line, cs.samples, cs.test_ids, cs.truncated,
       cs.example_test_id, cs.example_repr
FROM call_shapes cs
JOIN sh USING (shape_id)
LEFT JOIN fn USING (func_key)
LEFT JOIN fs USING (func_key)
WHERE list_has_any(cs.test_ids, (SELECT coalesce(list(test_id), []) FROM t))
   OR (cs.caller_kind = 'test' AND cs.caller_key = $nodeid)
ORDER BY path, line, cs.param, sh.shape_key, cs.caller_kind, cs.caller_key
"""


def test_shapes(store: FactsStore, repo_id: int, run_id: str, nodeid: str) -> list[ShapeObs]:
    """The run's ``call_shapes`` observations made while ``nodeid`` ran (its id is in ``test_ids``,
    or it is the direct caller), as :class:`~coverset.lang.base.ShapeObs`.

    ``func`` is the region triple ``(path, qualname, def_line)`` from ``functions`` (else the
    tracer's triple from ``func_sampling``), so it maps back to a ``func_key`` through the run's
    regions. ``samples`` counts every test of the row, not only this one: a row is aggregated per
    ``(function, parameter, shape, caller)``. Functions without a known triple are left out, as on
    the function page.
    """
    rows = _query(
        store,
        _TEST_SHAPES_SQL,
        {"nodeid": nodeid},
        repo_id=repo_id,
        run_id=run_id,
        tables=("tests_dim", "call_shapes", "shapes", "functions", "func_sampling"),
    ).to_pylist()
    if not rows:
        return []
    ids = store.dictionary(repo_id)
    out: list[ShapeObs] = []
    for row in rows:
        if row["path"] is None:
            continue
        caller: tuple[str, str, int] | str
        if row["caller_kind"] == "test":
            caller = row["caller_key"] or ""
        else:
            caller = (row["caller_path"] or "", row["caller_qualname"] or "?", row["caller_line"] or 0)
        example_test = row["example_test_id"]
        out.append(
            ShapeObs(
                func=(row["path"], row["qualname"], row["line"]),
                param=row["param"],
                shape_key=row["shape_key"],
                caller=caller,
                samples=int(row["samples"] or 0),
                tests=tuple(ids.nodeid(t) for t in row["test_ids"] or ()),
                truncated=bool(row["truncated"]),
                example=row["example_repr"],
                example_test=ids.nodeid(example_test) if example_test is not None else None,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class TestOutcomeRow:
    """A test's outcome in one run (None when the run's facts do not record the test)."""

    __test__ = False

    run_id: str
    created_at: datetime
    branch: str | None
    pr_number: int | None
    head_sha: str
    outcome: str | None
    duration_s: float | None


_TEST_HISTORY_SQL = """
WITH shard_at AS (
    SELECT run_id, shard_id, max(ingested_at) FILTER (WHERE status = 'ok') AS at
    FROM ingest_log GROUP BY run_id, shard_id
), t AS (
    SELECT td.run_id, arg_max(td.outcome, s.at) AS outcome, arg_max(td.duration_s, s.at) AS duration_s
    FROM tests_dim td JOIN shard_at s USING (run_id, shard_id)
    WHERE td.nodeid = $nodeid AND td.test_id <> 0
    GROUP BY td.run_id
)
SELECT r.run_id, r.created_at, r.branch, r.pr_number, r.head_sha, t.outcome, t.duration_s
FROM runs_dim r LEFT JOIN t USING (run_id)
ORDER BY r.created_at DESC, r.run_id DESC
"""


def test_history(store: FactsStore, repo_id: int, nodeid: str, last_n: int = 12) -> list[TestOutcomeRow]:
    """``nodeid``'s outcome in each of the repo's ``last_n`` most recent runs, newest first.

    The runs are picked from ``runs_dim`` first (every branch), then only their ``tests_dim`` and
    ``ingest_log`` are read; a test recorded by several shards of a run takes the most recently
    committed one (as :func:`run_summary`). These are the bundles' first-attempt outcomes: a retry
    that passed shows here as ``failed``; the control database's ``test_results`` has the final
    outcome and the flaky flag.
    """
    recent = _query(
        store,
        _RECENT_RUNS_SQL,
        {"last_n": last_n, "branch": None},
        repo_id=repo_id,
        tables=("runs_dim",),
    ).column("run_id")
    if not len(recent):
        return []
    rows = _query(
        store,
        _TEST_HISTORY_SQL,
        {"nodeid": nodeid},
        repo_id=repo_id,
        run_ids=recent.to_pylist(),
        tables=("runs_dim", "tests_dim", "ingest_log"),
    ).to_pylist()
    return [TestOutcomeRow(**row) for row in rows]


# Plain functions whose names start with ``test_``: keep pytest from collecting them where a test
# module imports them.
for _query_fn in (test_touched, test_shapes, test_history):
    _query_fn.__test__ = False  # type: ignore[attr-defined]


# -- inventory and ingest record (``coverset facts tables|explain``) -----------------------------


@dataclass(frozen=True, slots=True)
class TableInventory:
    """One facts table of a run: visible files, their bytes on disk, and rows (DuckDB count)."""

    table: str
    kind: Literal["run", "shard"]
    files: int
    rows: int
    bytes: int


def run_inventory(store: FactsStore, repo_id: int, run_id: str) -> dict[str, TableInventory]:
    """Every facts table of a run (in :data:`~coverset.facts.schema.TABLES` order) with its row
    count, from one ``count(*)`` per view, and the size of its visible (committed) files."""
    from coverset.facts.schema import RUN_TABLES, TABLES

    sql = " UNION ALL ".join(f"SELECT '{t}' AS name, count(*) AS n FROM {t}" for t in TABLES)
    counts = {
        row["name"]: int(row["n"])
        for row in _query(store, sql, repo_id=repo_id, run_id=run_id, tables=TABLES).to_pylist()
    }
    out: dict[str, TableInventory] = {}
    for table in TABLES:
        files = store.table_files(repo_id, run_id, table)
        size = 0
        for path in files:
            try:
                size += path.stat().st_size
            except FileNotFoundError:
                continue
        out[table] = TableInventory(
            table=table,
            kind="run" if table in RUN_TABLES else "shard",
            files=len(files),
            rows=counts.get(table, 0),
            bytes=size,
        )
    return out


@dataclass(frozen=True, slots=True)
class IngestRecord:
    """One ``ingest_log`` row: an ingest attempt of one shard and what the converter reported."""

    shard_id: int
    attempt: int
    status: str
    bundle_sha256: str | None
    ingested_at: datetime | None
    n_tests: int | None
    n_test_lines: int | None
    committed: bool
    """This is the shard's latest ``ok`` attempt (the one readers see)."""
    details: dict[str, object]

    @property
    def tracer_notes(self) -> list[str]:
        """Human-readable notes about the coverage tracer and the converter's drops."""
        notes = []
        if self.details.get("tracer_mismatch"):
            notes.append(f"coverage was collected with {self.details.get('tracer') or 'another tracer'}")
        if error := self.details.get("error"):
            notes.append(f"error: {error}")
        dropped = self.details.get("dropped")
        if isinstance(dropped, dict):
            hits = {k: v for k, v in dropped.items() if v}
            if hits:
                notes.append("dropped " + ", ".join(f"{k}={v}" for k, v in sorted(hits.items())))
        for key in ("unmatched_functions", "duplicate_tests"):
            if self.details.get(key):
                notes.append(f"{key.replace('_', ' ')}: {self.details[key]}")
        return notes

    @property
    def bundle_bytes(self) -> int | None:
        """The bundle's size as the converter measured it."""
        value = self.details.get("bundle_bytes")
        return value if isinstance(value, int) else None


_INGEST_SQL = """
SELECT shard_id, attempt, status, bundle_sha256, ingested_at, n_tests, n_test_lines, convert_json
FROM ingest_log ORDER BY shard_id, attempt
"""


def ingest_record(store: FactsStore, repo_id: int, run_id: str) -> list[IngestRecord]:
    """Every ingest attempt of the run's shards (``ingest_log``), by shard then attempt, with the
    converter's details parsed from ``convert_json`` (tracer, dropped records, bundle bytes)."""
    rows = _query(store, _INGEST_SQL, repo_id=repo_id, run_id=run_id, tables=("ingest_log",)).to_pylist()
    committed: dict[int, int] = {}
    for row in rows:
        if row["status"] == "ok":
            committed[row["shard_id"]] = max(row["attempt"], committed.get(row["shard_id"], 0))
    out = []
    for row in rows:
        try:
            details = json.loads(row["convert_json"]) if row["convert_json"] else {}
        except ValueError:
            details = {"unparsed": row["convert_json"]}
        out.append(
            IngestRecord(
                shard_id=row["shard_id"],
                attempt=row["attempt"],
                status=row["status"],
                bundle_sha256=row["bundle_sha256"],
                ingested_at=row["ingested_at"],
                n_tests=row["n_tests"],
                n_test_lines=row["n_test_lines"],
                committed=committed.get(row["shard_id"]) == row["attempt"],
                details=details if isinstance(details, dict) else {"value": details},
            )
        )
    return out


# -- history and overview (the repo page's overview, the function page's history) ---------------


@dataclass(frozen=True, slots=True)
class BaselinePoint:
    """Totals of one finalized default-branch run (one point of the repo page's trend).

    Line totals come from ``file_cov``; ``functions_*`` and ``classes_*`` count callables
    (``functions.kind`` function or method) joined to ``func_input_cov``; ``tests_total`` counts
    the distinct node ids of ``tests_dim`` without ``<import>``.
    """

    run_id: str
    created_at: datetime
    head_sha: str
    statements: int
    lines_run: int
    branches_taken: int
    branches_possible: int
    functions_called: int
    functions_total: int
    classes_expected: int
    classes_observed: int
    tests_total: int

    @property
    def line_pct(self) -> float | None:
        """Statements run ÷ statements, in percent."""
        return 100.0 * self.lines_run / self.statements if self.statements else None

    @property
    def branch_pct(self) -> float | None:
        """Branch arcs taken ÷ possible, in percent."""
        return 100.0 * self.branches_taken / self.branches_possible if self.branches_possible else None

    @property
    def input_pct(self) -> float | None:
        """Input classes observed ÷ expected, in percent (None when nothing is expected)."""
        return 100.0 * self.classes_observed / self.classes_expected if self.classes_expected else None


_BASELINE_RUNS_SQL = """
SELECT run_id FROM runs_dim
WHERE branch = $branch AND pr_number IS NULL AND finalized_at IS NOT NULL
ORDER BY created_at DESC, run_id DESC
LIMIT $last_n
"""

_CALLABLES = f"kind IN {CALLABLE_KINDS!r}"

_BASELINE_TREND_SQL = f"""
WITH fc AS (
    SELECT run_id, sum(statements)::BIGINT AS statements, sum(covered)::BIGINT AS lines_run,
           sum(branches_covered)::BIGINT AS branches_taken, sum(branches)::BIGINT AS branches_possible
    FROM file_cov GROUP BY run_id
), fn AS (
    SELECT DISTINCT run_id, func_key FROM functions WHERE {_CALLABLES}
), fnc AS (
    SELECT fn.run_id, count(*) AS functions_total,
           count(*) FILTER (WHERE fic.tests_called > 0) AS functions_called,
           coalesce(sum(fic.blocks_expected), 0)::BIGINT AS classes_expected,
           coalesce(sum(fic.blocks_observed), 0)::BIGINT AS classes_observed
    FROM fn LEFT JOIN func_input_cov fic USING (run_id, func_key)
    GROUP BY fn.run_id
), td AS (
    SELECT run_id, count(DISTINCT test_id) FILTER (WHERE test_id <> 0) AS tests_total
    FROM tests_dim GROUP BY run_id
)
SELECT r.run_id, r.created_at, r.head_sha,
       coalesce(fc.statements, 0) AS statements, coalesce(fc.lines_run, 0) AS lines_run,
       coalesce(fc.branches_taken, 0) AS branches_taken,
       coalesce(fc.branches_possible, 0) AS branches_possible,
       coalesce(fnc.functions_called, 0) AS functions_called,
       coalesce(fnc.functions_total, 0) AS functions_total,
       coalesce(fnc.classes_expected, 0) AS classes_expected,
       coalesce(fnc.classes_observed, 0) AS classes_observed,
       coalesce(td.tests_total, 0) AS tests_total
FROM runs_dim r
LEFT JOIN fc USING (run_id) LEFT JOIN fnc USING (run_id) LEFT JOIN td USING (run_id)
ORDER BY r.created_at, r.run_id
"""


def _baseline_runs(store: FactsStore, repo_id: int, branch: str, last_n: int) -> list[str]:
    """The ``last_n`` newest finalized runs of ``branch`` that belong to no PR (newest first)."""
    rows = _query(
        store,
        _BASELINE_RUNS_SQL,
        {"branch": branch, "last_n": max(0, last_n)},
        repo_id=repo_id,
        tables=("runs_dim",),
    )
    return [str(r) for r in rows.column("run_id").to_pylist()]


def baseline_trend(store: FactsStore, repo_id: int, branch: str, last_n: int = 12) -> list[BaselinePoint]:
    """The totals of the ``last_n`` newest baselines of ``branch``, oldest first.

    A baseline is a run whose ``runs_dim.branch`` is ``branch``, with no PR number, that
    ``finalize_run`` finished (``finalized_at`` set). The runs are picked from ``runs_dim`` first,
    then only their ``file_cov``, ``functions``, ``func_input_cov`` and ``tests_dim`` are read.
    """
    runs = _baseline_runs(store, repo_id, branch, last_n)
    if not runs:
        return []
    rows = _query(
        store,
        _BASELINE_TREND_SQL,
        repo_id=repo_id,
        run_ids=runs,
        tables=("runs_dim", "file_cov", "functions", "func_input_cov", "tests_dim"),
    ).to_pylist()
    return [BaselinePoint(**{**row, "created_at": _utc(row["created_at"])}) for row in rows]


_BODY_LINES_CTE = """
fnf AS (
    SELECT DISTINCT run_id, file_id FROM fn
), st AS (
    SELECT s.run_id, s.file_id, any_value(s.statements) AS statements, any_value(s.excluded) AS excluded
    FROM file_static s JOIN fnf USING (run_id, file_id) GROUP BY s.run_id, s.file_id
), stl AS (
    SELECT run_id, file_id, unnest(statements) AS line, coalesce(excluded, []) AS excluded FROM st
), hit AS (
    SELECT tl.run_id, tl.file_id, tl.line, bool_or(tl.test_id <> 0) AS by_test
    FROM test_lines tl JOIN fnf USING (run_id, file_id)
    GROUP BY tl.run_id, tl.file_id, tl.line
), body_lines AS (
    -- the function page's body rule: statements in [body_start, end_line] minus excluded; a line
    -- ran when a test ran it, or <import> did and it is not the def line (a one-line def)
    SELECT fn.run_id, fn.func_key, stl.line,
           coalesce(hit.line IS NOT NULL AND (hit.by_test OR stl.line <> fn.def_line), false) AS covered
    FROM fn JOIN stl ON stl.run_id = fn.run_id AND stl.file_id = fn.file_id
                    AND stl.line BETWEEN fn.body_start AND fn.end_line
    LEFT JOIN hit ON hit.run_id = stl.run_id AND hit.file_id = stl.file_id AND hit.line = stl.line
    WHERE NOT list_contains(stl.excluded, stl.line)
), body AS (
    SELECT run_id, func_key, count(*) AS statements, count(*) FILTER (WHERE covered) AS covered,
           list(line ORDER BY line) AS lines,
           coalesce(list(line ORDER BY line) FILTER (WHERE covered), []) AS covered_lines
    FROM body_lines GROUP BY run_id, func_key
)
"""
"""CTEs that turn ``fn(run_id, func_key, file_id, def_line, body_start, end_line)`` into
``body(run_id, func_key, statements, covered, lines, covered_lines)`` by the function page's rule
(``RunFacts.body_coverage``)."""


def _missing_classes(value: str | None) -> tuple[str, ...]:
    """The class names of a ``func_input_cov.missing_json`` value (``{param: [names]}``)."""
    if not value:
        return ()
    try:
        data = json.loads(value)
    except ValueError:
        return ()
    if not isinstance(data, dict):
        return ()
    names = {str(n) for names in data.values() if isinstance(names, list) for n in names}
    return tuple(sorted(names))


def _utc(moment: datetime) -> datetime:
    """``moment`` in UTC (DuckDB hands ``TIMESTAMPTZ`` back in the session's local zone)."""
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _pct(part: int | None, whole: int | None) -> float | None:
    return round(100.0 * (part or 0) / whole, 1) if whole else None


@dataclass(frozen=True, slots=True)
class FunctionPoint:
    """One function in one baseline: input classes, body lines and callers.

    ``present`` is false when the run's ``functions`` has no region with the key (the function did
    not exist yet, or moved); ``expected`` and ``observed`` are None when the run computed no
    input classes for it (never called, or no catalogue).
    """

    run_id: str
    created_at: datetime
    head_sha: str
    present: bool
    expected: int | None
    observed: int | None
    line_pct: float | None
    tests_called: int
    missing: tuple[str, ...]
    tests_truncated: int = 0
    """Tests that called the function but hit the tracer's sample cap (a missing class may then
    only have gone unsampled)."""

    @property
    def input_pct(self) -> float | None:
        """Observed ÷ expected input classes in percent (None when nothing is expected)."""
        return 100.0 * (self.observed or 0) / self.expected if self.expected else None


_FUNCTION_HISTORY_SQL = f"""
WITH fn AS (
    SELECT run_id, func_key, any_value(file_id) AS file_id, any_value(def_line) AS def_line,
           any_value(body_start) AS body_start, any_value(end_line) AS end_line
    FROM functions WHERE func_key = $func_key AND {_CALLABLES} GROUP BY run_id, func_key
), {_BODY_LINES_CTE}
SELECT r.run_id, r.created_at, r.head_sha, fn.func_key IS NOT NULL AS present,
       fic.blocks_expected AS expected, fic.blocks_observed AS observed,
       coalesce(fic.tests_called, 0) AS tests_called, coalesce(fic.tests_truncated, 0) AS tests_truncated,
       fic.missing_json, body.statements, body.covered
FROM runs_dim r
LEFT JOIN fn USING (run_id)
LEFT JOIN func_input_cov fic ON fic.run_id = r.run_id AND fic.func_key = $func_key
LEFT JOIN body ON body.run_id = r.run_id AND body.func_key = $func_key
ORDER BY r.created_at, r.run_id
"""


def function_history(
    store: FactsStore, repo_id: int, func_key: str, branch: str, last_n: int = 12
) -> list[FunctionPoint]:
    """``func_key`` in each of the ``last_n`` newest baselines of ``branch``, oldest first.

    Baselines are picked as in :func:`baseline_trend`; every baseline gets a point, with
    ``present`` false where the run has no such function. ``line_pct`` follows the function
    page's body rule (statements in ``[body_start, end_line]``; a one-line def needs a real test),
    and ``missing`` lists the classes ``func_input_cov`` stored as never observed that run.
    """
    runs = _baseline_runs(store, repo_id, branch, last_n)
    if not runs:
        return []
    rows = _query(
        store,
        _FUNCTION_HISTORY_SQL,
        {"func_key": func_key},
        repo_id=repo_id,
        run_ids=runs,
        tables=("runs_dim", "functions", "func_input_cov", "file_static", "test_lines"),
    ).to_pylist()
    return [
        FunctionPoint(
            run_id=row["run_id"],
            created_at=_utc(row["created_at"]),
            head_sha=row["head_sha"],
            present=bool(row["present"]),
            expected=row["expected"],
            observed=row["observed"],
            line_pct=_pct(row["covered"], row["statements"]),
            tests_called=int(row["tests_called"]),
            missing=_missing_classes(row["missing_json"]),
            tests_truncated=int(row["tests_truncated"]),
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class ClassChange:
    """How one function's input classes and body lines changed from a prior run to a run.

    Only the never-observed classes are stored (``func_input_cov.missing_json``), so a class is
    ``lost`` when it is missing now and was not missing then, and ``gained`` the other way round.
    That reads exactly while the function expects the same classes in both runs; when the expected
    count changed (``expected_changed``), a class can also have become expected or stopped being
    expected. A function no test passes anything to any more loses every class it had, unnamed
    (``lost_count`` counts them; ``gained_count`` likewise for a function newly called).
    ``newly_uncovered`` holds the body's statements (this run's line numbers) that ran in the prior
    run and not in this one, matched by offset from ``body_start``; it is only computed when the
    body hash is unchanged (``body_changed`` false). ``sampled`` is true when either run hit the
    tracer's sample cap for the function (``tests_truncated`` above 0): a class "lost" then may
    only have gone unsampled.
    """

    func_key: str
    prior_func_key: str
    path: str
    qualname: str
    def_line: int
    lost: tuple[str, ...]
    gained: tuple[str, ...]
    lost_count: int
    gained_count: int
    expected: int | None
    prior_expected: int | None
    observed: int | None
    prior_observed: int | None
    newly_uncovered: tuple[int, ...]
    body_changed: bool
    lines: tuple[int, int]
    """(covered, statements) of the body in this run."""
    prior_lines: tuple[int, int]
    sampled: bool = False

    @property
    def expected_changed(self) -> bool:
        """Whether the function expects a different number of classes than in the prior run."""
        return (
            self.expected is not None
            and self.prior_expected is not None
            and self.expected != self.prior_expected
        )


_CLASS_CHANGES_SQL = f"""
WITH fn AS (
    SELECT run_id, func_key, any_value(file_id) AS file_id, any_value(path) AS path,
           any_value(qualname) AS qualname, any_value(ordinal) AS ordinal, any_value(def_line) AS def_line,
           any_value(body_start) AS body_start, any_value(end_line) AS end_line,
           any_value(body_hash) AS body_hash
    FROM functions WHERE {_CALLABLES} GROUP BY run_id, func_key
), {_BODY_LINES_CTE}, side AS (
    SELECT fn.*, fic.blocks_expected, fic.blocks_observed, fic.missing_json,
           coalesce(fic.tests_truncated, 0) AS tests_truncated,
           coalesce(body.statements, 0) AS statements, coalesce(body.covered, 0) AS covered,
           coalesce(body.lines, []) AS lines, coalesce(body.covered_lines, []) AS covered_lines
    FROM fn
    LEFT JOIN func_input_cov fic USING (run_id, func_key)
    LEFT JOIN body USING (run_id, func_key)
), cur AS (
    SELECT * FROM side WHERE run_id = $run_id
), pri AS (
    SELECT * FROM side WHERE run_id = $prior_run_id
), by_key AS (
    SELECT c.func_key AS cur_key, p.func_key AS prior_key FROM cur c JOIN pri p USING (func_key)
), by_name AS (
    SELECT c.func_key AS cur_key, arg_min(p.func_key, p.ordinal) AS prior_key
    FROM cur c JOIN pri p ON p.path = c.path AND p.qualname = c.qualname
    WHERE c.func_key NOT IN (SELECT cur_key FROM by_key)
      AND p.func_key NOT IN (SELECT prior_key FROM by_key)
    GROUP BY c.func_key
), pairs AS (
    SELECT * FROM by_key UNION ALL SELECT * FROM by_name
)
SELECT c.func_key, p.func_key AS prior_key, c.path, c.qualname, c.def_line,
       c.body_start, p.body_start AS prior_body_start, c.body_hash, p.body_hash AS prior_body_hash,
       c.blocks_expected AS expected, p.blocks_expected AS prior_expected,
       c.blocks_observed AS observed, p.blocks_observed AS prior_observed,
       c.missing_json, p.missing_json AS prior_missing_json,
       c.tests_truncated > 0 OR p.tests_truncated > 0 AS sampled,
       c.statements, c.covered, c.lines, c.covered_lines,
       p.statements AS prior_statements, p.covered AS prior_covered, p.covered_lines AS prior_covered_lines
FROM pairs JOIN cur c ON c.func_key = pairs.cur_key JOIN pri p ON p.func_key = pairs.prior_key
ORDER BY c.path, c.def_line, c.qualname
"""


def class_changes(store: FactsStore, repo_id: int, run_id: str, prior_run_id: str) -> list[ClassChange]:
    """Per function present in both runs, the input classes lost and gained and the body lines
    newly uncovered from ``prior_run_id`` to ``run_id`` (only functions where something changed),
    by path and def line.

    Functions are matched by ``func_key``, else by ``(path, qualname)`` (the lowest prior ordinal)
    among the functions the key left unmatched; new and deleted functions are not listed. See
    :class:`ClassChange` for what "lost" can and cannot tell from the stored facts.
    """
    if run_id == prior_run_id:
        return []
    rows = _query(
        store,
        _CLASS_CHANGES_SQL,
        {"run_id": run_id, "prior_run_id": prior_run_id},
        repo_id=repo_id,
        run_ids=[run_id, prior_run_id],
        tables=("functions", "func_input_cov", "file_static", "test_lines"),
    ).to_pylist()
    out: list[ClassChange] = []
    for row in rows:
        missing, prior_missing = (
            set(_missing_classes(row["missing_json"])),
            set(_missing_classes(row["prior_missing_json"])),
        )
        expected, prior_expected = row["expected"], row["prior_expected"]
        observed, prior_observed = row["observed"], row["prior_observed"]
        lost: tuple[str, ...] = ()
        gained: tuple[str, ...] = ()
        lost_count = gained_count = 0
        if expected is not None and prior_expected is not None:
            lost, gained = tuple(sorted(missing - prior_missing)), tuple(sorted(prior_missing - missing))
            lost_count, gained_count = len(lost), len(gained)
        elif prior_expected is not None:
            lost_count = int(prior_observed or 0)
        elif expected is not None:
            gained_count = int(observed or 0)
        body_changed = row["body_hash"] != row["prior_body_hash"]
        newly: tuple[int, ...] = ()
        if not body_changed:
            before = {line - row["prior_body_start"] for line in row["prior_covered_lines"] or ()}
            now = set(row["covered_lines"] or ())
            newly = tuple(
                line for line in row["lines"] or () if line - row["body_start"] in before and line not in now
            )
        if not (lost_count or gained_count or newly):
            continue
        out.append(
            ClassChange(
                func_key=row["func_key"],
                prior_func_key=row["prior_key"],
                path=row["path"],
                qualname=row["qualname"],
                def_line=int(row["def_line"]),
                lost=lost,
                gained=gained,
                lost_count=lost_count,
                gained_count=gained_count,
                expected=expected,
                prior_expected=prior_expected,
                observed=observed,
                prior_observed=prior_observed,
                newly_uncovered=newly,
                body_changed=body_changed,
                lines=(int(row["covered"]), int(row["statements"])),
                prior_lines=(int(row["prior_covered"] or 0), int(row["prior_statements"] or 0)),
                sampled=bool(row["sampled"]),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class GapRow:
    """A called function with input classes no test passed in (the overview's "largest gaps")."""

    func_key: str
    path: str
    qualname: str
    def_line: int
    expected: int
    observed: int
    tests_called: int
    tests_truncated: int
    line_pct: float | None
    classes: tuple[str, ...]
    """The never-observed class names."""

    @property
    def missing(self) -> int:
        """Expected classes never observed."""
        return self.expected - self.observed


_TOP_GAPS_SQL = f"""
WITH fn AS (
    SELECT run_id, func_key, any_value(file_id) AS file_id, any_value(path) AS path,
           any_value(qualname) AS qualname, any_value(def_line) AS def_line,
           any_value(body_start) AS body_start, any_value(end_line) AS end_line
    FROM functions WHERE {_CALLABLES} GROUP BY run_id, func_key
), {_BODY_LINES_CTE}
SELECT fn.func_key, fn.path, fn.qualname, fn.def_line,
       fic.blocks_expected AS expected, fic.blocks_observed AS observed,
       fic.tests_called, fic.tests_truncated, fic.missing_json, body.statements, body.covered
FROM fn
JOIN func_input_cov fic USING (run_id, func_key)
LEFT JOIN body USING (run_id, func_key)
WHERE fic.tests_called > 0 AND fic.blocks_expected > coalesce(fic.blocks_observed, 0)
ORDER BY fic.blocks_expected - coalesce(fic.blocks_observed, 0) DESC, body.statements DESC NULLS LAST,
         fn.path, fn.qualname
LIMIT $limit
"""


def top_gaps(store: FactsStore, repo_id: int, run_id: str, limit: int = 10) -> list[GapRow]:
    """The ``limit`` called functions (some test ran their body) with the most expected input
    classes never observed in the run, most first (then larger bodies, then path and name), with
    the body's line percentage (the function page's rule) and the missing class names."""
    rows = _query(
        store,
        _TOP_GAPS_SQL,
        {"limit": max(0, limit)},
        repo_id=repo_id,
        run_id=run_id,
        tables=("functions", "func_input_cov", "file_static", "test_lines"),
    ).to_pylist()
    return [
        GapRow(
            func_key=row["func_key"],
            path=row["path"],
            qualname=row["qualname"],
            def_line=int(row["def_line"]),
            expected=int(row["expected"]),
            observed=int(row["observed"] or 0),
            tests_called=int(row["tests_called"]),
            tests_truncated=int(row["tests_truncated"] or 0),
            line_pct=_pct(row["covered"], row["statements"]),
            classes=_missing_classes(row["missing_json"]),
        )
        for row in rows
    ]
