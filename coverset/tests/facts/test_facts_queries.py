"""Queries over a small store: two runs of the minimal bundle (run-1 finalized, run-2 not)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from facts_helpers import (
    MODULE_PATH,
    T_ADD,
    T_BOX,
    T_FAIL,
    T_NEG,
    T_POS,
    int_catalogue,
    make_ctx,
    split_bundle,
)

from coverset.facts import queries
from coverset.facts.convert import read_python_bundle
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.queries import (
    ParquetPriorRun,
    function_input_cov,
    line_tests,
    patch_coverage,
    run_summary,
    shapes_for_function,
    treemap,
)
from coverset.facts.store import FactsStore, SqlNotAllowed
from coverset.lang.base import IMPORT_CONTEXT, FunctionRegion, PriorRun, func_key

INIT_PATH = "src/factspkg/__init__.py"
CLASSIFY = func_key(MODULE_PATH, "classify", 0)
ADD = func_key(MODULE_PATH, "add", 0)


@pytest.fixture
def facts(store: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]) -> FactsStore:
    bundle = read_python_bundle(minimal_bundle)
    ctx = make_ctx("run-1", day=1)
    ingest_shard(store, ctx, bundle, 0, functions)
    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue)
    later = split_bundle(bundle, {T_NEG, T_BOX}, with_import=True)
    ingest_shard(store, make_ctx("run-2", day=2, head_sha="c" * 40), later, 0, functions)
    return store


def test_patch_coverage(facts: FactsStore) -> None:
    changed = {MODULE_PATH: {5, 18, 19, 21, 22}, "tests/test_calc.py": {1}, "src/new.py": {1, 2}}
    result = patch_coverage(facts, 1, "run-1", changed)
    assert (result.covered, result.total) == (1, 2)  # 19 is blank, 21-22 are excluded
    assert result.per_file == {MODULE_PATH: (1, 2)}
    assert result.missing == {MODULE_PATH: [18]} and result.unknown == {}
    assert result.unmeasured == ("src/new.py", "tests/test_calc.py")
    assert result.percent == 50.0
    assert (result.complete, result.expected_shards, result.ingested_shards) == (True, 1, 1)


def test_patch_gaps_are_unknown_while_shards_are_missing(facts: FactsStore) -> None:
    pending = patch_coverage(facts, 1, "run-2", {MODULE_PATH: {5, 11, 18}})
    assert not pending.complete and pending.expected_shards is None  # never finalized
    assert pending.missing == {} and pending.unknown == {MODULE_PATH: [5, 18]}
    assert (pending.covered, pending.total) == (1, 3)
    finalize_run(facts, make_ctx("run-2", day=2, head_sha="c" * 40), expected_shards=2)
    partial = patch_coverage(facts, 1, "run-2", {MODULE_PATH: {18}})
    assert (partial.expected_shards, partial.ingested_shards) == (2, 1)
    assert partial.unknown == {MODULE_PATH: [18]} and partial.missing == {}
    assert patch_coverage(facts, 1, "run-404", {MODULE_PATH: {5}}).unmeasured == (MODULE_PATH,)


def test_patch_coverage_counts_import_time_lines(facts: FactsStore) -> None:
    result = patch_coverage(facts, 1, "run-1", {MODULE_PATH: {4, 17, 25}})
    assert (result.covered, result.total) == (3, 3)
    assert patch_coverage(facts, 1, "run-1", {}).percent is None


def test_tests_covering_and_line_tests(facts: FactsStore) -> None:
    assert queries.tests_covering(facts, 1, "run-1", MODULE_PATH, 10, 14) == sorted(
        [T_NEG, T_POS, T_BOX, T_FAIL]
    )
    assert queries.tests_covering(facts, 1, "run-1", MODULE_PATH, 4, 5) == [T_ADD]
    assert queries.tests_covering(facts, 1, "run-1", MODULE_PATH, 4, 5, exclude_import=False) == [
        IMPORT_CONTEXT,
        T_ADD,
    ]
    assert queries.tests_covering(facts, 1, "run-2", MODULE_PATH, 10, 14) == sorted([T_NEG, T_BOX])
    assert line_tests(facts, 1, "run-1", MODULE_PATH, 17) == [IMPORT_CONTEXT]
    assert line_tests(facts, 1, "run-1", MODULE_PATH, 18) == []
    assert line_tests(facts, 1, "run-1", "src/missing.py", 1) == []


def test_treemap(facts: FactsStore) -> None:
    nodes = {node.path: node for node in treemap(facts, 1, "run-1")}
    assert [n.path for n in treemap(facts, 1, "run-1")][:3] == ["src", "src/factspkg", INIT_PATH]
    assert nodes["src"].kind == "dir" and (nodes["src"].statements, nodes["src"].covered) == (18, 17)
    calc = nodes[MODULE_PATH]
    assert calc.kind == "file" and (calc.statements, calc.covered) == (18, 17)
    assert calc.input_cov == pytest.approx(5 / 9)
    assert (nodes[INIT_PATH].statements, nodes[INIT_PATH].input_cov) == (0, None)
    leaves = {path.split("::")[1]: node for path, node in nodes.items() if node.kind == "function"}
    assert (leaves["<module>"].statements, leaves["<module>"].func_key) == (6, None)  # def/class lines
    assert (leaves["classify"].statements, leaves["classify"].covered) == (5, 5)
    assert leaves["classify"].input_cov == 1.0 and leaves["classify"].def_line == 8
    assert (leaves["never_called"].statements, leaves["never_called"].covered) == (1, 0)
    assert leaves["add"].input_cov == pytest.approx(1 / 3)
    assert leaves["Box.total"].input_cov is None  # no input classes expected
    assert sum(leaf.statements for leaf in leaves.values()) == calc.statements


def test_treemap_leaves_match_coverage_py_function_regions(facts: FactsStore, minimal_bundle: Path) -> None:
    report = json.loads((minimal_bundle / "coverage.json").read_text())["files"][MODULE_PATH]["functions"]
    leaves = {
        node.path.split("::")[1]: node
        for node in treemap(facts, 1, "run-1")
        if node.path.startswith(f"{MODULE_PATH}::")
    }
    for qualname, region in report.items():
        leaf = leaves.get(qualname or "<module>")
        summary = region["summary"]
        if leaf is None:
            assert summary["num_statements"] == 0  # e.g. a fully excluded function
            continue
        assert (leaf.statements, leaf.covered) == (summary["num_statements"], summary["covered_lines"]), (
            qualname
        )


def test_shapes_for_function_over_recent_runs(facts: FactsStore) -> None:
    rows = {(r.param, r.shape_key): r for r in shapes_for_function(facts, 1, CLASSIFY, last_n_runs=5)}
    pos = rows[("n", "int|pos")]
    assert (pos.samples, pos.runs, pos.n_tests, pos.tests) == (5, 2, 2, tuple(sorted([T_BOX, T_POS])))
    assert rows[("n", "int|neg")].samples == 2 and rows[("n", "int|neg")].example == "-3"
    assert rows[("n", "int|zero")].runs == 1 and rows[("n", "int|zero")].example_test == T_FAIL
    latest = {(r.param, r.shape_key) for r in shapes_for_function(facts, 1, CLASSIFY, last_n_runs=1)}
    assert ("n", "int|zero") not in latest and ("n", "int|pos") in latest
    assert shapes_for_function(facts, 1, "0" * 16) == []
    capped = shapes_for_function(facts, 1, CLASSIFY, max_tests=1)
    assert all(len(r.tests) <= 1 for r in capped)
    assert shapes_for_function(facts, 1, CLASSIFY, branch="feature") == []
    assert {r.runs for r in shapes_for_function(facts, 1, CLASSIFY, branch="main")} == {1, 2}


def test_function_input_cov(facts: FactsStore) -> None:
    add = function_input_cov(facts, 1, "run-1", ADD)
    assert add is not None
    assert (add.blocks_expected, add.blocks_observed, add.tests_called) == (6, 2, 1)
    assert add.missing == {"a": ["neg", "zero"], "b": ["neg", "zero"]}
    assert add.ratio == pytest.approx(1 / 3)
    classify = function_input_cov(facts, 1, "run-1", CLASSIFY)
    assert classify is not None and classify.tests_truncated == 1
    assert function_input_cov(facts, 1, "run-1", "0" * 16) is None
    assert function_input_cov(facts, 1, "run-2", ADD) is None  # not finalized


def test_run_summary(facts: FactsStore) -> None:
    done = run_summary(facts, 1, "run-1")
    assert done is not None and done.finalized and done.coverage_complete
    assert (done.tests, done.outcomes) == (5, {"passed": 4, "failed": 1})
    assert (done.files, done.statements, done.covered, done.branches) == (2, 18, 17, 6)
    assert done.percent == pytest.approx(100 * 23 / 24)
    pending = run_summary(facts, 1, "run-2")
    assert pending is not None and not pending.finalized and pending.statements is None
    assert pending.head_sha == "c" * 40 and pending.ingested_shards == 1
    assert run_summary(facts, 1, "run-404") is None


def test_parquet_prior_run(facts: FactsStore) -> None:
    prior = ParquetPriorRun(facts, 1, "run-1")
    assert isinstance(prior, PriorRun)
    assert {f.qualname for f in prior.functions()} == {
        "add",
        "classify",
        "never_called",
        "excluded",
        "Box.__init__",
        "Box.total",
    }
    assert prior.tests_touching(MODULE_PATH, 10, 14) == {T_NEG, T_POS, T_BOX, T_FAIL}
    assert prior.tests_touching(MODULE_PATH, 17, 17) == set()  # import time only
    assert prior.tests_touching("src/unknown.py", 1, 9) == set()
    assert prior.test_lines(T_ADD) == {MODULE_PATH: {5}}
    assert prior.test_lines("tests/nope.py::t") == {}
    assert prior.module_level_lines(MODULE_PATH) == {25}
    durations = prior.durations()
    assert durations[T_ADD] == pytest.approx(0.01) and IMPORT_CONTEXT not in durations
    other = ParquetPriorRun(facts, 1, "run-2")
    assert other.tests_touching(MODULE_PATH, 10, 14) == {T_NEG, T_BOX}


# -- the store itself ----------------------------------------------------------------------------


def test_views_tolerate_missing_tables(store: FactsStore) -> None:
    con = store.connect()
    try:
        assert con.execute("SELECT count(*) FROM test_lines").fetchone() == (0,)
        assert "repo_id" in [c[0] for c in con.execute("DESCRIBE file_cov").fetchall()]
    finally:
        con.close()


def test_views_add_partition_columns(facts: FactsStore) -> None:
    con = facts.connect()
    try:
        runs = con.execute("SELECT DISTINCT repo_id, run_id FROM test_lines ORDER BY run_id").fetchall()
        assert runs == [(1, "run-1"), (1, "run-2")]
    finally:
        con.close()


def test_query_accepts_only_a_single_select(facts: FactsStore) -> None:
    table = facts.query(
        "SELECT run_id, count(*) AS n FROM tests_dim GROUP BY run_id ORDER BY run_id", repo_id=1
    )
    assert table.to_pylist() == [{"run_id": "run-1", "n": 6}, {"run_id": "run-2", "n": 3}]
    target = facts.run_dir(1, "run-1") / "test_lines.shard=9.attempt=1.parquet"
    for sql in (
        f"COPY (SELECT 1 AS x) TO '{target}'",
        "SELECT 1; SELECT 2",
        "CREATE TABLE t AS SELECT 1",
        "SET threads = 1",
    ):
        with pytest.raises(SqlNotAllowed):
            facts.query(sql)
    assert not target.exists()


def test_connect_can_scope_to_a_list_of_runs(facts: FactsStore) -> None:
    con = facts.connect(repo_id=1, run_ids=["run-2"], tables=("tests_dim",))
    try:
        assert con.execute("SELECT DISTINCT run_id FROM tests_dim").fetchall() == [("run-2",)]
    finally:
        con.close()
    with pytest.raises(ValueError, match="repo_id"):
        facts.connect(run_ids=["run-2"])


def test_connection_cannot_reach_other_files(facts: FactsStore) -> None:
    con = facts.connect()
    try:
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM read_csv('/etc/hosts')")
        with pytest.raises(duckdb.Error):
            con.execute("SET enable_external_access = true")
    finally:
        con.close()


def test_store_layout_and_validation(facts: FactsStore, tmp_path: Path) -> None:
    assert facts.list_runs(1) == ["run-1", "run-2"]
    assert facts.list_repos() == [1]
    assert facts.list_runs(99) == []
    with pytest.raises(ValueError, match="run id"):
        facts.run_dir(1, "../escape")
    with pytest.raises(ValueError, match="shard"):
        facts.write_table(1, "run-1", "test_lines", pa.table({}))
    with pytest.raises(ValueError, match="unknown"):
        facts.write_table(1, "run-1", "nope", pa.table({}))
    assert facts.read_table(1, "run-404", "test_lines").num_rows == 0
    assert facts.delete_run(1, "run-2") and not facts.delete_run(1, "run-2")
    assert facts.list_runs(1) == ["run-1"]
    assert not list(facts.run_dir(1, "run-1").glob(".*.tmp"))
