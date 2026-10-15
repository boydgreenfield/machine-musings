"""The run page's data: the run row, its events folded into a :class:`RunState`, and the live
regions (head, timeline, Gantt, failures, why-this-order) that the page renders first and the SSE
stream re-renders.

The same :func:`regions_context` feeds the first render (the page template includes the region
partials) and every SSE frame (each partial rendered alone), so a live run and a replay look the
same at every point. In replay mode the timeline shows only milestones reached by the replay clock,
the Gantt keeps the finished run's time axis so blocks do not rescale as they arrive, and a failing
test is pinned in "why this order" only once its failure has been replayed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import PullRequest, Repo, Run, RunEvent, Shard, TestCase, TestResult
from coverset.web.pages import load_numbers
from coverset.web.render.timeline import (
    Milestone,
    OrderRow,
    milestones,
    render_gantt,
    render_timeline,
    render_why_order,
)
from coverset.web.runstate import RunState

__all__ = [
    "REGIONS",
    "RunBundle",
    "TERMINAL",
    "WhyOrder",
    "load_bundle",
    "load_events",
    "order_rows",
    "regions_context",
]

REGIONS = ("head", "timeline", "gantt", "failures", "order")
"""Live regions: element id ``run-<name>``, SSE event ``<name>``, partial ``runs/_<name>.html``."""
TERMINAL = frozenset({"completed", "cancelled", "abandoned", "skipped", "error"})
WHY_ORDER_ROWS = 20
MAX_PINNED = 3
DEMO_REPO = "coverset-demo/ledger"
"""The demo repository: the only one with a measured cold path (``docs/numbers.json``)."""
GHOST_KEY = "t_cold_path_demo"
GHOST_FAILURE_KEY = "t_cold_path_demo_first_failure"
GHOST_LABEL = "measured cold path, demo app: fresh sandbox, install, plain pytest"


@dataclass(slots=True)
class WhyOrder:
    """The "why this order" panel: listed rows and how many ranked tests it leaves out."""

    rows: list[OrderRow] = field(default_factory=list)
    rest: int = 0
    rest_note: str = ""


@dataclass(slots=True)
class RunBundle:
    """A run with the rows its page needs."""

    run: Run
    repo: Repo
    pr: PullRequest | None
    shards: list[Shard]
    order: WhyOrder = field(default_factory=WhyOrder)
    basis: Run | None = None
    """The run whose coverage and history the scores were computed against."""

    @property
    def basis_label(self) -> str:
        """``merge-base`` when the basis ran the PR's base commit, else what it was."""
        if self.basis is None:
            return ""
        if self.run.base_sha and self.basis.head_sha == self.run.base_sha:
            return "merge-base"
        if self.basis.pr_id is not None and self.basis.pr_id == self.run.pr_id:
            return "previous run of this PR"
        return f"{self.basis.branch or 'default branch'} baseline"


async def load_bundle(session: AsyncSession, run_id: str) -> RunBundle | None:
    """The run, its repo, PR, shards, priority order and scoring basis (None when the run does
    not exist)."""
    run = await session.get(Run, run_id)
    if run is None:
        return None
    repo = await session.get(Repo, run.repo_id)
    if repo is None:
        return None
    pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
    shards = list(await session.scalars(select(Shard).where(Shard.run_id == run_id).order_by(Shard.shard_id)))
    order, _results = await order_rows(session, run)
    basis = await session.get(Run, run.scoring_basis_run_id) if run.scoring_basis_run_id else None
    return RunBundle(run, repo, pr, shards, order, basis)


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


def cold_path(repo: Repo) -> tuple[float | None, float | None]:
    """``(seconds, first failure seconds)`` of the measured cold path for ``repo``'s run pages.

    Only the demo repository has one (``t_cold_path_demo`` in ``docs/numbers.json``: a fresh
    sandbox, the install and plain pytest on the demo app); every other repository gets none,
    because the bench suite's cold path says nothing about its suite.
    """
    if repo.full_name != DEMO_REPO:
        return None, None
    numbers = load_numbers()

    def seconds(key: str) -> float | None:
        value = numbers.get(key)
        return float(value) if isinstance(value, int | float) and value > 0 else None

    return seconds(GHOST_KEY), seconds(GHOST_FAILURE_KEY)


def _origin(run: Run) -> datetime:
    marks, origin = milestones(run.timeline or {})
    return origin or run.created_at


def regions_context(
    bundle: RunBundle,
    state: RunState,
    *,
    replay: bool = False,
    final_clock: datetime | None = None,
    first_render: bool = False,
) -> dict[str, Any]:
    """Template context for the live regions at the state's clock (``first_render`` for the page
    itself, whose first failure starts open; stream frames never re-open a failure)."""
    run = bundle.run
    origin = _origin(run)
    clock = state.clock if replay else None
    marks, _ = milestones(run.timeline or {}, until=clock)
    ghost_s, ghost_failure_s = cold_path(bundle.repo)
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
    failure = _failure_mark(marks, state, origin)
    if failure is not None and failure.key == "failure_reported":
        marks = sorted([*marks, failure], key=lambda m: m.at)
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
        "first_render": first_render,
        "basis": bundle.basis,
        "basis_label": bundle.basis_label,
        "timeline_svg": render_timeline(
            marks,
            span=max(span, timeline_end),
            ghost=ghost_s,
            ghost_label=GHOST_LABEL,
            ghost_mark=ghost_failure_s if (run.timeline or {}).get("first_failure") else None,
        ),
        "ghost_s": ghost_s,
        "gantt_svg": render_gantt(
            blocks,
            span=max(span, 0.001),
            queued=queued,
            lane_status={sid: lane.status for sid, lane in state.lanes.items()},
            lane_bounds=bounds,
            failure_at=failure.at if failure is not None else None,
            failure_label=failure.label if failure is not None else "",
        ),
        "failures": state.failed_reports(),
        "confirmed": {f.get("nodeid"): f for f in state.failures},
        "order_html": _why_order(bundle.order, state if replay else None),
        "order_count": len(bundle.order.rows),
    }


def _failure_mark(marks: Sequence[Milestone], state: RunState, origin: datetime) -> Milestone | None:
    """The confirmed first failure when the clock has reached it, else the first failed report
    as a provisional "failure reported" mark (a retry has not confirmed it yet)."""
    confirmed = next((m for m in marks if m.key == "first_failure"), None)
    if confirmed is not None:
        return confirmed
    reported = state.failed_reports()
    if not reported:
        return None
    return Milestone("failure_reported", "failure reported", (reported[0].at - origin).total_seconds())


def _why_order(order: WhyOrder, replayed: RunState | None) -> Any:
    """The panel; while replaying, a failing test is pinned (and marked failed) only after the
    replay has reached its failed report, so the panel never gives the ending away."""
    rows = order.rows
    if replayed is not None:
        failed = {r.nodeid for r in replayed.failed_reports()}
        rows = [
            row
            if row.nodeid in failed
            else replace(
                row,
                pinned=False,
                outcome=replayed.tests[row.nodeid].outcome if row.nodeid in replayed.tests else "not_run",
            )
            for row in rows
            if row.nodeid in failed or not row.pinned or row.listed
        ]
    return render_why_order(rows, rest=order.rest, rest_note=order.rest_note)


async def order_rows(session: AsyncSession, run: Run) -> tuple[WhyOrder, list[dict[str, Any]]]:
    """The "why this order" panel and every result row for the tests table.

    The panel lists, in rank order, the first :data:`WHY_ORDER_ROWS` tests with a risk signal (a
    score above 0, or a place in wave 1); up to :data:`MAX_PINNED` failing tests are pinned on top
    (with how a tie on their score was broken); every other ranked test is summed up in one line.
    """
    rows = (
        await session.execute(
            select(TestResult, TestCase.nodeid, TestCase.quarantined)
            .join(TestCase, TestCase.id == TestResult.test_id)
            .where(TestResult.run_id == run.id)
            .order_by(TestResult.rank.is_(None), TestResult.rank, TestCase.nodeid)
        )
    ).all()
    results: list[dict[str, Any]] = []
    ranked: list[tuple[TestResult, str, dict[str, Any]]] = []
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
        if result.rank is not None:
            ranked.append((result, nodeid, explanation))
    order = _why_order_rows(ranked)
    results.sort(key=lambda r: (r["outcome"] not in ("failed", "error"), r["rank"] or 10**9))
    return order, results


def _why_order_rows(ranked: list[tuple[TestResult, str, dict[str, Any]]]) -> WhyOrder:
    scores = [float(result.score or 0.0) for result, _n, _e in ranked]
    out: list[OrderRow] = []
    listed_ids: set[str] = set()
    pinned = 0
    for result, nodeid, explanation in ranked:
        score = float(result.score or 0.0)
        wave1 = bool(explanation.get("wave1"))
        signal = score > 0 or wave1
        listed = signal and len(listed_ids) < WHY_ORDER_ROWS
        failing = result.outcome in ("failed", "error") and not result.flaky
        pin = failing and pinned < MAX_PINNED
        if not (listed or pin):
            continue
        pinned += pin
        text = str(explanation.get("text") or "")
        out.append(
            OrderRow(
                rank=result.rank or 0,
                nodeid=nodeid,
                score=score,
                components=dict(explanation.get("components") or {}),
                text=text,
                outcome=result.outcome,
                file_rank=explanation.get("file_rank"),
                wave1=wave1,
                pinned=pin,
                tie=_tie_note(score, scores, wave1, text) if pin else "",
                listed=listed,
            )
        )
        if listed:
            listed_ids.add(nodeid)
    rest = [
        (result, explanation)
        for result, nodeid, explanation in ranked
        if nodeid not in listed_ids and not any(r.nodeid == nodeid for r in out)
    ]
    return WhyOrder(out, len(rest), _rest_note(rest))


def _tie_note(score: float, scores: list[float], wave1: bool, text: str) -> str:
    """How the order broke a tie on ``score`` (empty when no other test has that score)."""
    others = sum(1 for s in scores if abs(s - score) < 1e-6) - 1
    if others <= 0:
        return ""
    noun = "other" if others == 1 else "others"
    head = f"tied at {score:.1f} with {others} {noun}"
    if wave1 and text.startswith("covers "):
        return f"{head}; wave 1 runs the test covering the most changed functions per second first"
    if wave1:
        return f"{head}; wave 1 runs these tests by name"
    return f"{head}; the shorter p50 ran first, then by name"


def _rest_note(rest: list[tuple[TestResult, dict[str, Any]]]) -> str:
    """In what order the unlisted tests ran: file order when their ranks follow it, else the
    scoring tie-break (shorter p50 first, then by name; an exploration sample moves a few up)."""
    file_ranks = [e.get("file_rank") for _r, e in rest]
    if all(isinstance(f, int) for f in file_ranks) and file_ranks == sorted(file_ranks):  # type: ignore[type-var]
        return "then in file order"
    return "then shortest p50 first"
