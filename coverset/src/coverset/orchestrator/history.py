"""Test history: the control-plane facts that drive scoring, and the demo's seeded run history.

* :func:`register_tests` keeps the ``tests`` table in step with what a run collected.
* :func:`prior_candidates`, :func:`previous_pr_run` and :func:`base_branch_run` find the runs whose
  facts a new run builds on (the prior run of the analysis, the base run for "pre-existing" failures).
* :func:`load_test_history` turns ``test_stats`` and recent results into
  :class:`~coverset.analysis.scoring.TestHistory` for scoring (with a parametrize-``family``
  fallback for new ids).
* :func:`update_test_stats` recomputes ``test_stats`` over each test's last 50 executed results
  (``not_run`` never counts; untrusted runs never update priors) and writes them in one upsert;
  :func:`p50_seconds` reads the p50s back by nodeid for the test economy at finalize.
* :func:`seed_history` replays the six demo ``main`` commits as baseline runs and then both demo
  PRs through the normal planner and runner.
* Retention: :func:`collect_garbage` is what ``coverset gc`` does (expired runs and their facts,
  their upload archives and the uploaded sources of commits no remaining run has as its head, old
  run events, old webhook payloads and delivery rows; the latest completed baseline per repo and
  the latest run per open PR always stay). ``serve`` runs it once at startup and then every
  :data:`GC_INTERVAL_S` (:func:`gc_forever`), and :func:`expire_pr_runs` applies the per-PR rule
  after each PR run. Both take :data:`GC_LOCK` without waiting: a gc already running in this
  process makes the other one skip.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import statistics
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.analysis.prior import PriorCandidate
from coverset.analysis.scoring import TestHistory
from coverset.db.models import PullRequest, Run, RunEvent, TestCase, TestResult, TestStats, WebhookDelivery
from coverset.db.session import lift_statement_timeout
from coverset.db.upsert import upsert
from coverset.facts.store import FactsStore

if TYPE_CHECKING:
    from coverset.config import Settings
    from coverset.db.session import Database
    from coverset.orchestrator.runner import OrchestratorContext

__all__ = [
    "BASELINE_TRIGGERS",
    "DELIVERY_KEEP_DAYS",
    "GC_INTERVAL_S",
    "GC_LOCK",
    "HISTORY_WINDOW",
    "GcReport",
    "SeededRun",
    "base_branch_run",
    "collect_garbage",
    "expire_pr_runs",
    "family_of",
    "gc_forever",
    "load_test_history",
    "nodeid_hash",
    "p50_seconds",
    "previous_pr_run",
    "prior_candidates",
    "prune_upload_files",
    "register_tests",
    "runs_to_keep",
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
GC_INTERVAL_S = 24 * 3600.0
"""How often ``serve`` runs :func:`collect_garbage` (once at startup, then this often)."""
DELIVERY_KEEP_DAYS = 30
"""Delivery rows stay this long (payload nulled earlier) so redeliveries are still deduplicated."""
GC_LOCK = threading.Lock()
"""Held while :func:`collect_garbage` or :func:`expire_pr_runs` runs in this process."""
_ACTIVE_STATUSES = ("queued", "running")

log = logging.getLogger(__name__)
_STATS_COLUMNS = (
    "repo_id",
    "test_id",
    "runs_n",
    "fails_n",
    "last_fail_run_id",
    "last_fail_at",
    "last_run_at",
    "fail_ewma",
    "dur_ewma_ms",
    "p50_ms",
    "p95_ms",
    "flaky_n",
    "flaky_score",
    "updated_run_id",
)


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

    A run qualifies only when its facts are usable: ``coverage_complete``, or (checked with
    ``store``) at least one committed shard ingest. A completed run without any (no tests
    collected, budget exhausted, every bundle rejected) would give scoring and wave 1 no coverage
    at all. With ``store``, runs without a facts directory are left out too (their facts were
    collected elsewhere or expired).
    """
    rows = await session.scalars(
        select(Run)
        .where(Run.repo_id == repo_id, Run.status == "completed", Run.id != exclude)
        .order_by(Run.created_at.desc())
        .limit(200)
    )
    candidates = []
    for run in rows:
        if store is not None:
            if not store.run_dir(repo_id, run.id).is_dir():
                continue
            if not run.coverage_complete and not _has_committed_shard(store, repo_id, run.id):
                continue
        elif not run.coverage_complete:
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


def _has_committed_shard(store: FactsStore, repo_id: int, run_id: str) -> bool:
    try:
        return bool(store.committed(repo_id, run_id))
    except Exception as exc:  # an unreadable ingest_log is no basis for scoring
        log.warning("cannot read the ingest log of run %s: %s", run_id, exc)
        return False


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
    stat_rows: list[dict[str, Any]] = []
    for test_id, results in executed.items():
        results.sort(key=lambda r: (r.at, r.run_id))
        stats = TestStats(repo_id=run.repo_id, test_id=test_id)
        _fill_stats(stats, results[-HISTORY_WINDOW:], run.id)
        stat_rows.append({column: getattr(stats, column) for column in _STATS_COLUMNS})
    # one upsert: concurrent runs of a repo may both create a test's first stats row
    await upsert_rows(session, TestStats, stat_rows, ("repo_id", "test_id"))
    return len(stat_rows)


async def p50_seconds(session: AsyncSession, repo_id: int, nodeids: Iterable[str]) -> dict[str, float]:
    """``nodeid -> p50 seconds`` from ``test_stats`` for the nodeids that have one (the durations the
    test economy weighs tests by: :class:`coverset.facts.economy.EconomyInputs`)."""
    by_hash = {nodeid_hash(n): n for n in dict.fromkeys(nodeids)}
    out: dict[str, float] = {}
    for chunk in _chunks(list(by_hash)):
        rows = await session.execute(
            select(TestCase.nodeid_hash, TestStats.p50_ms)
            .join(TestStats, (TestStats.test_id == TestCase.id) & (TestStats.repo_id == TestCase.repo_id))
            .where(
                TestCase.repo_id == repo_id, TestCase.nodeid_hash.in_(chunk), TestStats.p50_ms.is_not(None)
            )
        )
        for digest, p50_ms in rows:
            nodeid = by_hash.get(bytes(digest))
            if nodeid is not None:
                out[nodeid] = float(p50_ms) / 1000.0
    return out


async def upsert_rows(
    session: AsyncSession, model: type[Any], rows: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> None:
    """Insert ``rows`` into ``model``'s table, replacing the other columns where ``keys`` exist.

    One ``INSERT ... ON CONFLICT`` per distinct column set (:func:`coverset.db.upsert.upsert`).
    """
    await upsert(session, model, rows, keys)


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
    every run event; ``on_run(name, run_id)`` is called before each new run starts. Runs are
    inserted already claimed by ``ctx`` (``running``), so a ``serve`` sharing the database never
    picks them up and never abandons them at boot.
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
            claim=True,
        )
        if on_run is not None:
            on_run(name, run_id)
        await execute_run(ctx, run_id, on_event=on_event)
        seeded.append(_seeded(await _load(ctx, run_id), name))
    # The tip baseline schedules background work (the invariants scan, on Modal a re-snapshot);
    # let it finish so the PR runs, and the numbers measured on them, start from a quiet process.
    await ctx.finish_background()
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
            claim=True,
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


# -- retention ------------------------------------------------------------------------------------


@dataclass(slots=True)
class GcReport:
    """What :func:`collect_garbage` deleted (or, with ``dry_run``, would delete)."""

    dry_run: bool
    expired_db: list[str] = field(default_factory=list)
    expired_facts: list[str] = field(default_factory=list)
    events: int = 0
    payloads: int = 0
    deliveries: int = 0
    uploads: list[str] = field(default_factory=list)
    """Expired runs whose upload shard archives (``<runs_dir>/<run_id>/upload``) went."""
    sources: list[str] = field(default_factory=list)
    """``<repo_id>/<sha>`` of uploaded-source directories that went (no remaining run's head)."""
    kept: set[str] = field(default_factory=set)
    skipped: bool = False
    """Another gc held :data:`GC_LOCK`; nothing was done."""

    def lines(self, settings: Settings) -> list[str]:
        """The summary ``coverset gc`` prints."""
        if self.skipped:
            return ["skipped: another gc is running in this process"]
        verb = "would delete" if self.dry_run else "deleted"
        return [
            f"{verb} {len(self.expired_db)} run(s) from the database and "
            f"{len(self.expired_facts)} run(s) of facts",
            f"{verb} {self.events} run event(s) older than {settings.retention_events_days} d",
            f"{verb} the upload archives of {len(self.uploads)} run(s) and the uploaded sources of "
            f"{len(self.sources)} commit(s)",
            f"{'would clear' if self.dry_run else 'cleared'} {self.payloads} webhook payload(s) older than "
            f"{settings.retention_deliveries_days} d; {verb} {self.deliveries} delivery row(s)",
            f"kept {len(self.kept)} run(s): the latest baseline per repo and the latest run per open PR",
        ]


async def runs_to_keep(session: AsyncSession) -> set[str]:
    """The latest completed baseline per repo and the latest run per open PR."""
    keep: set[str] = set()
    baselines = await session.execute(
        select(Run.repo_id, Run.id)
        .where(Run.trigger.in_(BASELINE_TRIGGERS), Run.status == "completed")
        .order_by(Run.created_at.desc())
    )
    seen: set[int] = set()
    for repo_id, run_id in baselines:
        if repo_id not in seen:
            seen.add(repo_id)
            keep.add(run_id)
    for pr_id in await session.scalars(select(PullRequest.id).where(PullRequest.state == "open")):
        latest = await session.scalar(
            select(Run.id).where(Run.pr_id == pr_id).order_by(Run.created_at.desc()).limit(1)
        )
        if latest is not None:
            keep.add(latest)
    return keep


async def collect_garbage(
    db: Database, settings: Settings, store: FactsStore, *, dry_run: bool = False
) -> GcReport:
    """Apply the retention settings once (what ``coverset gc`` does); skipped while another gc runs.

    Deletes runs older than ``COVERSET_RETENTION_RUNS_DAYS`` that are not queued or running and not
    kept by :func:`runs_to_keep`, with their facts directories (``facts.gc.expire_runs``), their
    upload archives and the uploaded sources of head commits no remaining run of the repository
    has (:func:`prune_upload_files`); run events older than ``COVERSET_RETENTION_EVENTS_DAYS``;
    webhook payloads older than
    ``COVERSET_RETENTION_DELIVERIES_DAYS`` (nulled) and delivery rows older than
    ``max(30, COVERSET_RETENTION_DELIVERIES_DAYS)`` days.
    """
    if not GC_LOCK.acquire(blocking=False):
        return GcReport(dry_run=dry_run, skipped=True)
    try:
        return await _collect_garbage(db, settings, store, dry_run=dry_run)
    finally:
        GC_LOCK.release()


async def _collect_garbage(db: Database, settings: Settings, store: FactsStore, *, dry_run: bool) -> GcReport:
    from coverset.facts.gc import expire_runs

    now = settings.now()
    run_cutoff = now - timedelta(days=settings.retention_runs_days)
    event_cutoff = now - timedelta(days=settings.retention_events_days)
    payload_cutoff = now - timedelta(days=settings.retention_deliveries_days)
    delivery_cutoff = now - timedelta(days=max(DELIVERY_KEEP_DAYS, settings.retention_deliveries_days))
    report = GcReport(dry_run=dry_run)
    async with db.session() as session:
        keep = report.kept = await runs_to_keep(session)
        expired = [
            (int(repo_id), str(run_id), str(head_sha))
            for repo_id, run_id, head_sha in await session.execute(
                select(Run.repo_id, Run.id, Run.head_sha).where(
                    Run.created_at < run_cutoff,
                    Run.id.not_in(keep or {""}),
                    Run.status.not_in(_ACTIVE_STATUSES),
                )
            )
        ]
        report.expired_db = [run_id for _, run_id, _ in expired]
        report.events = int(
            await session.scalar(select(func.count(RunEvent.id)).where(RunEvent.at < event_cutoff)) or 0
        )
        report.payloads = int(
            await session.scalar(
                select(func.count(WebhookDelivery.id)).where(
                    WebhookDelivery.received_at < payload_cutoff, WebhookDelivery.payload.is_not(None)
                )
            )
            or 0
        )
        report.deliveries = int(
            await session.scalar(
                select(func.count(WebhookDelivery.id)).where(WebhookDelivery.received_at < delivery_cutoff)
            )
            or 0
        )
        if not dry_run:
            # The web engine's 30 s statement timeout must not cancel a backlog's delete for good.
            await lift_statement_timeout(session)
            if report.expired_db:
                await session.execute(delete(Run).where(Run.id.in_(report.expired_db)))
            await session.execute(delete(RunEvent).where(RunEvent.at < event_cutoff))
            await session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.received_at < payload_cutoff)
                .values(payload=None)
            )
            await session.execute(
                delete(WebhookDelivery).where(WebhookDelivery.received_at < delivery_cutoff)
            )
            await session.commit()
        report.uploads, report.sources = await prune_upload_files(session, settings, expired, dry_run=dry_run)
    report.expired_facts = await asyncio.to_thread(
        expire_runs, store, keep_run_ids=keep, older_than=run_cutoff, dry_run=dry_run
    )
    from coverset.storage.cachedir import evict  # keep this host's cache in its bound

    if (evicted := await asyncio.to_thread(evict, settings, dry_run=dry_run)).removed:
        log.info("gc: %s", evicted.line())
    return report


async def expire_pr_runs(
    db: Database, settings: Settings, store: FactsStore, pr_id: int, *, dry_run: bool = False
) -> list[str]:
    """The per-PR retention rule, applied after each PR run: keep the PR's latest run, delete its
    other runs older than ``COVERSET_RETENTION_RUNS_DAYS`` (never a queued or running one) with
    their facts. Returns the deleted run ids (empty when another gc holds :data:`GC_LOCK`)."""
    if not GC_LOCK.acquire(blocking=False):
        return []
    try:
        cutoff = settings.now() - timedelta(days=settings.retention_runs_days)
        async with db.session() as session:
            latest = await session.scalar(
                select(Run.id).where(Run.pr_id == pr_id).order_by(Run.created_at.desc()).limit(1)
            )
            rows = (
                await session.execute(
                    select(Run.repo_id, Run.id, Run.head_sha).where(
                        Run.pr_id == pr_id,
                        Run.created_at < cutoff,
                        Run.id != (latest or ""),
                        Run.status.not_in(_ACTIVE_STATUSES),
                    )
                )
            ).all()
            expired = [(int(repo_id), str(run_id), str(head_sha)) for repo_id, run_id, head_sha in rows]
            if expired and not dry_run:
                await lift_statement_timeout(session)
                await session.execute(delete(Run).where(Run.id.in_([run_id for _, run_id, _ in expired])))
                await session.commit()
            if expired:
                await prune_upload_files(session, settings, expired, dry_run=dry_run)
        if not dry_run:
            for repo_id, run_id, _ in expired:
                await asyncio.to_thread(store.delete_run, repo_id, run_id)
        return sorted(run_id for _, run_id, _ in expired)
    finally:
        GC_LOCK.release()


async def prune_upload_files(
    session: AsyncSession,
    settings: Settings,
    expired: Sequence[tuple[int, str, str]],
    *,
    dry_run: bool = False,
) -> tuple[list[str], list[str]]:
    """Remove what upload mode keeps on disk for ``expired`` runs (``(repo_id, run_id, head_sha)``).

    Each run's unpacked shards (``<runs_dir>/<run_id>/upload``) go, and each head commit's uploaded
    sources and upload archives in the blob store once no run of the repository other than the
    expired ones has that head (call it after the runs' rows are deleted, or with ``dry_run``).
    Returns ``(run ids, "<repo_id>/<sha>" commits)`` that were (or would be) removed.
    """
    from coverset.facts.gc import remove_upload_dirs
    from coverset.facts.sources import source_store
    from coverset.storage import from_settings
    from coverset.storage.uploads import remove_uploads

    if not expired:
        return [], []
    gone = {run_id for _, run_id, _ in expired}
    uploads = await asyncio.to_thread(remove_upload_dirs, settings.runs_dir, gone, dry_run=dry_run)
    by_repo: dict[int, set[str]] = defaultdict(set)
    for repo_id, _, sha in expired:
        by_repo[repo_id].add(sha)
    sources: list[str] = []
    for repo_id, shas in sorted(by_repo.items()):
        live = set(
            await session.scalars(
                select(Run.head_sha).where(
                    Run.repo_id == repo_id, Run.head_sha.in_(sorted(shas)), Run.id.not_in(sorted(gone))
                )
            )
        )
        removed = await asyncio.to_thread(
            source_store(settings).remove, repo_id, shas - live, dry_run=dry_run
        )
        sources += [f"{repo_id}/{sha}" for sha in removed]
        archived = set(
            await asyncio.to_thread(
                remove_uploads, from_settings(settings), repo_id, shas - live, dry_run=dry_run
            )
        )
        uploads += [r for rid, r, sha in expired if rid == repo_id and sha in archived and r not in uploads]
    return sorted(uploads), sources


async def gc_forever(
    db: Database,
    settings: Settings,
    store: FactsStore,
    stop: asyncio.Event,
    *,
    interval_s: float = GC_INTERVAL_S,
) -> None:
    """Run :func:`collect_garbage` now and then every ``interval_s`` until ``stop`` is set.

    A failed pass is logged and retried at the next interval; it never ends the loop.
    """
    while not stop.is_set():
        try:
            report = await collect_garbage(db, settings, store)
            if not report.skipped:
                log.info("gc: %s", "; ".join(report.lines(settings)))
        except Exception:
            log.exception("in-process gc failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue
