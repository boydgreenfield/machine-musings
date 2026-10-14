"""The compact layout ``test_file_bits``: one numbits bitset per (test, file) next to ``test_lines``.

Written at ingest when ``COVERSET_FACTS_COMPACT=1`` (``facts.store.compact_layout``); the long table
stays the source of truth, and readers use the bitsets only where every shard of a run has them.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from facts_helpers import T_ADD, T_BOX, T_NEG, make_ctx, split_bundle

from coverset.facts import cache, queries
from coverset.facts.convert import bitsets_to_lines, lines_to_bitsets, numbits_to_lines, read_python_bundle
from coverset.facts.ingest import ingest_shard
from coverset.facts.store import COMPACT_ENV, FactsStore, compact_layout
from coverset.lang.base import FunctionRegion


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    cache.CONNECTIONS.clear()
    cache.RESULTS.clear()


def _cells(test: np.ndarray, file: np.ndarray, line: np.ndarray) -> set[tuple[int, int, int]]:
    return set(zip(test.tolist(), file.tolist(), line.tolist(), strict=True))


def test_bitsets_round_trip_and_use_the_numbits_format() -> None:
    rng = np.random.default_rng(3)
    test = rng.integers(0, 40, 5000).astype(np.int32)
    file = rng.integers(0, 6, 5000).astype(np.int32)
    line = rng.integers(1, 4000, 5000).astype(np.int32)
    table = lines_to_bitsets(test, file, line)
    assert table.column_names == ["test_id", "file_id", "n_lines", "bits"]
    keys = list(zip(table.column("test_id").to_pylist(), table.column("file_id").to_pylist(), strict=True))
    assert keys == sorted(set(keys))  # one row per (test, file), sorted
    assert _cells(*bitsets_to_lines(table)) == _cells(test, file, line)  # duplicates collapse
    for row in table.slice(0, 25).to_pylist():
        decoded = numbits_to_lines(row["bits"])
        expected = sorted(
            {n for t, f, n in _cells(test, file, line) if (t, f) == (row["test_id"], row["file_id"])}
        )
        assert decoded.tolist() == expected and row["n_lines"] == len(expected)
    sliced = table.slice(7, 30)  # a slice keeps its own offsets
    assert _cells(*bitsets_to_lines(sliced)) == {
        cell for cell in _cells(test, file, line) if (cell[0], cell[1]) in set(keys[7:37])
    }
    empty = lines_to_bitsets(*(np.empty(0, dtype=np.int32),) * 3)
    assert empty.num_rows == 0 and all(a.size == 0 for a in bitsets_to_lines(empty))


def test_bitsets_are_much_smaller_than_the_long_table() -> None:
    """Tests that run overlapping windows of the same files (what real suites do): the measured
    ratio on the real 5,099-test bundle was 6.7x (docs/benchmarks.md)."""
    rng = np.random.default_rng(11)
    test, file, line = [], [], []
    for t in range(400):
        for f in rng.choice(30, 5, replace=False):
            start = int(rng.integers(1, 400))
            window = np.arange(start, start + int(rng.integers(20, 200)), dtype=np.int32)
            test.append(np.full(window.size, t, np.int32))
            file.append(np.full(window.size, f, np.int32))
            line.append(window)
    t_arr, f_arr, l_arr = (np.concatenate(x) for x in (test, file, line))
    order = np.lexsort((t_arr, l_arr, f_arr))  # test_lines' own order
    long = pa.table({"test_id": t_arr[order], "file_id": f_arr[order], "line": l_arr[order]})

    def size(table: pa.Table) -> int:
        sink = io.BytesIO()
        pq.write_table(table, sink, compression="zstd", use_dictionary=True)
        return sink.tell()

    assert size(lines_to_bitsets(t_arr, f_arr, l_arr)) * 3 < size(long)


def test_the_flag_writes_bitsets_per_attempt(
    store: FactsStore,
    minimal_bundle: Path,
    functions: list[FunctionRegion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = read_python_bundle(minimal_bundle)
    monkeypatch.delenv(COMPACT_ENV, raising=False)
    assert not compact_layout()
    ingest_shard(store, make_ctx("run-1"), bundle, 0, functions)
    assert not store.table_files(1, "run-1", "test_file_bits")
    assert not queries.has_compact_layout(store, 1, "run-1")

    monkeypatch.setenv(COMPACT_ENV, "1")
    ingest_shard(store, make_ctx("run-2"), bundle, 0, functions)
    files = store.table_files(1, "run-2", "test_file_bits")
    assert [f.name for f in files] == ["test_file_bits.shard=0.attempt=1.parquet"]
    long = store.read_table(1, "run-2", "test_lines")
    assert _cells(*bitsets_to_lines(store.read_table(1, "run-2", "test_file_bits"))) == _cells(
        long.column("test_id").to_numpy(), long.column("file_id").to_numpy(), long.column("line").to_numpy()
    )
    # A new attempt replaces the bitsets with the rest of the attempt's files.
    ingest_shard(store, make_ctx("run-2"), split_bundle(bundle, {T_NEG}, with_import=True), 0, functions)
    assert [f.name for f in store.table_files(1, "run-2", "test_file_bits")] == [
        "test_file_bits.shard=0.attempt=2.parquet"
    ]
    assert not list(store.run_dir(1, "run-2").glob("test_file_bits.shard=0.attempt=1.*"))


def test_test_touched_reads_the_bitsets_and_answers_the_same(
    store: FactsStore,
    minimal_bundle: Path,
    functions: list[FunctionRegion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = read_python_bundle(minimal_bundle)
    monkeypatch.delenv(COMPACT_ENV, raising=False)
    ingest_shard(store, make_ctx("long"), bundle, 0, functions)
    monkeypatch.setenv(COMPACT_ENV, "1")
    ingest_shard(store, make_ctx("compact"), bundle, 0, functions)
    assert queries.has_compact_layout(store, 1, "compact")
    assert (
        "test_hits" in queries._TOUCHED_FROM_BITS_SQL and "test_lines" not in queries._TOUCHED_FROM_BITS_SQL
    )
    for nodeid in (T_ADD, T_NEG, T_BOX, "tests/test_missing.py::nope"):
        assert queries.test_touched(store, 1, "compact", nodeid) == queries.test_touched(
            store, 1, "long", nodeid
        )
    assert queries.test_touched(store, 1, "compact", T_NEG)  # not trivially empty


def test_a_run_with_bitsets_in_only_some_shards_reads_test_lines(
    store: FactsStore,
    minimal_bundle: Path,
    functions: list[FunctionRegion],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = read_python_bundle(minimal_bundle)
    ctx = make_ctx("mixed")
    monkeypatch.setenv(COMPACT_ENV, "1")
    ingest_shard(store, ctx, split_bundle(bundle, {T_NEG}, with_import=True), 0, functions)
    monkeypatch.setenv(COMPACT_ENV, "0")
    ingest_shard(store, ctx, split_bundle(bundle, {T_ADD}, with_import=False), 1, functions)
    assert not queries.has_compact_layout(store, 1, "mixed")
    touched = queries.test_touched(store, 1, "mixed", T_ADD)
    assert touched and touched[0].lines  # shard 1's lines, which only test_lines holds
