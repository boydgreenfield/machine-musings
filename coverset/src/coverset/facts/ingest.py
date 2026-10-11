"""Ingest shard bundles into the facts store and compute run-level aggregates.

:func:`ingest_shard` turns one :class:`~coverset.lang.base.NeutralBundle` into this shard's rows of
every shard table and commits them as one unit: under the run lock it validates the bundle, builds
and type-checks every table, invalidates the run aggregates, writes the new attempt's files (hidden
from readers), then appends ``ok`` to the shard's ``ingest_log``, which is the commit point (see
:mod:`coverset.facts.store`). Re-ingesting the bundle already committed for a shard is a no-op; a
different bundle for the shard replaces it wholesale. :func:`finalize_run` computes ``file_cov``
and ``func_input_cov`` once the expected shards are in and records ``coverage_complete``.

Retried tests keep their first coverage: the runner's retries are never ingested (they run in a
separate output directory), and if a nodeid's coverage is already recorded by another shard of the
run, a later shard's lines for it are dropped (counted as ``duplicate_tests``) while its result is
still recorded; queries resolve such a test to the result of the most recently committed shard.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pyarrow as pa

from coverset.facts.dictionary import RepoDictionary
from coverset.facts.schema import CALLABLE_KINDS, SCHEMAS
from coverset.facts.store import FactsStore, conform
from coverset.facts.untrusted import BundleError, safe_rel_path
from coverset.lang.base import (
    IMPORT_CONTEXT,
    Catalogue,
    FunctionRegion,
    FuncTriple,
    NeutralBundle,
    ShapeObs,
    func_key,
)

__all__ = [
    "Catalogue",
    "FinalizeResult",
    "IngestResult",
    "RunContext",
    "finalize_run",
    "ingest_shard",
    "record_shard_failure",
]

_AGGREGATE_TABLES = ("file_cov", "func_input_cov")
_INT32 = 2**31 - 1
_INT64 = 2**63 - 1


@dataclass(frozen=True, slots=True)
class RunContext:
    """The control-plane facts about a run that the facts store mirrors into ``runs_dim``."""

    repo_id: int
    run_id: str
    head_sha: str
    base_sha: str | None
    branch: str | None
    pr_number: int | None
    created_at: datetime
    scoring_basis_run_id: str | None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What :func:`ingest_shard` did: ``skipped`` means the same bundle was already ingested.

    ``duplicate_tests`` counts tests whose lines were dropped because another shard of the run had
    already recorded their coverage.
    """

    run_id: str
    shard_id: int
    attempt: int
    bundle_sha256: str
    skipped: bool
    rows: Mapping[str, int]
    unmatched_functions: int = 0
    duplicate_tests: int = 0


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    """Run-level outcome of :func:`finalize_run`."""

    run_id: str
    expected_shards: int
    ingested_shards: int
    coverage_complete: bool
    files: int
    functions: int


class _FuncResolver:
    """Maps shape-tracer function triples to ``func_key`` via the commit's function regions."""

    def __init__(self, functions: Sequence[FunctionRegion]) -> None:
        self._by_triple = {(r.path, r.qualname, r.def_line): r for r in functions}
        by_name: dict[tuple[str, str], list[FunctionRegion]] = defaultdict(list)
        for region in functions:
            by_name[(region.path, region.qualname)].append(region)
        self._unique_by_name = {name: rs[0] for name, rs in by_name.items() if len(rs) == 1}
        self.unmatched: set[FuncTriple] = set()

    def region(self, triple: FuncTriple) -> FunctionRegion | None:
        found = self._by_triple.get(triple)
        return found or self._unique_by_name.get((triple[0], triple[1]))

    def key(self, triple: FuncTriple) -> str:
        """The region's key, else the plan's formula with ordinal 0 (counted as unmatched)."""
        region = self.region(triple)
        if region is not None:
            return region.func_key
        self.unmatched.add(triple)
        return func_key(triple[0], triple[1], 0)


class _LineIndex:
    """This shard's ``test_lines`` as numpy arrays sorted by (file_id, line, test_id).

    Tests in ``skip`` (coverage already recorded by another shard) contribute no lines.
    """

    def __init__(self, bundle: NeutralBundle, ids: RepoDictionary, skip: set[str]) -> None:
        tests, files, lines = [], [], []
        for nodeid, by_path in bundle.coverage.items():
            if nodeid in skip:
                continue
            test_id = ids.test_id(nodeid)
            for path, executed in by_path.items():
                arr = np.asarray(executed, dtype=np.int32)
                lines.append(arr)
                files.append(np.full(arr.size, ids.file_id(path), dtype=np.int32))
                tests.append(np.full(arr.size, test_id, dtype=np.int32))
        empty = np.empty(0, dtype=np.int32)
        test_arr = np.concatenate(tests) if tests else empty
        file_arr = np.concatenate(files) if files else empty
        line_arr = np.concatenate(lines) if lines else empty
        order = np.lexsort((test_arr, line_arr, file_arr))
        self.test_id = test_arr[order]
        self.file_id = file_arr[order]
        self.line = line_arr[order]

    def tests_in(self, file_id: int, start: int, end: int) -> set[int]:
        """Test ids (``<import>`` excluded) with a line of ``file_id`` in ``[start, end]``."""
        lo, hi = np.searchsorted(self.file_id, [file_id, file_id + 1])
        lines = self.line[lo:hi]
        a, b = np.searchsorted(lines, [start, end + 1])
        found = set(self.test_id[lo + a : lo + b].tolist())
        found.discard(0)
        return found

    def table(self) -> pa.Table:
        return pa.table({"test_id": self.test_id, "file_id": self.file_id, "line": self.line})


def _bundle_sha(bundle: NeutralBundle) -> str:
    recorded = bundle.meta.get("convert", {}).get("bundle_sha256") if isinstance(bundle.meta, dict) else None
    if isinstance(recorded, str) and recorded:
        return recorded
    content = {f.name: getattr(bundle, f.name) for f in dataclasses.fields(bundle) if f.name != "meta"}
    canonical = json.dumps(
        content, sort_keys=True, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else o
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _in_range(values: Iterable[int], low: int, high: int) -> bool:
    values = tuple(values)
    return not values or (low <= min(values) and max(values) <= high)


def _validate(bundle: NeutralBundle) -> None:
    """Reject a bundle whose numbers do not fit the facts columns, before anything is written.

    ``read_python_bundle`` already enforces tighter limits; this guards bundles from any adapter.
    """
    for nodeid, by_path in bundle.coverage.items():
        for path, lines in by_path.items():
            if not _in_range(lines, 1, _INT32):
                raise BundleError(f"line numbers out of range for {nodeid!r} in {path!r}")
    for facts in bundle.static.values():
        arc_lines = [line for arc in facts.arcs_possible for line in arc]
        if not (
            0 <= facts.n_lines <= _INT32
            and _in_range(facts.statements, 1, _INT32)
            and _in_range(facts.excluded, 1, _INT32)
            and _in_range(arc_lines, -_INT32, _INT32)
        ):
            raise BundleError(f"static facts out of range for {facts.path!r}")
    for path, arcs in bundle.run_arcs.items():
        if not _in_range((line for arc in arcs for line in arc), -_INT32, _INT32):
            raise BundleError(f"executed arcs out of range for {path!r}")
    triples = [o.func for o in bundle.shapes] + [s.func for s in bundle.sampling]
    triples += [o.caller for o in bundle.shapes if not isinstance(o.caller, str)]
    if not _in_range((t[2] for t in triples), 0, _INT32):
        raise BundleError("function first lines out of range")
    counts = [o.samples for o in bundle.shapes] + [s.calls for s in bundle.sampling]
    if not _in_range(counts, 0, _INT64):
        raise BundleError("sample counts out of range")


def _test_file(nodeid: str, files: Mapping[str, str]) -> str | None:
    if nodeid in files:
        return files[nodeid]
    if nodeid == IMPORT_CONTEXT:
        return None
    return safe_rel_path(nodeid.split("::", 1)[0])


def _shard_rows(
    ctx: RunContext,
    shard_id: int,
    bundle: NeutralBundle,
    ids: RepoDictionary,
    resolver: _FuncResolver,
    index: _LineIndex,
    test_files: Mapping[str, str | None],
    skip: set[str],
) -> dict[str, pa.Table]:
    results = {row.nodeid: row for row in bundle.tests}
    nodeids = sorted({*results, *bundle.coverage}, key=ids.test_id)
    tests_dim = pa.table(
        {
            "test_id": [ids.test_id(n) for n in nodeids],
            "nodeid": nodeids,
            "file_id": [ids.file_id(f) if (f := test_files.get(n)) else None for n in nodeids],
            "outcome": [results[n].outcome if n in results else None for n in nodeids],
            "duration_s": [
                bundle.durations.get(n, results[n].duration_s if n in results else None) for n in nodeids
            ],
            "shard_id": [shard_id] * len(nodeids),
            "has_lines": [n in bundle.coverage and n not in skip for n in nodeids],
        }
    )
    static = sorted(bundle.static.values(), key=lambda s: ids.file_id(s.path))
    file_static = pa.table(
        {
            "commit_sha": [ctx.head_sha] * len(static),
            "file_id": [ids.file_id(s.path) for s in static],
            "n_lines": [s.n_lines for s in static],
            "statements": [list(s.statements) for s in static],
            "excluded": [list(s.excluded) for s in static],
            "branch_lines": [sorted({a for a, _ in s.arcs_possible if a > 0}) for s in static],
        }
    )
    possible = [(ids.file_id(s.path), a, b) for s in static for a, b in s.arcs_possible]
    run_arcs = sorted((ids.file_id(path), a, b) for path, arcs in bundle.run_arcs.items() for a, b in arcs)
    used_paths = sorted(
        {*bundle.static, *bundle.run_arcs, *(p for by in bundle.coverage.values() for p in by)}
        | {f for f in test_files.values() if f},
        key=ids.file_id,
    )
    return {
        "tests_dim": tests_dim,
        "files": pa.table({"file_id": [ids.file_id(p) for p in used_paths], "path": used_paths}),
        "file_static": file_static,
        "file_arcs_possible": pa.table(
            {
                "commit_sha": [ctx.head_sha] * len(possible),
                "file_id": [r[0] for r in possible],
                "from_line": [r[1] for r in possible],
                "to_line": [r[2] for r in possible],
            }
        ),
        "test_lines": index.table(),
        "run_arcs": pa.table(
            {
                "file_id": [r[0] for r in run_arcs],
                "from_line": [r[1] for r in run_arcs],
                "to_line": [r[2] for r in run_arcs],
            }
        ),
        "call_shapes": _call_shapes(bundle, ids, resolver),
        "func_sampling": _func_sampling(bundle, ids, resolver, index),
        "shapes": _shapes(bundle, ids),
    }


def _call_shapes(bundle: NeutralBundle, ids: RepoDictionary, resolver: _FuncResolver) -> pa.Table:
    rows: dict[str, list] = defaultdict(list)
    for obs in bundle.shapes:
        caller_is_test = isinstance(obs.caller, str)
        rows["func_key"].append(resolver.key(obs.func))
        rows["param"].append(obs.param)
        rows["shape_id"].append(ids.shape_id(obs.shape_key))
        rows["caller_kind"].append("test" if caller_is_test else "func")
        rows["caller_key"].append(obs.caller if caller_is_test else resolver.key(obs.caller))
        rows["caller_path"].append(None if caller_is_test else obs.caller[0])
        rows["caller_qualname"].append(None if caller_is_test else obs.caller[1])
        rows["caller_line"].append(None if caller_is_test else obs.caller[2])
        rows["samples"].append(obs.samples)
        rows["test_ids"].append([ids.test_id(t) for t in obs.tests])
        rows["truncated"].append(obs.truncated)
        rows["example_test_id"].append(ids.test_id(obs.example_test) if obs.example_test else None)
        rows["example_repr"].append(obs.example)
    return pa.table(rows) if rows else pa.table({})


def _func_sampling(
    bundle: NeutralBundle, ids: RepoDictionary, resolver: _FuncResolver, index: _LineIndex
) -> pa.Table:
    rows: dict[str, list] = defaultdict(list)
    for sampling in bundle.sampling:
        sampled = {ids.test_id(t) for t in sampling.tests}
        region = resolver.region(sampling.func)
        called = set(sampled)
        if region is not None and (file_id := ids.find_file_id(region.path)) is not None:
            called |= index.tests_in(file_id, region.body_start, region.end_line)
        # When the adaptive cap stopped sampling, every test that called the function without
        # being sampled is "truncated"; at least the test during which the cap was hit is.
        truncated = max(len(called - sampled), 1) if sampling.truncated else 0
        rows["func_key"].append(resolver.key(sampling.func))
        rows["path"].append(sampling.func[0])
        rows["qualname"].append(sampling.func[1])
        rows["firstlineno"].append(sampling.func[2])
        rows["calls"].append(sampling.calls)
        rows["tests_called"].append(len(called))
        rows["tests_truncated"].append(truncated)
        rows["truncated"].append(sampling.truncated)
        rows["test_ids"].append(sorted(sampled))
    return pa.table(rows) if rows else pa.table({})


def _shapes(bundle: NeutralBundle, ids: RepoDictionary) -> pa.Table:
    keys = sorted({obs.shape_key for obs in bundle.shapes}, key=ids.shape_id)
    return pa.table({"shape_id": [ids.shape_id(k) for k in keys], "shape_key": keys})


def _functions_table(
    ctx: RunContext, functions: Sequence[FunctionRegion], ids: RepoDictionary, language: str
) -> pa.Table:
    ordered = sorted(functions, key=lambda r: (r.path, r.def_line, r.qualname, r.ordinal))
    return pa.table(
        {
            "commit_sha": [ctx.head_sha] * len(ordered),
            "func_key": [r.func_key for r in ordered],
            "file_id": [ids.file_id(r.path) for r in ordered],
            "path": [r.path for r in ordered],
            "qualname": [r.qualname for r in ordered],
            "ordinal": [r.ordinal for r in ordered],
            "def_line": [r.def_line for r in ordered],
            "body_start": [r.body_start for r in ordered],
            "end_line": [r.end_line for r in ordered],
            "body_hash": [r.body_hash for r in ordered],
            "sig_hash": [r.sig_hash for r in ordered],
            "params_json": [json.dumps(list(r.params)) for r in ordered],
            "kind": [r.kind for r in ordered],
            "language": [language] * len(ordered),
        }
    )


def _conform_all(tables: Mapping[str, pa.Table]) -> dict[str, pa.Table]:
    """Type-check every table before anything is written; a value that does not fit is a BundleError."""
    try:
        return {name: conform(name, table) for name, table in tables.items()}
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
        raise BundleError(f"bundle values do not fit the facts schema: {exc}") from exc


def _ingested_shards(logs: Mapping[int, Sequence[tuple[int, str]]], expected: int | None) -> int:
    """Shards whose latest attempt is ``ok`` (only ids below ``expected`` count when it is known)."""
    return sum(
        1
        for shard_id, rows in logs.items()
        if rows and max(rows)[1] == "ok" and (expected is None or shard_id < expected)
    )


def _append_log(
    store: FactsStore,
    ctx: RunContext,
    shard_id: int,
    attempt: int,
    sha: str | None,
    status: str,
    details: Mapping[str, object],
    n_tests: int | None,
    n_test_lines: int | None,
) -> None:
    history = store.read_table(ctx.repo_id, ctx.run_id, "ingest_log", shard_id=shard_id)
    entry = pa.table(
        {
            "shard_id": [shard_id],
            "attempt": [attempt],
            "bundle_sha256": [sha],
            "status": [status],
            "ingested_at": [datetime.now(UTC)],
            "n_tests": [n_tests],
            "n_test_lines": [n_test_lines],
            "convert_json": [json.dumps(details, sort_keys=True)],
        }
    )
    combined = pa.concat_tables([history, conform("ingest_log", entry)])
    store.write_table(ctx.repo_id, ctx.run_id, "ingest_log", combined, shard_id=shard_id)


def _write_runs_dim(store: FactsStore, ctx: RunContext, *, expected: int | None, finalized: bool) -> int:
    """Rewrite ``runs_dim`` from the current ingest log; returns the ingested shard count."""
    ingested = _ingested_shards(store.shard_logs(ctx.repo_id, ctx.run_id), expected)
    store.write_table(
        ctx.repo_id,
        ctx.run_id,
        "runs_dim",
        pa.table(
            {
                "head_sha": [ctx.head_sha],
                "base_sha": [ctx.base_sha],
                "branch": [ctx.branch],
                "pr_number": [ctx.pr_number],
                "created_at": [ctx.created_at],
                "scoring_basis_run_id": [ctx.scoring_basis_run_id],
                "expected_shards": [expected],
                "ingested_shards": [ingested],
                "coverage_complete": [finalized and expected is not None and ingested >= expected],
                "finalized_at": [datetime.now(UTC) if finalized else None],
            }
        ),
    )
    return ingested


def _unfinalize(store: FactsStore, ctx: RunContext) -> None:
    """Drop the run aggregates and mark ``runs_dim`` not finalized, keeping the expected shard count."""
    for name in _AGGREGATE_TABLES:
        store.delete_table(ctx.repo_id, ctx.run_id, name)
    existing = store.read_table(ctx.repo_id, ctx.run_id, "runs_dim")
    expected = existing.column("expected_shards")[0].as_py() if existing.num_rows else None
    _write_runs_dim(store, ctx, expected=expected, finalized=False)


def _next_attempt(store: FactsStore, ctx: RunContext, shard_id: int) -> int:
    """One past every attempt the shard has logged or left files for (a crashed attempt included)."""
    logged = [attempt for attempt, _ in store.shard_logs(ctx.repo_id, ctx.run_id).get(shard_id, ())]
    on_disk = store.attempt_files(ctx.repo_id, ctx.run_id, shard_id)
    return max([0, *logged, *on_disk]) + 1


def _covered_elsewhere(store: FactsStore, ctx: RunContext, shard_id: int) -> set[str]:
    """Nodeids whose coverage another shard of the run already recorded."""
    rows = store.read_table(ctx.repo_id, ctx.run_id, "tests_dim").select(["nodeid", "shard_id", "has_lines"])
    return {
        row["nodeid"]
        for row in rows.to_pylist()
        if row["shard_id"] != shard_id and row["has_lines"] and row["nodeid"] != IMPORT_CONTEXT
    }


def ingest_shard(
    store: FactsStore,
    ctx: RunContext,
    bundle: NeutralBundle,
    shard_id: int,
    functions: Sequence[FunctionRegion] = (),
    *,
    language: str = "python",
    bundle_sha256: str | None = None,
) -> IngestResult:
    """Write one shard bundle's facts as one unit, replacing any earlier bundle for the same shard.

    ``functions`` are the head commit's function regions (from the language adapter); when given
    they are written as the run's ``functions`` table and used to key shape observations. The
    bundle's identity is ``bundle_sha256``, else ``meta['convert']['bundle_sha256']`` from
    :func:`~coverset.facts.convert.read_python_bundle`, else a digest of the bundle's content.

    A bundle whose values do not fit the facts schema raises :class:`BundleError` before anything
    is written or any id is assigned, and is logged as a ``failed`` attempt. Any run-level
    aggregates are invalidated until :func:`finalize_run` runs again.
    """
    sha = bundle_sha256 or _bundle_sha(bundle)
    with store.run_lock(ctx.repo_id, ctx.run_id):
        history = store.read_table(ctx.repo_id, ctx.run_id, "ingest_log", shard_id=shard_id).to_pylist()
        latest = max(history, key=lambda row: row["attempt"]) if history else None
        if latest is not None and latest["bundle_sha256"] == sha and latest["status"] == "ok":
            return IngestResult(ctx.run_id, shard_id, latest["attempt"], sha, skipped=True, rows={})
        attempt = _next_attempt(store, ctx, shard_id)
        try:
            _validate(bundle)
            prepared = _prepare(store, ctx, bundle, shard_id, functions, language)
        except BundleError as exc:
            _append_log(store, ctx, shard_id, attempt, sha, "failed", {"error": str(exc)}, None, None)
            _unfinalize(store, ctx)
            raise
        tables, resolver, skip, n_lines = prepared

        _unfinalize(store, ctx)
        for name, table in tables.items():
            if name != "functions":
                store.write_table(ctx.repo_id, ctx.run_id, name, table, shard_id=shard_id, attempt=attempt)
        if "functions" in tables:
            store.write_table(ctx.repo_id, ctx.run_id, "functions", tables["functions"])
        convert = bundle.meta.get("convert", {}) if isinstance(bundle.meta, dict) else {}
        details = {**convert, "unmatched_functions": len(resolver.unmatched), "duplicate_tests": len(skip)}
        _append_log(store, ctx, shard_id, attempt, sha, "ok", details, len(bundle.tests), n_lines)
        _unfinalize(store, ctx)
        for old, paths in store.attempt_files(ctx.repo_id, ctx.run_id, shard_id).items():
            if old != attempt:
                for path in paths:
                    path.unlink(missing_ok=True)
    return IngestResult(
        run_id=ctx.run_id,
        shard_id=shard_id,
        attempt=attempt,
        bundle_sha256=sha,
        skipped=False,
        rows={name: table.num_rows for name, table in tables.items()},
        unmatched_functions=len(resolver.unmatched),
        duplicate_tests=len(skip),
    )


def _prepare(
    store: FactsStore,
    ctx: RunContext,
    bundle: NeutralBundle,
    shard_id: int,
    functions: Sequence[FunctionRegion],
    language: str,
) -> tuple[dict[str, pa.Table], _FuncResolver, set[str], int]:
    """Assign ids and build every (conformed) table of the shard, plus the run's ``functions``."""
    results = {row.nodeid: row.file for row in bundle.tests}
    test_files = {n: _test_file(n, results) for n in {*results, *bundle.coverage}}
    ids = RepoDictionary.extend(
        store.repo_dir(ctx.repo_id),
        paths={
            *bundle.static,
            *bundle.run_arcs,
            *(p for by_path in bundle.coverage.values() for p in by_path),
            *(f for f in test_files.values() if f),
            *(r.path for r in functions),
        },
        nodeids={
            *test_files,
            *(t for obs in bundle.shapes for t in obs.tests),
            *(obs.example_test for obs in bundle.shapes if obs.example_test),
            *(obs.caller for obs in bundle.shapes if isinstance(obs.caller, str)),
            *(t for s in bundle.sampling for t in s.tests),
        },
        shapes={obs.shape_key for obs in bundle.shapes},
    )
    skip = _covered_elsewhere(store, ctx, shard_id) & bundle.coverage.keys()
    resolver = _FuncResolver(functions)
    index = _LineIndex(bundle, ids, skip)
    tables = _shard_rows(ctx, shard_id, bundle, ids, resolver, index, test_files, skip)
    if functions:
        tables["functions"] = _functions_table(ctx, functions, ids, language)
    return _conform_all(tables), resolver, skip, int(index.line.size)


def record_shard_failure(
    store: FactsStore, ctx: RunContext, shard_id: int, *, reason: str, bundle_sha256: str | None = None
) -> int:
    """Log a shard whose bundle could not be read (e.g. :func:`read_python_bundle` raised).

    The shard's committed rows, if any, stay visible but the shard no longer counts as ingested,
    so the run is reported as partial until a good bundle for the shard arrives. Returns the
    attempt number.
    """
    with store.run_lock(ctx.repo_id, ctx.run_id):
        attempt = _next_attempt(store, ctx, shard_id)
        _append_log(store, ctx, shard_id, attempt, bundle_sha256, "failed", {"error": reason}, None, None)
        _unfinalize(store, ctx)
    return attempt


_FILE_COV_SQL = """
WITH st AS (
    SELECT file_id, any_value(statements) AS statements FROM file_static GROUP BY file_id
), stl AS (
    SELECT file_id, unnest(statements) AS line FROM st
), cov AS (
    SELECT DISTINCT file_id, line FROM test_lines
), ln AS (
    SELECT stl.file_id, count(*) AS statements, count(cov.line) AS covered
    FROM stl LEFT JOIN cov ON cov.file_id = stl.file_id AND cov.line = stl.line
    GROUP BY stl.file_id
), ap AS (
    SELECT DISTINCT file_id, from_line, to_line FROM file_arcs_possible
), ra AS (
    SELECT DISTINCT file_id, from_line, to_line FROM run_arcs
), exits AS (
    SELECT ap.file_id, ap.from_line, count(*) AS n_exits, count(ra.to_line) AS n_taken
    FROM ap LEFT JOIN ra
      ON ra.file_id = ap.file_id AND ra.from_line = ap.from_line AND ra.to_line = ap.to_line
    GROUP BY ap.file_id, ap.from_line
), br AS (
    SELECT e.file_id, sum(e.n_exits) AS branches, sum(e.n_taken) AS branches_covered,
           count(*) FILTER (WHERE e.n_taken < e.n_exits AND cov.line IS NOT NULL) AS partial_branches
    FROM exits e LEFT JOIN cov ON cov.file_id = e.file_id AND cov.line = e.from_line
    GROUP BY e.file_id
), paths AS (
    SELECT file_id, any_value(path) AS path FROM files GROUP BY file_id
)
SELECT st.file_id, paths.path,
       coalesce(ln.statements, 0) AS statements, coalesce(ln.covered, 0) AS covered,
       coalesce(br.branches, 0) AS branches, coalesce(br.branches_covered, 0) AS branches_covered,
       coalesce(br.partial_branches, 0) AS partial_branches,
       CASE WHEN coalesce(ln.statements, 0) + coalesce(br.branches, 0) = 0 THEN 100.0
            ELSE 100.0 * (coalesce(ln.covered, 0) + coalesce(br.branches_covered, 0))
                 / (coalesce(ln.statements, 0) + coalesce(br.branches, 0)) END AS percent
FROM st JOIN paths USING (file_id) LEFT JOIN ln USING (file_id) LEFT JOIN br USING (file_id)
ORDER BY paths.path
"""

_FUNC_CALLED_SQL = f"""
WITH fn AS (
    SELECT DISTINCT func_key, file_id, body_start, end_line FROM functions
    WHERE kind IN {CALLABLE_KINDS!r}
), called AS (
    SELECT fn.func_key, count(DISTINCT tl.test_id) AS tests_called
    FROM fn JOIN test_lines tl
      ON tl.file_id = fn.file_id AND tl.line BETWEEN fn.body_start AND fn.end_line
    WHERE tl.test_id <> 0
    GROUP BY fn.func_key
), samp AS (
    SELECT func_key, sum(tests_truncated) AS tests_truncated,
           len(list_distinct(flatten(list(test_ids)))) AS tests_sampled
    FROM func_sampling GROUP BY func_key
), keys AS (
    SELECT func_key FROM fn UNION SELECT func_key FROM samp
)
SELECT keys.func_key, coalesce(called.tests_called, 0) AS tests_called,
       coalesce(samp.tests_sampled, 0) AS tests_sampled,
       coalesce(samp.tests_truncated, 0) AS tests_truncated
FROM keys LEFT JOIN called USING (func_key) LEFT JOIN samp USING (func_key)
ORDER BY keys.func_key
"""


def _observations(store: FactsStore, ctx: RunContext, ids: RepoDictionary) -> dict[str, list[ShapeObs]]:
    triples: dict[str, FuncTriple] = {}
    for row in store.read_table(ctx.repo_id, ctx.run_id, "func_sampling").to_pylist():
        triples[row["func_key"]] = (row["path"], row["qualname"], row["firstlineno"])
    for row in store.read_table(ctx.repo_id, ctx.run_id, "functions").to_pylist():
        triples[row["func_key"]] = (row["path"], row["qualname"], row["def_line"])
    by_func: dict[str, list[ShapeObs]] = defaultdict(list)
    for row in store.read_table(ctx.repo_id, ctx.run_id, "call_shapes").to_pylist():
        triple = triples.get(row["func_key"])
        if triple is None:
            continue
        caller: FuncTriple | str = (
            row["caller_key"]
            if row["caller_kind"] == "test"
            else (row["caller_path"], row["caller_qualname"], row["caller_line"])
        )
        example_test = row["example_test_id"]
        by_func[row["func_key"]].append(
            ShapeObs(
                func=triple,
                param=row["param"],
                shape_key=ids.shape_key(row["shape_id"]),
                caller=caller,
                samples=row["samples"],
                tests=tuple(ids.nodeid(t) for t in row["test_ids"]),
                truncated=row["truncated"],
                example=row["example_repr"],
                example_test=ids.nodeid(example_test) if example_test is not None else None,
            )
        )
    return by_func


def _input_cov_columns(
    store: FactsStore, ctx: RunContext, func_keys: list[str], catalogue: Catalogue | None
) -> dict[str, list]:
    columns: dict[str, list] = {"blocks_expected": [], "blocks_observed": [], "missing_json": []}
    observations = _observations(store, ctx, store.dictionary(ctx.repo_id)) if catalogue else {}
    for key in func_keys:
        obs = observations.get(key)
        if not obs or catalogue is None:
            for column in columns.values():
                column.append(None)
            continue
        per_param = catalogue(obs)
        columns["blocks_expected"].append(sum(len(c.expected) for c in per_param.values()))
        columns["blocks_observed"].append(sum(c.observed_expected for c in per_param.values()))
        missing = {param: list(c.missing) for param, c in sorted(per_param.items()) if c.missing}
        columns["missing_json"].append(json.dumps(missing, sort_keys=True))
    return columns


def finalize_run(
    store: FactsStore, ctx: RunContext, *, expected_shards: int, catalogue: Catalogue | None = None
) -> FinalizeResult:
    """Compute ``file_cov`` and ``func_input_cov`` and record completeness in ``runs_dim``.

    Shards are numbered ``0 .. expected_shards - 1``; ``coverage_complete`` is true only when each
    of them has an ``ok`` latest attempt (extra shard ids, e.g. a re-run under a new id, neither
    count nor block completeness). Without a ``catalogue`` the input-class columns of
    ``func_input_cov`` stay null and only ``tests_called``, ``tests_sampled`` and
    ``tests_truncated`` are filled.
    """
    with store.run_lock(ctx.repo_id, ctx.run_id):
        con = store.connect(repo_id=ctx.repo_id, run_id=ctx.run_id)
        try:
            file_cov = con.execute(_FILE_COV_SQL).to_arrow_table()
            called = con.execute(_FUNC_CALLED_SQL).to_arrow_table()
        finally:
            con.close()
        store.write_table(ctx.repo_id, ctx.run_id, "file_cov", file_cov)
        func_keys = called.column("func_key").to_pylist()
        input_cov = called
        for name, values in _input_cov_columns(store, ctx, func_keys, catalogue).items():
            input_cov = input_cov.append_column(
                name, pa.array(values, type=SCHEMAS["func_input_cov"].field(name).type)
            )
        store.write_table(ctx.repo_id, ctx.run_id, "func_input_cov", input_cov)
        ingested = _write_runs_dim(store, ctx, expected=expected_shards, finalized=True)
    return FinalizeResult(
        run_id=ctx.run_id,
        expected_shards=expected_shards,
        ingested_shards=ingested,
        coverage_complete=ingested >= expected_shards,
        files=file_cov.num_rows,
        functions=input_cov.num_rows,
    )
