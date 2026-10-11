"""The daily spend guard: Modal runs stop once today's estimated cost reaches the budget.

Cost is the sum of ``runs.cost_estimate`` over runs on the executor created since midnight UTC. A
finished run's estimate is its executor rate card times the seconds each worker was held (acquire to
release). A run still executing holds a **reservation** there instead: :func:`reserve` admits a run
only while today's spend (finished runs plus the reservations of the others in flight) is below
``COVERSET_MODAL_DAILY_BUDGET_USD`` and then writes the run's predicted cost, which the runner
refines after planning and replaces with the real cost at the end. So concurrent runs see each other.
Local runs cost nothing and are never limited.
"""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import Run

__all__ = ["BUDGETED_EXECUTORS", "budget_exhausted", "reserve", "spent_today"]

BUDGETED_EXECUTORS = frozenset({"modal"})


def _midnight(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)


async def spent_today(
    session: AsyncSession, settings: Settings, executor: str = "modal", *, exclude: str | None = None
) -> float:
    """Estimated USD spent (or reserved) today (UTC) by runs on ``executor``, except ``exclude``."""
    since = _midnight(settings.now())
    query = select(func.coalesce(func.sum(Run.cost_estimate), 0.0)).where(
        Run.executor == executor, Run.created_at >= since
    )
    if exclude is not None:
        query = query.where(Run.id != exclude)
    total = await session.scalar(query)
    return float(total or 0.0)


async def budget_exhausted(session: AsyncSession, settings: Settings, executor: str) -> bool:
    """True when ``executor`` is budgeted and today's spend reached ``modal_daily_budget_usd``."""
    if executor not in BUDGETED_EXECUTORS:
        return False
    return await spent_today(session, settings, executor) >= settings.modal_daily_budget_usd


async def reserve(
    session: AsyncSession, settings: Settings, run_id: str, executor: str, amount_usd: float
) -> bool:
    """Admit ``run_id`` under today's budget and reserve ``amount_usd`` on it; False when exhausted.

    The caller commits, and serialises concurrent calls (the runner holds a per-process lock).
    """
    if executor not in BUDGETED_EXECUTORS:
        return True
    if await spent_today(session, settings, executor, exclude=run_id) >= settings.modal_daily_budget_usd:
        return False
    run = await session.get(Run, run_id)
    if run is not None:
        run.cost_estimate = max(0.0, float(amount_usd))
    return True
