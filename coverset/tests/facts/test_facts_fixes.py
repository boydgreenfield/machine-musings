"""Regression tests for the correctness review: the lost-tracer record and one sample-cap count."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from facts_helpers import MODULE_PATH, make_ctx

from coverset.analysis.shapes import cap_hits
from coverset.facts.convert import read_python_bundle, tracer_mismatch
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion, NeutralBundle, func_key


@pytest.mark.security
def test_a_lost_tracer_is_a_tracer_mismatch() -> None:
    assert tracer_mismatch({"tracer": "CTracer"}) is None
    assert tracer_mismatch({"tracer": "CTracer", "tracer_lost_at": ""}) is None
    assert tracer_mismatch({"tracer": "CTracer", "tracer_lost_at": "tests/t.py::test_x"}) == (
        "CTracer lost at tests/t.py::test_x"
    )
    assert tracer_mismatch({"tracer_lost_at": "<collection>"}) == "CTracer lost at <collection>"
    hostile = tracer_mismatch({"tracer": "ctrace", "tracer_lost_at": "x\x1b[31m\n" + "y" * 500})
    assert hostile is not None and hostile.isprintable() and len(hostile) <= 64
    assert tracer_mismatch({"tracer": "PyTracer", "tracer_lost_at": "t"}) == "PyTracer"  # foreign wins
    assert tracer_mismatch({"tracer": ["unhashable"]}) == "['unhashable']"
    assert tracer_mismatch({"tracer_lost_at": 5}) is None


@pytest.mark.security
def test_a_lost_tracer_is_recorded_in_the_ingest_log(minimal_bundle: Path, tmp_path: Path) -> None:
    store = FactsStore(tmp_path / "facts")
    bundle = read_python_bundle(minimal_bundle)
    lost = dataclasses.replace(bundle, meta={**bundle.meta, "tracer_lost_at": "tests/t.py::test_x"})
    ingest_shard(store, make_ctx(), lost, 0)
    log = store.read_table(1, "run-1", "ingest_log").to_pylist()[-1]
    assert '"tracer_mismatch": true' in log["convert_json"]
    assert "lost at tests/t.py::test_x" in log["convert_json"]


def test_cap_hits_has_one_definition() -> None:
    assert cap_hits({"a"}, {"a", "b"}, truncated=False) == 0
    assert cap_hits({"a", "b"}, {"a", "b"}, truncated=True) == 1
    assert cap_hits({"a"}, {"a", "b", "c"}, truncated=True) == 2
    assert cap_hits(set(), (), truncated=True) == 1


def test_the_run_counts_cap_hits_once_over_its_shards(
    minimal_bundle: Path, tmp_path: Path, functions: list[FunctionRegion]
) -> None:
    """Two shards that both capped ``classify`` (with the same callers, all sampled) hit the cap with
    one test over the run, as the verdict and the UI count it, not one per shard."""
    store = FactsStore(tmp_path / "facts")
    bundle: NeutralBundle = read_python_bundle(minimal_bundle)
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0, functions)
    ingest_shard(store, ctx, bundle, 1, functions)
    finalize_run(store, ctx, expected_shards=2)
    rows = {r["func_key"]: r for r in store.read_table(1, "run-1", "func_input_cov").to_pylist()}
    classify = rows[func_key(MODULE_PATH, "classify", 0)]
    assert classify["tests_truncated"] == 1
    assert rows[func_key(MODULE_PATH, "add", 0)]["tests_truncated"] == 0
