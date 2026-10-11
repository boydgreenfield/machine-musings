"""Sharding: never plan an empty shard (an empty args file makes pytest run the whole suite)."""

from __future__ import annotations

from coverset.analysis.cover import CoverSet
from coverset.analysis.scoring import ScoredTest
from coverset.analysis.sharding import plan_shards, reshard


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
