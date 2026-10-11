"""Warm-pool maintenance, generic over executors.

Executors that advertise ``capabilities().warm_pool`` and implement
:class:`~coverset.executors.base.PoolingExecutor` (Modal) keep up to ``COVERSET_POOL_SIZE`` pre-created
workers per active repo while ``COVERSET_POOL_TTL_S`` has not elapsed since that repo's last run.
For every other executor (the local one) these functions do nothing.

:class:`PoolMaintainer` is what ``coverset serve`` runs (every :data:`POOL_INTERVAL_S`, see
:func:`coverset.app.schedule_executor_upkeep`): for each repo with a run on this executor in the last
``pool_ttl_s`` seconds it tops the pool up from the newest ready, unexpired trusted snapshot (repos
without one get no pool), drains the pools of repos that went quiet, and drains everything while the
daily budget is exhausted. Fills go through the executor, so they share its sandbox semaphore
(``COVERSET_MAX_SANDBOXES``) and its own budget guard. The runner calls :meth:`PoolMaintainer.refill`
after it re-snapshots a repo, and the app drains the pool when it stops serving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import Env, Run, Snapshot
from coverset.db.session import Database
from coverset.executors.base import EnvRef, Executor, PoolingExecutor, SnapshotRef
from coverset.orchestrator.budget import budget_exhausted

__all__ = [
    "POOL_INTERVAL_S",
    "PoolMaintainer",
    "active_repos",
    "drain_pool",
    "maintain_pool",
    "newest_snapshot",
    "supports_pool",
]

log = logging.getLogger(__name__)

POOL_INTERVAL_S = 30.0
"""How often ``serve`` tops the pools up."""


def supports_pool(executor: Executor) -> bool:
    """Whether ``executor`` has a warm pool to maintain."""
    return executor.capabilities().warm_pool and isinstance(executor, PoolingExecutor)


async def active_repos(session: AsyncSession, settings: Settings, executor: str | None = None) -> list[int]:
    """Repos with a run created within ``pool_ttl_s`` (on ``executor``, when given), least recent first."""
    since = settings.now() - timedelta(seconds=settings.pool_ttl_s)
    query = select(Run.repo_id).where(Run.created_at >= since)
    if executor is not None:
        query = query.where(Run.executor == executor)
    rows = await session.scalars(query.group_by(Run.repo_id).order_by(func.max(Run.created_at)))
    return list(rows)


async def newest_snapshot(session: AsyncSession, settings: Settings, repo_id: int) -> SnapshotRef | None:
    """The newest ready, unexpired trusted snapshot of ``repo_id`` (any environment), or None."""
    now = settings.now()
    row = await session.scalar(
        select(Snapshot)
        .where(
            Snapshot.repo_id == repo_id,
            Snapshot.status == "ready",
            Snapshot.image_id.is_not(None),
            (Snapshot.expires_at.is_(None)) | (Snapshot.expires_at > now),
        )
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    if row is None or row.image_id is None:
        return None
    env = await session.get(Env, row.env_hash)
    env_ref = EnvRef(
        env_hash=row.env_hash,
        python=env.python if env is not None else "",
        location=(env.image_id or "") if env is not None else "",
    )
    return SnapshotRef(env=env_ref, sha=row.sha, image_id=row.image_id, snapshot_id=row.id)


async def maintain_pool(executor: Executor, base: EnvRef | SnapshotRef, repo: str, settings: Settings) -> int:
    """Top ``repo``'s pool up to ``pool_size``; returns workers created (0 without pool support)."""
    if not supports_pool(executor) or settings.pool_size <= 0:
        return 0
    assert isinstance(executor, PoolingExecutor)
    return await executor.fill_pool(base, repo, settings.pool_size, settings.pool_ttl_s)


async def drain_pool(executor: Executor, repo: str | None = None) -> int:
    """Terminate pooled workers (of ``repo``, or all); 0 without pool support."""
    if not supports_pool(executor):
        return 0
    assert isinstance(executor, PoolingExecutor)
    return await executor.drain_pool(repo)


class PoolMaintainer:
    """Keeps the warm pools of active repos filled (see the module docstring).

    Repo keys are ``str(repo_id)``, the ``repo`` label the planner passes to ``acquire``, so pooled
    workers are claimed by that repo's runs. One lock serialises passes, so a refill after a new
    snapshot and the periodic pass never overfill a pool.
    """

    def __init__(self, db: Database, settings: Settings, executor: Executor) -> None:
        self.db = db
        self.settings = settings
        self.executor = executor
        self.filled: set[str] = set()
        """Repos this maintainer has filled a pool for (drained when they go quiet)."""
        self.passes = 0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Whether there is anything to maintain (pool support and ``pool_size > 0``)."""
        return supports_pool(self.executor) and self.settings.pool_size > 0

    async def maintain_once(self) -> dict[str, int]:
        """One pass over every active repo; returns ``{repo: workers created}``."""
        if not self.enabled:
            return {}
        async with self._lock:
            self.passes += 1
            async with self.db.session() as session:
                if await budget_exhausted(session, self.settings, self.executor.name):
                    exhausted = True
                    bases: dict[str, SnapshotRef | None] = {}
                else:
                    exhausted = False
                    repos = await active_repos(session, self.settings, self.executor.name)
                    bases = {str(r): await newest_snapshot(session, self.settings, r) for r in repos}
            if exhausted:
                if self.filled:
                    log.info("daily budget exhausted: draining the warm pool")
                    await self._drain_repos(set(self.filled))
                return {}
            await self._drain_repos({r for r in self.filled if bases.get(r) is None})
            created: dict[str, int] = {}
            for repo, base in bases.items():
                if base is not None:
                    created[repo] = await self._fill(repo, base)
            return created

    async def refill(self, repo_id: int) -> int:
        """Top up ``repo_id``'s pool now (after a new trusted snapshot); returns workers created."""
        if not self.enabled:
            return 0
        async with self._lock:
            async with self.db.session() as session:
                if await budget_exhausted(session, self.settings, self.executor.name):
                    return 0
                base = await newest_snapshot(session, self.settings, repo_id)
            if base is None:
                return 0
            return await self._fill(str(repo_id), base)

    async def drain(self) -> int:
        """Terminate every pooled worker (used when the app stops serving)."""
        async with self._lock:
            self.filled.clear()
            return await drain_pool(self.executor)

    async def run_forever(
        self, stop: asyncio.Event | None = None, *, interval_s: float = POOL_INTERVAL_S
    ) -> None:
        """:meth:`maintain_once` now and every ``interval_s`` until ``stop`` is set (or cancelled).

        A failed pass is logged and retried at the next interval; it never ends the loop.
        """
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                created = await self.maintain_once()
                if any(created.values()):
                    log.info("warm pool: created %s", created)
            except Exception:
                log.exception("warm-pool maintenance failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def _fill(self, repo: str, base: SnapshotRef) -> int:
        try:
            created = await maintain_pool(self.executor, base, repo, self.settings)
        except Exception as exc:  # capacity or API errors: the next pass tries again
            log.warning("filling the warm pool of repo %s failed: %s", repo, exc)
            return 0
        self.filled.add(repo)
        return created

    async def _drain_repos(self, repos: set[str]) -> None:
        for repo in sorted(repos):
            self.filled.discard(repo)
            try:
                drained = await drain_pool(self.executor, repo)
            except Exception as exc:
                log.warning("draining the warm pool of repo %s failed: %s", repo, exc)
                continue
            if drained:
                log.info("warm pool of repo %s drained (%d workers)", repo, drained)
