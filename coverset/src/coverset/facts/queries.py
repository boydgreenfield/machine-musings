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
from typing import Literal

import duckdb
import numpy as np
import pyarrow as pa

from coverset.facts.dictionary import RepoDictionary
from coverset.facts.schema import CALLABLE_KINDS
from coverset.facts.store import FactsStore
from coverset.lang.base import IMPORT_CONTEXT, FunctionRegion

__all__ = [
    "MODULE_LEAF",
    "FuncInputCov",
    "ParquetPriorRun",
    "PatchCoverage",
    "RunSummary",
    "ShapeRow",
    "TreeNode",
    "function_input_cov",
    "line_tests",
    "patch_coverage",
    "run_summary",
    "shapes_for_function",
    "tests_covering",
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
    SELECT run_id, any_value(created_at) AS created_at FROM runs_dim GROUP BY run_id
), cs AS (
    SELECT cs.*, r.created_at FROM call_shapes cs JOIN recent r USING (run_id)
    WHERE cs.func_key = $func_key
)
SELECT param, shape_id, sum(samples)::BIGINT AS samples, count(DISTINCT run_id) AS runs,
       list_sort(list_distinct(flatten(list(test_ids)))) AS test_ids,
       arg_max(example_repr, created_at) FILTER (WHERE example_repr IS NOT NULL) AS example,
       arg_max(example_test_id, created_at) FILTER (WHERE example_repr IS NOT NULL) AS example_test_id
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

    Recent runs are picked from ``runs_dim`` first (only runs of ``branch`` when given), so only
    those runs' ``call_shapes`` files are read. ``tests`` holds at most ``max_tests`` nodeids
    (sorted); ``n_tests`` is the full count. The example is the newest non-null redacted example.
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
        example_test = row["example_test_id"]
        result.append(
            ShapeRow(
                param=row["param"],
                shape_key=ids.shape_key(row["shape_id"]),
                samples=row["samples"],
                runs=row["runs"],
                n_tests=len(nodeids),
                tests=tuple(nodeids[:max_tests]),
                example=row["example"],
                example_test=ids.nodeid(example_test) if example_test is not None else None,
            )
        )
    return result


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
