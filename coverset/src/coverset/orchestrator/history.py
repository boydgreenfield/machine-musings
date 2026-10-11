"""Test history: the control-plane facts that drive scoring, and the demo's seeded run history.

* :func:`register_tests` keeps the ``tests`` table in step with what a run collected.
* :func:`prior_candidates`, :func:`previous_pr_run` and :func:`base_branch_run` find the runs whose
  facts a new run builds on (the prior run of the analysis, the base run for "pre-existing" failures).
* :func:`load_test_history` turns ``test_stats`` and recent results into
  :class:`~coverset.analysis.scoring.TestHistory` for scoring (with a parametrize-``family``
  fallback for new ids).
* :func:`update_test_stats` recomputes ``test_stats`` over each test's last 50 executed results
  (``not_run`` never counts; untrusted runs never update priors).
* :func:`seed_history` replays the six demo ``main`` commits as baseline runs and then both demo
  PRs through the normal planner and runner.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.analysis.prior import PriorCandidate
from coverset.analysis.scoring import TestHistory
from coverset.db.models import Run, TestCase, TestResult, TestStats
from coverset.facts.store import FactsStore

if TYPE_CHECKING:
    from coverset.orchestrator.runner import OrchestratorContext

__all__ = [
    "BASELINE_TRIGGERS",
    "HISTORY_WINDOW",
    "SeededRun",
    "base_branch_run",
    "family_of",
    "load_test_history",
    "nodeid_hash",
    "previous_pr_run",
    "prior_candidates",
    "register_tests",
    "seed_history",
    "update_test_stats",
]

HISTORY_WINDOW = 50
"""How many of a test's most recent executed results ``test_stats`` is computed over."""
EWMA_ALPHA = 0.3
COFAIL_RUNS = 20
BASELINE_TRIGGERS = frozenset({"baseline", "push"})
FAILED = frozenset({"failed", "error"})
_IN_CHUNK = 500


def nodeid_hash(nodeid: str) -> bytes:
    """The 16-byte ``tests.nodeid_hash`` of a nodeid (blake2b-128)."""
    return hashlib.blake2b(nodeid.encode("utf-8", "surrogatepass"), digest_size=16).digest()


def family_of(nodeid: str) -> str:
    """The parametrize family of a nodeid: everything before the first ``[``."""
    return nodeid.split("[", 1)[0]


def _chunks[T](items: Sequence[T], size: int = _IN_CHUNK) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def register_tests(
    session: AsyncSession, repo_id: int, nodeids: Sequence[str], run_id: str | None
) -> dict[str, TestCase]:
    """Find or create a ``tests`` row per nodeid (new rows remember ``run_id``); returns them by nodeid."""
    by_hash = {nodeid_hash(n): n for n in nodeids}
    found: dict[str, TestCase] = {}
    hashes = list(by_hash)
    for chunk in _chunks(hashes):
        rows = await session.scalars(
            select(TestCase).where(TestCase.repo_id == repo_id, TestCase.nodeid_hash.in_(chunk))
        )
        for row in rows:
            found[row.nodeid] = row
    for digest, nodeid in by_hash.items():
        if nodeid not in found:
            row = TestCase(
                repo_id=repo_id,
                nodeid=nodeid,
                nodeid_hash=digest,
                family=family_of(nodeid),
                first_seen_run_id=run_id,
            )
            session.add(row)
            found[nodeid] = row
    await session.flush()
    return found


async def prior_candidates(
    session: AsyncSession, repo_id: int, *, exclude: str, store: FactsStore | None = None
) -> list[PriorCandidate]:
    """Completed runs of the repo (newest first) that can serve as the prior run.

    With ``store``, runs without a facts directory are left out (their facts were collected
    elsewhere or expired).
    """
    rows = await session.scalars(
        select(Run)
        .where(Run.repo_id == repo_id, Run.status == "completed", Run.id != exclude)
        .order_by(Run.created_at.desc())
        .limit(200)
    )
    candidates = []
    for run in rows:
        if store is not None and not store.run_dir(repo_id, run.id).is_dir():
            continue
        candidates.append(
            PriorCandidate(
                run_id=run.id,
                head_sha=run.head_sha,
                branch=run.branch,
                created_at=run.created_at,
                baseline=run.trigger in BASELINE_TRIGGERS,
            )
        )
    return candidates


async def previous_pr_run(session: AsyncSession, run: Run) -> Run | None:
    """The most recent completed run of the same PR created before ``run``."""
    if run.pr_id is None:
        return None
    return await session.scalar(
        select(Run)
        .where(
            Run.pr_id == run.pr_id,
            Run.id != run.id,
            Run.status == "completed",
            Run.created_at <= run.created_at,
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )


async def latest_baseline(session: AsyncSession, repo_id: int, branch: str, *, exclude: str) -> Run | None:
    """The newest completed baseline run on ``branch``."""
    return await session.scalar(
        select(Run)
        .where(
            Run.repo_id == repo_id,
            Run.status == "completed",
            Run.branch == branch,
            Run.trigger.in_(BASELINE_TRIGGERS),
            Run.id != exclude,
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )


async def base_branch_run(
    session: AsyncSession, run: Run, *, merge_base: str | None, default_branch: str
) -> Run | None:
    """The run that says what already failed on the base: a completed run of the merge-base (or
    base) commit, else the latest default-branch baseline. None for baseline runs themselves."""
    if run.base_sha is None and merge_base is None:
        return None
    for sha in dict.fromkeys(s for s in (merge_base, run.base_sha) if s):
        found = await session.scalar(
            select(Run)
            .where(
                Run.repo_id == run.repo_id,
                Run.head_sha == sha,
                Run.status == "completed",
                Run.id != run.id,
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        if found is not None:
            return found
    return await latest_baseline(session, run.repo_id, default_branch, exclude=run.id)


async def failure_signatures(session: AsyncSession, run_id: str) -> set[bytes]:
    """``fail_sig`` of every failed or errored test result of a run."""
    rows = await session.scalars(
        select(TestResult.fail_sig).where(
            TestResult.run_id == run_id,
            TestResult.outcome.in_(FAILED),
            TestResult.fail_sig.is_not(None),
        )
    )
    return {sig for sig in rows if sig}


async def load_test_history(
    session: AsyncSession,
    repo_id: int,
    tests: Mapping[str, int],
    *,
    previous_run_id: str | None,
) -> dict[str, TestHistory]:
    """Scoring history per nodeid from ``test_stats`` plus the outcome in ``previous_run_id``.

    ``previous_run_id`` is the PR's previous run, else the latest baseline; a test failed there
    gives ``failed_in_prev_run``. Nodeids without stats borrow their family's median p50.
    """
    ids = list(tests.values())
    stats: dict[int, TestStats] = {}
    for chunk in _chunks(ids):
        for row in await session.scalars(
            select(TestStats).where(TestStats.repo_id == repo_id, TestStats.test_id.in_(chunk))
        ):
            stats[row.test_id] = row
    previous: dict[int, str] = {}
    if previous_run_id is not None:
        rows = await session.execute(
            select(TestResult.test_id, TestResult.outcome).where(TestResult.run_id == previous_run_id)
        )
        previous = {test_id: outcome for test_id, outcome in rows}
    cofail = await _cofail_counts(session, repo_id)
    family_p50: dict[str, list[float]] = defaultdict(list)
    for nodeid, test_id in tests.items():
        row = stats.get(test_id)
        if row is not None and row.p50_ms is not None:
            family_p50[family_of(nodeid)].append(row.p50_ms / 1000.0)
    history: dict[str, TestHistory] = {}
    for nodeid, test_id in tests.items():
        row = stats.get(test_id)
        prev_outcome = previous.get(test_id)
        ran_before = prev_outcome is not None and prev_outcome != "not_run"
        if row is None:
            family = family_p50.get(family_of(nodeid))
            if family is None and not ran_before:
                continue
            history[nodeid] = TestHistory(
                last_outcome=prev_outcome if ran_before else None,
                failed_in_prev_run=(prev_outcome in FAILED) if ran_before else None,
                p50_s=statistics.median(family) if family else None,
                cofail=cofail.get(nodeid, {}),
            )
            continue
        history[nodeid] = TestHistory(
            last_outcome=prev_outcome if ran_before else None,
            failed_in_prev_run=(prev_outcome in FAILED) if ran_before else None,
            last_fail_at=row.last_fail_at,
            fail_ewma=row.fail_ewma,
            flaky_score=row.flaky_score,
            p50_s=row.p50_ms / 1000.0 if row.p50_ms is not None else None,
            runs_n=row.runs_n,
            cofail=cofail.get(nodeid, {}),
        )
    return history


async def _cofail_counts(session: AsyncSession, repo_id: int) -> dict[str, dict[str, int]]:
    """How often each pair of tests failed in the same run, over the last :data:`COFAIL_RUNS` runs."""
    recent = list(
        await session.scalars(
            select(Run.id)
            .where(Run.repo_id == repo_id, Run.status == "completed")
            .order_by(Run.created_at.desc())
            .limit(COFAIL_RUNS)
        )
    )
    if not recent:
        return {}
    rows = await session.execute(
        select(TestResult.run_id, TestCase.nodeid)
        .join(TestCase, TestCase.id == TestResult.test_id)
        .where(TestResult.run_id.in_(recent), TestResult.outcome.in_(FAILED), TestResult.flaky.is_(False))
    )
    failed_by_run: dict[str, set[str]] = defaultdict(set)
    for run_id, nodeid in rows:
        failed_by_run[run_id].add(nodeid)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for failed in failed_by_run.values():
        if len(failed) < 2 or len(failed) > 200:
            continue
        for a in failed:
            for b in failed:
                if a != b:
                    counts[a][b] += 1
    return {a: dict(b) for a, b in counts.items()}


@dataclass(frozen=True, slots=True)
class _Executed:
    outcome: str
    duration_ms: float | None
    flaky: bool
    run_id: str
    at: datetime


async def update_test_stats(session: AsyncSession, run: Run, test_ids: Sequence[int]) -> int:
    """Recompute ``test_stats`` of ``test_ids`` over their last :data:`HISTORY_WINDOW` executed results.

    Only completed trusted runs (plus ``run`` itself) count. Returns the number of rows written.
    """
    if not run.trusted:
        return 0
    executed: dict[int, list[_Executed]] = defaultdict(list)
    for chunk in _chunks(list(test_ids)):
        rows = await session.execute(
            select(
                TestResult.test_id,
                TestResult.outcome,
                TestResult.duration_ms,
                TestResult.flaky,
                Run.id,
                Run.created_at,
            )
            .join(Run, Run.id == TestResult.run_id)
            .where(
                TestResult.test_id.in_(chunk),
                TestResult.outcome != "not_run",
                Run.repo_id == run.repo_id,
                Run.trusted.is_(True),
                (Run.status == "completed") | (Run.id == run.id),
            )
        )
        for test_id, outcome, duration_ms, flaky, run_id, at in rows:
            executed[test_id].append(_Executed(outcome, duration_ms, bool(flaky), run_id, at))
    written = 0
    for test_id, results in executed.items():
        results.sort(key=lambda r: (r.at, r.run_id))
        window = results[-HISTORY_WINDOW:]
        row = await session.get(TestStats, (run.repo_id, test_id))
        if row is None:
            row = TestStats(repo_id=run.repo_id, test_id=test_id)
            session.add(row)
        _fill_stats(row, window, run.id)
        written += 1
    await session.flush()
    return written


def _fill_stats(row: TestStats, window: Sequence[_Executed], run_id: str) -> None:
    """Set every statistic of ``row`` from ``window`` (oldest first)."""
    failures = [r for r in window if r.outcome in FAILED or r.flaky]
    durations = sorted(r.duration_ms for r in window if r.duration_ms is not None)
    fail_ewma = 0.0
    dur_ewma: float | None = None
    for result in window:
        failed = 1.0 if (result.outcome in FAILED or result.flaky) else 0.0
        fail_ewma = EWMA_ALPHA * failed + (1 - EWMA_ALPHA) * fail_ewma
        if result.duration_ms is not None:
            dur_ewma = (
                result.duration_ms
                if dur_ewma is None
                else EWMA_ALPHA * result.duration_ms + (1 - EWMA_ALPHA) * dur_ewma
            )
    flaky_n = sum(1 for r in window if r.flaky)
    row.runs_n = len(window)
    row.fails_n = sum(1 for r in window if r.outcome in FAILED)
    row.last_fail_run_id = failures[-1].run_id if failures else None
    row.last_fail_at = failures[-1].at if failures else None
    row.last_run_at = window[-1].at if window else None
    row.fail_ewma = round(fail_ewma, 6)
    row.dur_ewma_ms = round(dur_ewma, 3) if dur_ewma is not None else None
    row.p50_ms = _quantile(durations, 0.5)
    row.p95_ms = _quantile(durations, 0.95)
    row.flaky_n = flaky_n
    row.flaky_score = round(flaky_n / len(window), 6) if window else 0.0
    row.updated_run_id = run_id


def _quantile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank quantile of sorted ``values`` (None when empty)."""
    if not values:
        return None
    index = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return round(float(values[index]), 3)


# -- seeded demo history --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededRun:
    """One run :func:`seed_history` executed (or found already done), for the summary table."""

    run_id: str
    name: str
    trigger: str
    verdict: str | None
    first_failure_s: float | None
    rank_coverset: int | None
    rank_file_order: int | None
    reused: bool = False


async def seed_history(
    ctx: OrchestratorContext,
    repo_id: int,
    commits: Sequence[tuple[str, str]],
    *,
    prs: Sequence[Mapping[str, Any]] = (),
    executor: str = "local",
    retry_key: str = "COVERSET_DEMO_RUN",
    on_event: Any = None,
    on_run: Any = None,
) -> list[SeededRun]:
    """Run ``commits`` (``(name, sha)`` in order) as baseline runs, then each PR, sequentially.

    Baseline run *n* (1-based) gets ``extra_env={retry_key: str(n)}`` and
    ``retry_increment_keys=[retry_key]``, so a retried test sees ``n + 1``. ``prs`` are mappings
    with ``number``, ``title``, ``head_sha``, ``base_sha`` and ``branch`` (the demo's
    ``<out>.shas.json`` entries); PR runs leave ``extra_env`` empty. A commit or PR head that already
    has a completed run of the same trigger is reused, so seeding is idempotent. ``on_event`` gets
    every run event; ``on_run(name, run_id)`` is called before each new run starts.
    """
    from coverset.orchestrator.runner import create_run, execute_run

    seeded: list[SeededRun] = []
    async with ctx.db.session() as session:
        from coverset.db.models import Repo

        repo = await session.get(Repo, repo_id)
        if repo is None:
            raise ValueError(f"no repo {repo_id}")
        branch = repo.default_branch
    for ordinal, (name, sha) in enumerate(commits, start=1):
        existing = await _completed_run(ctx, repo_id, sha, trigger="baseline", pr_number=None)
        if existing is not None:
            seeded.append(_seeded(existing, name, reused=True))
            continue
        run_id = await create_run(
            ctx,
            repo_id=repo_id,
            head_sha=sha,
            base_sha=None,
            branch=branch,
            trigger="baseline",
            executor=executor,
            extra_env={retry_key: str(ordinal)},
            retry_increment_keys=[retry_key],
        )
        if on_run is not None:
            on_run(name, run_id)
        await execute_run(ctx, run_id, on_event=on_event)
        seeded.append(_seeded(await _load(ctx, run_id), name))
    for pr in prs:
        number = int(pr["number"])
        existing = await _completed_run(
            ctx, repo_id, str(pr["head_sha"]), trigger="pull_request", pr_number=number
        )
        if existing is not None:
            seeded.append(_seeded(existing, f"pr-{number}", reused=True))
            continue
        run_id = await create_run(
            ctx,
            repo_id=repo_id,
            head_sha=str(pr["head_sha"]),
            base_sha=str(pr["base_sha"]),
            branch=str(pr.get("branch") or f"pr-{number}"),
            trigger="pull_request",
            executor=executor,
            pr_number=number,
            pr_title=str(pr.get("title") or ""),
            base_ref=branch,
        )
        if on_run is not None:
            on_run(f"pr-{number}", run_id)
        await execute_run(ctx, run_id, on_event=on_event)
        seeded.append(_seeded(await _load(ctx, run_id), f"pr-{number}"))
    return seeded


async def _completed_run(
    ctx: OrchestratorContext, repo_id: int, sha: str, *, trigger: str, pr_number: int | None
) -> Run | None:
    from coverset.db.models import PullRequest

    async with ctx.db.session() as session:
        query = (
            select(Run)
            .where(
                Run.repo_id == repo_id, Run.head_sha == sha, Run.trigger == trigger, Run.status == "completed"
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        if pr_number is not None:
            query = query.join(PullRequest, PullRequest.id == Run.pr_id).where(
                PullRequest.number == pr_number
            )
        return await session.scalar(query)


async def _load(ctx: OrchestratorContext, run_id: str) -> Run:
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run


def _seeded(run: Run, name: str, *, reused: bool = False) -> SeededRun:
    summary = run.summary_json or {}
    return SeededRun(
        run_id=run.id,
        name=name,
        trigger=run.trigger,
        verdict=run.verdict,
        first_failure_s=summary.get("first_failure_s"),
        rank_coverset=run.rank_coverset,
        rank_file_order=run.rank_file_order,
        reused=reused,
    )
