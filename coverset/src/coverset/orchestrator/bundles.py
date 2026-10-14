"""Shard bundles: pull them from workers, then convert and ingest them concurrently.

A bundle is untrusted (the PR's own code ran next to the plugin that wrote it). Each one is pulled
with ``Worker.get_file`` before the worker is released, size-checked, written under
``<data_dir>/runs/<run_id>/``, then handed to :func:`ingest_one`: safe extraction
(:func:`~coverset.facts.untrusted.extract_bundle`), conversion with the head tree as the path
allow-list by :func:`~coverset.lang.read_any_bundle` (the manifest picks the reader: a neutral
bundle of any language through ``coverset_model.io``, else the pytest-coverset converter), or by a
job's own adapter when it names one, and a one-shard ingest
(:func:`~coverset.facts.ingest.ingest_shard`). The job's language is the authority, never the
bundle's manifest: for a ``python`` job the regions written for the run are the planner's index of
the head sources only, and the run's ``language`` is ``python``; for any other job they are that
index plus, for every path it could not index, the regions (and declared types) the bundle shipped
in ``functions.jsonl`` (:func:`~coverset.lang.neutral.merge_regions`). Extraction and conversion run
under a wall-clock limit (``IngestJob.read_timeout_s``) on top of the neutral reader's own budget.
A bundle that breaks a limit is logged with :func:`~coverset.facts.ingest.record_shard_failure` and
the run's coverage is then partial.

Conversion is CPU-bound (seconds for a large shard), so :class:`IngestPool` runs different shards
in parallel, in worker processes by default; ingest itself serialises on the facts run lock. A worker
process that dies breaks a ``ProcessPoolExecutor`` for good, so the pool is then recreated and the
affected jobs retried once; a job that still fails becomes a failed outcome (and a recorded shard
failure), never an exception, and the next run gets a working pool.
:func:`ingest_one` returns only what the verdict needs from the bundle (authoritative results,
collected items, static facts and line unions of the changed files, shapes of the functions of
interest), never the full per-test coverage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import Executor as FuturesExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from coverset.config import Settings
from coverset.facts.ingest import RunContext, ingest_shard, record_shard_failure
from coverset.facts.store import FactsStore
from coverset.facts.untrusted import BundleError, BundleLimits, extract_bundle
from coverset.lang import adapter_for_language, read_any_bundle
from coverset.lang.base import (
    CollectedItem,
    FuncSampling,
    FunctionRegion,
    FuncTriple,
    LanguageAdapter,
    NeutralBundle,
    ShapeObs,
    StaticFile,
    TestRow,
)
from coverset.lang.neutral import merge_regions

__all__ = [
    "READ_TIMEOUT_S",
    "IngestJob",
    "IngestTimeout",
    "IngestOutcome",
    "IngestPool",
    "bundle_limits",
    "ingest_one",
    "job_regions",
    "shared_ingest_pool",
]

type PoolMode = Literal["process", "thread"]

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 180.0
"""Default wall-clock limit of one shard's extraction plus conversion (the neutral reader's own
budget, ``coverset_model.io.Limits.max_seconds``, is 120 s, so it reports first)."""


def bundle_limits(settings: Settings) -> BundleLimits:
    """The untrusted-bundle limits for these settings."""
    return BundleLimits(max_bundle_bytes=settings.max_bundle_bytes)


@dataclass(frozen=True, slots=True)
class IngestJob:
    """One shard bundle to convert and ingest (picklable: it crosses a process boundary)."""

    facts_dir: str
    ctx: RunContext
    shard_id: int
    tarball: str
    limits: BundleLimits
    source_root: str
    """The control plane's checkout of the head commit (``n_lines`` and path checks)."""
    head_paths: frozenset[str] | None
    """The head tree's paths (rows naming any other path are dropped); None only in upload mode
    without a head tree, where the bundle itself is the only description of the commit."""
    functions: tuple[FunctionRegion, ...]
    keep_paths: frozenset[str] = frozenset()
    """Paths whose static facts and executed-line union come back in the outcome."""
    keep_funcs: frozenset[FuncTriple] = frozenset()
    """Functions whose shape observations and sampling come back in the outcome."""
    language: str = "python"
    adapter: LanguageAdapter | None = None
    """The language adapter whose ``read_bundle`` converts the bundle (picklable for the process
    pool); None means :func:`~coverset.lang.read_any_bundle`, which picks the reader by the
    bundle's manifest. It must accept the keyword arguments ``limits``, ``source_root`` and
    ``head_paths``."""
    read_timeout_s: float = READ_TIMEOUT_S
    """Wall-clock limit of extraction plus conversion; past it the shard fails (enforced with
    ``SIGALRM`` where the job runs on a process's main thread, as in the process pool)."""
    regions_from_bundle: bool = False
    """Take the bundle's shipped regions (and declared types) for paths ``functions`` lacks even
    for a ``python`` job. Only upload mode sets it, when no head checkout exists (no mirror and no
    uploaded sources): the uploader is authenticated as the repository and nothing else describes
    its functions. Managed runs never set it."""


class IngestTimeout(Exception):
    """Reading a bundle took longer than the job's ``read_timeout_s``."""


@contextlib.contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    """Raise :class:`IngestTimeout` in this thread after ``seconds`` (main thread only, else no-op)."""
    usable = (
        seconds > 0 and hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    )
    if not usable:
        yield
        return

    def expired(signum: int, frame: object) -> None:
        raise IngestTimeout(f"reading the bundle took longer than {seconds:g} s")

    previous = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    outer, interval = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer > 0:  # someone else's timer (a test runner's timeout) keeps running
            left = max(outer - (time.monotonic() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, interval)


def adapter_for(job: IngestJob) -> LanguageAdapter:
    """The adapter serving ``job``'s language (its own adapter when it names one)."""
    if job.adapter is not None:
        return job.adapter
    return adapter_for_language(job.language)


def read_job_bundle(job: IngestJob, bundle_dir: Path) -> NeutralBundle:
    """Convert ``job``'s extracted bundle: the job's adapter when set, else by the manifest."""
    if job.adapter is not None:
        return job.adapter.read_bundle(  # type: ignore[call-arg]
            bundle_dir, limits=job.limits, source_root=Path(job.source_root), head_paths=job.head_paths
        )
    return read_any_bundle(
        bundle_dir, limits=job.limits, source_root=Path(job.source_root), head_paths=job.head_paths
    )


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What one shard's ingest produced for the verdict."""

    shard_id: int
    ok: bool
    error: str | None = None
    bundle_sha256: str | None = None
    tests: tuple[TestRow, ...] = ()
    collected: tuple[CollectedItem, ...] = ()
    statics: dict[str, StaticFile] = field(default_factory=dict)
    union_lines: dict[str, frozenset[int]] = field(default_factory=dict)
    shapes: tuple[ShapeObs, ...] = ()
    sampling: tuple[FuncSampling, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    types: dict[tuple[str, str, int], Any] = field(default_factory=dict)
    """Declared types the bundle shipped for the regions it owns (non-Python jobs only), by triple."""


def ingest_one(job: IngestJob) -> IngestOutcome:
    """Extract, convert and ingest one shard bundle (runs in a pool worker)."""
    store = FactsStore(Path(job.facts_dir))
    workdir = Path(tempfile.mkdtemp(prefix=f"coverset-ingest-{job.shard_id}-"))
    try:
        with _deadline(job.read_timeout_s):
            extract_bundle(Path(job.tarball), workdir / "bundle", job.limits)
            bundle = read_job_bundle(job, workdir / "bundle")
        functions, types = job_regions(job, bundle)
        result = ingest_shard(
            store, job.ctx, bundle, job.shard_id, functions, language=job.language, types=types
        )
    except Exception as exc:  # a bundle (or its ingest) failed: the run's coverage becomes partial
        known = isinstance(exc, BundleError | IngestTimeout)
        reason = str(exc)[:500] if known else f"{type(exc).__name__}: {exc}"[:500]
        with contextlib.suppress(Exception):  # the failure record itself is best effort
            record_shard_failure(store, job.ctx, job.shard_id, reason=reason)
        return IngestOutcome(shard_id=job.shard_id, ok=False, error=reason)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    union: dict[str, set[int]] = {}
    for by_path in bundle.coverage.values():
        for path, lines in by_path.items():
            if path in job.keep_paths:
                union.setdefault(path, set()).update(lines)
    meta = bundle.meta if isinstance(bundle.meta, dict) else {}
    return IngestOutcome(
        shard_id=job.shard_id,
        ok=True,
        bundle_sha256=result.bundle_sha256,
        tests=bundle.tests,
        collected=bundle.collected,
        statics={p: s for p, s in bundle.static.items() if p in job.keep_paths},
        union_lines={p: frozenset(lines) for p, lines in union.items()},
        shapes=tuple(o for o in bundle.shapes if o.func in job.keep_funcs),
        sampling=tuple(s for s in bundle.sampling if s.func in job.keep_funcs),
        types={triple: dict(found) for triple, found in types.items()},
        meta={
            k: meta[k]
            for k in ("exit_code", "counts", "stop_requested", "tracer", "tracer_lost_at", "convert")
            if k in meta
        },
    )


def job_regions(job: IngestJob, bundle: NeutralBundle) -> tuple[list[FunctionRegion], dict[FuncTriple, Any]]:
    """The run's function regions and the shipped declared types to store with them.

    A ``python`` job keeps the planner's index of the head sources and nothing the bundle shipped
    (the sources are the reference, and a bundle is untrusted); any other job adds, for each path
    the index lacks, the bundle's regions and their declared types.
    """
    if job.language == "python" and not job.regions_from_bundle:
        return list(job.functions), {}
    functions = merge_regions(job.functions, bundle.functions)
    shipped = {r.triple for r in functions} - {r.triple for r in job.functions}
    return functions, {triple: found for triple, found in bundle.types.items() if triple in shipped}


class IngestPool:
    """Runs :func:`ingest_one` for several shards at once (processes by default, or threads)."""

    def __init__(self, mode: PoolMode = "process", max_workers: int | None = None) -> None:
        self.mode = mode
        self.max_workers = max_workers or max(1, min(4, os.cpu_count() or 1))
        self._executor: FuturesExecutor | None = None

    def _pool(self) -> FuturesExecutor:
        if self._executor is None:
            if self.mode == "process":
                self._executor = ProcessPoolExecutor(max_workers=self.max_workers)
            else:
                self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="ingest")
        return self._executor

    async def run(self, jobs: Sequence[IngestJob]) -> list[IngestOutcome]:
        """Ingest every job concurrently; a crashed job becomes a failed outcome, never an exception.

        Jobs hit by a broken pool (a worker process died) are retried once on a fresh pool.
        """
        if not jobs:
            return []
        results = await self._run_once(jobs)
        broken = [i for i, result in enumerate(results) if isinstance(result, BrokenExecutor)]
        if broken:
            log.warning("the ingest pool broke; recreating it and retrying %d job(s)", len(broken))
            self._reset()
            retried = await self._run_once([jobs[i] for i in broken])
            for index, result in zip(broken, retried, strict=True):
                results[index] = result
            if any(isinstance(result, BrokenExecutor) for result in retried):
                self._reset()  # leave a working pool for the next run
        outcomes: list[IngestOutcome] = []
        for job, result in zip(jobs, results, strict=True):
            if isinstance(result, BaseException):
                reason = f"ingest crashed: {result!r}"[:500]
                await asyncio.to_thread(_record_failure, job, reason)
                outcomes.append(IngestOutcome(shard_id=job.shard_id, ok=False, error=reason))
            else:
                outcomes.append(result)
        return outcomes

    async def _run_once(self, jobs: Sequence[IngestJob]) -> list[IngestOutcome | BaseException]:
        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future[IngestOutcome]] = []
        for job in jobs:
            try:
                futures.append(loop.run_in_executor(self._pool(), ingest_one, job))
            except (BrokenExecutor, RuntimeError) as exc:  # broken or shut down: submit raises
                failed: asyncio.Future[IngestOutcome] = loop.create_future()
                failed.set_exception(exc if isinstance(exc, BrokenExecutor) else BrokenExecutor(str(exc)))
                futures.append(failed)
        return list(await asyncio.gather(*futures, return_exceptions=True))

    def _reset(self) -> None:
        """Drop the current (broken) executor; the next submit starts a fresh one."""
        executor, self._executor = self._executor, None
        if executor is not None:
            with contextlib.suppress(Exception):
                executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Shut the pool down (idempotent)."""
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None


_shared: dict[PoolMode, IngestPool] = {}


def shared_ingest_pool(mode: PoolMode = "process") -> IngestPool:
    """A process-wide pool, so worker processes are started once and reused across runs."""
    pool = _shared.get(mode)
    if pool is None:
        pool = _shared[mode] = IngestPool(mode)
    return pool


def _record_failure(job: IngestJob, reason: str) -> None:
    """Best-effort shard-failure record for a job whose worker never got to record one."""
    with contextlib.suppress(Exception):
        record_shard_failure(FactsStore(Path(job.facts_dir)), job.ctx, job.shard_id, reason=reason)


def merge_sampling(rows: Sequence[FuncSampling]) -> dict[FuncTriple, FuncSampling]:
    """Combine per-shard sampling rows of each function (calls summed, tests unioned, any truncation,
    the first cap named)."""
    merged: dict[FuncTriple, FuncSampling] = {}
    for row in rows:
        prev = merged.get(row.func)
        if prev is None:
            merged[row.func] = row
            continue
        merged[row.func] = FuncSampling(
            func=row.func,
            calls=prev.calls + row.calls,
            tests=tuple(sorted(set(prev.tests) | set(row.tests))),
            truncated=prev.truncated or row.truncated,
            cap=prev.cap or row.cap,
        )
    return merged
