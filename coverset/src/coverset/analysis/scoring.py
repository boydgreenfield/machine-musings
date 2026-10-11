"""Stage-0 heuristic test prioritisation: likeliest failures first, each with a one-line reason.

``score = 10·failed_last_run + 8·(new_test or test_file_changed) + 6·impacted_by_diff
+ 3·cov_overlap_frac + 2·fail_decay + 1·recently_flaky + 0.5·cofail_norm``; ties go to the shorter
p50 duration (unknown durations count as the median), then to the nodeid. An ε ≈ 3% seeded sample
of tests outside the first 30% is moved into the first 30% so failure labels keep flowing for
low-score tests; the seed comes from the run id (:func:`exploration_seed`), so each run explores
different tests while any one run stays reproducible. The component values stored on each :class:`ScoredTest` are weighted, so they sum
to its score.

Component definitions:

* ``failed_last_run``: the test failed in the PR's previous run, else in the latest baseline
  (``TestHistory.failed_in_prev_run``; when that is unknown, ``last_outcome`` is used).
* ``new_test``/``test_file_changed``: no history at all, or the test's file is in the diff.
* ``impacted_by_diff``: the test's prior-run lines touch a changed function body or module-level
  lines of a changed file (see :mod:`coverset.analysis.impact`).
* ``cov_overlap_frac``: the fraction of the changed prior regions this test touched.
* ``fail_decay``: ``2 ** (-days since last failure / 7)``.
* ``recently_flaky``: ``flaky_score > 0``.
* ``cofail_norm``: how often the test failed together with a test that failed last run,
  normalised by the largest such count in the suite.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from coverset.analysis.functions import FunctionChange
from coverset.analysis.impact import ImpactIndex
from coverset.lang.base import CollectedItem, PriorRun

__all__ = [
    "EXPLORATION_RATE",
    "WEIGHTS",
    "ScoredTest",
    "ScoringContext",
    "TestHistory",
    "exploration_seed",
    "score_tests",
]

WEIGHTS: Final[Mapping[str, float]] = {
    "failed_last_run": 10.0,
    "new_or_changed": 8.0,
    "impacted_by_diff": 6.0,
    "cov_overlap_frac": 3.0,
    "fail_decay": 2.0,
    "recently_flaky": 1.0,
    "cofail_norm": 0.5,
}
"""Weight of each score component, in the order the explanation lists them."""

EXPLORATION_RATE: Final = 0.03
EXPLORATION_HEAD: Final = 0.30
FAIL_HALF_LIFE_DAYS: Final = 7.0
_FAILED: Final = frozenset({"failed", "error"})
_MAX_NAMED_FUNCS: Final = 2


@dataclass(frozen=True, slots=True)
class TestHistory:
    """What the control DB knows about one test (from ``test_stats`` and recent results)."""

    __test__ = False  # not a pytest test class

    last_outcome: str | None = None
    failed_in_prev_run: bool | None = None
    """Failed in the PR's previous run, else in the latest baseline; None when neither ran it."""
    last_fail_at: datetime | None = None
    fail_ewma: float = 0.0
    flaky_score: float = 0.0
    p50_s: float | None = None
    runs_n: int = 0
    cofail: Mapping[str, int] = field(default_factory=dict)
    """Number of runs in which this test failed together with each other nodeid."""


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything :func:`score_tests` reads besides the collected items."""

    prior: PriorRun | None
    changed: Sequence[FunctionChange]
    changed_files: Collection[str]
    test_history: Mapping[str, TestHistory]
    now: datetime
    rng_seed: int
    """Seed of the exploration sample; use :func:`exploration_seed` of the run id."""


@dataclass(frozen=True, slots=True)
class ScoredTest:
    """One test's priority: total score, weighted components and a one-line explanation."""

    nodeid: str
    score: float
    components: dict[str, float]
    explanation: str
    p50_s: float | None


def exploration_seed(run_id: str) -> int:
    """A 64-bit exploration seed derived from ``run_id`` (stable per run, different across runs)."""
    return int.from_bytes(hashlib.blake2b(run_id.encode(), digest_size=8).digest(), "big")


def score_tests(items: Sequence[CollectedItem], ctx: ScoringContext) -> list[ScoredTest]:
    """Score ``items`` and return them in run order (descending priority, exploration applied)."""
    impact = ImpactIndex.build(ctx.prior, ctx.changed, ctx.changed_files)
    prior_durations = ctx.prior.durations() if ctx.prior is not None else {}
    failed_last = {n for n, h in ctx.test_history.items() if _failed_last_run(h)}
    cofail_max = max(
        (
            count
            for h in ctx.test_history.values()
            for other, count in h.cofail.items()
            if other in failed_last
        ),
        default=0,
    )
    scored: list[ScoredTest] = []
    for item in items:
        history = ctx.test_history.get(item.nodeid)
        p50 = history.p50_s if history and history.p50_s is not None else prior_durations.get(item.nodeid)
        raw, notes = _components(item, history, impact, ctx, failed_last, cofail_max)
        weighted = {name: WEIGHTS[name] * value for name, value in raw.items()}
        total = round(sum(weighted.values()), 6)
        explanation = "; ".join(notes) or _quiet_explanation(history, p50)
        scored.append(ScoredTest(item.nodeid, total, weighted, explanation, p50))
    known = [t.p50_s for t in scored if t.p50_s is not None]
    median = statistics.median(known) if known else 0.0
    scored.sort(key=lambda t: (-t.score, t.p50_s if t.p50_s is not None else median, t.nodeid))
    return _explore(scored, ctx.rng_seed)


def _components(
    item: CollectedItem,
    history: TestHistory | None,
    impact: ImpactIndex,
    ctx: ScoringContext,
    failed_last: set[str],
    cofail_max: int,
) -> tuple[dict[str, float], list[str]]:
    notes: list[str] = []
    failed = history is not None and _failed_last_run(history)
    if failed:
        notes.append("failed in previous run")
    is_new = history is None or history.runs_n == 0
    file_changed = item.file in ctx.changed_files
    if is_new:
        notes.append("new test")
    elif file_changed:
        notes.append("test file changed")
    touched = impact.regions_touched_by(item.nodeid)
    impacted = bool(touched) or item.nodeid in impact.module_tests
    if touched:
        names = [r.qualname for r in touched[:_MAX_NAMED_FUNCS]]
        more = len(touched) - len(names)
        notes.append(f"covers {', '.join(names)} (changed)" + (f" +{more} more" if more else ""))
    elif impacted:
        notes.append("touches module-level code of a changed file")
    overlap = len(touched) / len(impact.regions) if impact.regions else 0.0
    decay = 0.0
    if history is not None and history.last_fail_at is not None:
        age_days = max(0.0, (ctx.now - history.last_fail_at).total_seconds() / 86_400)
        decay = 2.0 ** (-age_days / FAIL_HALF_LIFE_DAYS)
        if not failed:
            notes.append(f"last failed {_age(age_days)} ago")
    flaky = history is not None and history.flaky_score > 0
    if flaky:
        notes.append("recently flaky")
    cofail = 0.0
    if history is not None and cofail_max:
        partners = {o: c for o, c in history.cofail.items() if o in failed_last and o != item.nodeid}
        if partners:
            partner, count = max(partners.items(), key=lambda kv: (kv[1], kv[0]))
            cofail = count / cofail_max
            notes.append(f"fails together with {partner}")
    raw = {
        "failed_last_run": float(failed),
        "new_or_changed": float(is_new or file_changed),
        "impacted_by_diff": float(impacted),
        "cov_overlap_frac": overlap,
        "fail_decay": decay,
        "recently_flaky": float(flaky),
        "cofail_norm": cofail,
    }
    return raw, notes


def _failed_last_run(history: TestHistory) -> bool:
    if history.failed_in_prev_run is not None:
        return history.failed_in_prev_run
    return history.last_outcome in _FAILED


def _explore(ordered: list[ScoredTest], seed: int) -> list[ScoredTest]:
    """Move a seeded ε-sample of tests from beyond the first 30% to the end of the first 30%."""
    n = len(ordered)
    head = math.ceil(EXPLORATION_HEAD * n)
    tail = ordered[head:]
    k = min(len(tail), round(EXPLORATION_RATE * n))
    if k == 0:
        return ordered
    picked = set(random.Random(seed).sample(range(len(tail)), k))
    label = f"exploration sample (ε = 3%, seed {seed})"
    explored = [
        ScoredTest(t.nodeid, t.score, t.components, f"{label}; {t.explanation}", t.p50_s)
        for i, t in enumerate(tail)
        if i in picked
    ]
    rest = [t for i, t in enumerate(tail) if i not in picked]
    cut = head - k
    return ordered[:cut] + explored + ordered[cut:head] + rest


def _duration(seconds: float) -> str:
    """A p50 the way the UI's tests page shows durations: ``0.4 ms``, ``12 ms``, ``1.4 s``."""
    ms = seconds * 1000.0
    if ms < 10:
        return f"{ms:.1f} ms"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{seconds:.1f} s"


def _quiet_explanation(history: TestHistory | None, p50: float | None) -> str:
    """The reason of a test with no risk signal (score 0): its fail rate if any, and its p50."""
    duration = f"p50 {_duration(p50)}" if p50 is not None else "duration unknown"
    if history is not None and history.fail_ewma > 0:
        return f"no recent risk signal; fail rate {history.fail_ewma:.0%}; {duration}"
    return f"no risk signal; {duration}"


def _age(days: float) -> str:
    if days < 1:
        return f"{max(1, round(days * 24))} h"
    return f"{round(days)} d"
