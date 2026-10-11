"""The daily spend guard: Modal runs stop once today's estimated cost reaches the budget.

Cost is the sum of ``runs.cost_estimate`` (each run's executor rate card times its worker seconds)
over runs created since midnight UTC. Local runs cost nothing and are never limited.
"""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import Run

__all__ = ["budget_exhausted", "spent_today"]

BUDGETED_EXECUTORS = frozenset({"modal"})


def _midnight(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)


async def spent_today(session: AsyncSession, settings: Settings, executor: str = "modal") -> float:
    """Estimated USD spent today (UTC) by runs on ``executor``."""
    since = _midnight(settings.now())
    total = await session.scalar(
        select(func.coalesce(func.sum(Run.cost_estimate), 0.0)).where(
            Run.executor == executor, Run.created_at >= since
        )
    )
    return float(total or 0.0)


async def budget_exhausted(session: AsyncSession, settings: Settings, executor: str) -> bool:
    """True when ``executor`` is budgeted and today's spend reached ``modal_daily_budget_usd``."""
    if executor not in BUDGETED_EXECUTORS:
        return False
    return await spent_today(session, settings, executor) >= settings.modal_daily_budget_usd
