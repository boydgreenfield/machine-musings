"""``/``: the dashboard (repos, live and recent runs with status chips, quick numbers)."""

from __future__ import annotations

import statistics
from datetime import datetime

from quart import Blueprint, Response
from sqlalchemy import func, select

from coverset.db.models import PullRequest, Repo, Run, TestCase
from coverset.web.context import app_db, app_settings
from coverset.web.loaders import run_counts
from coverset.web.pages import render_page
from coverset.web.principal import visible_repo_ids
from coverset.web.render.timeline import milestones

bp = Blueprint("dashboard", __name__)

RECENT_RUNS = 20
LIVE_STATUSES = ("queued", "running", "awaiting_approval", "paused")


def first_failure_s(run: Run) -> float | None:
    """Seconds from webhook (else queued) to the first confirmed failure."""
    marks, _ = milestones(run.timeline or {})
    return next((m.at for m in marks if m.key == "first_failure"), None)


def first_test_s(run: Run) -> float | None:
    """Seconds from webhook (else queued) to the first test result."""
    marks, _ = milestones(run.timeline or {})
    return next((m.at for m in marks if m.key == "first_test"), None)


@bp.get("/")
async def index() -> Response:
    """Repos, live runs, recent runs and a few numbers."""
    async with app_db().session() as session:
        repos = list(await session.scalars(select(Repo).order_by(Repo.full_name)))
        rids = visible_repo_ids([r.id for r in repos])  # everything below is scoped to these
        repos = [r for r in repos if r.id in rids]
        live = list(
            await session.scalars(
                select(Run)
                .where(Run.status.in_(LIVE_STATUSES), Run.repo_id.in_(rids))
                .order_by(Run.created_at.desc())
                .limit(10)
            )
        )
        recent = list(
            await session.scalars(
                select(Run)
                .where(Run.status.not_in(LIVE_STATUSES), Run.repo_id.in_(rids))
                .order_by(Run.created_at.desc())
                .limit(RECENT_RUNS)
            )
        )
        prs = {
            pr.id: pr
            for pr in await session.scalars(select(PullRequest).where(PullRequest.repo_id.in_(rids)))
        }
        counts = await run_counts(session, [r.id for r in live + recent])
        total_runs = (
            await session.scalar(select(func.count()).select_from(Run).where(Run.repo_id.in_(rids))) or 0
        )
        tests = (
            await session.scalar(select(func.count()).select_from(TestCase).where(TestCase.repo_id.in_(rids)))
            or 0
        )
        per_repo = dict(
            (
                await session.execute(
                    select(Run.repo_id, func.count()).where(Run.repo_id.in_(rids)).group_by(Run.repo_id)
                )
            ).all()
        )
        open_prs = dict(
            (
                await session.execute(
                    select(PullRequest.repo_id, func.count())
                    .where(PullRequest.state == "open", PullRequest.repo_id.in_(rids))
                    .group_by(PullRequest.repo_id)
                )
            ).all()
        )
    failures = [s for r in recent if (s := first_failure_s(r)) is not None]
    starts = [s for r in recent if (s := first_test_s(r)) is not None]
    numbers = {
        "runs": total_runs,
        "tests": tests,
        "first_failure_median": statistics.median(failures) if failures else None,
        "first_test_median": statistics.median(starts) if starts else None,
        "failing": sum(1 for r in recent if r.verdict == "failure"),
    }
    now: datetime = app_settings().now()
    return await render_page(
        "dashboard.html",
        repos=repos,
        live=live,
        recent=recent,
        prs=prs,
        counts=counts,
        numbers=numbers,
        per_repo=per_repo,
        open_prs=open_prs,
        now=now,
        first_failure_s=first_failure_s,
    )
