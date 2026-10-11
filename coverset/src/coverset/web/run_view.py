"""The run page's data: the run row, its events folded into a :class:`RunState`, and the live
regions (head, timeline, Gantt, failures) that the page renders first and the SSE stream re-renders.

The same :func:`regions_context` feeds the first render (the page template includes the region
partials) and every SSE frame (each partial rendered alone), so a live run and a replay look the
same at every point. In replay mode the timeline shows only milestones reached by the replay clock
and the Gantt keeps the finished run's time axis so blocks do not rescale as they arrive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import PullRequest, Repo, Run, RunEvent, Shard, TestCase, TestResult
from coverset.web.pages import load_numbers
from coverset.web.render.timeline import OrderRow, milestones, render_gantt, render_timeline
from coverset.web.runstate import RunState

__all__ = [
    "REGIONS",
    "RunBundle",
    "TERMINAL",
    "load_bundle",
    "load_events",
    "order_rows",
    "regions_context",
]

REGIONS = ("head", "timeline", "gantt", "failures")
"""Live regions: element id ``run-<name>``, SSE event ``<name>``, partial ``runs/_<name>.html``."""
TERMINAL = frozenset({"completed", "cancelled", "abandoned", "skipped", "error"})
WHY_ORDER_ROWS = 20
MAX_PINNED = 3
GHOST_KEY = "t_cold_path"


@dataclass(slots=True)
class RunBundle:
    """A run with the rows its page needs."""

    run: Run
    repo: Repo
    pr: PullRequest | None
    shards: list[Shard]


async def load_bundle(session: AsyncSession, run_id: str) -> RunBundle | None:
    """The run, its repo, PR and shards (None when the run does not exist)."""
    run = await session.get(Run, run_id)
    if run is None:
        return None
    repo = await session.get(Repo, run.repo_id)
    if repo is None:
        return None
    pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
    shards = list(await session.scalars(select(Shard).where(Shard.run_id == run_id).order_by(Shard.shard_id)))
    return RunBundle(run, repo, pr, shards)


async def load_events(
    session: AsyncSession, run_id: str, *, after: int = 0, upto: int | None = None
) -> list[RunEvent]:
    """Stored events with ``after < seq`` (``<= upto`` when given), in order."""
    query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after)
    if upto is not None:
        query = query.where(RunEvent.seq <= upto)
    return list(await session.scalars(query.order_by(RunEvent.seq)))


def build_state(run_id: str, events: Sequence[RunEvent]) -> RunState:
    """A state with ``events`` applied."""
    state = RunState(run_id)
    for event in events:
        state.apply(event.seq, event.kind, event.payload, event.at)
    return state


def _origin(run: Run) -> datetime:
    marks, origin = milestones(run.timeline or {})
    return origin or run.created_at


def regions_context(
    bundle: RunBundle,
    state: RunState,
    *,
    replay: bool = False,
    final_clock: datetime | None = None,
) -> dict[str, Any]:
    """Template context for the live regions at the state's clock."""
    run = bundle.run
    origin = _origin(run)
    clock = state.clock if replay else None
    marks, _ = milestones(run.timeline or {}, until=clock)
    numbers = load_numbers()
    ghost = numbers.get(GHOST_KEY)
    ghost_s = float(ghost) if isinstance(ghost, int | float) and ghost > 0 else None
    end_moment = final_clock or state.clock or origin
    if not replay and run.status not in TERMINAL:
        predicted = max((s.predicted_s or 0.0 for s in bundle.shards), default=0.0)
        sandbox = next((m.at for m in marks if m.key == "sandbox_ready"), 0.0)
        span = max((end_moment - origin).total_seconds(), sandbox + predicted)
    else:
        finished = (run.timeline or {}).get("finished")
        span = (end_moment - origin).total_seconds()
        if finished and not replay:
            span = max(span, (datetime.fromisoformat(finished) - origin).total_seconds())
    blocks = state.blocks(origin)
    finished_lanes = {sid for sid, lane in state.lanes.items() if lane.finished is not None}
    queued = {
        sid: max(0, lane.planned - len(lane.reports))
        for sid, lane in state.lanes.items()
        if sid not in finished_lanes and not (state.done and not replay)
    }
    bounds = {
        sid: (
            (lane.started - origin).total_seconds() if lane.started else None,
            (lane.finished - origin).total_seconds() if lane.finished else None,
        )
        for sid, lane in state.lanes.items()
    }
    timeline_end = max([m.at for m in marks] or [0.0])
    counts = state.counts
    verdict = state.verdict
    live = not state.done and (replay or run.status not in TERMINAL)
    return {
        "run": run,
        "repo": bundle.repo,
        "pr": bundle.pr,
        "state": state,
        "counts": counts,
        "failed_count": counts.get("failed", 0) + counts.get("error", 0),
        "flaky_count": sum(1 for r in state.tests.values() if r.flaky),
        "verdict": verdict,
        "live": live,
        "replay": replay,
        "timeline_svg": render_timeline(marks, span=max(span, timeline_end), ghost=ghost_s),
        "ghost_s": ghost_s,
        "gantt_svg": render_gantt(
            blocks,
            span=max(span, 0.001),
            queued=queued,
            lane_status={sid: lane.status for sid, lane in state.lanes.items()},
            lane_bounds=bounds,
        ),
        "failures": state.failed_reports(),
        "confirmed": {f.get("nodeid"): f for f in state.failures},
    }


async def order_rows(session: AsyncSession, run: Run) -> tuple[list[OrderRow], list[dict[str, Any]]]:
    """The first tests by priority (failing tests pinned) and every result row for the tests table."""
    rows = (
        await session.execute(
            select(TestResult, TestCase.nodeid, TestCase.quarantined)
            .join(TestCase, TestCase.id == TestResult.test_id)
            .where(TestResult.run_id == run.id)
            .order_by(TestResult.rank.is_(None), TestResult.rank, TestCase.nodeid)
        )
    ).all()
    results: list[dict[str, Any]] = []
    order: list[OrderRow] = []
    for result, nodeid, quarantined in rows:
        explanation = result.score_explanation or {}
        results.append(
            {
                "nodeid": nodeid,
                "test_id": result.test_id,
                "outcome": result.outcome,
                "duration_ms": result.duration_ms,
                "shard": result.shard_id,
                "rank": result.rank,
                "flaky": result.flaky,
                "attempts": result.attempts,
                "quarantined": quarantined or result.quarantined,
                "file_rank": explanation.get("file_rank"),
                "why": explanation.get("text") or "",
            }
        )
        if result.rank is None:
            continue
        failing = result.outcome in ("failed", "error") and not result.flaky
        pinned = failing and sum(1 for o in order if o.pinned) < MAX_PINNED
        if result.rank <= WHY_ORDER_ROWS or pinned:
            order.append(
                OrderRow(
                    rank=result.rank,
                    nodeid=nodeid,
                    score=float(result.score or 0.0),
                    components=dict(explanation.get("components") or {}),
                    text=str(explanation.get("text") or ""),
                    outcome=result.outcome,
                    file_rank=explanation.get("file_rank"),
                    wave1=bool(explanation.get("wave1")),
                    pinned=pinned,
                )
            )
    results.sort(key=lambda r: (r["outcome"] not in ("failed", "error"), r["rank"] or 10**9))
    return order, results
