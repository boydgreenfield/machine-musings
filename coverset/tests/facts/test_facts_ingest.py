"""Shard ingest (ids, idempotence, replacement) and run finalisation."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from facts_helpers import (
    MODULE_PATH,
    T_ADD,
    T_BOX,
    T_FAIL,
    T_NEG,
    T_POS,
    TESTS,
    int_catalogue,
    make_ctx,
    split_bundle,
)

from coverset.facts.convert import BundleError, read_python_bundle
from coverset.facts.dictionary import DICTIONARY_FILE
from coverset.facts.ingest import finalize_run, ingest_shard, record_shard_failure
from coverset.facts.queries import ParquetPriorRun, run_summary
from coverset.facts.store import FactsStore
from coverset.lang.base import (
    IMPORT_CONTEXT,
    FunctionRegion,
    NeutralBundle,
    ShapeObs,
    StaticFile,
    TestRow,
    func_key,
)


@pytest.fixture
def bundle(minimal_bundle: Path) -> NeutralBundle:
    return read_python_bundle(minimal_bundle)


def _rows(store: FactsStore, table: str, run_id: str = "run-1") -> list[dict]:
    return store.read_table(1, run_id, table).to_pylist()


def test_ingest_writes_shard_tables(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    result = ingest_shard(store, make_ctx(), bundle, 0, functions)
    assert not result.skipped and result.attempt == 1 and result.unmatched_functions == 0
    run_dir = store.run_dir(1, "run-1")
    names = {p.name for p in run_dir.iterdir()}
    assert {
        "test_lines.shard=0.attempt=1.parquet",
        "ingest_log.shard=0.parquet",
        "functions.parquet",
    } <= names
    assert "runs_dim.parquet" in names

    ids = store.dictionary(1)
    assert ids.test_id(IMPORT_CONTEXT) == 0
    tests = {r["nodeid"]: r for r in _rows(store, "tests_dim")}
    assert set(tests) == {IMPORT_CONTEXT, *TESTS}
    assert tests[T_FAIL]["outcome"] == "failed" and tests[IMPORT_CONTEXT]["outcome"] is None
    assert tests[T_ADD]["file_id"] == ids.file_id("tests/test_calc.py")

    lines = _rows(store, "test_lines")
    calc = ids.file_id(MODULE_PATH)
    assert {
        (r["test_id"], r["line"])
        for r in lines
        if r["file_id"] == calc and r["test_id"] == ids.test_id(T_ADD)
    } == {(ids.test_id(T_ADD), 5)}
    keys = [(r["file_id"], r["line"]) for r in lines]
    assert keys == sorted(keys)

    static = {r["file_id"]: r for r in _rows(store, "file_static")}
    assert static[calc]["branch_lines"] == [10, 12, 31]
    assert static[calc]["commit_sha"] == "a" * 40
    assert len(_rows(store, "file_arcs_possible")) == 6
    assert len(_rows(store, "run_arcs")) == 6

    shapes = _rows(store, "call_shapes")
    assert len(shapes) == 9
    classify_key = func_key(MODULE_PATH, "classify", 0)
    total_key = func_key(MODULE_PATH, "Box.total", 0)
    from_total = [r for r in shapes if r["caller_kind"] == "func"]
    assert from_total[0]["func_key"] == classify_key and from_total[0]["caller_key"] == total_key
    assert from_total[0]["caller_qualname"] == "Box.total"
    assert {r["shape_key"] for r in _rows(store, "shapes")} >= {"int|pos", "int|neg", "obj:factspkg.calc.Box"}

    sampling = {r["func_key"]: r for r in _rows(store, "func_sampling")}
    assert sampling[classify_key]["tests_called"] == 4 and sampling[classify_key]["tests_truncated"] == 1
    assert sampling[total_key]["tests_truncated"] == 0

    functions_rows = _rows(store, "functions")
    assert {r["qualname"] for r in functions_rows} >= {"add", "classify", "Box.total"}
    assert {r["language"] for r in functions_rows} == {"python"}
    assert json.loads(next(r for r in functions_rows if r["qualname"] == "add")["params_json"]) == ["a", "b"]

    runs = _rows(store, "runs_dim")
    assert runs[0]["ingested_shards"] == 1 and runs[0]["coverage_complete"] is False


def test_reingesting_the_same_bundle_is_a_no_op(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    first = ingest_shard(store, make_ctx(), bundle, 0, functions)
    shard_file = store.table_path(1, "run-1", "test_lines", 0, 1)
    before = shard_file.stat().st_mtime_ns
    second = ingest_shard(store, make_ctx(), bundle, 0, functions)
    assert second.skipped and second.bundle_sha256 == first.bundle_sha256 and second.attempt == 1
    assert shard_file.stat().st_mtime_ns == before
    assert len(_rows(store, "ingest_log")) == 1


def test_a_different_bundle_replaces_the_shard(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    ingest_shard(store, make_ctx(), bundle, 0, functions)
    smaller = split_bundle(bundle, {T_ADD}, with_import=False)
    result = ingest_shard(store, make_ctx(), smaller, 0, functions)
    assert not result.skipped and result.attempt == 2
    ids = store.dictionary(1)
    assert {r["test_id"] for r in _rows(store, "test_lines")} == {ids.test_id(T_ADD)}
    assert {r["nodeid"] for r in _rows(store, "tests_dim")} == {T_ADD}
    log = _rows(store, "ingest_log")
    assert [r["attempt"] for r in log] == [1, 2]
    assert log[0]["bundle_sha256"] != log[1]["bundle_sha256"]
    assert set(store.attempt_files(1, "run-1", 0)) == {2}  # the superseded attempt is cleaned up


def test_ids_are_stable_across_runs(store: FactsStore, bundle: NeutralBundle) -> None:
    ingest_shard(store, make_ctx("run-1"), bundle, 0)
    ids_before = store.dictionary(1)
    ingest_shard(store, make_ctx("run-2"), split_bundle(bundle, {T_BOX}, with_import=True), 0)
    ids_after = store.dictionary(1)
    for nodeid in TESTS:
        assert ids_before.test_id(nodeid) == ids_after.test_id(nodeid)
    assert ids_after.file_id(MODULE_PATH) == ids_before.file_id(MODULE_PATH)


def test_unmatched_functions_fall_back_to_ordinal_zero(store: FactsStore, bundle: NeutralBundle) -> None:
    result = ingest_shard(store, make_ctx(), bundle, 0)
    assert result.unmatched_functions == 3
    keys = {r["func_key"] for r in _rows(store, "call_shapes")}
    assert func_key(MODULE_PATH, "classify", 0) in keys
    log = _rows(store, "ingest_log")[0]
    assert json.loads(log["convert_json"])["unmatched_functions"] == 3


def test_finalize_tracks_shard_completeness(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    ctx = make_ctx()
    first = split_bundle(bundle, {T_ADD, T_NEG}, with_import=True)
    second = split_bundle(bundle, {T_POS, T_BOX, T_FAIL}, with_import=False)
    ingest_shard(store, ctx, first, 0, functions)
    partial = finalize_run(store, ctx, expected_shards=2)
    assert (partial.ingested_shards, partial.coverage_complete) == (1, False)
    assert _rows(store, "runs_dim")[0]["coverage_complete"] is False

    ingest_shard(store, ctx, second, 1, functions)
    assert not store.table_files(1, "run-1", "file_cov")  # aggregates invalidated by ingest
    done = finalize_run(store, ctx, expected_shards=2)
    assert (done.ingested_shards, done.coverage_complete) == (2, True)
    runs = _rows(store, "runs_dim")[0]
    assert runs["coverage_complete"] is True and runs["expected_shards"] == 2
    assert runs["finalized_at"] is not None

    file_cov = {r["path"]: r for r in _rows(store, "file_cov")}
    calc = file_cov[MODULE_PATH]
    assert (calc["statements"], calc["covered"]) == (18, 17)
    assert (calc["branches"], calc["branches_covered"], calc["partial_branches"]) == (6, 6, 0)
    assert calc["percent"] == pytest.approx(100 * 23 / 24)
    assert file_cov["src/factspkg/__init__.py"]["percent"] == 100.0


def test_partial_branches(store: FactsStore, bundle: NeutralBundle) -> None:
    arcs = {MODULE_PATH: tuple(a for a in bundle.run_arcs[MODULE_PATH] if a != (12, 13))}
    ctx = make_ctx()
    ingest_shard(store, ctx, dataclasses.replace(bundle, run_arcs=arcs), 0)
    finalize_run(store, ctx, expected_shards=1)
    calc = next(r for r in _rows(store, "file_cov") if r["path"] == MODULE_PATH)
    assert (calc["branches_covered"], calc["partial_branches"]) == (5, 1)


def test_finalize_input_coverage(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0, functions)
    finalize_run(store, ctx, expected_shards=1)
    plain = {r["func_key"]: r for r in _rows(store, "func_input_cov")}
    classify = func_key(MODULE_PATH, "classify", 0)
    assert plain[classify]["tests_called"] == 4
    assert plain[classify]["tests_sampled"] == 4 and plain[classify]["tests_truncated"] == 1
    assert plain[classify]["blocks_expected"] is None
    assert plain[func_key(MODULE_PATH, "never_called", 0)]["tests_called"] == 0

    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue)
    rows = {r["func_key"]: r for r in _rows(store, "func_input_cov")}
    add = rows[func_key(MODULE_PATH, "add", 0)]
    assert (add["blocks_expected"], add["blocks_observed"]) == (6, 2)
    assert json.loads(add["missing_json"]) == {"a": ["neg", "zero"], "b": ["neg", "zero"]}
    assert (rows[classify]["blocks_expected"], rows[classify]["blocks_observed"]) == (3, 3)
    assert json.loads(rows[classify]["missing_json"]) == {}
    assert rows[func_key(MODULE_PATH, "Box.total", 0)]["blocks_expected"] == 0
    assert rows[func_key(MODULE_PATH, "never_called", 0)]["blocks_expected"] is None


def test_catalogue_receives_reconstructed_observations(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0, functions)
    seen: list[list[ShapeObs]] = []
    finalize_run(store, ctx, expected_shards=1, catalogue=lambda obs: seen.append(obs) or {})
    classify = next(obs for obs in seen if obs[0].func[1] == "classify")
    original = {o for o in bundle.shapes if o.func[1] == "classify"}
    assert set(classify) == original


def test_failed_shard_counts_as_missing(store: FactsStore, bundle: NeutralBundle) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0)
    assert record_shard_failure(store, ctx, 1, reason="bundle is missing results.jsonl") == 1
    result = finalize_run(store, ctx, expected_shards=2)
    assert (result.ingested_shards, result.coverage_complete) == (1, False)
    failed = [r for r in _rows(store, "ingest_log") if r["shard_id"] == 1]
    assert failed[0]["status"] == "failed" and "results.jsonl" in failed[0]["convert_json"]
    retry = ingest_shard(store, ctx, split_bundle(bundle, {T_ADD}, with_import=False), 1)
    assert retry.attempt == 2
    assert finalize_run(store, ctx, expected_shards=2).coverage_complete


# -- one shard bundle per transaction ---------------------------------------------------------------


def _visible(store: FactsStore) -> dict[str, list[dict]]:
    return {t: _rows(store, t) for t in ("test_lines", "tests_dim", "file_static", "call_shapes", "files")}


@pytest.mark.security
def test_a_rejected_bundle_changes_nothing_but_the_log(store: FactsStore, bundle: NeutralBundle) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0)
    finalize_run(store, ctx, expected_shards=1)
    before = _visible(store)
    dictionary = (store.repo_dir(1) / DICTIONARY_FILE).read_bytes()
    huge = StaticFile("src/new.py", 10, (2**40,), (), ())
    hostile = dataclasses.replace(
        split_bundle(bundle, {T_ADD}, with_import=False),
        static={**bundle.static, "src/new.py": huge},
        tests=(
            *bundle.tests,
            TestRow("tests/t.py::fresh", "tests/t.py", "passed", 0.1, None, None, None, None, None),
        ),
    )
    with pytest.raises(BundleError, match="out of range"):
        ingest_shard(store, ctx, hostile, 0)
    assert _visible(store) == before
    assert (store.repo_dir(1) / DICTIONARY_FILE).read_bytes() == dictionary  # no id was assigned
    log = _rows(store, "ingest_log")
    assert [(r["attempt"], r["status"]) for r in log] == [(1, "ok"), (2, "failed")]
    runs = _rows(store, "runs_dim")[0]
    assert runs["finalized_at"] is None and runs["ingested_shards"] == 0  # the shard needs a good bundle
    assert not store.table_files(1, "run-1", "file_cov")


@pytest.mark.security
def test_sample_counts_beyond_int64_are_rejected(store: FactsStore, bundle: NeutralBundle) -> None:
    obs = dataclasses.replace(bundle.shapes[0], samples=2**63)
    with pytest.raises(BundleError, match="sample counts"):
        ingest_shard(store, make_ctx(), dataclasses.replace(bundle, shapes=(obs,)), 0)
    assert store.committed(1, "run-1") == {}


def test_a_crash_mid_ingest_leaves_the_committed_attempt_visible(
    store: FactsStore, bundle: NeutralBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0)
    finalize_run(store, ctx, expected_shards=1)
    before = _visible(store)
    real_write = FactsStore.write_table
    calls = {"n": 0}

    def flaky_write(self: FactsStore, *args: object, **kwargs: object):
        if kwargs.get("attempt") == 2:
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(28, "No space left on device")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(FactsStore, "write_table", flaky_write)
    with pytest.raises(OSError, match="No space"):
        ingest_shard(store, ctx, split_bundle(bundle, {T_ADD}, with_import=False), 0)
    monkeypatch.undo()
    assert _visible(store) == before  # attempt 2's two written files are invisible
    assert set(store.attempt_files(1, "run-1", 0)) == {1, 2}
    assert [r["attempt"] for r in _rows(store, "ingest_log")] == [1]
    assert _rows(store, "runs_dim")[0]["finalized_at"] is None  # aggregates were invalidated first
    retry = ingest_shard(store, ctx, split_bundle(bundle, {T_ADD}, with_import=False), 0)
    assert retry.attempt == 3 and set(store.attempt_files(1, "run-1", 0)) == {3}
    assert {r["nodeid"] for r in _rows(store, "tests_dim")} == {T_ADD}


def test_uncommitted_files_are_invisible_to_every_reader(store: FactsStore, bundle: NeutralBundle) -> None:
    ingest_shard(store, make_ctx(), bundle, 0)
    rows = _rows(store, "tests_dim")
    store.write_table(
        1, "run-1", "tests_dim", store.read_table(1, "run-1", "tests_dim"), shard_id=0, attempt=7
    )
    store.write_table(
        1, "run-1", "tests_dim", store.read_table(1, "run-1", "tests_dim"), shard_id=4, attempt=1
    )
    assert _rows(store, "tests_dim") == rows
    con = store.connect(repo_id=1, run_id="run-1", tables=("tests_dim",))
    try:
        assert con.execute("SELECT count(*) FROM tests_dim").fetchone() == (len(rows),)
    finally:
        con.close()


# -- retried tests keep their first coverage --------------------------------------------------------


def test_a_test_rerun_in_another_shard_keeps_its_first_coverage(
    store: FactsStore, bundle: NeutralBundle
) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0)
    first_lines = {
        r["line"] for r in _rows(store, "test_lines") if r["test_id"] == store.dictionary(1).test_id(T_ADD)
    }
    rerun = split_bundle(bundle, {T_ADD}, with_import=False)
    failed = dataclasses.replace(rerun.tests[0], outcome="failed", duration_s=9.0)
    moved = {T_ADD: {MODULE_PATH: (4, 5, 17)}}
    result = ingest_shard(
        store, ctx, dataclasses.replace(rerun, tests=(failed,), coverage=moved, durations={}), 1
    )
    assert result.duplicate_tests == 1 and result.rows["test_lines"] == 0
    ids = store.dictionary(1)
    lines = {r["line"] for r in _rows(store, "test_lines") if r["test_id"] == ids.test_id(T_ADD)}
    assert lines == first_lines == {5}
    has_lines = {(r["shard_id"], r["has_lines"]) for r in _rows(store, "tests_dim") if r["nodeid"] == T_ADD}
    assert has_lines == {(0, True), (1, False)}
    finalize_run(store, ctx, expected_shards=2)
    summary = run_summary(store, 1, "run-1")
    assert summary is not None and summary.tests == len(TESTS)
    assert summary.outcomes == {"passed": 3, "failed": 2}  # T_ADD resolves to its latest result
    assert ParquetPriorRun(store, 1, "run-1").durations()[T_ADD] == 9.0
    log = [r for r in _rows(store, "ingest_log") if r["shard_id"] == 1]
    assert json.loads(log[0]["convert_json"])["duplicate_tests"] == 1


def test_extra_shard_ids_do_not_block_completeness(store: FactsStore, bundle: NeutralBundle) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, split_bundle(bundle, {T_ADD}, with_import=True), 0)
    ingest_shard(store, ctx, split_bundle(bundle, {T_NEG}, with_import=False), 5)
    partial = finalize_run(store, ctx, expected_shards=2)
    assert (partial.ingested_shards, partial.coverage_complete) == (1, False)
    ingest_shard(store, ctx, split_bundle(bundle, {T_POS}, with_import=False), 1)
    done = finalize_run(store, ctx, expected_shards=2)
    assert (done.ingested_shards, done.coverage_complete) == (2, True)
    assert _rows(store, "runs_dim")[0]["coverage_complete"] is True


def test_tracer_mismatch_is_recorded_in_the_ingest_log(
    store: FactsStore, bundle_copy: Path, functions: list[FunctionRegion]
) -> None:
    meta = json.loads((bundle_copy / "meta.json").read_text())
    (bundle_copy / "meta.json").write_text(json.dumps({**meta, "tracer": "PyTracer"}))
    bundle = read_python_bundle(bundle_copy)
    assert bundle.meta["convert"]["tracer_mismatch"] is True
    ingest_shard(store, make_ctx(), bundle, 0, functions)
    details = json.loads(_rows(store, "ingest_log")[0]["convert_json"])
    assert details["tracer_mismatch"] is True and details["tracer"] == "PyTracer"
    # a bundle from another converter is checked from its meta alone
    other = dataclasses.replace(bundle, meta={"tracer": "PyTracer"})
    ingest_shard(store, make_ctx("run-2"), other, 0, functions)
    rows = store.read_table(1, "run-2", "ingest_log", shard_id=0).to_pylist()
    assert json.loads(rows[0]["convert_json"])["tracer_mismatch"] is True
