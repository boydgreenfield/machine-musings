"""Planning a run: environment, workers, collection, diff, prior run, wave 1, scores and shards.

:func:`plan_run` takes a ``queued`` run to the point where every worker holds the head source and
its shard's files, in this order:

1. refresh the repo's bare mirror; resolve the merge-base; export the head commit into a temporary
   control-plane checkout (for the environment export, function indexing and bundle conversion);
2. build the :class:`~coverset.lang.base.EnvSpec` (``uv export`` + the pytest-coverset wheel) and
   ``executor.ensure_env``; use the newest ready trusted snapshot when the executor has them;
3. acquire workers (as many as the last run of the repo needed, at least one) and ``put_tree`` the
   head archive into each: ``sandbox_ready``;
4. collect on worker 0 (``pytest --collect-only`` with pytest-coverset, read back ``collect.json``;
   the ``-q`` node id listing is the fallback);
5. diff ``base...head`` on the mirror and classify changed functions; pick the prior run (the PR's
   previous head, else the merge-base, else the latest default-branch baseline);
6. score every test, compute wave 1 (the change-covering set), deal shards (priority-snake LPT) and
   acquire more workers or re-shard to the number obtained (none at all fails the run with reason
   ``capacity``);
7. drop empty shards (an empty args file would make pytest run the whole suite), write each shard's
   args file, scores file and the full-trace list into its worker, and record per-test score,
   explanation, rank and file-order rank in ``test_results``.

**Database discipline.** Planning takes minutes at worst (environment build, collection), so it never
holds a write transaction open across that work: everything up to step 6 only *reads* (sessions with
``no_autoflush`` that never add or modify rows); the ``envs`` row and the ``tests`` rows are written in
their own short committed transactions; and the plan's run fields, ``shards`` and ``test_results`` are
written at the end in one short transaction through ``transact`` (the runner passes its event sink's,
so the run's own events keep flushing while it plans). CPU-bound analysis (indexing, scoring, wave 1,
sharding, YAML) runs in a thread, off the event loop.

Worker layout (paths relative to the worker root): ``repo/`` the checkout, ``run/`` the run's
files (``shard.args``, ``shard.scores.json``, ``full-trace.txt``, ``out/``, ``retry-<n>/``,
``STOP``). The plan owns the workers and the temporary checkout until :meth:`RunPlan.cleanup`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import secrets
import shutil
import statistics
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.analysis.cover import CoverSet, change_covering_set
from coverset.analysis.diff import FileDiff, changed_functions, parse_diff, run_git_diff
from coverset.analysis.functions import FunctionChange, FunctionRegion
from coverset.analysis.invariants import InvariantError, load_invariants, resolve_target
from coverset.analysis.prior import PriorChoice, select_prior_run
from coverset.analysis.scoring import ScoredTest, ScoringContext, exploration_seed, score_tests
from coverset.analysis.sharding import ShardPlan, plan_shards, reshard, shard_scores
from coverset.config import Settings
from coverset.db.models import Env, Repo, Run, Shard, Snapshot, TestResult, TestStats
from coverset.db.session import Database
from coverset.executors.base import (
    EnvRef,
    Executor,
    ExecutorError,
    Resources,
    SnapshotRef,
    Worker,
    release_all,
)
from coverset.executors.env_export import build_env_spec, build_plugin_wheel
from coverset.facts.queries import ParquetPriorRun
from coverset.facts.store import FactsStore
from coverset.lang.base import CollectedItem, EnvSpec, LanguageAdapter, PriorRun
from coverset.lang.python import CoversetConfig, PythonAdapter, read_tool_config
from coverset.orchestrator import history
from coverset.orchestrator.repos import (
    archive,
    ensure_mirror_async,
    extract_archive,
    merge_base,
    read_blobs,
    tree_paths,
)

__all__ = [
    "COLLECT_TIMEOUT_S",
    "REPO_DIR",
    "RUN_DIR",
    "PlanError",
    "RunPlan",
    "ShardSpec",
    "WorkerLayout",
    "WorkerLifetimes",
    "plan_run",
    "pythonpath",
    "run_transaction",
    "worker_env",
]

log = logging.getLogger(__name__)

REPO_DIR = "repo"
RUN_DIR = "run"
COLLECT_TIMEOUT_S = 300.0
MAX_NODEID_CHARS = 512
DEFAULT_MEDIAN_S = 0.5
"""Duration assumed for a test nobody has timed yet, when no other test has been timed either."""

WRITE_ATTEMPTS = 5
WRITE_RETRY_DELAY_S = 0.2

type EnvSpecBuilder = Callable[[Path, CoversetConfig], EnvSpec]
type Work[T] = Callable[[AsyncSession], Awaitable[T]]
type Transact = Callable[[Work[Any]], Awaitable[Any]]
"""Runs one unit of database work in a short transaction (with retries) and returns its result."""


async def run_transaction[T](
    db: Database,
    work: Work[T],
    *,
    lock: asyncio.Lock | None = None,
    attempts: int = WRITE_ATTEMPTS,
    delay_s: float = WRITE_RETRY_DELAY_S,
) -> T:
    """Run ``work(session)`` and commit, retrying with backoff while the database is busy.

    ``work`` must be safe to repeat: a failed attempt is rolled back before the next one.
    """
    for attempt in range(1, attempts + 1):
        try:
            async with lock if lock is not None else contextlib.nullcontext(), db.session() as session:
                result = await work(session)
                await session.commit()
                return result
        except OperationalError:
            if attempt == attempts:
                raise
            log.warning("database busy (attempt %d of %d); retrying", attempt, attempts)
            await asyncio.sleep(delay_s * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


class WorkerLifetimes:
    """When each worker of a run was acquired and released: the basis of ``runs.cost_estimate``.

    Cost is ``Σ executor.cost_usd(resources, released - acquired)`` over every worker the run held,
    including extras released right after sharding; a worker not yet released counts until now.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._spans: dict[str, list[Any]] = {}

    def acquired(self, workers: Sequence[Worker], resources: Resources) -> None:
        """Start the clock of ``workers``."""
        now = self._clock()
        for worker in workers:
            self._spans.setdefault(worker.id, [resources, now, None])

    def released(self, workers: Sequence[Worker]) -> None:
        """Stop the clock of ``workers`` (the first release counts)."""
        now = self._clock()
        for worker in workers:
            span = self._spans.get(worker.id)
            if span is not None and span[2] is None:
                span[2] = now

    def spans(self) -> list[tuple[Resources, float]]:
        """``(resources, seconds held)`` per worker."""
        now = self._clock()
        return [
            (r, max(0.0, (end if end is not None else now) - start)) for r, start, end in self._spans.values()
        ]

    def cost_usd(self, executor: Executor) -> float:
        """The run's worker cost on ``executor``'s rate card."""
        return sum(executor.cost_usd(resources, seconds) for resources, seconds in self.spans())

    def __len__(self) -> int:
        return len(self._spans)


class PlanError(RuntimeError):
    """The run cannot be planned; ``reason`` is the short machine-readable cause."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WorkerLayout:
    """Worker-relative paths of one shard's files."""

    args_file: str = f"{RUN_DIR}/shard.args"
    scores_file: str = f"{RUN_DIR}/shard.scores.json"
    full_trace_file: str = f"{RUN_DIR}/full-trace.txt"
    out_dir: str = f"{RUN_DIR}/out"
    stop_file: str = f"{RUN_DIR}/STOP"
    collect_dir: str = f"{RUN_DIR}/collect"

    @staticmethod
    def retry_dir(n: int) -> str:
        """Output directory of retry number ``n`` (never ingested)."""
        return f"{RUN_DIR}/retry-{n}"

    @staticmethod
    def retry_args(n: int) -> str:
        """Args file of retry number ``n``."""
        return f"{RUN_DIR}/retry-{n}.args"


LAYOUT = WorkerLayout()


@dataclass(slots=True)
class ShardSpec:
    """One shard: its tests in run order, its worker and its predicted duration."""

    shard_id: int
    nodeids: list[str]
    predicted_s: float
    worker: Worker


@dataclass(slots=True)
class RunPlan:
    """Everything the runner needs; owns the workers and the temporary checkout."""

    run_id: str
    repo_id: int
    default_branch: str
    head_sha: str
    base_sha: str | None
    merge_base: str | None
    mirror: Path
    checkout: Path
    config: CoversetConfig
    env: EnvRef
    workers: list[Worker]
    shards: list[ShardSpec]
    collected: list[CollectedItem]
    """Collected items in collection (file) order."""
    order: list[str]
    """The global priority order: wave 1 first, then the remaining tests by score."""
    scored: dict[str, ScoredTest]
    wave1: CoverSet
    changes: list[FunctionChange]
    changed_lines: dict[str, set[int]]
    changed_files: set[str]
    head_paths: frozenset[str]
    measured_sources: dict[str, str]
    head_functions: list[FunctionRegion]
    test_ids: dict[str, int]
    durations: dict[str, float]
    """p50 seconds by nodeid (unknown durations left out)."""
    full_trace: list[str]
    prior: PriorChoice | None
    quarantined: set[str]
    resources: Resources
    sandbox_ready_at: datetime
    collect_errors: list[dict[str, Any]] = field(default_factory=list)
    diff_text: dict[str, str] = field(default_factory=dict)
    """Each changed file's own unified diff text, by head path."""
    invariant_targets: dict[str, FunctionRegion] = field(default_factory=dict)
    snapshot_id: int | None = None
    lifetimes: WorkerLifetimes = field(default_factory=WorkerLifetimes)

    @property
    def ranks(self) -> dict[str, int]:
        """1-based position of each test in the global priority order."""
        return {nodeid: index for index, nodeid in enumerate(self.order, start=1)}

    @property
    def file_ranks(self) -> dict[str, int]:
        """1-based position of each test in plain collection (file) order."""
        return {item.nodeid: index for index, item in enumerate(self.collected, start=1)}

    @property
    def items_by_nodeid(self) -> dict[str, CollectedItem]:
        """Collected items by nodeid."""
        return {item.nodeid: item for item in self.collected}

    async def release_workers(self) -> None:
        """Release every worker of the plan (idempotent)."""
        workers, self.workers = self.workers, []
        try:
            await release_all(workers)
        finally:
            self.lifetimes.released(workers)

    async def cleanup(self) -> None:
        """Release the workers and delete the temporary checkout."""
        await self.release_workers()
        await asyncio.to_thread(shutil.rmtree, self.checkout, True)


def pythonpath(worker: Worker, config: CoversetConfig) -> str:
    """``PYTHONPATH`` for a worker: the source root (and the repo root when they differ or it is ``.``)."""
    root = worker.path(REPO_DIR)
    if config.source_root in ("", "."):
        return root
    return f"{worker.path(f'{REPO_DIR}/{config.source_root}')}:{root}"


def worker_env(
    worker: Worker, config: CoversetConfig, nonce: str, extra_env: Mapping[str, str]
) -> dict[str, str]:
    """The environment of every exec: ``extra_env``, then ``PYTHONPATH`` and the event nonce."""
    return {**extra_env, "PYTHONPATH": pythonpath(worker, config), "COVERSET_EVENT_NONCE": nonce}


async def plan_run(
    db: Database,
    settings: Settings,
    store: FactsStore,
    run: Run,
    *,
    executor: Executor,
    adapter: LanguageAdapter | None = None,
    env_spec_builder: EnvSpecBuilder | None = None,
    shards_override: int | None = None,
    forge: Any = None,
    clock: Callable[[], datetime] | None = None,
    on_sandbox_ready: Callable[[datetime], Any] | None = None,
    transact: Transact | None = None,
    lifetimes: WorkerLifetimes | None = None,
) -> RunPlan:
    """Plan ``run`` (see the module docstring). Raises :class:`PlanError` with a reason.

    ``run`` is only read (a detached row is fine). ``transact`` runs the final write (default: a
    retried transaction on ``db``); ``lifetimes`` records when workers are acquired and released.
    """
    adapter = adapter or PythonAdapter()
    now = clock or settings.now
    lifetimes = lifetimes if lifetimes is not None else WorkerLifetimes()
    write: Transact = transact or (lambda work: run_transaction(db, work))
    async with db.session() as session:
        repo = await session.get(Repo, run.repo_id)
    if repo is None:
        raise PlanError("repo", f"run {run.id} has no repo")
    try:
        mirror = await ensure_mirror_async(settings, repo, forge=forge)
    except Exception as exc:
        raise PlanError("mirror", f"cannot refresh the mirror: {exc}") from exc
    base_commit = None
    if run.base_sha:
        base_commit = await asyncio.to_thread(merge_base, mirror, run.base_sha, run.head_sha) or run.base_sha
    try:
        tarball = await asyncio.to_thread(archive, mirror, run.head_sha)
    except Exception as exc:
        raise PlanError("archive", f"cannot export {run.head_sha[:12]}: {exc}") from exc
    checkout = Path(tempfile.mkdtemp(prefix="coverset-plan-"))
    workers: list[Worker] = []
    try:
        await asyncio.to_thread(extract_archive, tarball, checkout)
        config = await asyncio.to_thread(_load_config, adapter, checkout)
        head_paths = frozenset(await asyncio.to_thread(tree_paths, mirror, run.head_sha))
        env = await _ensure_env(db, settings, executor, adapter, checkout, config, env_spec_builder)
        base_ref = await _snapshot_or_env(db, executor, env, repo, run)
        resources = Resources(
            cpu=settings.shard_cpu_limit,
            memory_mib=settings.shard_memory_mib,
            network=run.network or settings.sandbox_network,
            timeout_s=settings.run_timeout_s,
        )
        labels = {"coverset_run": run.id, "repo": str(repo.id), "role": "shard"}
        want = shards_override or await _estimated_shards(db, repo.id, config, settings)
        workers = await _acquire(executor, base_ref, want, resources, settings, labels)
        lifetimes.acquired(workers, resources)
        await asyncio.gather(*(w.put_tree(tarball, REPO_DIR) for w in workers))
        sandbox_ready = now()
        if on_sandbox_ready is not None:
            await _maybe_await(on_sandbox_ready(sandbox_ready))
        collected, collect_errors = await _collect(workers[0], config, run, settings)
        analysis = await _analyse(
            db,
            settings,
            store,
            run,
            repo,
            adapter,
            mirror=mirror,
            checkout=checkout,
            base_commit=base_commit,
            config=config,
            collected=collected,
            head_paths=head_paths,
            now=now(),
        )
        limit = min(shards_override or config.max_shards, settings.max_shards)
        shard_plan = await asyncio.to_thread(analysis.shard_plan, limit, forced=shards_override is not None)
        dealt = [list(nodeids) for nodeids in shard_plan.shards if nodeids]
        predicted = [
            p for nodeids, p in zip(shard_plan.shards, shard_plan.predicted_s, strict=True) if nodeids
        ]
        workers = await _fit_workers(
            executor, base_ref, workers, len(dealt), resources, settings, labels, tarball, lifetimes
        )
        if len(workers) < len(dealt):
            shard_plan = reshard(shard_plan, len(workers))
            dealt = [list(nodeids) for nodeids in shard_plan.shards if nodeids]
            predicted = [
                p for nodeids, p in zip(shard_plan.shards, shard_plan.predicted_s, strict=True) if nodeids
            ]
            workers = await _fit_workers(
                executor, base_ref, workers, len(dealt), resources, settings, labels, tarball, lifetimes
            )
        shards = [
            ShardSpec(shard_id=k, nodeids=nodeids, predicted_s=predicted[k], worker=workers[k])
            for k, nodeids in enumerate(dealt)
        ]
        result = RunPlan(
            run_id=run.id,
            repo_id=repo.id,
            default_branch=repo.default_branch,
            head_sha=run.head_sha,
            base_sha=run.base_sha,
            merge_base=base_commit,
            mirror=mirror,
            checkout=checkout,
            config=config,
            env=env,
            workers=workers,
            shards=shards,
            collected=collected,
            order=analysis.order,
            scored={t.nodeid: t for t in analysis.scored_list},
            wave1=analysis.wave1,
            changes=analysis.changes,
            changed_lines=analysis.changed_lines,
            changed_files=analysis.changed_files,
            diff_text=analysis.diff_text,
            head_paths=head_paths,
            measured_sources=analysis.measured_sources,
            head_functions=analysis.head_functions,
            test_ids=analysis.test_ids,
            durations=analysis.durations,
            full_trace=analysis.full_trace,
            prior=analysis.prior,
            quarantined=analysis.quarantined,
            resources=resources,
            sandbox_ready_at=sandbox_ready,
            collect_errors=collect_errors,
            invariant_targets=analysis.invariant_targets,
            snapshot_id=base_ref.snapshot_id if isinstance(base_ref, SnapshotRef) else None,
            lifetimes=lifetimes,
        )
        await _write_shard_files(result)
        await write(_plan_writer(run.id, result))
        return result
    except BaseException:
        try:
            await release_all(workers)
        finally:
            lifetimes.released(workers)
            await asyncio.to_thread(shutil.rmtree, checkout, True)
        raise


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        await value


def _load_config(adapter: LanguageAdapter, checkout: Path) -> CoversetConfig:
    loader = getattr(adapter, "load_config", None)
    try:
        if loader is not None:
            return loader(checkout)
        pyproject = checkout / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
        return read_tool_config(text, repo_root=checkout)
    except ValueError as exc:
        raise PlanError("config", f"[tool.coverset] is invalid: {exc}") from exc


async def _ensure_env(
    db: Database,
    settings: Settings,
    executor: Executor,
    adapter: LanguageAdapter,
    checkout: Path,
    config: CoversetConfig,
    builder: EnvSpecBuilder | None,
) -> EnvRef:
    try:
        if builder is not None:
            spec = await asyncio.to_thread(builder, checkout, config)
        else:
            wheel = await asyncio.to_thread(build_plugin_wheel)
            spec = await asyncio.to_thread(
                build_env_spec, adapter, checkout, {"python": config.python}, wheel
            )
        env = await executor.ensure_env(spec)
    except (ExecutorError, RuntimeError, ValueError, OSError) as exc:
        raise PlanError("environment", f"cannot build the test environment: {exc}") from exc
    await _record_env(db, settings, env)
    return env


async def _record_env(db: Database, settings: Settings, env: EnvRef) -> None:
    """Insert the ``envs`` row in its own short transaction (a concurrent insert of it is fine)."""

    async def work(session: AsyncSession) -> None:
        if await session.get(Env, env.env_hash) is None:
            session.add(
                Env(env_hash=env.env_hash, python=env.python, image_id=None, created_at=settings.now())
            )
            await session.flush()

    with contextlib.suppress(IntegrityError):  # another run inserted the same environment first
        await run_transaction(db, work)


async def _snapshot_or_env(
    db: Database, executor: Executor, env: EnvRef, repo: Repo, run: Run
) -> EnvRef | SnapshotRef:
    """The newest ready, unexpired trusted snapshot for this env (never waits for one), else ``env``."""
    if not executor.capabilities().fs_snapshot:
        return env
    async with db.session() as session:
        snapshot = await session.scalar(
            select(Snapshot)
            .where(
                Snapshot.repo_id == repo.id,
                Snapshot.env_hash == env.env_hash,
                Snapshot.status == "ready",
                Snapshot.image_id.is_not(None),
            )
            .order_by(Snapshot.created_at.desc())
            .limit(1)
        )
    if snapshot is None or snapshot.image_id is None:
        return env
    if snapshot.expires_at is not None and snapshot.expires_at <= run.created_at:
        return env
    return SnapshotRef(env=env, sha=snapshot.sha, image_id=snapshot.image_id, snapshot_id=snapshot.id)


async def _estimated_shards(db: Database, repo_id: int, config: CoversetConfig, settings: Settings) -> int:
    """Workers to ask for before collection: what the repo's timed tests need, at least one."""
    async with db.session() as session:
        total_ms = await session.scalar(
            select(func.sum(TestStats.p50_ms)).where(TestStats.repo_id == repo_id)
        )
    cap = max(1, min(config.max_shards, settings.max_shards))
    if not total_ms:
        return 1
    return max(1, min(cap, math.ceil(total_ms / 1000.0 / config.target_shard_seconds)))


async def _acquire(
    executor: Executor,
    base: EnvRef | SnapshotRef,
    n: int,
    resources: Resources,
    settings: Settings,
    labels: Mapping[str, str],
) -> list[Worker]:
    try:
        workers = await executor.acquire(base, n, resources, settings.pool_ttl_s, labels)
    except ExecutorError as exc:
        raise PlanError("capacity", f"no workers available: {exc}") from exc
    if not workers:
        raise PlanError("capacity", "no workers available (capacity)")
    return list(workers)


async def _fit_workers(
    executor: Executor,
    base: EnvRef | SnapshotRef,
    workers: list[Worker],
    wanted: int,
    resources: Resources,
    settings: Settings,
    labels: Mapping[str, str],
    tarball: bytes,
    lifetimes: WorkerLifetimes,
) -> list[Worker]:
    """Acquire more workers or release extras so there is one per shard (fewer if capacity is short)."""
    if len(workers) > wanted:
        extra = workers[wanted:]
        try:
            await release_all(extra)
        finally:
            lifetimes.released(extra)
        return workers[:wanted]
    if len(workers) < wanted:
        try:
            more = await executor.acquire(base, wanted - len(workers), resources, settings.pool_ttl_s, labels)
        except ExecutorError as exc:
            log.warning("could not acquire %d more workers: %s", wanted - len(workers), exc)
            more = []
        lifetimes.acquired(more, resources)
        if more:
            try:
                await asyncio.gather(*(w.put_tree(tarball, REPO_DIR) for w in more))
            except BaseException:
                try:
                    await release_all(list(more))
                finally:
                    lifetimes.released(more)
                raise
        return [*workers, *more]
    return workers


async def _collect(
    worker: Worker, config: CoversetConfig, run: Run, settings: Settings
) -> tuple[list[CollectedItem], list[dict[str, Any]]]:
    """Collect on worker 0 and return the items in collection order (plus collection errors)."""
    nonce = secrets.token_hex(16)
    lines: list[str] = []
    cmd = [
        "python",
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--rootdir={worker.path(REPO_DIR)}",
        "--coverset-out",
        worker.path(LAYOUT.collect_dir),
        "--coverset-shapes",
        "off",
        *config.testpaths,
    ]
    env = worker_env(worker, config, nonce, run.extra_env or {})
    result = await worker.run(
        cmd,
        cwd=REPO_DIR,
        env=env,
        timeout_s=COLLECT_TIMEOUT_S,
        on_line=lambda text, stream: lines.append(text) if stream == "stdout" else None,
        stdout_cap=settings.max_stdout_bytes,
    )
    if result.exit_code not in (0, 5):
        tail = "\n".join(lines[-20:])[-2000:]
        raise PlanError("collection", f"pytest --collect-only exited {result.exit_code}: {tail}")
    try:
        raw = await worker.get_file(f"{LAYOUT.collect_dir}/collect.json")
        items, errors = await asyncio.to_thread(parse_collect_json, raw)
    except (ExecutorError, ValueError, RecursionError) as exc:
        log.warning("collect.json unavailable (%s); falling back to the -q listing", exc)
        items, errors = parse_collect_listing(lines), []
    return items, errors


def parse_collect_json(raw: bytes) -> tuple[list[CollectedItem], list[dict[str, Any]]]:
    """Items and errors of the plugin's ``collect.json`` (untrusted: validated field by field)."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("collect.json has no items list")
    items: list[CollectedItem] = []
    seen: set[str] = set()
    for entry in data["items"]:
        if not isinstance(entry, dict):
            continue
        nodeid, file = entry.get("nodeid"), entry.get("file")
        if not isinstance(nodeid, str) or not nodeid or len(nodeid) > MAX_NODEID_CHARS or nodeid in seen:
            continue
        if "\n" in nodeid or "\0" in nodeid:
            continue
        line = entry.get("line")
        seen.add(nodeid)
        items.append(
            CollectedItem(
                nodeid=nodeid,
                file=file if isinstance(file, str) else nodeid.split("::", 1)[0],
                line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                markers=tuple(m for m in entry.get("markers", ()) if isinstance(m, str))[:64],
                invariant_ids=tuple(i for i in entry.get("invariant_ids", ()) if isinstance(i, str))[:64],
                hypothesis=entry.get("hypothesis") is True,
            )
        )
    errors = [e for e in data.get("errors", []) if isinstance(e, dict)][:100]
    return items, errors


def parse_collect_listing(lines: Sequence[str]) -> list[CollectedItem]:
    """Node ids from ``pytest --collect-only -q`` output (one ``path::name`` per line)."""
    items: list[CollectedItem] = []
    seen: set[str] = set()
    for line in lines:
        text = line.strip()
        if "::" not in text or text.startswith(("\x1e", "=", "-")) or len(text) > MAX_NODEID_CHARS:
            continue
        if text in seen:
            continue
        seen.add(text)
        items.append(CollectedItem(text, text.split("::", 1)[0], None, (), (), False))
    return items


@dataclass(slots=True)
class _Analysis:
    """The repo-side half of a plan (everything but workers)."""

    scored_list: list[ScoredTest]
    order: list[str]
    wave1: CoverSet
    changes: list[FunctionChange]
    changed_lines: dict[str, set[int]]
    changed_files: set[str]
    diff_text: dict[str, str]
    measured_sources: dict[str, str]
    head_functions: list[FunctionRegion]
    test_ids: dict[str, int]
    durations: dict[str, float]
    full_trace: list[str]
    prior: PriorChoice | None
    quarantined: set[str]
    invariant_targets: dict[str, FunctionRegion]
    groups: dict[str, str]
    median_s: float
    target_shard_seconds: float

    def shard_plan(self, max_shards: int, *, forced: bool = False) -> ShardPlan:
        """Deal the scored tests over at most ``max_shards`` shards (exactly that many when ``forced``)."""
        if not self.scored_list:
            return ShardPlan(shards=[[]], predicted_s=[0.0])
        return plan_shards(
            self.scored_list,
            self.wave1,
            target_shard_seconds=1e-9 if forced else self.target_shard_seconds,
            max_shards=max(1, max_shards),
            median_s=self.median_s,
            groups=self.groups,
        )


async def _analyse(
    db: Database,
    settings: Settings,
    store: FactsStore,
    run: Run,
    repo: Repo,
    adapter: LanguageAdapter,
    *,
    mirror: Path,
    checkout: Path,
    base_commit: str | None,
    config: CoversetConfig,
    collected: list[CollectedItem],
    head_paths: frozenset[str],
    now: datetime,
) -> _Analysis:
    """Diff, functions, prior run, history, scores and wave 1 (reads only, plus the ``tests`` rows)."""
    diffs, diff_text = (
        await asyncio.to_thread(_diff, mirror, base_commit, run.head_sha) if base_commit else ({}, {})
    )
    changed_files = {d.path for d in diffs.values() if d.status != "deleted"}
    changed_lines = {d.path: set(d.added) for d in diffs.values() if d.status != "deleted" and d.added}
    measured_sources, head_functions, invariant_targets = await asyncio.to_thread(
        _index_head, adapter, checkout, config, head_paths
    )
    changes = await asyncio.to_thread(_changes, adapter, mirror, base_commit, checkout, diffs, head_paths)

    tests = await _register_tests(db, repo.id, [i.nodeid for i in collected], run.id)
    test_ids = {nodeid: test_id for nodeid, (test_id, _) in tests.items()}
    quarantined = {nodeid for nodeid, (_, flag) in tests.items() if flag}
    async with db.session() as session:
        with session.no_autoflush:
            prior_choice, prior = await _prior(session, store, run, repo, base_commit)
            previous = await history.previous_pr_run(session, run)
            if previous is None:
                baseline = await history.latest_baseline(
                    session, repo.id, repo.default_branch, exclude=run.id
                )
                previous_id = baseline.id if baseline is not None else None
            else:
                previous_id = previous.id
            test_history = await history.load_test_history(
                session, repo.id, test_ids, previous_run_id=previous_id
            )
    scoring_ctx = ScoringContext(
        prior=prior,
        changed=changes,
        changed_files=changed_files,
        test_history=test_history,
        now=now,
        rng_seed=exploration_seed(run.id),
    )
    scored_list, wave1, order, durations, median_s = await asyncio.to_thread(
        _score, collected, changes, prior, scoring_ctx, changed_files
    )
    full_trace = sorted(
        {p for p in changed_files if p.endswith(".py") and p in measured_sources}
        | {r.path for r in invariant_targets.values()}
    )
    return _Analysis(
        scored_list=scored_list,
        order=order,
        wave1=wave1,
        changes=changes,
        changed_lines=changed_lines,
        changed_files=changed_files,
        diff_text=diff_text,
        measured_sources=measured_sources,
        head_functions=head_functions,
        test_ids=test_ids,
        durations=durations,
        full_trace=full_trace,
        prior=prior_choice,
        quarantined=quarantined,
        invariant_targets=invariant_targets,
        groups={item.nodeid: item.file for item in collected},
        median_s=median_s,
        target_shard_seconds=config.target_shard_seconds,
    )


def _index_head(
    adapter: LanguageAdapter, checkout: Path, config: CoversetConfig, head_paths: frozenset[str]
) -> tuple[dict[str, str], list[FunctionRegion], dict[str, FunctionRegion]]:
    """Measured sources, their function regions and the invariant targets (CPU and file I/O)."""
    measured_sources = _measured_sources(checkout, config, head_paths)
    head_functions = [
        r for path, text in measured_sources.items() for r in adapter.index_functions(path, text)
    ]
    return measured_sources, head_functions, _invariant_targets(checkout, config, head_functions)


def _score(
    collected: list[CollectedItem],
    changes: list[FunctionChange],
    prior: PriorRun | None,
    scoring_ctx: ScoringContext,
    changed_files: set[str],
) -> tuple[list[ScoredTest], CoverSet, list[str], dict[str, float], float]:
    """Scores, wave 1, the global order, known p50s and their median (pure CPU)."""
    scored_list = score_tests(collected, scoring_ctx)
    durations = {t.nodeid: t.p50_s for t in scored_list if t.p50_s is not None}
    wave1 = change_covering_set(collected, changes, prior, durations, changed_files=changed_files)
    order = list(dict.fromkeys([*wave1.tests, *(t.nodeid for t in scored_list)]))
    known = [d for d in durations.values() if d > 0]
    median_s = statistics.median(known) if known else DEFAULT_MEDIAN_S
    return scored_list, wave1, order, durations, median_s


async def _register_tests(
    db: Database, repo_id: int, nodeids: list[str], run_id: str
) -> dict[str, tuple[int, bool]]:
    """``(test id, quarantined)`` by nodeid, creating missing ``tests`` rows in a short transaction.

    Two runs registering the same new tests race on the unique key; the loser retries and finds
    the winner's rows.
    """

    async def work(session: AsyncSession) -> dict[str, tuple[int, bool]]:
        rows = await history.register_tests(session, repo_id, nodeids, run_id)
        return {nodeid: (row.id, bool(row.quarantined)) for nodeid, row in rows.items()}

    for attempt in range(1, 4):
        try:
            return await run_transaction(db, work)
        except IntegrityError:
            if attempt == 3:
                raise
            await asyncio.sleep(0.05 * attempt)
    raise AssertionError("unreachable")


def _diff(mirror: Path, base: str, head: str) -> tuple[dict[str, FileDiff], dict[str, str]]:
    """Parsed per-file changes of ``base...head`` and each file's own unified diff text."""
    text = run_git_diff(mirror, base, head)
    return parse_diff(text), split_diff(text)


def split_diff(text: str) -> dict[str, str]:
    """Unified diff ``text`` cut into one chunk per file, keyed by the head-side path (else base-side)."""
    chunks: dict[str, str] = {}
    current: list[str] = []
    paths: dict[str, str] = {}

    def close() -> None:
        path = paths.get("b") or paths.get("a")
        if path is not None and current:
            chunks[path] = "".join(current)

    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            close()
            current, paths = [line], {}
            continue
        current.append(line)
        for marker, side in (("--- a/", "a"), ("+++ b/", "b")):
            if line.startswith(marker) and side not in paths:
                paths[side] = line[len(marker) :].rstrip("\n")
    close()
    return chunks


def _changes(
    adapter: LanguageAdapter,
    mirror: Path,
    base_commit: str | None,
    checkout: Path,
    diffs: Mapping[str, FileDiff],
    head_paths: frozenset[str],
) -> list[FunctionChange]:
    """Classified function changes of the diff's Python files."""
    py_diffs = {p: d for p, d in diffs.items() if p.endswith(".py") or d.base_path.endswith(".py")}
    if not py_diffs or base_commit is None:
        return []
    base_blobs = read_blobs(mirror, base_commit, [d.base_path for d in py_diffs.values()])
    base_tree = {p: _text(b) for p, b in base_blobs.items()}
    head_tree: dict[str, str | None] = {}
    for path in py_diffs:
        if path in head_paths and (checkout / path).is_file():
            head_tree[path] = _text((checkout / path).read_bytes())
    return changed_functions(adapter, base_tree, head_tree, py_diffs)  # type: ignore[arg-type]


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _measured_sources(checkout: Path, config: CoversetConfig, head_paths: frozenset[str]) -> dict[str, str]:
    """Head text of every non-test ``.py`` file under the source root that is in the head tree."""
    root = "" if config.source_root in ("", ".") else config.source_root.rstrip("/") + "/"
    sources: dict[str, str] = {}
    for path in sorted(head_paths):
        if not path.endswith(".py") or not path.startswith(root):
            continue
        name = path.rpartition("/")[2]
        if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
            continue
        parts = path.split("/")
        if any(part in ("tests", "test", ".venv", "venv") for part in parts[:-1]):
            continue
        top = path[len(root) :].split("/", 1)[0].removesuffix(".py")
        if config.source and top not in {name.split(".", 1)[0] for name in config.source}:
            continue
        file = checkout / path
        if file.is_file() and not file.is_symlink():
            text = _text(file.read_bytes())
            if text is not None:
                sources[path] = text
    return sources


def _invariant_targets(
    checkout: Path, config: CoversetConfig, head_functions: Sequence[FunctionRegion]
) -> dict[str, FunctionRegion]:
    """Function regions targeted by the head's invariants (their files are fully traced)."""
    path = checkout / config.invariants
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        invariants = load_invariants(path.read_text(encoding="utf-8"))
    except (InvariantError, UnicodeDecodeError):
        return {}
    targets: dict[str, FunctionRegion] = {}
    for inv in invariants:
        region = resolve_target(inv, head_functions, config.source_root)
        if region is not None and region.kind in ("function", "method"):
            targets[inv.id] = region
    return targets


async def _prior(
    session: AsyncSession, store: FactsStore, run: Run, repo: Repo, base_commit: str | None
) -> tuple[PriorChoice | None, PriorRun | None]:
    candidates = await history.prior_candidates(session, repo.id, exclude=run.id, store=store)
    previous = await history.previous_pr_run(session, run)
    choice = select_prior_run(
        candidates,
        previous_head=previous.head_sha if previous is not None else None,
        merge_base=base_commit,
        default_branch=repo.default_branch,
    )
    if choice is None:
        return None, None
    return choice, ParquetPriorRun(store, repo.id, choice.run_id)


async def _write_shard_files(plan: RunPlan) -> None:
    """Args, scores and full-trace files into each shard's worker."""
    scores = shard_scores(
        ShardPlan(shards=[s.nodeids for s in plan.shards], predicted_s=[0.0] * len(plan.shards))
    )
    full_trace = "".join(f"{p}\n" for p in plan.full_trace).encode()
    writes = []
    for shard in plan.shards:
        args = "".join(f"{n}\n" for n in shard.nodeids).encode("utf-8")
        writes.append(shard.worker.write_file(LAYOUT.args_file, args))
        writes.append(
            shard.worker.write_file(LAYOUT.scores_file, json.dumps(scores[shard.shard_id]).encode())
        )
        writes.append(shard.worker.write_file(LAYOUT.full_trace_file, full_trace))
    await asyncio.gather(*writes)


def _plan_writer(run_id: str, plan: RunPlan) -> Work[None]:
    """The final write of a plan: run fields, shard rows and one ``test_results`` row per collected test."""
    ranks, file_ranks = plan.ranks, plan.file_ranks
    shard_of = {n: s.shard_id for s in plan.shards for n in s.nodeids}
    basis = plan.prior.describe() if plan.prior is not None else "no prior run"
    wave1 = set(plan.wave1.tests)
    shard_rows = [
        {
            "run_id": run_id,
            "shard_id": shard.shard_id,
            "status": "pending",
            "worker_ref": shard.worker.id,
            "test_count": len(shard.nodeids),
            "predicted_s": shard.predicted_s,
        }
        for shard in plan.shards
    ]
    result_rows = []
    for item in plan.collected:
        scored = plan.scored.get(item.nodeid)
        explanation = {
            "text": plan.wave1.reasons.get(item.nodeid) or (scored.explanation if scored else ""),
            "components": scored.components if scored else {},
            "basis": basis,
            "wave1": item.nodeid in wave1,
            "file_rank": file_ranks[item.nodeid],
        }
        result_rows.append(
            {
                "run_id": run_id,
                "test_id": plan.test_ids[item.nodeid],
                "outcome": "not_run",
                "shard_id": shard_of.get(item.nodeid),
                "quarantined": item.nodeid in plan.quarantined,
                "score": scored.score if scored else None,
                "score_explanation": explanation,
                "rank": ranks.get(item.nodeid),
                "attempts": 0,
                "flaky": False,
            }
        )

    async def work(session: AsyncSession) -> None:
        live = await session.get(Run, run_id)
        if live is None:
            raise PlanError("run", f"run {run_id} disappeared while planning")
        live.env_hash = plan.env.env_hash
        live.snapshot_id = plan.snapshot_id
        live.scoring_basis_run_id = plan.prior.run_id if plan.prior is not None else None
        live.shard_count = len(plan.shards)
        timeline = dict(live.timeline or {})
        timeline.setdefault("sandbox_ready", plan.sandbox_ready_at.isoformat())
        live.timeline = timeline
        if shard_rows:
            await session.execute(insert(Shard), shard_rows)
        if result_rows:
            await session.execute(insert(TestResult), result_rows)

    return work
