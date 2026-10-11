"""Sharding: never plan an empty shard (an empty args file makes pytest run the whole suite), and never
fan a small or untimed suite out into one-test shards."""

from __future__ import annotations

import pytest

from coverset.analysis.cover import CoverSet
from coverset.analysis.scoring import ScoredTest
from coverset.analysis.sharding import MIN_TESTS_PER_SHARD, UNKNOWN_DURATION_S, plan_shards, reshard


def _one_file(n: int, seconds: float = 1.0) -> list[ScoredTest]:
    return [ScoredTest(f"tests/test_one.py::test_{i:02d}", float(n - i), {}, "", seconds) for i in range(n)]


def test_fixture_groups_cap_the_shard_count_at_the_unit_count() -> None:
    scored = _one_file(40)
    groups = {t.nodeid: "tests/test_one.py" for t in scored}
    plan = plan_shards(
        scored, CoverSet(), target_shard_seconds=1e-9, max_shards=16, median_s=1.0, groups=groups
    )
    assert len(plan.shards) == len(plan.units) == 10  # one unit per priority decile of the file
    assert all(plan.shards), [len(s) for s in plan.shards]
    assert sorted(n for s in plan.shards for n in s) == sorted(t.nodeid for t in scored)


def test_reshard_never_deals_more_shards_than_units() -> None:
    scored = _one_file(12)
    groups = {t.nodeid: "f" for t in scored}
    plan = plan_shards(
        scored, CoverSet(), target_shard_seconds=1e-9, max_shards=4, median_s=1.0, groups=groups
    )
    smaller = reshard(plan, 2)
    assert len(smaller.shards) == 2 and all(smaller.shards)


# -- a first baseline without duration history ------------------------------------------------------


def _untimed(n: int) -> list[ScoredTest]:
    return [ScoredTest(f"tests/test_m{i % 4}.py::test_{i:02d}", float(n - i), {}, "", None) for i in range(n)]


def test_untimed_suite_uses_the_conservative_default_and_one_shard() -> None:
    plan = plan_shards(_untimed(14), CoverSet(), target_shard_seconds=1.0, max_shards=32)
    assert len(plan.shards) == 1, "14 x 0.05 s = 0.7 s fits one 1 s shard (it used to be 7 shards)"
    assert plan.predicted_s == [pytest.approx(14 * UNKNOWN_DURATION_S)]


def test_guessed_durations_never_make_shards_of_fewer_than_three_tests() -> None:
    plan = plan_shards(_untimed(14), CoverSet(), target_shard_seconds=0.01, max_shards=32)
    assert len(plan.shards) == 14 // MIN_TESTS_PER_SHARD == 4
    assert all(len(shard) >= 3 for shard in plan.shards)
    assert sorted(n for s in plan.shards for n in s) == sorted(t.nodeid for t in _untimed(14))


def test_measured_durations_lift_the_small_suite_cap() -> None:
    slow = [ScoredTest(f"t::{i}", float(6 - i), {}, "", 10.0) for i in range(6)]
    plan = plan_shards(slow, CoverSet(), target_shard_seconds=5.0, max_shards=32, median_s=10.0)
    assert len(plan.shards) == 6, "60 measured seconds over a 5 s target: one test per shard"
    half = [*slow[:2], *(ScoredTest(f"u::{i}", 0.0, {}, "", None) for i in range(4))]
    guessed = plan_shards(half, CoverSet(), target_shard_seconds=15.0, max_shards=32, median_s=10.0)
    assert len(guessed.shards) == 6 // 3, "60 s predicted, but only 20 s measured: not above 15 s x 2"
    measured = plan_shards(slow, CoverSet(), target_shard_seconds=15.0, max_shards=32, median_s=10.0)
    assert len(measured.shards) == 4, "60 measured seconds over a 15 s target"


def test_a_forced_shard_count_ignores_the_small_suite_cap() -> None:
    plan = plan_shards(
        _untimed(14), CoverSet(), target_shard_seconds=1e-9, max_shards=8, min_tests_per_shard=1
    )
    assert len(plan.shards) == 8 and all(plan.shards)


def test_the_cap_keeps_wave1_first_and_balanced() -> None:
    scored = _untimed(12)
    wave1 = CoverSet(tests=[scored[11].nodeid, scored[10].nodeid], reasons={})
    plan = plan_shards(scored, wave1, target_shard_seconds=0.01, max_shards=32)
    assert len(plan.shards) == 4
    assert [s[0] for s in plan.shards[:2]] == [scored[11].nodeid, scored[10].nodeid]
    assert max(plan.predicted_s) - min(plan.predicted_s) <= UNKNOWN_DURATION_S + 1e-9


def test_min_tests_per_shard_must_be_positive() -> None:
    with pytest.raises(ValueError, match="min_tests_per_shard"):
        plan_shards(_untimed(3), CoverSet(), target_shard_seconds=1.0, max_shards=4, min_tests_per_shard=0)
