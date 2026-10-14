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
import asyncio
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
    "facts_treemap_memo_p50_ms",
    "facts_treemap_memo_p95_ms",
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
    archives = pack_shards(work, p.shards)
    end_to_end = {
        kind: ingest_archives_in_child(work / "facts", f"e2e-{kind}", paths, repo.functions)
        for kind, paths in archives.items()
    }
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

    def one_treemap_uncached() -> None:
        from coverset.facts.cache import RESULTS

        RESULTS.clear()  # the treemap is memoised per run stamp: time the query itself
        treemap(store, REPO_ID, latest.run_id)

    tree_p50, tree_p95 = p50_p95(timed(one_treemap_uncached, max(p.query_samples // 5, 5)))
    tree_hit_p50, tree_hit_p95 = p50_p95(
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
        "facts_treemap_memo_p50_ms": tree_hit_p50,
        "facts_treemap_memo_p95_ms": tree_hit_p95,
        "facts_shapes_p50_ms": shapes_p50,
        "facts_shapes_p95_ms": shapes_p95,
        "facts_end_to_end": end_to_end,
        "facts_bench_params": {
            **asdict(p),
            "what": {
                "convert": "read_python_bundle over every shard of a run, serial",
                "ingest": "ingest_shard over every shard of a run, serial",
                "finalize": "finalize_run (file_cov + func_input_cov, no catalogue)",
                "mb_per_run": "Parquet bytes of one fully ingested run directory",
                "queries": "per call through a leased (cached) DuckDB connection; line/patch/treemap "
                "on the newest fully ingested run (treemap with its result memo cleared each call, "
                "treemap_memo a memo hit), shapes over the last 5 runs",
                "end_to_end": "archives (python bundle.tar.gz, neutral tar.gz) through extract, "
                "read_any_bundle (strict limits), ingest_shard and finalize_run in a fresh process",
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


# -- real repository mode ------------------------------------------------------------------------
#
# ``--real DIR --repo-root GIT`` ingests a pytest-coverset output directory (``bundle.tar.gz`` plus
# ``meta.json``, as ``pytest --coverset`` writes it) into a scratch control plane the way the runner
# does (extract the archive, convert, index the head sources, ingest, finalize), then serves the UI
# from it in-process and times every page with the Quart test client. The source repository is only
# read: its mirror is a ``git clone --mirror`` under the data directory.


def rss_mb() -> float:
    """The resident set of this process now, in MB (``ps``; no psutil dependency)."""
    import subprocess

    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True)
    return round(int(out.stdout.strip() or 0) / 1024, 1)


def footprint_mb() -> float | None:
    """The process's physical footprint in MB on macOS (``proc_pid_rusage``), else None.

    macOS counts pages that malloc freed but has not yet handed back (``MADV_FREE``) in the resident
    set until the system needs them, so ``ps`` RSS keeps climbing over a loop that frees everything
    it allocates (reading and sorting one ``test_lines`` 24 times: RSS 189 -> 504 MB, footprint
    109 -> 151 MB). The footprint leaves those pages out: it is what Activity Monitor shows and
    what the system's memory pressure acts on.
    """
    if sys.platform != "darwin":
        return None
    import ctypes

    names = [
        "user_time",
        "system_time",
        "pkg_idle_wkups",
        "interrupt_wkups",
        "pageins",
        "wired_size",
        "resident_size",
        "phys_footprint",
        "proc_start_abstime",
        "proc_exit_abstime",
        "child_user_time",
        "child_system_time",
        "child_pkg_idle_wkups",
        "child_interrupt_wkups",
        "child_pageins",
        "child_elapsed_abstime",
        "diskio_bytesread",
        "diskio_byteswritten",
    ]

    class RUsageInfoV2(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16), *((name, ctypes.c_uint64) for name in names)]

    info = RUsageInfoV2()
    try:
        if ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) != 0:
            return None
    except OSError:
        return None
    return round(info.phys_footprint / 2**20, 1)


def peak_rss_mb() -> float:
    """The peak resident set of this process so far, in MB."""
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def table_sizes(store: FactsStore, run_id: str) -> dict[str, dict[str, int]]:
    """Visible Parquet bytes and rows per table of one run."""
    import pyarrow.parquet as pq

    from coverset.facts.schema import TABLES

    out: dict[str, dict[str, int]] = {}
    for table in TABLES:
        files = store.table_files(REPO_ID, run_id, table)
        if not files:
            continue
        out[table] = {
            "bytes": sum(f.stat().st_size for f in files),
            "rows": sum(pq.ParquetFile(f).metadata.num_rows for f in files),
        }
    return out


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def ingest_real(
    store: FactsStore, ctx: RunContext, tarball: Path, repo_root: Path, work: Path
) -> tuple[dict[str, object], object]:
    """Extract, convert, index, ingest and finalize one real bundle as one shard; timings and RSS.

    Mirrors :func:`coverset.orchestrator.bundles.ingest_one` plus the runner's ``finalize_run`` with
    the catalogue of the measured sources, under :meth:`BundleLimits.local` (the bundle is the
    user's own and exceeds the strict caps). Returns the timings and the bundle (for DB rows).
    """
    from coverset.facts.untrusted import BundleLimits, extract_bundle
    from coverset.lang import read_any_bundle
    from coverset.lang.python import PythonAdapter

    limits = BundleLimits.local()
    head_paths = frozenset(_git(repo_root, "ls-files", "-z").split("\0")) - {""}
    timings: dict[str, object] = {"rss_mb_start": rss_mb()}
    t0 = time.perf_counter()
    target = work / f"extract-{ctx.run_id}"
    extract_bundle(tarball, target, limits)
    t1 = time.perf_counter()
    inner = target
    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
    bundle = read_any_bundle(inner, limits=limits, source_root=repo_root, head_paths=head_paths)
    t2 = time.perf_counter()
    timings["rss_mb_after_convert"] = rss_mb()
    adapter = PythonAdapter()
    sources = {
        path: (repo_root / path).read_text(encoding="utf-8", errors="replace")
        for path in bundle.static
        if path.endswith(".py") and (repo_root / path).is_file()
    }
    functions = [r for path, text in sources.items() for r in adapter.index_functions(path, text)]
    t3 = time.perf_counter()
    ingest_shard(store, ctx, bundle, 0, functions, language="python")
    t4 = time.perf_counter()
    timings["rss_mb_after_ingest"] = rss_mb()
    catalogue = adapter.shape_catalogue(sources)
    finalize_run(store, ctx, expected_shards=1, catalogue=catalogue)
    t5 = time.perf_counter()
    shutil.rmtree(target, ignore_errors=True)
    timings.update(
        extract_s=round(t1 - t0, 2),
        convert_s=round(t2 - t1, 2),
        index_s=round(t3 - t2, 2),
        ingest_s=round(t4 - t3, 2),
        finalize_s=round(t5 - t4, 2),
        total_s=round(t5 - t0, 2),
        rss_mb_after_finalize=rss_mb(),
        peak_rss_mb=peak_rss_mb(),
        functions=len(functions),
    )
    return timings, bundle


async def _seed_db(
    settings: object, repo_root: Path, runs: list[dict[str, object]], bundle: object
) -> tuple[int, list[str]]:
    """Register the repo (and its mirror), the runs, the tests and their results; returns ids."""
    from sqlalchemy import select

    from coverset.db.models import PullRequest, Run, TestResult
    from coverset.db.session import Database, init_db
    from coverset.orchestrator import history
    from coverset.orchestrator.repos import ensure_mirror, register_local_repo

    await init_db(settings)  # type: ignore[arg-type]
    db = Database.from_url(settings.database_url)  # type: ignore[attr-defined]
    try:
        async with db.session() as session:
            repo = await register_local_repo(session, repo_root, name="real")
            await session.commit()
            await asyncio.to_thread(ensure_mirror, settings, str(repo_root), repo)
            repo_id = repo.id
            run_ids = []
            for spec in runs:
                pr_id = None
                if spec.get("pr_number") is not None:
                    pr = await session.scalar(
                        select(PullRequest).where(
                            PullRequest.repo_id == repo_id, PullRequest.number == spec["pr_number"]
                        )
                    )
                    if pr is None:
                        pr = PullRequest(
                            repo_id=repo_id,
                            number=spec["pr_number"],
                            head_sha=spec["head_sha"],
                            base_sha=spec["base_sha"],
                            head_ref="bench",
                            base_ref=repo.default_branch,
                            title="Benchmark PR",
                        )
                        session.add(pr)
                        await session.flush()
                    pr_id = pr.id
                run = Run(
                    id=spec["run_id"],
                    repo_id=repo_id,
                    head_sha=spec["head_sha"],
                    base_sha=spec.get("base_sha"),
                    branch=repo.default_branch if pr_id is None else "bench",
                    pr_id=pr_id,
                    trigger="baseline" if pr_id is None else "pull_request",
                    status="completed",
                    verdict="success",
                    coverage_complete=True,
                    shard_count=1,
                    created_at=spec["created_at"],
                    timeline={"queued": spec["created_at"].isoformat()},
                )
                session.add(run)
                await session.flush()
                tests = await history.register_tests(
                    session,
                    repo_id,
                    [t.nodeid for t in bundle.tests],
                    run.id,  # type: ignore[attr-defined]
                )
                for row in bundle.tests:  # type: ignore[attr-defined]
                    session.add(
                        TestResult(
                            run_id=run.id,
                            test_id=tests[row.nodeid].id,
                            outcome=row.outcome,
                            duration_ms=round(row.duration_s * 1000, 3),
                            shard_id=0,
                            attempts=1,
                        )
                    )
                await session.flush()
                await history.update_test_stats(session, run, [t.id for t in tests.values()])
                run_ids.append(run.id)
            await session.commit()
        return repo_id, run_ids
    finally:
        await db.engine.dispose()


async def _page_latencies(
    settings: object, repo_id: int, baseline: str, pr_number: int | None, samples: int, repo_root: Path
) -> dict[str, dict[str, float]]:
    """Cold (first) and p50/p95 server time of every page, through the in-process test client."""
    from urllib.parse import quote

    from coverset.app import create_app
    from coverset.facts.store import FactsStore as Store
    from coverset.web.facts_view import run_facts

    app = create_app(settings)  # type: ignore[arg-type]
    store = Store(settings.facts_dir)  # type: ignore[attr-defined]
    facts = run_facts(store, repo_id, baseline)
    statics = facts.statics
    largest = max(statics.values(), key=lambda s: s.n_lines).path
    callables = facts.callables(largest)
    func = max(callables, key=lambda r: r.end_line - r.body_start) if callables else None
    lf = facts.line_facts(largest)
    busiest = max(lf.tests.items(), key=lambda kv: kv[1])[0] if lf.tests else 1
    test_id_row = None
    out: dict[str, dict[str, float]] = {}
    async with app.test_app():
        client = app.test_client()
        from sqlalchemy import select

        from coverset.db.models import TestCase

        async with app.extensions["db"].session() as session:
            test_id_row = await session.scalar(
                select(TestCase.id).where(TestCase.repo_id == repo_id).order_by(TestCase.id).limit(1)
            )
        r = f"/repos/{repo_id}"
        pages = {
            "dashboard": "/",
            "repo overview": r,
            "map (gap)": f"{r}/map?mode=gap",
            "map (lines)": f"{r}/map?mode=lines",
            "map (inputs)": f"{r}/map?mode=inputs",
            f"source, largest file ({statics[largest].n_lines} lines)": f"{r}/files/{quote(largest)}",
            "line-tests fragment (busiest line)": f"{r}/files/{quote(largest)}/lines/{busiest}/tests",
            "tests page": f"{r}/tests",
            "test page": f"{r}/tests/{test_id_row}",
        }
        if func is not None:
            pages["function page"] = f"{r}/functions/{func.func_key}"
        if pr_number is not None:
            pages["PR page"] = f"{r}/prs/{pr_number}"
        # The three largest pages again as a browser asks for them: gzipped (bytes on the wire).
        gzipped = {"map (gap), gzip": f"{r}/map?mode=gap", "tests page, gzip": f"{r}/tests"}
        gzipped["source, largest file, gzip"] = f"{r}/files/{quote(largest)}"
        pages.update(gzipped)
        for name, url in pages.items():
            headers = {"HX-Request": "true"} if "lines/" in url else {}
            if name in gzipped:  # as the browser sends them on the app's own navigation (BREACH rule)
                headers["Accept-Encoding"] = "gzip"
                headers["Sec-Fetch-Site"] = "same-origin"
            times = []
            for i in range(samples + 1):
                start = time.perf_counter()
                response = await client.get(url, headers=headers or None)
                body = await response.get_data()
                elapsed = (time.perf_counter() - start) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f"{url}: HTTP {response.status_code}")
                times.append(elapsed)
                if i == 0:
                    size = len(body)
            p50, p95 = p50_p95(times[1:])
            out[name] = {"cold_ms": round(times[0], 1), "p50_ms": p50, "p95_ms": p95, "bytes": size}
            print(f"{name:45s} cold {times[0]:8.1f}  p50 {p50:8.1f}  p95 {p95:8.1f}  {size} B", flush=True)
        # The facts SQL path: one ad-hoc query through FactsStore.query (what `coverset facts query`
        # and the agent API run), scoped to the run.
        sql = "SELECT path, statements, covered FROM file_cov ORDER BY covered DESC LIMIT 20"
        times = timed(lambda: store.query(sql, repo_id=repo_id, run_id=baseline), samples + 1)
        p50, p95 = p50_p95(times[1:])
        out["facts SQL (file_cov top 20)"] = {"cold_ms": round(times[0], 1), "p50_ms": p50, "p95_ms": p95}
        from coverset.facts.queries import line_tests

        times = timed(lambda: line_tests(store, repo_id, baseline, largest, busiest), samples + 1)
        p50, p95 = p50_p95(times[1:])
        out["facts line_tests query"] = {"cold_ms": round(times[0], 1), "p50_ms": p50, "p95_ms": p95}
    out["_targets"] = {"largest_file": largest, "function": func.qualname if func else None}  # type: ignore[dict-item]
    return out


def bench_real(
    bundle_dir: Path,
    repo_root: Path,
    work: Path,
    *,
    baselines: int,
    pr_base: str | None,
    samples: int,
    pages_only: bool = False,
) -> dict[str, object]:
    """Ingest a real bundle as ``baselines`` default-branch runs (and one PR run against
    ``pr_base``), then time every page. Returns the measurements as JSON-able data.

    With ``pages_only`` an existing scratch control plane in ``work`` (from an earlier call with
    the same ``baselines`` and ``pr_base``) is measured again without ingesting."""
    from coverset.config import Settings

    tarball = bundle_dir / "bundle.tar.gz"
    if not tarball.is_file():
        raise SystemExit(f"{tarball} not found: pass the pytest --coverset output directory")
    work.mkdir(parents=True, exist_ok=True)
    settings = Settings.for_tests(
        data_dir=work / "data", database_url=f"sqlite+aiosqlite:///{work / 'coverset.db'}"
    )
    store = FactsStore(settings.facts_dir)
    head = _git(repo_root, "rev-parse", "HEAD")
    branch = _git(repo_root, "symbolic-ref", "--short", "HEAD")
    base_time = datetime.now(UTC) - timedelta(days=1)
    specs: list[dict[str, object]] = []
    for i in range(baselines):
        specs.append(
            {"run_id": f"run_bench{i:03d}", "head_sha": head, "created_at": base_time + timedelta(hours=i)}
        )
    pr_number = None
    if pr_base:
        pr_number = 1
        base_sha = _git(repo_root, "rev-parse", pr_base)
        specs.append(
            {
                "run_id": "run_benchpr1",
                "head_sha": head,
                "base_sha": base_sha,
                "pr_number": pr_number,
                "created_at": base_time + timedelta(hours=baselines),
            }
        )
    ingests: list[dict[str, object]] = []
    bundle = None
    if pages_only:
        if not (work / "coverset.db").is_file():
            raise SystemExit(f"--pages-only needs an earlier run's scratch control plane in {work}")
        specs_to_ingest: list[dict[str, object]] = []
    else:
        specs_to_ingest = specs
    for spec in specs_to_ingest:
        ctx = RunContext(
            REPO_ID,
            str(spec["run_id"]),
            head,
            spec.get("base_sha"),  # type: ignore[arg-type]
            branch if spec.get("pr_number") is None else "bench",
            spec.get("pr_number"),  # type: ignore[arg-type]
            spec["created_at"],  # type: ignore[arg-type]
            None,
        )
        timings, bundle = ingest_real(store, ctx, tarball, repo_root, work)
        print(f"ingest {spec['run_id']}: {json.dumps(timings)}", flush=True)
        ingests.append(timings)
        del timings
    if pages_only:
        repo_id, run_ids = REPO_ID, [str(spec["run_id"]) for spec in specs]
    else:
        repo_id, run_ids = asyncio.run(_seed_db(settings, repo_root, specs, bundle))
        assert repo_id == REPO_ID, "the scratch control plane must be fresh (repo id 1)"
    del bundle
    sizes = table_sizes(store, run_ids[0])
    pages = asyncio.run(
        _page_latencies(settings, repo_id, run_ids[baselines - 1], pr_number, samples, repo_root)
    )
    return {
        "ingest": ingests,
        "tables": sizes,
        "run_bytes": dir_bytes(store.run_dir(REPO_ID, run_ids[0])),
        "pages": pages,
        "rss_mb_after_pages": rss_mb(),
        "peak_rss_mb": peak_rss_mb(),
        "date": datetime.now(UTC).date().isoformat(),
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "pyarrow": pa.__version__,
    }


def bench_cache_memory(work: Path, runs: int) -> dict[str, object]:
    """Resident set while ``runs`` distinct runs pass through the web's run-facts cache.

    Needs the scratch control plane of an earlier ``--real`` call in ``work``. Clones its first run
    ``runs`` times under new run ids (new inodes, so new stamps), then warms each through
    :func:`coverset.web.loaders.cached_run_facts` the way the heaviest pages do (line facts and a
    line popover on every measured file, body coverage, shapes, sampling and the input-class view of
    600 functions), recording RSS, live Arrow memory and the cache's own count and estimate after
    each. The clones are deleted afterwards.
    """
    import gc

    from coverset.web import loaders

    store = FactsStore(work / "data" / "facts")
    source = store.list_runs(REPO_ID)
    if not source:
        raise SystemExit(f"--cache-memory needs an earlier --real scratch control plane in {work}")
    clones = [f"run_benchmem{i:03d}" for i in range(runs)]
    for run in clones:
        if not store.run_dir(REPO_ID, run).is_dir():
            shutil.copytree(store.run_dir(REPO_ID, source[0]), store.run_dir(REPO_ID, run))
    loaders.clear_caches()
    gc.collect()
    start, start_footprint = rss_mb(), footprint_mb()
    steps: list[dict[str, float]] = []
    try:
        for i, run in enumerate(clones):
            facts = loaders.cached_run_facts(store, REPO_ID, run)
            for path in facts.statics:
                facts.line_facts(path)
                facts.tests_in_range(path, 1, 5)
            for region in facts.callables()[:600]:
                facts.body_coverage(region)
                facts.observations(region.func_key)
                facts.sampling(region)
                facts.function_view(region, None)
            del facts
            loaders.cached_run_facts(store, REPO_ID, run)  # the next lookup weighs what was loaded
            gc.collect()
            cached, weight = loaders.run_facts_weight()
            step = {
                "runs_seen": i + 1,
                "cached": cached,
                "estimated_mb": round(weight / 2**20, 1),
                "arrow_mb": round(pa.total_allocated_bytes() / 2**20, 1),
                "rss_mb": round(rss_mb(), 1),
                "footprint_mb": footprint_mb() or 0.0,
            }
            steps.append(step)
            print(f"cache memory: {json.dumps(step)}", flush=True)
    finally:
        for run in clones:
            store.delete_run(REPO_ID, run)
    return {
        "rss_mb_start": round(start, 1),
        "footprint_mb_start": start_footprint,
        "entries": loaders.RUN_FACTS_ENTRIES,
        "budget_mb": loaders.RUN_FACTS_BUDGET // 2**20,
        "steps": steps,
        "peak_rss_mb": peak_rss_mb(),
    }


# -- end to end: archives through the control plane's own ingest path ------------------------------


def pack_shards(work: Path, shards: int) -> dict[str, list[Path]]:
    """Each shard as the archives a worker uploads: the pytest-coverset ``bundle.tar.gz`` and the
    same bundle converted to the language-neutral format (``coverset_model.io.write_bundle``,
    gzipped ``.jsonl`` files, packed flat)."""
    import tarfile

    from coverset_model.io import pack, write_bundle

    out: dict[str, list[Path]] = {"python": [], "neutral": []}
    for shard in range(shards):
        source = work / "bundles" / f"shard-{shard}"
        archive = work / "archives" / f"python-{shard}.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            for name in sorted(os.listdir(source)):
                tar.add(source / name, arcname=name)
        out["python"].append(archive)
        neutral_dir = work / "neutral" / f"shard-{shard}"
        write_bundle(read_python_bundle(source), neutral_dir, compress=True, manifest={"shard": shard})
        out["neutral"].append(pack(neutral_dir, work / "archives" / f"neutral-{shard}.tar.gz"))
    return out


def _ingest_archives(
    facts_dir: str, run_id: str, archives: list[str], functions: list[FunctionRegion]
) -> dict[str, object]:
    """In a fresh process: every archive through extract, read, ingest, then one finalize, as
    :func:`coverset.orchestrator.bundles.ingest_one` and the runner do (strict limits: the archives
    stand for a pull request's upload). Returns the phase times and this process's peak RSS."""
    import resource

    from coverset.facts.untrusted import BundleLimits, extract_bundle
    from coverset.lang import read_any_bundle

    store = FactsStore(Path(facts_dir))
    ctx = RunContext(REPO_ID, run_id, "0" * 40, None, "main", None, datetime(2026, 10, 2, tzinfo=UTC), None)
    limits = BundleLimits()
    phases = {"extract_s": 0.0, "read_s": 0.0, "ingest_s": 0.0}
    rows = 0
    for shard, archive in enumerate(archives):
        target = Path(facts_dir).parent / "extract" / f"{run_id}-{shard}"
        t0 = time.perf_counter()
        extract_bundle(Path(archive), target, limits)
        t1 = time.perf_counter()
        bundle = read_any_bundle(target, limits=limits)
        t2 = time.perf_counter()
        result = ingest_shard(store, ctx, bundle, shard, functions)
        t3 = time.perf_counter()
        rows += int(result.rows.get("test_lines", 0))
        phases["extract_s"] += t1 - t0
        phases["read_s"] += t2 - t1
        phases["ingest_s"] += t3 - t2
        del bundle
        shutil.rmtree(target, ignore_errors=True)
    t0 = time.perf_counter()
    finalize_run(store, ctx, expected_shards=len(archives))
    phases["finalize_s"] = time.perf_counter() - t0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        **{k: round(v, 2) for k, v in phases.items()},
        "total_s": round(sum(phases.values()), 2),
        "test_lines_rows": rows,
        "archive_mb": round(sum(Path(a).stat().st_size for a in archives) / 1e6, 1),
        "run_mb": round(dir_bytes(store.run_dir(REPO_ID, run_id)) / 1e6, 1),
        "peak_rss_mb": round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1),
    }


def ingest_archives_in_child(
    facts_dir: Path, run_id: str, archives: list[Path], functions: list[FunctionRegion]
) -> dict[str, object]:
    """:func:`_ingest_archives` in a spawned process, so its peak RSS is the ingest's alone."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return pool.submit(
            _ingest_archives, str(facts_dir), run_id, [str(a) for a in archives], list(functions)
        ).result()


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


def main_real(argv: list[str]) -> int:
    """``--real``: the real-repository mode (see the section comment above)."""
    parser = argparse.ArgumentParser(description="Ingest a real bundle and time the UI pages.")
    parser.add_argument("--real", type=Path, required=True, help="a pytest --coverset output directory")
    parser.add_argument("--repo-root", type=Path, required=True, help="the git checkout the tests ran in")
    parser.add_argument("--data-dir", type=Path, required=True, help="a new, empty scratch directory")
    parser.add_argument("--baselines", type=int, default=2, help="default-branch runs to ingest")
    parser.add_argument("--pr-base", help="a commit of the repo to diff a synthetic PR run against")
    parser.add_argument("--samples", type=int, default=20, help="requests per page after the first")
    parser.add_argument("--json-out", type=Path, help="also write the results here")
    parser.add_argument(
        "--pages-only", action="store_true", help="measure the pages of an earlier --data-dir again"
    )
    parser.add_argument(
        "--cache-memory",
        type=int,
        metavar="N",
        help="instead: RSS while N distinct runs pass through the run-facts cache (an earlier --data-dir)",
    )
    args = parser.parse_args(argv)
    if args.cache_memory:
        results = bench_cache_memory(args.data_dir.resolve(), args.cache_memory)
        text = json.dumps(results, indent=2, default=str)
        print(text)
        if args.json_out is not None:
            args.json_out.write_text(text + "\n")
        return 0
    results = bench_real(
        args.real.expanduser().resolve(),
        args.repo_root.expanduser().resolve(),
        args.data_dir.resolve(),
        baselines=max(args.baselines, 1),
        pr_base=args.pr_base,
        samples=max(args.samples, 1),
        pages_only=args.pages_only,
    )
    text = json.dumps(results, indent=2, default=str)
    print(text)
    if args.json_out is not None:
        args.json_out.write_text(text + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--real" in argv:
        return main_real(argv)
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
