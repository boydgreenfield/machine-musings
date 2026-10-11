"""Every facts table's Arrow schema, in one place.

Files under ``repo=<repo_id>/run=<run_id>/`` never store ``repo_id`` or ``run_id``: the DuckDB views
add both from the Hive partition directories. *Shard tables* hold one shard bundle's rows and are
written once per ingest attempt as ``<table>.shard=<n>.attempt=<k>.parquet`` (``ATTEMPT_TABLES``),
except ``ingest_log.shard=<n>.parquet``, the shard's history and commit record; *run tables* are
written once per run as ``<table>.parquet``.

``tests_dim`` has one row per nodeid of the shard (``<import>`` included); ``has_lines`` says
whether this shard's ``test_lines`` hold the test's coverage (a test whose coverage another shard
recorded first keeps that coverage, see :func:`coverset.facts.ingest.ingest_shard`).

``call_shapes.caller_key`` is the caller's ``func_key`` (``caller_kind = 'func'``, with the caller's
path, qualname and first line alongside) or the calling test's nodeid (``caller_kind = 'test'``).

Line numbers are int32 (coverage.py arcs use negative numbers for code-object entry and exit);
``test_id`` 0 is the ``<import>`` pseudo test.
"""

from __future__ import annotations

import pyarrow as pa

__all__ = ["ATTEMPT_TABLES", "CALLABLE_KINDS", "RUN_TABLES", "SCHEMAS", "SHARD_TABLES", "TABLES"]

CALLABLE_KINDS = ("function", "method")
"""``functions.kind`` values that are callables (classes and modules are regions, not callees)."""

_LINES = pa.list_(pa.int32())
_TS = pa.timestamp("us", tz="UTC")

SCHEMAS: dict[str, pa.Schema] = {
    # -- shard tables ------------------------------------------------------------------------
    "tests_dim": pa.schema(
        [
            ("test_id", pa.int32()),
            ("nodeid", pa.string()),
            ("file_id", pa.int32()),
            ("outcome", pa.string()),
            ("duration_s", pa.float64()),
            ("shard_id", pa.int32()),
            ("has_lines", pa.bool_()),
        ]
    ),
    "files": pa.schema([("file_id", pa.int32()), ("path", pa.string())]),
    "file_static": pa.schema(
        [
            ("commit_sha", pa.string()),
            ("file_id", pa.int32()),
            ("blob_sha", pa.string()),
            ("n_lines", pa.int32()),
            ("statements", _LINES),
            ("excluded", _LINES),
            ("branch_lines", _LINES),
        ]
    ),
    "file_arcs_possible": pa.schema(
        [
            ("commit_sha", pa.string()),
            ("file_id", pa.int32()),
            ("from_line", pa.int32()),
            ("to_line", pa.int32()),
        ]
    ),
    "test_lines": pa.schema([("test_id", pa.int32()), ("file_id", pa.int32()), ("line", pa.int32())]),
    "run_arcs": pa.schema([("file_id", pa.int32()), ("from_line", pa.int32()), ("to_line", pa.int32())]),
    "call_shapes": pa.schema(
        [
            ("func_key", pa.string()),
            ("param", pa.string()),
            ("shape_id", pa.int32()),
            ("caller_kind", pa.string()),
            ("caller_key", pa.string()),
            ("caller_path", pa.string()),
            ("caller_qualname", pa.string()),
            ("caller_line", pa.int32()),
            ("samples", pa.int64()),
            ("test_ids", _LINES),
            ("truncated", pa.bool_()),
            ("example_test_id", pa.int32()),
            ("example_repr", pa.string()),
        ]
    ),
    "func_sampling": pa.schema(
        [
            ("func_key", pa.string()),
            ("path", pa.string()),
            ("qualname", pa.string()),
            ("firstlineno", pa.int32()),
            ("calls", pa.int64()),
            ("tests_called", pa.int32()),
            ("tests_truncated", pa.int32()),
            ("truncated", pa.bool_()),
            ("test_ids", _LINES),
        ]
    ),
    "shapes": pa.schema([("shape_id", pa.int32()), ("shape_key", pa.string())]),
    "ingest_log": pa.schema(
        [
            ("shard_id", pa.int32()),
            ("attempt", pa.int32()),
            ("bundle_sha256", pa.string()),
            ("status", pa.string()),
            ("ingested_at", _TS),
            ("n_tests", pa.int32()),
            ("n_test_lines", pa.int64()),
            ("convert_json", pa.string()),
        ]
    ),
    # -- run tables --------------------------------------------------------------------------
    "functions": pa.schema(
        [
            ("commit_sha", pa.string()),
            ("func_key", pa.string()),
            ("file_id", pa.int32()),
            ("path", pa.string()),
            ("qualname", pa.string()),
            ("ordinal", pa.int32()),
            ("def_line", pa.int32()),
            ("body_start", pa.int32()),
            ("end_line", pa.int32()),
            ("body_hash", pa.string()),
            ("sig_hash", pa.string()),
            ("params_json", pa.string()),
            ("kind", pa.string()),
            ("language", pa.string()),
        ]
    ),
    "runs_dim": pa.schema(
        [
            ("head_sha", pa.string()),
            ("base_sha", pa.string()),
            ("branch", pa.string()),
            ("pr_number", pa.int64()),
            ("created_at", _TS),
            ("scoring_basis_run_id", pa.string()),
            ("expected_shards", pa.int32()),
            ("ingested_shards", pa.int32()),
            ("coverage_complete", pa.bool_()),
            ("finalized_at", _TS),
        ]
    ),
    "file_cov": pa.schema(
        [
            ("file_id", pa.int32()),
            ("path", pa.string()),
            ("statements", pa.int32()),
            ("covered", pa.int32()),
            ("branches", pa.int32()),
            ("branches_covered", pa.int32()),
            ("partial_branches", pa.int32()),
            ("percent", pa.float64()),
        ]
    ),
    "func_input_cov": pa.schema(
        [
            ("func_key", pa.string()),
            ("tests_called", pa.int32()),
            ("tests_sampled", pa.int32()),
            ("tests_truncated", pa.int32()),
            ("blocks_expected", pa.int32()),
            ("blocks_observed", pa.int32()),
            ("missing_json", pa.string()),
        ]
    ),
}

SHARD_TABLES: frozenset[str] = frozenset(
    {
        "tests_dim",
        "files",
        "file_static",
        "file_arcs_possible",
        "test_lines",
        "run_arcs",
        "call_shapes",
        "func_sampling",
        "shapes",
        "ingest_log",
    }
)
ATTEMPT_TABLES: frozenset[str] = SHARD_TABLES - {"ingest_log"}
RUN_TABLES: frozenset[str] = frozenset({"functions", "runs_dim", "file_cov", "func_input_cov"})
TABLES: tuple[str, ...] = tuple(SCHEMAS)
