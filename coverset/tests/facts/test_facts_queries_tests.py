"""One test's facts and a run's inventory: ``test_touched``, ``test_shapes``, ``test_history``,
``run_inventory`` and ``ingest_record`` over two runs of the minimal bundle (seeded like
``test_facts_queries.py``: run-1 finalized, run-2 holds two of the tests and is not)."""

from __future__ import annotations

from pathlib import Path

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
from coverset.facts.ingest import finalize_run, ingest_shard, record_shard_failure
from coverset.facts.queries import (
    ingest_record,
    run_inventory,
    test_history,
    test_shapes,
    test_touched,
)
from coverset.facts.schema import TABLES
from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion


@pytest.fixture
def facts(store: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]) -> FactsStore:
    bundle = read_python_bundle(minimal_bundle)
    ctx = make_ctx("run-1", day=1)
    ingest_shard(store, ctx, bundle, 0, functions)
    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue)
    later = split_bundle(bundle, {T_NEG, T_BOX}, with_import=True)
    ingest_shard(store, make_ctx("run-2", day=2, head_sha="c" * 40), later, 0, functions)
    return store


def _region(functions: list[FunctionRegion], qualname: str) -> FunctionRegion:
    return next(r for r in functions if r.qualname == qualname)


def test_touched_lists_files_functions_and_body_lines(
    facts: FactsStore, functions: list[FunctionRegion]
) -> None:
    touched = test_touched(facts, 1, "run-1", T_BOX)
    calc = next(f for f in touched if f.path == MODULE_PATH)
    names = [fn.qualname for fn in calc.functions]
    assert names == ["classify", "Box.__init__", "Box.total"]  # def_line order
    assert [fn.def_line for fn in calc.functions] == sorted(fn.def_line for fn in calc.functions)
    for fn in calc.functions:
        region = _region(functions, fn.qualname)
        assert fn.func_key == region.func_key
        assert all(region.body_start <= line <= region.end_line for line in fn.lines)
        assert 0 < len(fn.lines) <= fn.body_statements
        # Agrees with the covering-test rule: the test is among the function's covering tests.
        assert T_BOX in queries.tests_covering(
            facts, 1, "run-1", MODULE_PATH, region.body_start, region.end_line
        )
    assert set(calc.lines) == {line for fn in calc.functions for line in fn.lines} | set(calc.module_lines)
    assert calc.statements == len(calc.lines)
    never = _region(functions, "never_called")
    assert not any(never.body_start <= line <= never.end_line for line in calc.lines)


def test_touched_never_counts_import_time(facts: FactsStore) -> None:
    add = test_touched(facts, 1, "run-1", T_ADD)
    assert [fn.qualname for f in add for fn in f.functions] == ["add"]
    assert all(not f.module_lines for f in add)  # def lines ran at import, not in the test
    assert test_touched(facts, 1, "run-1", "<import>") == []
    assert test_touched(facts, 1, "run-1", "tests/test_calc.py::nope") == []
    assert test_touched(facts, 1, "run-2", T_ADD) == []  # run-2 does not hold test_add


def test_shapes_of_one_test(facts: FactsStore) -> None:
    box = test_shapes(facts, 1, "run-1", T_BOX)
    params = {(o.func[1], o.param, o.shape_key) for o in box}
    assert ("classify", "n", "int|pos") in params
    assert ("Box.total", "<receiver>", "obj:factspkg.calc.Box") in params
    via = next(o for o in box if o.func[1] == "classify")
    assert via.caller == (MODULE_PATH, "Box.total", via.caller[2])  # called through Box.total
    receiver = next(o for o in box if o.param == "<receiver>")
    assert receiver.caller == T_BOX and receiver.tests == (T_BOX,)
    assert all(T_BOX in o.tests or o.caller == T_BOX for o in box)
    neg = test_shapes(facts, 1, "run-1", T_NEG)
    assert {(o.param, o.shape_key) for o in neg} == {("n", "int|neg"), ("return", "str|ascii|len:2-10")}
    assert all(o.func[0] == MODULE_PATH and o.func[1] == "classify" for o in neg)
    assert test_shapes(facts, 1, "run-1", "tests/test_calc.py::nope") == []


def test_history_newest_first(facts: FactsStore) -> None:
    rows = test_history(facts, 1, T_FAIL, last_n=5)
    assert [r.run_id for r in rows] == ["run-2", "run-1"]
    assert rows[0].outcome is None  # run-2 did not run it
    assert rows[1].outcome == "failed" and rows[1].branch == "main" and rows[1].head_sha == "a" * 40
    assert [r.outcome for r in test_history(facts, 1, T_NEG)] == ["passed", "passed"]
    assert [r.run_id for r in test_history(facts, 1, T_POS, last_n=1)] == ["run-2"]
    assert test_history(facts, 2, T_POS) == []


def test_run_inventory(facts: FactsStore) -> None:
    inventory = run_inventory(facts, 1, "run-1")
    assert list(inventory) == list(TABLES)
    assert inventory["tests_dim"].rows == 6  # five tests and <import>
    assert inventory["tests_dim"].kind == "shard" and inventory["functions"].kind == "run"
    assert inventory["test_lines"].rows > 0 and inventory["test_lines"].bytes > 0
    assert inventory["ingest_log"].files == 1 and inventory["ingest_log"].rows == 1
    assert inventory["file_cov"].files == 1
    pending = run_inventory(facts, 1, "run-2")
    assert pending["file_cov"].files == 0 and pending["file_cov"].rows == 0 and pending["file_cov"].bytes == 0
    assert run_inventory(facts, 1, "run-9")["tests_dim"].rows == 0


def test_ingest_record_and_notes(facts: FactsStore) -> None:
    (record,) = ingest_record(facts, 1, "run-1")
    assert record.shard_id == 0 and record.attempt == 1 and record.status == "ok" and record.committed
    assert record.bundle_sha256 and record.n_tests == 5
    assert record.bundle_bytes is None or record.bundle_bytes > 0
    record_shard_failure(facts, make_ctx("run-1", day=1), 0, reason="bundle too large")
    rows = ingest_record(facts, 1, "run-1")
    assert [(r.attempt, r.status, r.committed) for r in rows] == [(1, "ok", True), (2, "failed", False)]
    assert "error: bundle too large" in rows[1].tracer_notes
