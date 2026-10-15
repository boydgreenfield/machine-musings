"""The daily spend guard: Modal work stops once today's estimated cost would pass the budget.

Today's spend (UTC) on an executor is the sum of two tables:

* ``runs.cost_estimate`` over runs on the executor created since midnight. A finished run's estimate
  is its executor rate card times the seconds each worker was held (acquire to release). A run still
  executing holds a **reservation** there instead (see below), so concurrent runs see each other.
* ``executor_spend`` rows of the day: spend that belongs to no run (pool idle time, trusted snapshot
  builds, smoke tests, benchmarks, env image builds), written by the executor, so it survives
  restarts and is seen by every process that shares the database.

:func:`reserve` admits a run only if today's spend (finished runs, the reservations of the others in
flight, non-run spend) **plus the amount it is about to reserve** stays within
``COVERSET_MODAL_DAILY_BUDGET_USD``, then writes the reservation. The runner reserves a small amount
before planning and, once it knows the number of workers, re-reserves the run's worst case
(``n_workers x COVERSET_RUN_TIMEOUT_S`` at the limit rate) capped by ``COVERSET_MAX_RUN_COST_USD``
(:func:`run_cost_cap`), which it also enforces live. Local runs cost nothing and are never limited.
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import ExecutorSpend, Run

__all__ = [
    "BUDGETED_EXECUTORS",
    "budget_exhausted",
    "builds_today",
    "record_spend",
    "reserve",
    "run_cost_cap",
    "spent_today",
    "today",
]

BUDGETED_EXECUTORS = frozenset({"modal"})


def _midnight(now: datetime) -> datetime:
    return datetime.combine(now.date(), time(0), tzinfo=now.tzinfo)


def today(settings: Settings) -> date:
    """The UTC day spend is charged to now."""
    return settings.now().date()


def run_cost_cap(settings: Settings) -> float:
    """The most one run may cost: ``COVERSET_MAX_RUN_COST_USD``, never above the daily budget."""
    return max(0.0, min(settings.max_run_cost_usd, settings.modal_daily_budget_usd))


async def spent_today(
    session: AsyncSession, settings: Settings, executor: str = "modal", *, exclude: str | None = None
) -> float:
    """Estimated USD spent (or reserved) today (UTC) on ``executor``: runs except ``exclude`` plus
    non-run spend (``executor_spend``)."""
    since = _midnight(settings.now())
    query = select(func.coalesce(func.sum(Run.cost_estimate), 0.0)).where(
        Run.executor == executor, Run.created_at >= since
    )
    if exclude is not None:
        query = query.where(Run.id != exclude)
    runs = float(await session.scalar(query) or 0.0)
    other = await session.scalar(
        select(func.coalesce(func.sum(ExecutorSpend.usd), 0.0)).where(
            ExecutorSpend.executor == executor, ExecutorSpend.day == today(settings)
        )
    )
    return runs + float(other or 0.0)


async def builds_today(session: AsyncSession, settings: Settings, executor: str = "modal") -> int:
    """Env image builds recorded today on ``executor`` (the ``COVERSET_MAX_IMAGE_BUILDS_PER_DAY`` count)."""
    count = await session.scalar(
        select(func.count(ExecutorSpend.id)).where(
            ExecutorSpend.executor == executor,
            ExecutorSpend.day == today(settings),
            ExecutorSpend.kind == "build",
        )
    )
    return int(count or 0)


def record_spend(
    session: AsyncSession,
    settings: Settings,
    executor: str,
    kind: str,
    usd: float,
    *,
    sandbox_id: str | None = None,
) -> None:
    """Add one ``executor_spend`` row for today (the caller commits)."""
    session.add(
        ExecutorSpend(
            day=today(settings),
            executor=executor,
            kind=kind,
            usd=max(0.0, float(usd)),
            sandbox_id=sandbox_id,
            created_at=settings.now(),
        )
    )


async def budget_exhausted(session: AsyncSession, settings: Settings, executor: str) -> bool:
    """True when ``executor`` is budgeted and today's spend reached ``modal_daily_budget_usd``."""
    if executor not in BUDGETED_EXECUTORS:
        return False
    return await spent_today(session, settings, executor) >= settings.modal_daily_budget_usd


async def reserve(
    session: AsyncSession, settings: Settings, run_id: str, executor: str, amount_usd: float
) -> bool:
    """Admit ``run_id`` and reserve ``amount_usd`` on it; False when today's spend (without this run's
    own earlier reservation) plus ``amount_usd`` would pass the daily budget.

    The caller commits, and serialises concurrent calls (the runner holds a per-process lock).
    """
    if executor not in BUDGETED_EXECUTORS:
        return True
    amount = max(0.0, float(amount_usd))
    spent = await spent_today(session, settings, executor, exclude=run_id)
    budget = settings.modal_daily_budget_usd
    if spent >= budget or spent + amount > budget:
        return False
    run = await session.get(Run, run_id)
    if run is not None:
        run.cost_estimate = amount
    return True
