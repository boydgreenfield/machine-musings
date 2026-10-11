"""Reaping workers that outlived their run, generic over executors.

Executors whose workers can outlive the control plane (Modal sandboxes) implement
:class:`~coverset.executors.base.ReapingExecutor`; :func:`reap` hands them the ids of runs that are
still active so they terminate everything else (or everything with ``all=True``). Local workers die
with their process and are swept from the temp directory at start-up, so for them this is a no-op.
``coverset modal reap [--all]`` and the 60 s loop that ``coverset serve`` starts when the executor is
Modal (:func:`reap_forever`, via :func:`coverset.app.schedule_executor_upkeep`) call :func:`reap`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import Run
from coverset.db.session import Database
from coverset.executors.base import Executor, ReapingExecutor, ReapReport

__all__ = ["REAP_INTERVAL_S", "active_run_ids", "reap", "reap_forever"]

log = logging.getLogger(__name__)

REAP_INTERVAL_S = 60.0


async def active_run_ids(session: AsyncSession) -> set[str]:
    """Ids of runs that may legitimately hold workers right now."""
    rows = await session.scalars(select(Run.id).where(Run.status.in_(("queued", "running"))))
    return set(rows)


async def reap(executor: Executor, *, all: bool = False, active: set[str] | None = None) -> ReapReport:
    """Terminate ``executor``'s workers not tied to ``active`` runs (every one with ``all``)."""
    if not isinstance(executor, ReapingExecutor):
        return ReapReport()
    return await executor.reap(active_run_ids=set() if all else set(active or ()), all=all)


async def reap_forever(
    db: Database,
    executor: Executor,
    *,
    interval_s: float = REAP_INTERVAL_S,
    stop: asyncio.Event | None = None,
) -> int:
    """Reap now and every ``interval_s`` until ``stop`` is set or the task is cancelled.

    Errors are logged, never raised. Returns the number of passes made (0 for executors that do not
    reap).
    """
    if not isinstance(executor, ReapingExecutor):
        return 0
    stop = stop or asyncio.Event()
    passes = 0
    while not stop.is_set():
        passes += 1
        try:
            async with db.session() as session:
                active = await active_run_ids(session)
            report = await reap(executor, active=active)
            if report.terminated:
                log.info("reaped %d of %d workers", report.terminated, report.found)
            for error in report.errors:
                log.warning("reaping failed: %s", error)
        except Exception as exc:
            log.warning("reaper pass failed: %s: %s", type(exc).__name__, exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
    return passes
