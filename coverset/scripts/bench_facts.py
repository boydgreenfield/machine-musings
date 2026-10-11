"""Benchmark the facts store end to end on synthetic shard bundles.

Generates one real pytest-coverset-style bundle per shard (``coverage.sqlite`` written through
coverage.py's own ``CoverageData`` with one context per test and branch arcs, ``coverage.json``
format 3, ``results.jsonl``, ``collect.json``, ``durations.json``, ``shapes.jsonl.gz`` per
Addendum B, ``meta.json``), then for each of ``--timed-runs`` runs times the real path:

- convert: :func:`coverset.facts.convert.read_python_bundle` for every shard,
- ingest: :func:`coverset.facts.ingest.ingest_shard` for every shard,
- finalize: :func:`coverset.facts.ingest.finalize_run`,

all serial on one core (the orchestrator can convert and ingest shards in parallel). The rest of
the ``--runs`` history is filled with copies of the last run's ``runs_dim``, ``ingest_log`` and
``call_shapes`` (the only tables a cross-run query reads), so the shapes query sees the full
history. Then it measures p50/p95 latency, each including its per-request DuckDB connection, of
``line_tests``, a 500-line ``patch_coverage`` and ``treemap`` on the newest run and of
``shapes_for_function`` over the last 5 runs.

Defaults follow the plan: 2,000 files, 200,000 statement lines, 10,000 tests x 2,000 lines per test
(20M test_lines rows per run) in 8 shards, 30 runs, 1,000,000 call_shapes rows over all runs.
``--quick`` divides files, statements, tests and call-shape rows by 20, lines per test by 10, uses
2 shards and divides runs by 5. Prints JSON; ``--out docs/numbers.json`` merges the ``facts_*``
keys into that file (other keys are kept).

    uv run python scripts/bench_facts.py --quick
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import coverage
import duckdb
import numpy as np
import pyarrow as pa
from coverage import CoverageData

from coverset.facts.convert import read_python_bundle
from coverset.facts.ingest import RunContext, finalize_run, ingest_shard
from coverset.facts.queries import line_tests, patch_coverage, shapes_for_function, treemap
from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion, func_key

REPO_ID = 1
FUNCS_PER_FILE = 10
MIN_FILES_PER_TEST = 4
SHAPE_KEYS = 200
BRANCH_EVERY = 7
HISTORY_TABLES = ("runs_dim", "ingest_log", "call_shapes")
NUMBERS_KEYS = (
    "facts_convert_s_per_run",
    "facts_ingest_s_per_run",
    "facts_finalize_s_per_run",
    "facts_mb_per_run",
    "facts_line_tests_p50_ms",
    "facts_line_tests_p95_ms",
    "facts_patch_p50_ms",
    "facts_patch_p95_ms",
    "facts_treemap_p50_ms",
    "facts_treemap_p95_ms",
    "facts_shapes_p50_ms",
    "facts_shapes_p95_ms",
    "facts_bench_params",
)


@dataclass(frozen=True, slots=True)
class Params:
    """Size of the synthetic repo, its runs and the query samples."""

    files: int
    lines: int
    tests: int
    lines_per_test: int
    shards: int
    runs: int
    timed_runs: int
    call_shape_rows: int
    query_samples: int
    seed: int
    quick: bool


@dataclass(frozen=True, slots=True)
class Repo:
    """The synthetic repo's static shape, shared by every run."""

    paths: list[str]
    stmt_line: np.ndarray  # statement lines, file after file
    file_start: np.ndarray  # index of each file's first statement in stmt_line
    per_file: int
    functions: list[FunctionRegion]
    nodeids: list[str]


def build_repo(p: Params, rng: np.random.Generator) -> Repo:
    per_file = max(p.lines // p.files, FUNCS_PER_FILE * 3)
    paths = [f"src/pkg{i // 50:03d}/mod{i:05d}.py" for i in range(p.files)]
    gaps = rng.integers(1, 3, size=(p.files, per_file), dtype=np.int64)
    lines_2d = np.cumsum(gaps, axis=1)
    chunk = per_file // FUNCS_PER_FILE
    functions = []
    for f, path in enumerate(paths):
        for i in range(FUNCS_PER_FILE):
            first, last = i * chunk, (i + 1) * chunk - 1
            qualname = f"f{i}"
            functions.append(
                FunctionRegion(
                    func_key=func_key(path, qualname, 0),
                    path=path,
                    qualname=qualname,
                    ordinal=0,
                    def_line=int(lines_2d[f, first]),
                    body_start=int(lines_2d[f, first + 1]),
                    end_line=int(lines_2d[f, last]),
                    body_hash=f"b{f}.{i}",
                    sig_hash=f"s{f}.{i}",
                    params=("a", "b"),
                    kind="function",
                )
            )
    nodeids = [f"tests/test_{t // 20:04d}.py::test_{t:05d}" for t in range(p.tests)]
    return Repo(
        paths=paths,
        stmt_line=lines_2d.ravel(),
        file_start=np.arange(p.files, dtype=np.int64) * per_file,
        per_file=per_file,
        functions=functions,
        nodeids=nodeids,
    )


def _chain(lines: list[int]) -> list[tuple[int, int]]:
    """The arcs coverage.py records for straight-line execution of ``lines`` in one code object."""
    return [(-lines[0], lines[0]), *zip(lines, lines[1:], strict=False), (lines[-1], -lines[0])]


def _coverage_json(repo: Repo, executed: np.ndarray) -> dict:
    files = {}
    for f, path in enumerate(repo.paths):
        start = int(repo.file_start[f])
        lines = repo.stmt_line[start : start + repo.per_file]
        hit = executed[start : start + repo.per_file]
        branch_index = np.arange(0, repo.per_file - 2, BRANCH_EVERY)
        taken = [[int(lines[i]), int(lines[i + 1])] for i in branch_index if hit[i]]
        missed = [[int(lines[i]), int(lines[i + 2])] for i in branch_index]
        missed += [[int(lines[i]), int(lines[i + 1])] for i in branch_index if not hit[i]]
        files[path] = {
            "executed_lines": lines[hit].tolist(),
            "missing_lines": lines[~hit].tolist(),
            "excluded_lines": [],
            "executed_branches": taken,
            "missing_branches": missed,
        }
    return {"meta": {"format": 3, "version": coverage.__version__}, "files": files}


def write_shard(p: Params, repo: Repo, shard: int, dest: Path, rng: np.random.Generator) -> int:
    """Write one shard's bundle directory; returns its number of (test, line) cells."""
    dest.mkdir(parents=True)
    tests = [t for t in range(p.tests) if t % p.shards == shard]
    # Each test runs a window of consecutive statements in each of several files, using as many
    # files as it takes to reach ``lines_per_test`` (a window never exceeds one file).
    files_per_test = min(max(MIN_FILES_PER_TEST, -(-p.lines_per_test // repo.per_file)), len(repo.paths))
    window = max(min(p.lines_per_test // files_per_test, repo.per_file), 2)
    data = CoverageData(basename=str(dest / "coverage.sqlite"))
    data.set_context("")
    data.add_arcs(
        {
            path: _chain([r.def_line for r in repo.functions[f * FUNCS_PER_FILE : (f + 1) * FUNCS_PER_FILE]])
            for f, path in enumerate(repo.paths)
        }
    )
    executed = np.zeros(repo.stmt_line.size, dtype=bool)
    cells = 0
    for t in tests:
        data.set_context(repo.nodeids[t])
        arcs = {}
        for f in rng.choice(len(repo.paths), size=files_per_test, replace=False).tolist():
            offset = int(rng.integers(0, max(repo.per_file - window, 1)))
            start = int(repo.file_start[f]) + offset
            stop = min(start + window, int(repo.file_start[f]) + repo.per_file)
            executed[start:stop] = True
            arcs[repo.paths[f]] = _chain(repo.stmt_line[start:stop].tolist())
            cells += stop - start
        data.add_arcs(arcs)
    data.write()
    (dest / "coverage.json").write_text(json.dumps(_coverage_json(repo, executed)))
    results = [
        {"nodeid": repo.nodeids[t], "file": repo.nodeids[t].split("::")[0], "outcome": "passed"}
        | {"duration_s": float(d), "failed_phase": None, "fail_sig": None, "longrepr": None}
        for t, d in zip(tests, rng.lognormal(-3, 1, len(tests)), strict=True)
    ]
    (dest / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results))
    (dest / "durations.json").write_text(json.dumps({r["nodeid"]: r["duration_s"] for r in results}))
    items = [
        {"nodeid": repo.nodeids[t], "file": repo.nodeids[t].split("::")[0], "line": 1, "markers": []}
        | {"invariant_ids": [], "hypothesis": False}
        for t in tests
    ]
    (dest / "collect.json").write_text(json.dumps({"items": items, "errors": []}))
    (dest / "meta.json").write_text(json.dumps({"generator": "scripts/bench_facts.py", "tracer": "CTracer"}))
    _write_shapes(p, repo, tests, dest, rng)
    return cells


def _write_shapes(p: Params, repo: Repo, tests: list[int], dest: Path, rng: np.random.Generator) -> None:
    n_obs = max(p.call_shape_rows // (p.runs * p.shards), 1)
    picked = rng.integers(0, len(repo.functions), n_obs)
    funcs, func_index = np.unique(picked, return_inverse=True)
    callers = rng.integers(0, len(tests), n_obs)
    header = {
        "kind": "header",
        "version": 1,
        "funcs": [list(repo.functions[i].triple) for i in funcs.tolist()],
        "shapes": [f"int|k{i:03d}" for i in range(SHAPE_KEYS)],
        "tests": [repo.nodeids[t] for t in tests],
        "callers": [{"kind": "test", "nodeid": repo.nodeids[t]} for t in tests],
    }
    params = ("a", "b", "return")
    with gzip.open(dest / "shapes.jsonl.gz", "wt") as out:
        out.write(json.dumps(header) + "\n")
        for f, c, s, param, n in zip(
            func_index.tolist(),
            callers.tolist(),
            rng.integers(0, SHAPE_KEYS, n_obs).tolist(),
            rng.integers(0, len(params), n_obs).tolist(),
            rng.integers(1, 50, n_obs).tolist(),
            strict=True,
        ):
            row = {"kind": "obs", "f": f, "p": params[param], "s": s, "c": c, "n": n, "t": [c]}
            out.write(json.dumps(row | {"trunc": False, "ex": "1", "ext": c}) + "\n")
        for f in range(funcs.size):
            out.write(json.dumps({"kind": "func_sampling", "f": f, "calls": 64, "tests": [0]}) + "\n")


def timed(fn: Callable[[], object], samples: int) -> list[float]:
    out = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def p50_p95(ms: list[float]) -> tuple[float, float]:
    ordered = sorted(ms)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return round(statistics.median(ordered), 2), round(p95, 2)


def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _context(run: int, base: datetime) -> RunContext:
    return RunContext(
        REPO_ID, f"bench-{run:03d}", "0" * 40, None, "main", None, base + timedelta(hours=run), None
    )


def fill_history(store: FactsStore, source: RunContext, runs: range, base: datetime) -> None:
    """Give ``runs`` copies of ``source``'s cross-run tables, each with its own ``runs_dim``."""
    template = store.read_table(REPO_ID, source.run_id, "runs_dim")
    for run in runs:
        ctx = _context(run, base)
        target = store.run_dir(REPO_ID, ctx.run_id)
        target.mkdir(parents=True)
        for table in HISTORY_TABLES[1:]:
            for path in store.table_files(REPO_ID, source.run_id, table):
                shutil.copyfile(path, target / path.name)
        store.write_table(
            REPO_ID,
            ctx.run_id,
            "runs_dim",
            template.set_column(
                template.schema.get_field_index("created_at"), "created_at", pa.array([ctx.created_at])
            ),
        )


def bench(p: Params, work: Path) -> dict[str, object]:
    rng = np.random.default_rng(p.seed)
    repo = build_repo(p, rng)
    start = time.perf_counter()
    cells = [write_shard(p, repo, s, work / "bundles" / f"shard-{s}", rng) for s in range(p.shards)]
    generate_s = time.perf_counter() - start
    bundle_mb = dir_bytes(work / "bundles") / 1e6
    store = FactsStore(work / "facts")
    base = datetime(2026, 10, 1, tzinfo=UTC)
    convert_s, ingest_s, finalize_s, run_mb = [], [], [], []
    for run in range(p.timed_runs):
        ctx = _context(run, base)
        t_convert = t_ingest = 0.0
        for shard in range(p.shards):
            t0 = time.perf_counter()
            bundle = read_python_bundle(work / "bundles" / f"shard-{shard}")
            t1 = time.perf_counter()
            ingest_shard(store, ctx, bundle, shard, repo.functions)
            t_convert += t1 - t0
            t_ingest += time.perf_counter() - t1
            del bundle
        t0 = time.perf_counter()
        finalize_run(store, ctx, expected_shards=p.shards)
        finalize_s.append(time.perf_counter() - t0)
        convert_s.append(t_convert)
        ingest_s.append(t_ingest)
        run_mb.append(dir_bytes(store.run_dir(REPO_ID, ctx.run_id)) / 1e6)
    latest = _context(p.timed_runs - 1, base)
    fill_history(store, latest, range(p.timed_runs, p.runs), base)
    newest = _context(p.runs - 1, base).run_id if p.runs > p.timed_runs else latest.run_id
    picks = itertools.cycle(rng.integers(0, repo.stmt_line.size, p.query_samples).tolist())
    funcs = itertools.cycle(rng.integers(0, len(repo.functions), p.query_samples).tolist())

    def one_line_tests() -> None:
        k = next(picks)
        line_tests(store, REPO_ID, latest.run_id, repo.paths[k // repo.per_file], int(repo.stmt_line[k]))

    def one_patch() -> None:
        changed: dict[str, set[int]] = {}
        for f in rng.choice(len(repo.paths), size=5, replace=False).tolist():
            s = int(repo.file_start[f])
            changed[repo.paths[f]] = set(repo.stmt_line[s : s + 100].tolist())
        patch_coverage(store, REPO_ID, latest.run_id, changed)

    def one_shapes() -> None:
        shapes_for_function(store, REPO_ID, repo.functions[next(funcs)].func_key, last_n_runs=5)

    line_p50, line_p95 = p50_p95(timed(one_line_tests, p.query_samples))
    patch_p50, patch_p95 = p50_p95(timed(one_patch, max(p.query_samples // 2, 5)))
    tree_p50, tree_p95 = p50_p95(
        timed(lambda: treemap(store, REPO_ID, latest.run_id), max(p.query_samples // 5, 5))
    )
    shapes_p50, shapes_p95 = p50_p95(timed(one_shapes, p.query_samples))
    test_lines_rows = (
        store.read_table(REPO_ID, latest.run_id, "ingest_log").column("n_test_lines").to_pylist()
    )
    return {
        "facts_convert_s_per_run": round(statistics.mean(convert_s), 2),
        "facts_ingest_s_per_run": round(statistics.mean(ingest_s), 2),
        "facts_finalize_s_per_run": round(statistics.mean(finalize_s), 2),
        "facts_mb_per_run": round(statistics.mean(run_mb), 1),
        "facts_line_tests_p50_ms": line_p50,
        "facts_line_tests_p95_ms": line_p95,
        "facts_patch_p50_ms": patch_p50,
        "facts_patch_p95_ms": patch_p95,
        "facts_treemap_p50_ms": tree_p50,
        "facts_treemap_p95_ms": tree_p95,
        "facts_shapes_p50_ms": shapes_p50,
        "facts_shapes_p95_ms": shapes_p95,
        "facts_bench_params": {
            **asdict(p),
            "what": {
                "convert": "read_python_bundle over every shard of a run, serial",
                "ingest": "ingest_shard over every shard of a run, serial",
                "finalize": "finalize_run (file_cov + func_input_cov, no catalogue)",
                "mb_per_run": "Parquet bytes of one fully ingested run directory",
                "queries": "per call incl. its DuckDB connection; line/patch/treemap on the newest "
                "fully ingested run, shapes over the last 5 runs",
                "history": f"runs after the first {p.timed_runs} hold copies of {', '.join(HISTORY_TABLES)}",
            },
            "test_lines_rows_per_run": int(sum(test_lines_rows)),
            "cells_generated_per_run": int(sum(cells)),
            "bundle_mb_per_run": round(bundle_mb, 1),
            "generate_s": round(generate_s, 1),
            "newest_history_run": newest,
            "patch_changed_lines": 500,
            "date": datetime.now(UTC).date().isoformat(),
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "pyarrow": pa.__version__,
            "coverage": coverage.__version__,
            "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
            "synthetic": True,
        },
    }


def merge_numbers(path: Path, results: dict[str, object]) -> None:
    """Merge the ``facts_*`` keys of ``results`` into the JSON object at ``path``."""
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update({key: results[key] for key in NUMBERS_KEYS})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> tuple[Params, Path | None, Path | None]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=2_000)
    parser.add_argument("--lines", type=int, default=200_000, help="statement lines in the repo")
    parser.add_argument("--tests", type=int, default=10_000)
    parser.add_argument("--lines-per-test", type=int, default=2_000)
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--timed-runs", type=int, default=2, help="runs converted and ingested for real")
    parser.add_argument("--call-shapes", type=int, default=1_000_000, help="call_shapes rows over all runs")
    parser.add_argument("--samples", type=int, default=50, help="line_tests and shapes queries to time")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--quick", action="store_true", help="a small run for CI and smoke tests")
    parser.add_argument("--data-dir", type=Path, help="keep bundles and the store here (default: temp dir)")
    parser.add_argument("--out", type=Path, help="merge the facts_* numbers into this JSON file")
    args = parser.parse_args(argv)
    scale, line_scale, run_scale = (20, 10, 5) if args.quick else (1, 1, 1)
    runs = max(args.runs // run_scale, 1)
    params = Params(
        files=max(args.files // scale, 1),
        lines=max(args.lines // scale, 1),
        tests=max(args.tests // scale, 1),
        lines_per_test=max(args.lines_per_test // line_scale, 2),
        shards=2 if args.quick else max(args.shards, 1),
        runs=runs,
        timed_runs=min(max(args.timed_runs, 1), runs),
        call_shape_rows=max(args.call_shapes // scale, 1),
        query_samples=args.samples,
        seed=args.seed,
        quick=args.quick,
    )
    return params, args.data_dir, args.out


def main(argv: list[str] | None = None) -> int:
    params, data_dir, out = parse_args(argv)
    if data_dir is not None:
        results = bench(params, data_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="coverset-bench-facts-") as tmp:
            results = bench(params, Path(tmp))
    print(json.dumps(results, indent=2))
    if out is not None:
        merge_numbers(out, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
