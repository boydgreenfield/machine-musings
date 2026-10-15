"""Per-run summaries of ``test_lines``, computed once at finalize so the pages never scan it.

``test_lines`` is the source of truth (one row per test, file and executed line: 2.1 million rows
for a 5,000-test repository), and every page question that only needs *how many* or *whether* used
to aggregate it per request: the treemap's leaves, a function body's line percentage in the
overview's gaps, its history over twelve baselines and the class changes between two runs, and the
per-line test counts of the source view. :func:`compute_summaries` runs those aggregations once,
inside :func:`~coverset.facts.ingest.finalize_run` on the connection it already has open, and
:func:`write_summaries` stores them as three run tables (:data:`~coverset.facts.schema.SUMMARY_TABLES`):

``func_body``
    ``(func_key, statements, covered, lines, covered_lines)`` per callable: the statements of its
    body ``[body_start, end_line]`` minus excluded lines, and those that ran by the function page's
    rule (a test ran it, or ``<import>`` did and it is not the def line). Exactly the ``body`` CTE
    of :mod:`coverset.facts.queries`, which reads it instead when every run in scope has it.
``tree_leaf``
    ``(file_id, func_key, statements, covered)``: the treemap's leaves (the innermost callable
    owns a statement, else the file's module leaf with a null ``func_key``).
``line_hits``
    ``(file_id, line, tests, by_import)`` per executed line: distinct tests other than
    ``<import>`` that ran it, and whether ``<import>`` did. The source view's line states and
    per-line test counts.

The summaries are a cache of ``test_lines``, never a second truth: they are dropped with
``file_cov`` whenever a shard is (re-)ingested and rebuilt by the next finalize, and every reader
falls back to computing from ``test_lines`` for a run that has none (one finalized before they
existed). On the real 5,099-test bundle (2.1 million ``test_lines`` rows): ``func_body`` 3,571
rows (125 KB, its line lists included), ``tree_leaf`` 4,053 (48 KB), ``line_hits`` 42,027 (78 KB):
6 % of the run's bytes, computed in 0.06 s of the finalize (0.4 s at 20 million rows).
"""

from __future__ import annotations

import duckdb
import pyarrow as pa

from coverset.facts.queries import _BODY_LINES_CTE, _TREE_LEAVES_CTE
from coverset.facts.schema import CALLABLE_KINDS, SUMMARY_TABLES
from coverset.facts.store import FactsStore

__all__ = ["SUMMARY_TABLES", "compute_summaries", "write_summaries"]

_FUNC_BODY_SQL = f"""
WITH fn AS (
    SELECT run_id, func_key, any_value(file_id) AS file_id, any_value(def_line) AS def_line,
           any_value(body_start) AS body_start, any_value(end_line) AS end_line
    FROM functions WHERE kind IN {CALLABLE_KINDS!r} GROUP BY run_id, func_key
), {_BODY_LINES_CTE}
SELECT func_key, statements::INTEGER AS statements, covered::INTEGER AS covered,
       lines::INTEGER[] AS lines, covered_lines::INTEGER[] AS covered_lines
FROM body ORDER BY func_key
"""

_TREE_LEAF_SQL = f"""
WITH {_TREE_LEAVES_CTE}
SELECT file_id, func_key, statements::INTEGER AS statements, covered::INTEGER AS covered
FROM all_leaves ORDER BY file_id, func_key NULLS FIRST
"""

_LINE_HITS_SQL = """
SELECT file_id, line,
       (count(*) FILTER (WHERE test_id <> 0))::INTEGER AS tests,
       bool_or(test_id = 0) AS by_import
FROM test_lines GROUP BY file_id, line ORDER BY file_id, line
"""
"""``count(*)``, not ``count(DISTINCT test_id)``: a run stores each (test, file, line) cell once (a
shard's rows are deduplicated at ingest and a test's lines live in one shard only, see
:func:`~coverset.facts.ingest.ingest_shard`), and the distinct count's per-group hash sets took
3.6x the memory (683 MB against 187 MB peak on 20 million rows) and 4x the time."""


def compute_summaries(con: duckdb.DuckDBPyConnection) -> dict[str, pa.Table]:
    """The summaries of the one run ``con``'s views cover (``functions``, ``files``,
    ``file_static`` and ``test_lines`` must be among them)."""
    return {
        "func_body": con.execute(_FUNC_BODY_SQL).to_arrow_table(),
        "tree_leaf": con.execute(_TREE_LEAF_SQL).to_arrow_table(),
        "line_hits": con.execute(_LINE_HITS_SQL).to_arrow_table(),
    }


def write_summaries(store: FactsStore, repo_id: int, run_id: str, tables: dict[str, pa.Table]) -> None:
    """Write :func:`compute_summaries`' tables as the run's summary tables (atomic per file)."""
    for name in SUMMARY_TABLES:
        if name in tables:
            store.write_table(repo_id, run_id, name, tables[name])
