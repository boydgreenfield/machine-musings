"""Shard bundles: pull them from workers, then convert and ingest them concurrently.

A bundle is untrusted (the PR's own code ran next to the plugin that wrote it). Each one is pulled
with ``Worker.get_file`` before the worker is released, size-checked, written under
``<data_dir>/runs/<run_id>/``, then handed to :func:`ingest_one`: safe extraction
(:func:`~coverset.facts.untrusted.extract_bundle`), conversion with the head tree as the path
allow-list (:func:`~coverset.facts.convert.read_python_bundle`) and a one-shard ingest
(:func:`~coverset.facts.ingest.ingest_shard`). A bundle that breaks a limit is logged with
:func:`~coverset.facts.ingest.record_shard_failure` and the run's coverage is then partial.

Conversion is CPU-bound (seconds for a large shard), so :class:`IngestPool` runs different shards
in parallel, in worker processes by default; ingest itself serialises on the facts run lock.
:func:`ingest_one` returns only what the verdict needs from the bundle (authoritative results,
collected items, static facts and line unions of the changed files, shapes of the functions of
interest), never the full per-test coverage.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from concurrent.futures import Executor as FuturesExecutor
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from coverset.config import Settings
from coverset.facts.convert import read_python_bundle
from coverset.facts.ingest import RunContext, ingest_shard, record_shard_failure
from coverset.facts.store import FactsStore
from coverset.facts.untrusted import BundleError, BundleLimits, extract_bundle
from coverset.lang.base import (
    CollectedItem,
    FuncSampling,
    FunctionRegion,
    FuncTriple,
    ShapeObs,
    StaticFile,
    TestRow,
)

__all__ = [
    "IngestJob",
    "IngestOutcome",
    "IngestPool",
    "bundle_limits",
    "ingest_one",
    "shared_ingest_pool",
]

type PoolMode = Literal["process", "thread"]


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
    head_paths: frozenset[str]
    functions: tuple[FunctionRegion, ...]
    keep_paths: frozenset[str] = frozenset()
    """Paths whose static facts and executed-line union come back in the outcome."""
    keep_funcs: frozenset[FuncTriple] = frozenset()
    """Functions whose shape observations and sampling come back in the outcome."""
    language: str = "python"


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


def ingest_one(job: IngestJob) -> IngestOutcome:
    """Extract, convert and ingest one shard bundle (runs in a pool worker)."""
    store = FactsStore(Path(job.facts_dir))
    workdir = Path(tempfile.mkdtemp(prefix=f"coverset-ingest-{job.shard_id}-"))
    try:
        extract_bundle(Path(job.tarball), workdir / "bundle", job.limits)
        bundle = read_python_bundle(
            workdir / "bundle",
            limits=job.limits,
            source_root=Path(job.source_root),
            head_paths=job.head_paths,
        )
        result = ingest_shard(store, job.ctx, bundle, job.shard_id, job.functions, language=job.language)
    except Exception as exc:  # a bundle (or its ingest) failed: the run's coverage becomes partial
        reason = str(exc)[:500] if isinstance(exc, BundleError) else f"{type(exc).__name__}: {exc}"[:500]
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
        meta={
            k: meta[k] for k in ("exit_code", "counts", "stop_requested", "tracer", "convert") if k in meta
        },
    )


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
        """Ingest every job concurrently; a crashed job becomes a failed outcome, never an exception."""
        loop = asyncio.get_running_loop()
        pool = self._pool()
        futures = [loop.run_in_executor(pool, ingest_one, job) for job in jobs]
        results = await asyncio.gather(*futures, return_exceptions=True)
        outcomes: list[IngestOutcome] = []
        for job, result in zip(jobs, results, strict=True):
            if isinstance(result, BaseException):
                outcomes.append(
                    IngestOutcome(shard_id=job.shard_id, ok=False, error=f"ingest crashed: {result!r}"[:500])
                )
            else:
                outcomes.append(result)
        return outcomes

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


def merge_sampling(rows: Sequence[FuncSampling]) -> dict[FuncTriple, FuncSampling]:
    """Combine per-shard sampling rows of each function (calls summed, tests unioned, any truncation)."""
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
        )
    return merged
