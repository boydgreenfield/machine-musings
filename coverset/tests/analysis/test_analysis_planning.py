"""Scoring, wave-1 cover set and sharding against a fake prior run."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from analysis_helpers import FakePriorRun, item

from coverset.analysis.cover import CoverSet, change_covering_set
from coverset.analysis.functions import FunctionChange, index_functions
from coverset.analysis.impact import ImpactIndex
from coverset.analysis.prior import PriorCandidate, select_prior_run
from coverset.analysis.scoring import (
    WEIGHTS,
    ScoredTest,
    ScoringContext,
    TestHistory,
    exploration_seed,
    score_tests,
)
from coverset.analysis.sharding import plan_shards, reshard, shard_scores

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
SRC = textwrap.dedent(
    """\
    RATE = 2


    def allocate_cents(total, weights):
        share = total / sum(weights)
        return [share * w for w in weights]


    def refund(amount):
        return -amount


    def format_total(total):
        return f"{total:.2f}"
    """
)
REGIONS = {r.qualname: r for r in index_functions("src/ledger/billing.py", SRC)}
T_ALLOC = "tests/test_billing.py::test_allocate"
T_ALLOC_SLOW = "tests/test_billing.py::test_allocate_slow"
T_REFUND = "tests/test_refund.py::test_refund"
T_FORMAT = "tests/test_format.py::test_format"
T_IMPORT = "tests/test_import.py::test_reimport"
T_NEW = "tests/test_new.py::test_brand_new"


def _prior() -> FakePriorRun:
    path = "src/ledger/billing.py"
    return FakePriorRun(
        run_id="run-1",
        regions=list(REGIONS.values()),
        lines={
            T_ALLOC: {path: {5, 6}},
            T_ALLOC_SLOW: {path: {5, 6, 10}},
            T_REFUND: {path: {10}},
            T_FORMAT: {path: {14}},
            T_IMPORT: {path: {1}},
            "<import>": {path: {1, 4, 9, 13}},
        },
        module_lines={path: {1}},
        p50={T_ALLOC: 0.5, T_ALLOC_SLOW: 3.0, T_REFUND: 0.2, T_FORMAT: 0.1, T_IMPORT: 0.1},
    )


def _modified(name: str) -> FunctionChange:
    """A head-side edit of ``name``: same key, new body hash."""
    return FunctionChange(replace(REGIONS[name], body_hash="0" * 16), REGIONS[name], "modified")


def _ctx(**overrides) -> ScoringContext:
    values = {
        "prior": _prior(),
        "changed": [_modified("allocate_cents")],
        "changed_files": {"src/ledger/billing.py"},
        "test_history": {
            T_ALLOC: TestHistory(runs_n=10, p50_s=0.5),
            T_ALLOC_SLOW: TestHistory(runs_n=10, p50_s=3.0),
            T_REFUND: TestHistory(runs_n=10, p50_s=0.2, failed_in_prev_run=True),
            T_FORMAT: TestHistory(runs_n=10, p50_s=0.1, last_fail_at=NOW - timedelta(days=7)),
            T_IMPORT: TestHistory(runs_n=10, p50_s=0.1, flaky_score=0.3),
        },
        "now": NOW,
        "rng_seed": 7,
    }
    values.update(overrides)
    return ScoringContext(**values)


ITEMS = [item(n) for n in (T_FORMAT, T_IMPORT, T_REFUND, T_ALLOC_SLOW, T_ALLOC, T_NEW)]


def test_score_components_and_order() -> None:
    scored = {t.nodeid: t for t in score_tests(ITEMS, _ctx())}
    order = [t.nodeid for t in score_tests(ITEMS, _ctx())]
    assert order[:2] == [T_REFUND, T_ALLOC]  # failed last run (10) beats impacted (6 + 3)
    assert order.index(T_ALLOC) < order.index(T_ALLOC_SLOW)  # equal score, shorter p50 first
    refund = scored[T_REFUND]
    assert refund.components["failed_last_run"] == WEIGHTS["failed_last_run"]
    assert refund.explanation == "failed in previous run"
    alloc = scored[T_ALLOC]
    assert alloc.components["impacted_by_diff"] == 6.0
    assert alloc.components["cov_overlap_frac"] == pytest.approx(3.0)
    assert alloc.score == pytest.approx(sum(alloc.components.values()))
    assert alloc.explanation == "covers allocate_cents (changed)"
    assert scored[T_NEW].components["new_or_changed"] == 8.0
    assert scored[T_NEW].explanation == "new test"
    assert scored[T_FORMAT].components["fail_decay"] == pytest.approx(1.0)  # 2 * 2**(-7/7)
    assert "last failed 7 d ago" in scored[T_FORMAT].explanation
    importer = scored[T_IMPORT]
    assert importer.components["impacted_by_diff"] == 6.0  # module-level line of a changed file
    assert importer.components["recently_flaky"] == 1.0
    assert "module-level" in importer.explanation


def test_failed_and_changed_test_file_explanation() -> None:
    ctx = _ctx(changed_files={"src/ledger/billing.py", "tests/test_refund.py"})
    refund = next(t for t in score_tests(ITEMS, ctx) if t.nodeid == T_REFUND)
    assert refund.explanation == "failed in previous run; test file changed"
    assert refund.components["new_or_changed"] == 8.0


def test_trivial_changes_do_not_impact() -> None:
    ctx = _ctx(
        changed=[FunctionChange(REGIONS["allocate_cents"], REGIONS["allocate_cents"], "trivial")],
        changed_files=set(),
    )
    alloc = next(t for t in score_tests(ITEMS, ctx) if t.nodeid == T_ALLOC)
    assert alloc.components["impacted_by_diff"] == 0.0


def test_cofail_and_last_outcome_fallback() -> None:
    history = dict(_ctx().test_history)
    history[T_FORMAT] = TestHistory(runs_n=5, last_outcome="failed")
    history[T_ALLOC] = TestHistory(runs_n=5, cofail={T_FORMAT: 4})
    scored = {t.nodeid: t for t in score_tests(ITEMS, _ctx(test_history=history))}
    assert scored[T_FORMAT].components["failed_last_run"] == 10.0
    assert scored[T_ALLOC].components["cofail_norm"] == 0.5
    assert f"fails together with {T_FORMAT}" in scored[T_ALLOC].explanation


def test_no_prior_run_scores_without_impact() -> None:
    scored = score_tests(ITEMS, _ctx(prior=None, test_history={}))
    assert all(t.components["impacted_by_diff"] == 0 for t in scored)
    assert all(t.components["new_or_changed"] == 8.0 for t in scored)


def test_exploration_sample_is_seeded_and_lands_in_first_30_percent() -> None:
    items = [item(f"tests/test_many.py::test_{i:03d}") for i in range(100)]
    history = {i.nodeid: TestHistory(runs_n=3, p50_s=1.0 + n / 100) for n, i in enumerate(items)}
    ctx = _ctx(prior=None, changed=[], changed_files=set(), test_history=history, rng_seed=11)
    first = score_tests(items, ctx)
    again = score_tests(items, ctx)
    assert [t.nodeid for t in first] == [t.nodeid for t in again]
    explored = [i for i, t in enumerate(first) if t.explanation.startswith("exploration sample")]
    assert len(explored) == 3
    assert all(i < 30 for i in explored)
    other = score_tests(
        items, _ctx(prior=None, changed=[], changed_files=set(), test_history=history, rng_seed=12)
    )
    assert [t.nodeid for t in other] != [t.nodeid for t in first]


def test_change_covering_set_is_greedy_by_duration() -> None:
    changed = [_modified("allocate_cents"), _modified("refund")]
    items = [item(n) for n in (T_ALLOC, T_ALLOC_SLOW, T_REFUND, T_FORMAT, T_IMPORT, T_NEW)]
    cover = change_covering_set(items, changed, _prior(), _prior().durations())
    # allocate_slow covers both bodies but costs 3.0 s; allocate (0.5 s) + refund (0.2 s) win.
    assert cover.tests[:2] == [T_REFUND, T_ALLOC]
    assert T_ALLOC_SLOW not in cover.tests
    assert cover.reasons[T_ALLOC] == "covers allocate_cents (changed)"
    assert cover.reasons[T_NEW] == "no coverage history"
    assert cover.tests[-1] == T_NEW


def test_change_covering_set_prefers_one_test_covering_many_when_cheap() -> None:
    prior = _prior()
    prior.p50[T_ALLOC_SLOW] = 0.3
    changed = [_modified("allocate_cents"), _modified("refund")]
    items = [item(n) for n in (T_ALLOC, T_ALLOC_SLOW, T_REFUND)]
    cover = change_covering_set(items, changed, prior, prior.durations())
    assert cover.tests == [T_ALLOC_SLOW]
    assert cover.reasons[T_ALLOC_SLOW] == "covers allocate_cents, refund (changed)"


def test_change_covering_set_adds_changed_test_files_and_reports_uncovered() -> None:
    changed = [_modified("format_total")]
    items = [item(T_REFUND), item(T_ALLOC)]
    cover = change_covering_set(items, changed, _prior(), {}, changed_files={"tests/test_refund.py"})
    assert cover.tests == [T_REFUND]
    assert cover.reasons[T_REFUND] == "in changed file tests/test_refund.py"
    assert cover.uncovered == ["format_total"]


def _scored(n: int, seconds: float = 1.0) -> list[ScoredTest]:
    return [ScoredTest(f"t::{i:02d}", float(n - i), {}, "", seconds) for i in range(n)]


def test_shard_count_and_balance() -> None:
    plan = plan_shards(_scored(20), CoverSet(), target_shard_seconds=5, max_shards=16, median_s=1.0)
    assert len(plan.shards) == 4
    assert plan.predicted_s == [5.0, 5.0, 5.0, 5.0]
    assert plan.shards[0][:2] == ["t::00", "t::04"]  # priority order is kept within a shard
    capped = plan_shards(_scored(20), CoverSet(), target_shard_seconds=1, max_shards=3, median_s=1.0)
    assert len(capped.shards) == 3
    tiny = plan_shards(_scored(2), CoverSet(), target_shard_seconds=0.1, max_shards=16, median_s=1.0)
    assert len(tiny.shards) == 2
    empty = plan_shards([], CoverSet(), target_shard_seconds=5, max_shards=4, median_s=1.0)
    assert empty.shards == [[]]


def test_wave1_runs_first_on_every_shard() -> None:
    scored = _scored(12)
    wave1 = CoverSet(tests=["t::11", "t::10", "t::09"], reasons={})
    plan = plan_shards(scored, wave1, target_shard_seconds=4, max_shards=8, median_s=1.0)
    assert len(plan.shards) == 3
    assert [shard[0] for shard in plan.shards] == ["t::11", "t::10", "t::09"]
    assert sorted(n for s in plan.shards for n in s) == sorted(t.nodeid for t in scored)


def test_unknown_durations_use_the_median_and_lpt_balances() -> None:
    scored = [
        ScoredTest("a", 9, {}, "", 6.0),
        ScoredTest("b", 8, {}, "", None),
        ScoredTest("c", 7, {}, "", 1.0),
        ScoredTest("d", 6, {}, "", 1.0),
    ]
    plan = plan_shards(scored, CoverSet(), target_shard_seconds=6, max_shards=4, median_s=2.0)
    assert plan.shards == [["a"], ["b", "c", "d"]]  # Σ = 10 s over a 6 s target: 2 shards
    assert plan.predicted_s == [6.0, 4.0]


def test_fixture_groups_stay_together_within_a_decile() -> None:
    scored = _scored(20)
    groups = {"t::00": "mod_a", "t::01": "mod_a", "t::05": "mod_a", "t::02": "mod_b"}
    plan = plan_shards(scored, CoverSet(), target_shard_seconds=5, max_shards=4, median_s=1.0, groups=groups)
    shard_of = {n: i for i, s in enumerate(plan.shards) for n in s}
    assert shard_of["t::00"] == shard_of["t::01"]  # same group, same decile (ranks 0 and 1)
    assert plan.shards[shard_of["t::00"]][:2] == ["t::00", "t::01"]
    units = [u.nodeids for u in plan.units]
    assert ("t::00", "t::01") in units
    assert ("t::05",) in units  # same group but a later priority decile


def test_reshard_to_fewer_workers_keeps_wave1_first() -> None:
    wave1 = CoverSet(tests=["t::19", "t::18"], reasons={})
    plan = plan_shards(_scored(20), wave1, target_shard_seconds=4, max_shards=8, median_s=1.0)
    assert len(plan.shards) == 5
    smaller = reshard(plan, 2)
    assert [s[0] for s in smaller.shards] == ["t::19", "t::18"]
    assert sum(smaller.predicted_s) == pytest.approx(sum(plan.predicted_s))
    assert reshard(plan, 9) is plan
    with pytest.raises(ValueError, match="capacity"):
        reshard(plan, 0)


def test_select_prior_run_order_of_preference() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    candidates = [
        PriorCandidate("r1", "base", "main", t0, baseline=True),
        PriorCandidate("r2", "mb", "main", t0 + timedelta(hours=1), baseline=True),
        PriorCandidate("r3", "prev", "feature", t0 + timedelta(hours=2), baseline=False),
        PriorCandidate("r4", "other", "main", t0 + timedelta(hours=3), baseline=True),
    ]
    assert (
        select_prior_run(candidates, previous_head="prev", merge_base="mb", default_branch="main").run_id
        == "r3"
    )
    choice = select_prior_run(candidates, previous_head="gone", merge_base="mb", default_branch="main")
    assert (choice.run_id, choice.basis) == ("r2", "merge_base")
    choice = select_prior_run(candidates, previous_head=None, merge_base="zzz", default_branch="main")
    assert (choice.run_id, choice.basis) == ("r4", "baseline")
    assert choice.describe() == "run r4 (baseline)"
    assert select_prior_run([], previous_head=None, merge_base=None, default_branch="main") is None


PR_SRC = SRC + "\n\ndef apply_discount(total):\n    return total * 0.9\n"
PR_REGIONS = {r.qualname: r for r in index_functions("src/ledger/billing.py", PR_SRC)}
T_DISCOUNT = "tests/test_other.py::test_uses_discount"


def _previous_head_prior() -> FakePriorRun:
    """The PR's previous push: it already added apply_discount, which test_uses_discount calls."""
    return FakePriorRun(
        run_id="run-prev-head",
        regions=list(PR_REGIONS.values()),
        lines={T_DISCOUNT: {"src/ledger/billing.py": {18}}, T_ALLOC: {"src/ledger/billing.py": {5, 6}}},
        p50={T_DISCOUNT: 0.1, T_ALLOC: 0.5},
    )


def test_added_function_is_resolved_against_the_previous_head() -> None:
    edited = replace(PR_REGIONS["apply_discount"], body_hash="f" * 16)  # edited again in this push
    changed = [FunctionChange(edited, None, "added")]  # added relative to the merge-base
    prior = _previous_head_prior()
    impact = ImpactIndex.build(prior, changed)
    assert impact.impacted(T_DISCOUNT) and not impact.impacted(T_ALLOC)
    cover = change_covering_set([item(T_DISCOUNT), item(T_ALLOC)], changed, prior, prior.durations())
    assert cover.tests == [T_DISCOUNT]
    assert cover.reasons[T_DISCOUNT] == "covers apply_discount (changed)"
    ctx = _ctx(prior=prior, changed=changed, changed_files=set(), test_history={})
    scored = {t.nodeid: t for t in score_tests([item(T_DISCOUNT), item(T_ALLOC)], ctx)}
    assert scored[T_DISCOUNT].components["impacted_by_diff"] == 6.0


def test_change_already_in_the_prior_run_is_not_impacted() -> None:
    # Modified relative to the merge-base, but this push left it as the previous head had it.
    base_side = replace(PR_REGIONS["apply_discount"], body_hash="b" * 16)
    changed = [FunctionChange(PR_REGIONS["apply_discount"], base_side, "modified")]
    impact = ImpactIndex.build(_previous_head_prior(), changed)
    assert impact.regions == {} and not impact.impacted(T_DISCOUNT)


def test_deleted_function_is_always_risky() -> None:
    changed = [FunctionChange(None, PR_REGIONS["apply_discount"], "deleted")]
    assert ImpactIndex.build(_previous_head_prior(), changed).impacted(T_DISCOUNT)


def test_change_covering_set_without_a_prior_run() -> None:
    items = [item(T_REFUND), item(T_ALLOC), item(T_NEW)]
    cover = change_covering_set(
        items, [_modified("allocate_cents")], None, {}, changed_files={"tests/test_refund.py"}
    )
    assert cover.tests == [T_REFUND]  # no history at all: score order decides the rest
    assert cover.uncovered == []


def test_exploration_seed_comes_from_the_run_id() -> None:
    assert exploration_seed("run-41") == exploration_seed("run-41")
    assert exploration_seed("run-41") != exploration_seed("run-42")
    items = [item(f"tests/test_many.py::test_{i:03d}") for i in range(100)]
    history = {i.nodeid: TestHistory(runs_n=3, p50_s=1.0) for i in items}
    seed = exploration_seed("run-41")
    ctx = _ctx(prior=None, changed=[], changed_files=set(), test_history=history, rng_seed=seed)
    explored = [t for t in score_tests(items, ctx) if t.explanation.startswith("exploration sample")]
    assert explored and all(f"seed {seed}" in t.explanation for t in explored)


@dataclass
class _FakeItem:
    """Enough of a pytest.Item for pytest_coverset.ordering.priority_order."""

    nodeid: str

    def getparent(self, cls: type) -> None:
        return None


def test_shard_scores_reproduce_the_plan_order_in_the_plugin() -> None:
    ordering = pytest.importorskip("pytest_coverset.ordering")
    scored = [ScoredTest(f"tests/test_{i:02d}.py::test", float(20 - i), {}, "", 1.0) for i in range(12)]
    wave1 = CoverSet(tests=["tests/test_11.py::test", "tests/test_10.py::test"], reasons={})
    plan = plan_shards(scored, wave1, target_shard_seconds=6, max_shards=4, median_s=1.0)
    raw = {t.nodeid: t.score for t in scored}
    for shard, scores in zip(plan.shards, shard_scores(plan), strict=True):
        shuffled = [_FakeItem(n) for n in reversed(shard)]
        assert [i.nodeid for i in ordering.priority_order(shuffled, scores)] == shard
    first = plan.shards[0]
    assert [i.nodeid for i in ordering.priority_order([_FakeItem(n) for n in first], raw)] != first
