"""Warm-pool maintenance, generic over executors.

Executors that advertise ``capabilities().warm_pool`` and implement
:class:`~coverset.executors.base.PoolingExecutor` (Modal) keep up to ``COVERSET_POOL_SIZE`` pre-created
workers per active repo while ``COVERSET_POOL_TTL_S`` has not elapsed since that repo's last run.
For every other executor (the local one) these functions do nothing.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import Run
from coverset.executors.base import EnvRef, Executor, PoolingExecutor, SnapshotRef

__all__ = ["active_repos", "drain_pool", "maintain_pool", "supports_pool"]


def supports_pool(executor: Executor) -> bool:
    """Whether ``executor`` has a warm pool to maintain."""
    return executor.capabilities().warm_pool and isinstance(executor, PoolingExecutor)


async def active_repos(session: AsyncSession, settings: Settings) -> list[int]:
    """Repos with a run created within ``pool_ttl_s``: the ones worth keeping a pool for."""
    since = settings.now() - timedelta(seconds=settings.pool_ttl_s)
    rows = await session.scalars(
        select(Run.repo_id)
        .where(Run.created_at >= since)
        .group_by(Run.repo_id)
        .order_by(func.max(Run.created_at))
    )
    return list(rows)


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
