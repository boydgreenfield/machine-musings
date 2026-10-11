"""Live Modal benchmarks for the README numbers paragraph: ``docs/numbers.json`` + ``docs/benchmarks.md``.

Spends real money (a few tens of cents with the defaults). It prints the estimated cost first,
refuses to start when that exceeds ``--max-cost-usd`` (default $2.00), stops starting scenarios once
the running estimate would pass it, waits for this run's sandboxes to terminate between scenarios,
and terminates everything it created in ``finally`` blocks (then reaps by tag).

Scenarios (each repeated ``--repetitions`` times, default 5, reported as median and p90):

* ``image_cold``: the bench suite's env image built with ``MODAL_IGNORE_CACHE=1`` (n = 1 always).
* ``image_cached``: the same definition built again (Modal returns the cached image).
* ``create_base``: ``Sandbox.create`` from the env image, then the first exec (``python -c pass``).
* ``snapshot`` + ``create_snapshot``: a trusted snapshot of the bench suite's ``main`` (built once,
  timed), then create-from-snapshot followed by the first exec.
* ``exec_pooled``: an exec on a sandbox created earlier and left idle for 10 s (a pooled sandbox).
* ``bench_<N>`` for N in ``--shards`` (default 1, 16, 32): ``examples/bench-suite`` (1,200 synthetic
  tests) as a baseline run through the real orchestrator (planner + runner, coverage and shapes on)
  with N shards. ``t_bench_N`` = run start to the last test result (sandboxes, source upload,
  collection and tests; ingest afterwards is reported separately).
* ``cold_path``: the measured cold path: a fresh ``debian_slim`` sandbox, ``pip install uv``, ``uv pip
  install`` of the exported requirements, plain ``python -m pytest`` in file order (no coverset).
  Also on the demo's refunds PR (``cold_path_demo``), recording when its failing test is reported.
* ``webhook``: the demo app seeded and run on Modal, a trusted snapshot of its ``main``, the warm pool
  filled, then a re-signed ``check_run.rerequested`` delivery posted to the in-process app per
  repetition: ``first_test - webhook_received`` from the run's timeline.

Usage::

    uv run python scripts/bench_modal.py                       # everything, defaults
    uv run python scripts/bench_modal.py --dry-run             # print the plan and estimate only
    uv run python scripts/bench_modal.py --scenarios webhook --data-dir data/bench-modal --resume

Raw results accumulate in ``<data-dir>/bench-raw.json`` after every scenario (``--resume`` skips
scenarios already there), so an interrupted run loses at most one scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coverset.config import Settings  # noqa: E402
from coverset.executors.base import Resources  # noqa: E402
from coverset.executors.modal_ import CPU_REQUEST, ModalExecutor, rate_usd_per_s  # noqa: E402

BENCH_SRC = ROOT / "examples" / "bench-suite"
DOCS = ROOT / "docs"
NUMBERS = DOCS / "numbers.json"
BENCHMARKS_MD = DOCS / "benchmarks.md"
RERUN_FIXTURE = ROOT / "fixtures" / "webhooks" / "check_run_rerequested.json"
SCENARIOS = (
    "meta",
    "image_cold",
    "image_cached",
    "create_base",
    "snapshot",
    "create_snapshot",
    "exec_pooled",
    "bench",
    "cold_path",
    "webhook",
    "cold_path_demo",
)
POOLED_IDLE_S = 10.0
QUIET_TIMEOUT_S = 120.0
SERIAL_SUITE_S = 173.7
"""The bench suite's measured serial runtime (examples/bench-suite/README.md), for the cost estimate."""
COVERAGE_OVERHEAD = 1.8
"""Assumed slowdown of the instrumented run for the estimate only (the README reports the measured one)."""


# -- statistics and output -----------------------------------------------------------------------------


def summarize(values: list[float]) -> dict[str, Any]:
    """Median, p90 (inclusive quantiles), min, max and the raw values (rounded)."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return {"n": 0}
    p90 = statistics.quantiles(clean, n=10, method="inclusive")[-1] if len(clean) > 1 else clean[0]
    return {
        "n": len(clean),
        "median": round(statistics.median(clean), 3),
        "p90": round(p90, 3),
        "min": round(clean[0], 3),
        "max": round(clean[-1], 3),
        "values": [round(v, 3) for v in values if v is not None],
    }


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


# -- cost ------------------------------------------------------------------------------------------


def estimate(args: argparse.Namespace, settings: Settings) -> dict[str, float]:
    """Upper-bound USD per scenario: sandbox lifetimes at the CPU/memory *limits* (what we may be billed)."""
    limit = rate_usd_per_s(settings.shard_cpu_limit, settings.shard_memory_mib)
    idle = rate_usd_per_s(CPU_REQUEST, 512)
    reps = args.repetitions
    instrumented = SERIAL_SUITE_S * COVERAGE_OVERHEAD
    plan: dict[str, float] = {
        "image_cold": 120 * limit,
        "image_cached": 0.0,
        "create_base": reps * 15 * limit,
        "snapshot": 90 * limit,
        "create_snapshot": reps * 15 * limit,
        "exec_pooled": reps * (POOLED_IDLE_S * idle + 10 * limit),
        "cold_path": reps * (SERIAL_SUITE_S + 90) * limit,
        "webhook": 8 * 4 * 60 * limit + reps * (4 * 30 * idle + 4 * 45 * limit),
        "cold_path_demo": reps * 120 * limit,
    }
    for n in args.shards:
        plan[f"bench_{n}"] = reps * (n * 40 + instrumented * 1.15) * limit
    selected = set(args.scenarios)
    return {k: v for k, v in plan.items() if k.split("_")[0] in selected or k in selected}


# -- the benchmark context ---------------------------------------------------------------------------


class Bench:
    """Shared state: settings, the executor, raw results and the spend tracker."""

    def __init__(self, args: argparse.Namespace, settings: Settings) -> None:
        self.args = args
        self.settings = settings
        self.executor = ModalExecutor(settings)
        self.raw_path = settings.data_dir / "bench-raw.json"
        self.raw: dict[str, Any] = {}
        if args.resume and self.raw_path.is_file():
            self.raw = json.loads(self.raw_path.read_text(encoding="utf-8"))
        self.estimates = estimate(args, settings)
        self.ctx: Any = None
        self.bench_repo: dict[str, Any] = {}
        self.demo: dict[str, Any] = {}
        self.build_usd = 0.0
        """Estimated spend of the image builds the scenarios run directly (not through the executor)."""

    @property
    def modal(self) -> Any:
        return self.executor.modal

    def spent(self) -> float:
        """Estimated spend so far: every sandbox lifetime at its rate (limits; requests while pooled)
        plus every image build at the limit rate for its wall time (Modal's build billing is unverified)."""
        return (
            self.executor.stats.lifetime_usd
            + self.executor.live_cost_usd()
            + self.executor.stats.build_usd
            + self.build_usd
            + self.raw.get("_spent_before", 0.0)
        )

    def add_build(self, seconds: float) -> None:
        """Count a direct image build (``build_and_record``) as spend: limit rate x wall time."""
        self.build_usd += (
            rate_usd_per_s(self.settings.shard_cpu_limit, self.settings.shard_memory_mib) * seconds
        )

    def save(self) -> None:
        self.raw["_spent_total"] = round(self.spent(), 4)
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.raw_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.raw_path)

    def fits(self, scenario: str) -> bool:
        cost = self.estimates.get(scenario, 0.0)
        if self.spent() + cost > self.args.max_cost_usd:
            log(f"skipping {scenario}: ${self.spent():.2f} spent + ${cost:.2f} estimated > cap")
            self.raw.setdefault("_skipped", {})[scenario] = "cost cap"
            return False
        return True

    async def quiet(self) -> None:
        """Wait until no sandbox of this installation is running (terminate() is asynchronous)."""
        await self.executor.drain_pool()
        deadline = time.monotonic() + QUIET_TIMEOUT_S
        while time.monotonic() < deadline:
            running = await self.executor.list_running()
            if not running:
                return
            await asyncio.sleep(2)
        log(f"warning: {len(running)} sandbox(es) still listed after {QUIET_TIMEOUT_S:.0f} s")

    def capacity_mark(self) -> int:
        return len(self.executor.stats.capacity_errors)

    def capacity_since(self, mark: int) -> list[str]:
        return list(self.executor.stats.capacity_errors[mark:])


# -- helpers: repositories and environments -----------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "coverset bench",
        "GIT_AUTHOR_EMAIL": "bench@coverset.invalid",
        "GIT_COMMITTER_NAME": "coverset bench",
        "GIT_COMMITTER_EMAIL": "bench@coverset.invalid",
        "GIT_AUTHOR_DATE": "2026-10-01T12:00:00Z",
        "GIT_COMMITTER_DATE": "2026-10-01T12:00:00Z",
    }
    out = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def make_bench_repo(target: Path) -> Path:
    """A standalone git repository holding ``examples/bench-suite`` (fixed dates: a stable SHA)."""
    if (target / ".git").is_dir():
        return target
    shutil.copytree(
        BENCH_SRC,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".venv", ".pytest_cache", "*.pyc", ".coverage*"),
    )
    _git(["init", "-q", "-b", "main"], target)
    _git(["add", "-A"], target)
    _git(["commit", "-q", "-m", "bench suite"], target)
    return target


async def env_spec_for(checkout: Path) -> Any:
    from coverset.executors.env_export import build_env_spec, build_plugin_wheel
    from coverset.lang.python import PythonAdapter

    adapter = PythonAdapter()
    config = adapter.load_config(checkout)
    wheel = await asyncio.to_thread(build_plugin_wheel)
    return await asyncio.to_thread(build_env_spec, adapter, checkout, {"python": config.python}, wheel)


async def prepare_bench_repo(bench: Bench) -> dict[str, Any]:
    """Register the bench suite repo, mirror it and build its env spec (once)."""
    if bench.bench_repo:
        return bench.bench_repo
    from coverset.orchestrator.repos import (
        archive,
        ensure_mirror_async,
        extract_archive,
        register_local_repo,
        resolve_commit,
    )

    source = make_bench_repo(bench.settings.data_dir / "bench-suite-src")
    async with bench.ctx.db.session() as session:
        repo = await register_local_repo(session, source, name="bench-suite")
        await session.commit()
    mirror = await ensure_mirror_async(bench.settings, repo)
    sha = await asyncio.to_thread(resolve_commit, mirror, "main")
    tarball = await asyncio.to_thread(archive, mirror, sha)
    checkout = Path(tempfile.mkdtemp(prefix="coverset-bench-"))
    await asyncio.to_thread(extract_archive, tarball, checkout)
    spec = await env_spec_for(checkout)
    bench.bench_repo = {
        "repo_id": repo.id,
        "full_name": repo.full_name,
        "sha": sha,
        "tarball": tarball,
        "spec": spec,
        "checkout": checkout,
    }
    return bench.bench_repo


# -- scenarios -----------------------------------------------------------------------------------------


async def scenario_meta(bench: Bench) -> dict[str, Any]:
    from coverset.commands.modal import token_info

    modal = bench.modal
    info = await token_info(modal)
    image = modal.Image.debian_slim(python_version="3.13")
    sandbox, terminate = await bench.executor.create_sandbox(image, role="bench", repo="bench", run="meta")
    try:
        proc = await sandbox.exec.aio(
            "sh", "-c", 'echo "${MODAL_REGION:-unknown} ${MODAL_CLOUD_PROVIDER:-unknown}"; nproc', timeout=60
        )
        await proc.wait.aio()
        out = (await proc.stdout.read.aio()).split()
    finally:
        await terminate()
    return {
        "bench_date": datetime.now(UTC).date().isoformat(),
        "modal_version": getattr(modal, "__version__", "?"),
        "account": info.split(",")[0],
        "region": out[0] if out else "unknown",
        "cloud": out[1] if len(out) > 1 else "unknown",
        "nproc": out[2] if len(out) > 2 else "?",
        "cpu": f"{CPU_REQUEST}-{bench.settings.shard_cpu_limit}",
        "memory_mib": f"512-{bench.settings.shard_memory_mib}",
        "plan_tier": bench.args.plan_tier,
        "python": "3.13",
        "image_builder_version": os.environ.get("MODAL_IMAGE_BUILDER_VERSION"),
    }


async def scenario_image_cold(bench: Bench) -> dict[str, Any]:
    from coverset.executors.modal_images import build_and_record, env_image

    info = await prepare_bench_repo(bench)
    app = await bench.executor.app()
    image = await asyncio.to_thread(env_image, info["spec"], info["spec"].plugin_wheel, modal=bench.modal)
    os.environ["MODAL_IGNORE_CACHE"] = "1"
    try:
        started = time.perf_counter()
        try:
            image_id = await build_and_record(app, image, modal=bench.modal)
        finally:
            seconds = time.perf_counter() - started
            bench.add_build(seconds)
    finally:
        os.environ.pop("MODAL_IGNORE_CACHE", None)
    return {"seconds": [seconds], "image_id": image_id, "n": 1, "note": "MODAL_IGNORE_CACHE=1, n=1"}


async def scenario_image_cached(bench: Bench) -> dict[str, Any]:
    from coverset.executors.modal_images import build_and_record, env_image

    info = await prepare_bench_repo(bench)
    app = await bench.executor.app()
    seconds = []
    for _ in range(bench.args.repetitions):
        image = await asyncio.to_thread(env_image, info["spec"], info["spec"].plugin_wheel, modal=bench.modal)
        started = time.perf_counter()
        try:
            image_id = await build_and_record(app, image, modal=bench.modal)
        finally:
            seconds.append(time.perf_counter() - started)
            bench.add_build(seconds[-1])
    env = await bench.executor.ensure_env(info["spec"])
    return {"seconds": seconds, "image_id": image_id, "env_image": env.location}


async def _create_then_exec(bench: Bench, image_id: str, role: str) -> tuple[float, float]:
    from coverset.executors.modal_images import image_from_id

    started = time.perf_counter()
    sandbox, terminate = await bench.executor.create_sandbox(
        image_from_id(image_id, modal=bench.modal), role=role, repo="bench", run=role
    )
    try:
        created = time.perf_counter()
        worker = bench.executor.worker_for(sandbox)
        result = await worker.run(["python", "-c", "pass"], cwd="", env={}, timeout_s=120, stdout_cap=4096)
        if result.exit_code != 0:
            raise RuntimeError(f"first exec exited {result.exit_code}")
        done = time.perf_counter()
    finally:
        await terminate()
    return done - started, created - started


async def scenario_create_base(bench: Bench) -> dict[str, Any]:
    info = await prepare_bench_repo(bench)
    env = await bench.executor.ensure_env(info["spec"])
    totals, creates = [], []
    for _ in range(bench.args.repetitions):
        total, create = await _create_then_exec(bench, env.location, "bench-base")
        totals.append(total)
        creates.append(create)
    return {"seconds": totals, "create_only": creates, "image": env.location}


async def scenario_snapshot(bench: Bench) -> dict[str, Any]:
    info = await prepare_bench_repo(bench)
    env = await bench.executor.ensure_env(info["spec"])
    started = time.perf_counter()
    ref = await bench.executor.ensure_snapshot(env, info["full_name"], info["sha"])
    seconds = time.perf_counter() - started
    assert ref is not None
    return {"seconds": [seconds], "image": ref.image_id, "snapshot_id": ref.snapshot_id}


async def scenario_create_snapshot(bench: Bench) -> dict[str, Any]:
    info = await prepare_bench_repo(bench)
    env = await bench.executor.ensure_env(info["spec"])
    ref = await bench.executor.ensure_snapshot(env, info["full_name"], info["sha"])
    assert ref is not None
    totals, creates = [], []
    for _ in range(bench.args.repetitions):
        total, create = await _create_then_exec(bench, ref.image_id, "bench-snapshot")
        totals.append(total)
        creates.append(create)
    return {"seconds": totals, "create_only": creates, "image": ref.image_id}


async def scenario_exec_pooled(bench: Bench) -> dict[str, Any]:
    from coverset.executors.modal_images import image_from_id

    info = await prepare_bench_repo(bench)
    env = await bench.executor.ensure_env(info["spec"])
    ref = await bench.executor.ensure_snapshot(env, info["full_name"], info["sha"])
    image_id = ref.image_id if ref is not None else env.location
    seconds = []
    for _ in range(bench.args.repetitions):
        sandbox, terminate = await bench.executor.create_sandbox(
            image_from_id(image_id, modal=bench.modal), role="bench-pool", repo="bench", run="pooled"
        )
        try:
            await asyncio.sleep(POOLED_IDLE_S)
            worker = bench.executor.worker_for(sandbox)
            started = time.perf_counter()
            result = await worker.run(
                ["python", "-c", "pass"], cwd="", env={}, timeout_s=120, stdout_cap=4096
            )
            seconds.append(time.perf_counter() - started)
            if result.exit_code != 0:
                raise RuntimeError(f"exec exited {result.exit_code}")
        finally:
            await terminate()
    return {"seconds": seconds, "idle_s": POOLED_IDLE_S}


async def _orchestrated_run(bench: Bench, repo_id: int, sha: str, shards: int) -> dict[str, Any]:
    from coverset.db.models import Run
    from coverset.orchestrator.runner import create_run, execute_run

    run_id = await create_run(
        bench.ctx,
        repo_id=repo_id,
        head_sha=sha,
        base_sha=None,
        branch="main",
        trigger="baseline",
        executor="modal",
    )
    marks: dict[str, float] = {}
    started = time.perf_counter()
    lifetime_before = bench.executor.stats.lifetime_usd

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        now = time.perf_counter() - started
        if kind == "test":
            marks.setdefault("first_test", now)
            marks["last_test"] = now
        elif kind == "status" and payload.get("status") == "planned":
            marks["planned"] = now

    outcome = await execute_run(bench.ctx, run_id, on_event=on_event, shards=shards)
    total = time.perf_counter() - started
    async with bench.ctx.db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        row = {
            "run_id": run_id,
            "status": outcome.status,
            "conclusion": outcome.conclusion,
            "reason": outcome.reason,
            "shards_requested": shards,
            "shards": run.shard_count,
            "cost_estimate_exec": run.cost_estimate,
            "timeline": dict(run.timeline or {}),
        }
    row.update(
        {
            "t_last_test": marks.get("last_test"),
            "t_first_test": marks.get("first_test"),
            "t_planned": marks.get("planned"),
            "t_total": total,
            "cost_lifetime": bench.executor.stats.lifetime_usd - lifetime_before,
        }
    )
    return row


async def scenario_bench(bench: Bench, shards: int) -> dict[str, Any]:
    info = await prepare_bench_repo(bench)
    mark = bench.capacity_mark()
    runs = []
    for i in range(bench.args.repetitions):
        log(f"bench_{shards}: repetition {i + 1}/{bench.args.repetitions}")
        row = await _orchestrated_run(bench, info["repo_id"], info["sha"], shards)
        log(
            f"  {row['status']} {row['conclusion']} shards={row['shards']} last_test={row['t_last_test']} "
            f"total={row['t_total']:.1f}s"
        )
        runs.append(row)
        await bench.quiet()
        if row["status"] != "completed" or (row["shards"] or 0) < shards:
            break  # capacity or failure: record what happened, do not keep paying for it
    ok = [r for r in runs if r["status"] == "completed" and r["shards"] == shards and r["t_last_test"]]
    return {
        "shards": shards,
        "seconds": [r["t_last_test"] for r in ok],
        "total_seconds": [r["t_total"] for r in ok],
        "cost_lifetime": [r["cost_lifetime"] for r in ok],
        "cost_exec": [r["cost_estimate_exec"] for r in ok],
        "runs": runs,
        "capacity_errors": bench.capacity_since(mark),
        "succeeded": bool(ok) and len(ok) == len(runs),
    }


async def _cold_path(
    bench: Bench, tarball: bytes, requirements: str, pytest_args: list[str], fail_pattern: str | None
) -> dict[str, Any]:
    """Fresh debian_slim sandbox, install, plain pytest; times to setup done, first result, first failure, end."""
    python = "3.13"
    image = bench.modal.Image.debian_slim(python_version=python)
    resources = Resources(
        cpu=bench.settings.shard_cpu_limit,
        memory_mib=bench.settings.shard_memory_mib,
        network="open",  # the cold path installs from PyPI like a CI job; our own trusted code only
        timeout_s=min(1500, bench.settings.run_timeout_s),
    )
    marks: dict[str, float] = {}
    started = time.perf_counter()
    sandbox, terminate = await bench.executor.create_sandbox(
        image, role="bench-cold", resources=resources, run="cold"
    )
    try:
        worker = bench.executor.worker_for(sandbox)
        await worker.put_tree(tarball, "repo")
        await worker.write_file("requirements.txt", requirements.encode())
        install = await worker.run(
            ["sh", "-c", "pip install -q uv && uv pip install --system -q -r /work/requirements.txt"],
            cwd="",
            env={},
            timeout_s=600,
            stdout_cap=1 << 20,
        )
        if install.exit_code != 0:
            raise RuntimeError(f"install exited {install.exit_code}")
        marks["setup"] = time.perf_counter() - started
        outcome = re.compile(r" (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)")

        def on_line(text: str, _stream: str) -> None:
            now = time.perf_counter() - started
            if outcome.search(text):
                marks.setdefault("first_test", now)
            if fail_pattern and fail_pattern in text and " FAILED" in text:
                marks.setdefault("first_failure", now)

        result = await worker.run(
            ["python", "-m", "pytest", "-v", "-p", "no:cacheprovider", *pytest_args],
            cwd="repo",
            env={"PYTHONPATH": "/work/repo/src:/work/repo"},
            timeout_s=1200,
            on_line=on_line,
            stdout_cap=50 << 20,
        )
        marks["total"] = time.perf_counter() - started
        marks["exit_code"] = result.exit_code
    finally:
        await terminate()
    return marks


async def scenario_cold_path(bench: Bench) -> dict[str, Any]:
    info = await prepare_bench_repo(bench)
    rows = []
    for i in range(bench.args.repetitions):
        log(f"cold_path: repetition {i + 1}/{bench.args.repetitions}")
        rows.append(await _cold_path(bench, info["tarball"], info["spec"].requirements_txt, [], None))
        log(f"  {rows[-1]}")
    return {
        "seconds": [r["total"] for r in rows],
        "setup_seconds": [r["setup"] for r in rows],
        "first_test_seconds": [r.get("first_test") for r in rows if r.get("first_test")],
        "rows": rows,
    }


async def prepare_demo(bench: Bench) -> dict[str, Any]:
    """Seed the demo repo and run its history + PRs on Modal (reused when already done)."""
    if bench.demo:
        return bench.demo
    from sqlalchemy import select

    from coverset.db.models import Repo
    from coverset.demo.flow import format_table, run_demo
    from coverset.orchestrator.repos import archive, ensure_mirror_async, extract_archive, resolve_commit

    lifetime_before = bench.executor.stats.lifetime_usd
    started = time.perf_counter()
    result = await run_demo(bench.ctx, out=bench.settings.data_dir / "demo.git", executor="modal")
    log("demo runs:\n" + format_table(result.runs))
    verdicts = {r.name: r.verdict for r in result.runs}
    if verdicts.get("pr-1") != "failure" or verdicts.get("pr-2") != "success":
        raise RuntimeError(f"the demo runs did not reproduce the expected verdicts: {verdicts}")
    async with bench.ctx.db.session() as session:
        repo = await session.scalar(select(Repo).where(Repo.id == result.repo_id))
        assert repo is not None
    mirror = await ensure_mirror_async(bench.settings, repo)
    main_sha = await asyncio.to_thread(resolve_commit, mirror, "main")
    pr_sha = next((sha for name, sha in result.shas["branches"].items() if "refund" in name), None)
    tar_main = await asyncio.to_thread(archive, mirror, main_sha)
    checkout = Path(tempfile.mkdtemp(prefix="coverset-bench-demo-"))
    await asyncio.to_thread(extract_archive, tar_main, checkout)
    spec = await env_spec_for(checkout)
    bench.demo = {
        "repo_id": result.repo_id,
        "full_name": repo.full_name,
        "main_sha": main_sha,
        "pr_sha": pr_sha,
        "mirror": mirror,
        "spec": spec,
        "runs": [
            {
                "name": r.name,
                "verdict": r.verdict,
                "rank": r.rank_coverset,
                "rank_file": r.rank_file_order,
                "reused": r.reused,
            }
            for r in result.runs
        ],
        "seed_seconds": time.perf_counter() - started,
        "seed_cost_lifetime": bench.executor.stats.lifetime_usd - lifetime_before,
    }
    return bench.demo


async def scenario_cold_path_demo(bench: Bench) -> dict[str, Any]:
    from coverset.orchestrator.repos import archive

    demo = await prepare_demo(bench)
    if not demo["pr_sha"]:
        raise RuntimeError("the demo has no refunds PR branch")
    tarball = await asyncio.to_thread(archive, demo["mirror"], demo["pr_sha"])
    rows = []
    for i in range(bench.args.repetitions):
        log(f"cold_path_demo: repetition {i + 1}/{bench.args.repetitions}")
        rows.append(
            await _cold_path(bench, tarball, demo["spec"].requirements_txt, [], "test_refund_partial")
        )
        log(f"  {rows[-1]}")
    return {
        "seconds": [r["total"] for r in rows],
        "setup_seconds": [r["setup"] for r in rows],
        "first_test_seconds": [r.get("first_test") for r in rows if r.get("first_test")],
        "first_failure_seconds": [r.get("first_failure") for r in rows if r.get("first_failure")],
        "rows": rows,
    }


async def scenario_webhook(bench: Bench) -> dict[str, Any]:
    from coverset.app import create_app
    from coverset.orchestrator.queue import install_run_queue

    demo = await prepare_demo(bench)
    env = await bench.executor.ensure_env(demo["spec"])
    started = time.perf_counter()
    snapshot = await bench.executor.ensure_snapshot(env, demo["full_name"], demo["main_sha"])
    snapshot_seconds = time.perf_counter() - started
    assert snapshot is not None
    app = create_app(bench.settings)
    dispatcher = install_run_queue(app, ctx=bench.ctx)  # returns the app's own queue when it has one
    dispatcher.ctx.executors["modal"] = bench.executor  # runs must see the pool this script fills
    data = json.loads(RERUN_FIXTURE.read_text(encoding="utf-8"))
    template = json.loads(str(data["body"]))
    secret = bench.settings.github_webhook_secret.get_secret_value()  # type: ignore[union-attr]
    rows = []
    async with app.test_app():
        try:
            rows = await _webhook_reps(bench, app, demo, snapshot, data, template, secret)
        finally:
            await bench.executor.drain_pool()
    return {
        "seconds": [r["first_test_s"] for r in rows if r["first_test_s"] is not None],
        "sandbox_ready_seconds": [r["sandbox_ready_s"] for r in rows if r["sandbox_ready_s"] is not None],
        "snapshot_seconds": snapshot_seconds,
        "rows": rows,
        "demo_runs": demo["runs"],
        "demo_seed_seconds": demo["seed_seconds"],
        "demo_seed_cost_lifetime": demo["seed_cost_lifetime"],
    }


async def _webhook_reps(
    bench: Bench,
    app: Any,
    demo: dict[str, Any],
    snapshot: Any,
    data: dict[str, Any],
    template: dict[str, Any],
    secret: str,
) -> list[dict[str, Any]]:
    from coverset.db.models import Run
    from coverset.forge.signing import SIGNATURE_HEADER, sign_body

    rows: list[dict[str, Any]] = []
    client = app.test_client()
    for i in range(bench.args.repetitions):
        filled = await bench.executor.fill_pool(
            snapshot, str(demo["repo_id"]), bench.settings.pool_size, bench.settings.pool_ttl_s
        )
        await asyncio.sleep(3.0)  # the pool sits idle before the event, as it would in practice
        # A distinct delivery each time: the intake deduplicates on the GUID and on the body digest.
        payload_in = dict(template)
        payload_in["check_run"] = {**template["check_run"], "id": 9_000_000 + int(time.time() * 1000) % 10**6}
        body = json.dumps(payload_in).encode("utf-8")
        headers = {str(k): str(v) for k, v in data["headers"].items()}
        headers[SIGNATURE_HEADER] = sign_body(secret, body)
        headers["X-GitHub-Delivery"] = f"bench-{secrets.token_hex(8)}"
        claims_before = bench.executor.stats.pool_claims
        response = await client.post("/webhooks/github", data=body, headers=headers)
        payload = dict(await response.get_json() or {})
        run_id = payload.get("run_id")
        if response.status_code >= 300 or not run_id or payload.get("status") != "created":
            raise RuntimeError(f"replay did not create a run: HTTP {response.status_code} {payload}")
        status = await _wait_run(bench, str(run_id))
        async with bench.ctx.db.session() as session:
            run = await session.get(Run, str(run_id))
            assert run is not None
            timeline = dict(run.timeline or {})
        t0 = _ts(timeline.get("webhook_received"))
        first = _ts(timeline.get("first_test"))
        ready = _ts(timeline.get("sandbox_ready"))
        row = {
            "run_id": run_id,
            "status": status,
            "pool_filled": filled,
            "pool_claims": bench.executor.stats.pool_claims - claims_before,
            "first_test_s": (first - t0) if first and t0 else None,
            "sandbox_ready_s": (ready - t0) if ready and t0 else None,
            "timeline": timeline,
        }
        log(f"webhook {i + 1}: {row['status']} first_test={row['first_test_s']} claims={row['pool_claims']}")
        rows.append(row)
    return rows


def _ts(value: Any) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value)).timestamp()


async def _wait_run(bench: Bench, run_id: str, timeout_s: float = 900) -> str:
    from coverset.db.models import Run

    deadline = time.monotonic() + timeout_s
    status = "queued"
    while time.monotonic() < deadline:
        async with bench.ctx.db.session() as session:
            run = await session.get(Run, run_id)
            status = run.status if run is not None else "missing"
        if status not in ("queued", "running", "paused", "awaiting_approval"):
            return status
        await asyncio.sleep(0.5)
    return status


# -- driver ---------------------------------------------------------------------------------------------


async def run_all(bench: Bench) -> None:
    from coverset.db.session import init_db
    from coverset.orchestrator.runner import OrchestratorContext

    await init_db(bench.settings)
    bench.ctx = OrchestratorContext.from_settings(
        bench.settings, executor_factory=lambda kind: bench.executor
    )
    steps: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = []
    wanted = set(bench.args.scenarios)
    simple = {
        "meta": scenario_meta,
        "image_cold": scenario_image_cold,
        "image_cached": scenario_image_cached,
        "create_base": scenario_create_base,
        "snapshot": scenario_snapshot,
        "create_snapshot": scenario_create_snapshot,
        "exec_pooled": scenario_exec_pooled,
    }
    for name in SCENARIOS:
        if name not in wanted:
            continue
        if name == "bench":
            for n in bench.args.shards:
                steps.append((f"bench_{n}", lambda n=n: scenario_bench(bench, n)))
        elif name in simple:
            steps.append((name, lambda fn=simple[name]: fn(bench)))
        elif name == "cold_path":
            steps.append((name, lambda: scenario_cold_path(bench)))
        elif name == "webhook":
            steps.append((name, lambda: scenario_webhook(bench)))
        elif name == "cold_path_demo":
            steps.append((name, lambda: scenario_cold_path_demo(bench)))
    try:
        for name, step in steps:
            if name in bench.raw and bench.args.resume:
                log(f"{name}: already measured (resume)")
                continue
            if not bench.fits(name):
                bench.save()
                continue
            log(f"{name}: start (spent so far ${bench.spent():.3f})")
            mark = bench.capacity_mark()
            started = time.perf_counter()
            try:
                result = await step()
            except Exception as exc:
                log(f"{name}: FAILED {type(exc).__name__}: {exc}")
                result = {"error": f"{type(exc).__name__}: {exc}"}
            result["wall_s"] = round(time.perf_counter() - started, 2)
            result.setdefault("capacity_errors", bench.capacity_since(mark))
            bench.raw[name] = result
            bench.save()
            log(f"{name}: done in {result['wall_s']} s (spent so far ${bench.spent():.3f})")
            await bench.quiet()
    finally:
        await bench.executor.drain_pool()
        report = await bench.executor.reap(active_run_ids=set(), all=True)
        log(f"final reap: found {report.found}, terminated {report.terminated}")
        await bench.ctx.close()
        bench.save()


# -- docs -------------------------------------------------------------------------------------------------


def numbers_from_raw(raw: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """The ``docs/numbers.json`` keys this script owns."""
    out: dict[str, Any] = {}
    meta = raw.get("meta", {})
    for key in ("bench_date", "modal_version", "cpu", "plan_tier", "region"):
        if key in meta:
            out[key] = meta[key]

    def put(key: str, values: list[float] | None, digits: int = 2) -> None:
        stats = summarize([v for v in (values or []) if v is not None])
        if stats.get("n"):
            out[key] = round(stats["median"], digits)
            out[f"{key}_p90"] = round(stats["p90"], digits)
            out[f"{key}_n"] = stats["n"]

    put("t_image_cold", raw.get("image_cold", {}).get("seconds"), 1)
    put("t_image_cached", raw.get("image_cached", {}).get("seconds"))
    put("t_create_base", raw.get("create_base", {}).get("seconds"))
    put("t_create_snapshot", raw.get("create_snapshot", {}).get("seconds"))
    put("t_snapshot_build", raw.get("snapshot", {}).get("seconds"), 1)
    put("t_exec_pooled", raw.get("exec_pooled", {}).get("seconds"))
    succeeded = []
    for n in args.shards:
        result = raw.get(f"bench_{n}", {})
        put(f"t_bench_{n}", result.get("seconds"), 1)
        put(f"t_bench_{n}_total", result.get("total_seconds"), 1)
        if result.get("succeeded"):
            succeeded.append(n)
        lifetime = summarize(result.get("cost_lifetime") or [])
        if lifetime.get("n"):
            out[f"cost_bench_{n}"] = round(lifetime["median"], 3)
        exec_cost = summarize(result.get("cost_exec") or [])
        if exec_cost.get("n"):
            out[f"cost_bench_{n}_exec"] = round(exec_cost["median"], 4)
    if succeeded:
        out["bench_max_shards_succeeded"] = max(succeeded)
    put("t_cold_path", raw.get("cold_path", {}).get("seconds"), 1)
    put("t_cold_path_setup", raw.get("cold_path", {}).get("setup_seconds"))
    put("t_cold_path_first_test", raw.get("cold_path", {}).get("first_test_seconds"))
    demo_cold = raw.get("cold_path_demo", {})
    put("t_cold_path_demo", demo_cold.get("seconds"), 1)
    put("t_cold_path_demo_setup", demo_cold.get("setup_seconds"))
    put("t_cold_path_demo_first_test", demo_cold.get("first_test_seconds"))
    put("t_cold_path_demo_first_failure", demo_cold.get("first_failure_seconds"))
    put("t_webhook_first_test", raw.get("webhook", {}).get("seconds"))
    failures = []
    for row in raw.get("webhook", {}).get("rows", []):
        t0, failed = _ts(row["timeline"].get("webhook_received")), _ts(row["timeline"].get("first_failure"))
        if t0 and failed:
            failures.append(failed - t0)
    put("t_webhook_first_failure", failures)
    put("t_webhook_sandbox_ready", raw.get("webhook", {}).get("sandbox_ready_seconds"))
    counts = [v for k, v in out.items() if k.endswith("_n") and isinstance(v, int)]
    if counts:
        out["n_trials"] = args.repetitions
    out["bench_tests"] = 1200
    errors: list[str] = []
    for name, result in raw.items():
        if isinstance(result, dict):
            errors += [f"{name}: {e}" for e in result.get("capacity_errors", [])]
    out["bench_capacity_errors"] = errors
    out["bench_cost_estimate_usd"] = round(float(raw.get("_spent_total", 0.0)), 3)
    return out


def merge_numbers(new: dict[str, Any]) -> dict[str, Any]:
    """Merge ``new`` into ``docs/numbers.json``, keeping every other key."""
    current: dict[str, Any] = {}
    if NUMBERS.is_file():
        current = json.loads(NUMBERS.read_text(encoding="utf-8"))
    current.update(new)
    if "t_create_base" in new:
        current.pop("modal_benchmarks_not_run", None)
    NUMBERS.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return current


def write_markdown(raw: dict[str, Any], numbers: dict[str, Any], args: argparse.Namespace) -> None:
    meta = raw.get("meta", {})
    rows = [
        ("image build, cold (MODAL_IGNORE_CACHE=1)", "image_cold", "seconds"),
        ("image build, cached", "image_cached", "seconds"),
        ("create from env image -> first exec", "create_base", "seconds"),
        ("  of which Sandbox.create", "create_base", "create_only"),
        ("trusted snapshot build (put_tree, collect, compileall, snapshot)", "snapshot", "seconds"),
        ("create from snapshot -> first exec", "create_snapshot", "seconds"),
        ("  of which Sandbox.create", "create_snapshot", "create_only"),
        ("exec on a pooled sandbox (idle 10 s)", "exec_pooled", "seconds"),
    ]
    for n in args.shards:
        rows.append((f"bench suite, {n} shard(s): start -> last test", f"bench_{n}", "seconds"))
        rows.append(
            (f"bench suite, {n} shard(s): start -> done (incl. ingest)", f"bench_{n}", "total_seconds")
        )
    rows += [
        ("cold path, bench suite: total", "cold_path", "seconds"),
        ("cold path, bench suite: sandbox + install", "cold_path", "setup_seconds"),
        ("cold path, bench suite: first test result", "cold_path", "first_test_seconds"),
        ("cold path, demo refunds PR: total", "cold_path_demo", "seconds"),
        ("cold path, demo refunds PR: first failure reported", "cold_path_demo", "first_failure_seconds"),
        ("webhook -> first test (pooled sandbox, demo rerun)", "webhook", "seconds"),
        ("webhook -> sandbox ready", "webhook", "sandbox_ready_seconds"),
        ("webhook -> first confirmed failure (incl. its retry)", "webhook", "first_failure_seconds"),
    ]
    lines = [
        "# Benchmarks",
        "",
        "Live Modal measurements behind the README numbers paragraph, written by `scripts/bench_modal.py`.",
        "Medians and p90 over the listed repetitions; every value is in seconds unless marked.",
        "The bench suite is **synthetic** (`examples/bench-suite`: 1,200 CPU-bound tests, 173.7 s of drawn",
        "serial work); the cold path is a fresh `debian_slim` sandbox with a plain install, which is what a",
        "cold CI runner does, followed by plain `pytest` in file order.",
        "",
        "## Environment",
        "",
        "| key | value |",
        "| --- | --- |",
    ]
    for key in ("bench_date", "modal_version", "account", "region", "cloud", "cpu", "memory_mib", "plan_tier",
                "python", "image_builder_version"):  # fmt: skip
        lines.append(f"| {key} | {meta.get(key, '?')} |")
    lines += [
        f"| repetitions | {args.repetitions} (image_cold: 1) |",
        f"| estimated spend of this benchmark | ${raw.get('_spent_total', 0):.3f} (sandbox lifetimes at the CPU/memory limits; an upper bound) |",
        "",
        "## Results",
        "",
        "| scenario | n | median | p90 | min | max | raw |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    webhook = raw.get("webhook", {})
    if webhook.get("rows") and "first_failure_seconds" not in webhook:
        webhook["first_failure_seconds"] = [
            _ts(r["timeline"]["first_failure"]) - _ts(r["timeline"]["webhook_received"])  # type: ignore[operator]
            for r in webhook["rows"]
            if r["timeline"].get("first_failure") and r["timeline"].get("webhook_received")
        ]
    for label, key, field in rows:
        result = raw.get(key, {})
        if "error" in result:
            lines.append(f"| {label} | - | error | | | | `{result['error'][:120]}` |")
            continue
        stats = summarize([v for v in (result.get(field) or []) if v is not None])
        if not stats.get("n"):
            reason = raw.get("_skipped", {}).get(key, "not run")
            lines.append(f"| {label} | 0 | {reason} | | | | |")
            continue
        raw_values = ", ".join(f"{v:.2f}" for v in stats["values"])
        lines.append(
            f"| {label} | {stats['n']} | {stats['median']:.2f} | {stats['p90']:.2f} | {stats['min']:.2f} | "
            f"{stats['max']:.2f} | {raw_values} |"
        )
    lines += ["", "## Fan-out runs", "", "| shards | run | status | shards obtained | last test s | total s | runs.cost_estimate | cost (sandbox lifetime, limits) |",
              "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |"]  # fmt: skip
    for n in args.shards:
        for row in raw.get(f"bench_{n}", {}).get("runs", []):
            lines.append(
                f"| {n} | `{row['run_id']}` | {row['status']} {row['conclusion']} | {row['shards']} | "
                f"{(row['t_last_test'] or 0):.1f} | {row['t_total']:.1f} | ${(row['cost_estimate_exec'] or 0):.4f} | "
                f"${row['cost_lifetime']:.4f} |"
            )
    webhook = raw.get("webhook", {})
    if webhook.get("demo_runs"):
        lines += ["", "## Demo on Modal (seeded history and both PRs)", "", "| run | verdict | rank coverset / file order | reused |",
                  "| --- | --- | --- | --- |"]  # fmt: skip
        for row in webhook["demo_runs"]:
            rank = f"{row['rank']}/{row['rank_file']}" if row.get("rank") else "-"
            lines.append(f"| {row['name']} | {row['verdict']} | {rank} | {row['reused']} |")
    errors = numbers.get("bench_capacity_errors") or []
    lines += ["", "## Capacity errors", ""]
    lines += [f"- {e}" for e in errors] or ["None recorded."]
    if numbers.get("bench_max_shards_succeeded"):
        lines += [
            "",
            f"Largest shard count that succeeded on every repetition: {numbers['bench_max_shards_succeeded']}.",
        ]
    lines += [
        "",
        "## Cost model",
        "",
        "Modal bills `max(request, usage)` per second: $0.142 per core-hour, $0.024 per GiB-hour. Every",
        "sandbox requests 0.125 core and 512 MiB with limits of `COVERSET_SHARD_CPU_LIMIT` (2.0) and",
        "`COVERSET_SHARD_MEMORY_MIB` (2048). `cost_bench_N` prices each sandbox's whole lifetime at its",
        "limits (an upper bound); `cost_bench_N_exec` is what the run records (`runs.cost_estimate`:",
        "the time it held each worker, at the limits). A single-threaded pytest uses about one core and",
        "far less than 2 GiB, so the bill is lower than either.",
        "",
        "## Commands",
        "",
        "```sh",
        "uv run coverset modal smoke",
        "COVERSET_DATA_DIR=<scratch> uv run coverset demo --executor modal --no-serve",
        f"uv run python scripts/bench_modal.py --max-cost-usd {args.max_cost_usd:.2f} --repetitions {args.repetitions} "
        f"--shards {','.join(str(n) for n in args.shards)}",
        "uv run coverset modal reap --all",
        "```",
        "",
        "Raw per-repetition data (timelines, run ids) is kept in `<data-dir>/bench-raw.json` of the run.",
        "A scenario that failed is measured again on its own with `--resume --data-dir <same dir>` (the",
        "2026-10-11 run did this for `webhook`, whose first attempt hit an import error in a module that",
        "was being edited at the time); `--docs-only` rewrites these files from the raw data.",
        "",
    ]
    BENCHMARKS_MD.write_text("\n".join(lines), encoding="utf-8")


# -- main -------------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--max-cost-usd", type=float, default=2.00)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--shards", type=lambda s: [int(x) for x in s.split(",") if x], default=[1, 16, 32])
    parser.add_argument("--scenarios", type=lambda s: [x for x in s.split(",") if x], default=list(SCENARIOS))
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "bench-modal")
    parser.add_argument("--plan-tier", default="Starter")
    parser.add_argument("--resume", action="store_true", help="Skip scenarios already in bench-raw.json.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and the estimate, then exit.")
    parser.add_argument(
        "--no-docs", action="store_true", help="Do not write docs/numbers.json or benchmarks.md."
    )
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="Rewrite the docs from <data-dir>/bench-raw.json; no Modal calls.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging from the Modal executor.")
    args = parser.parse_args(argv)
    unknown = set(args.scenarios) - set(SCENARIOS)
    if unknown:
        parser.error(f"unknown scenarios: {sorted(unknown)}")
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    return args


CEILING_KEYS = ("modal_daily_budget_usd", "max_shards", "max_sandboxes", "max_run_cost_usd")
"""Settings the benchmark raises for itself, but never above a value the environment (or .env) sets."""


def make_settings(args: argparse.Namespace) -> Settings:
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    common: dict[str, Any] = {
        "data_dir": data_dir,
        "database_url": f"sqlite+aiosqlite:///{data_dir / 'coverset.db'}",
        "executor": "modal",
        "github_webhook_secret": secrets.token_hex(24),
        "forge": "fake",
    }
    configured = Settings(**common)  # what the environment and .env say, before the bench's own values
    ceilings = {k: getattr(configured, k) for k in CEILING_KEYS if k in configured.model_fields_set}
    wanted: dict[str, Any] = {
        "max_shards": max(32, *args.shards),
        "max_sandboxes": max(48, max(args.shards) + 8),
        "modal_daily_budget_usd": max(5.0, args.max_cost_usd * 2),
    }
    values = {k: min(v, ceilings[k]) if k in ceilings else v for k, v in wanted.items()}
    if "max_run_cost_usd" in ceilings:
        values["max_run_cost_usd"] = min(ceilings["max_run_cost_usd"], values["modal_daily_budget_usd"])
    return Settings(**common, **values)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verbose:
        import logging

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        logging.getLogger("coverset.executors.modal_").setLevel(logging.DEBUG)
    settings = make_settings(args)
    bench = Bench(args, settings)
    print("coverset Modal benchmark")
    print(f"  data dir:    {settings.data_dir}")
    print(f"  environment: {settings.modal_environment} (app coverset-runner)")
    print(f"  scenarios:   {', '.join(args.scenarios)}; shards {args.shards}; repetitions {args.repetitions}")
    print("  estimated cost (upper bound: sandbox lifetimes at the CPU/memory limits):")
    for name, cost in bench.estimates.items():
        print(f"    {name:<16} ${cost:.3f}")
    total = sum(bench.estimates.values())
    print(f"    {'total':<16} ${total:.3f}   (cap ${args.max_cost_usd:.2f})")
    if total > args.max_cost_usd:
        print("estimated cost exceeds --max-cost-usd: lower --repetitions or drop scenarios", file=sys.stderr)
        return 2
    if args.dry_run:
        return 0
    if args.docs_only:
        bench.raw = json.loads(bench.raw_path.read_text(encoding="utf-8"))
        merged = merge_numbers(numbers_from_raw(bench.raw, args))
        write_markdown(bench.raw, merged, args)
        print(
            f"wrote {NUMBERS.relative_to(ROOT)} and {BENCHMARKS_MD.relative_to(ROOT)} from {bench.raw_path}"
        )
        return 0
    if bench.raw:
        bench.raw["_spent_before"] = float(bench.raw.get("_spent_total", 0.0))
    asyncio.run(run_all(bench))
    numbers = numbers_from_raw(bench.raw, args)
    if not args.no_docs:
        merged = merge_numbers(numbers)
        write_markdown(bench.raw, merged, args)
        print(f"wrote {NUMBERS.relative_to(ROOT)} and {BENCHMARKS_MD.relative_to(ROOT)}")
    print(json.dumps(numbers, indent=2, sort_keys=True))
    with contextlib.suppress(Exception):
        shutil.rmtree(bench.bench_repo.get("checkout", ""), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
