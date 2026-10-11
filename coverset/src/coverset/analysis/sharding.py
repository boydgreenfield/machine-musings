"""Priority-snake LPT sharding with wave 1 at the head of every shard.

``K = clamp(ceil(Σ p50 / target_shard_seconds), 1, max_shards)`` (and never more shards than
assignment units, so no shard is planned empty: a fixture group of many tests is one unit). Wave-1 tests are dealt first, one by one in wave-1 order, each to the least-loaded shard,
so every shard starts with part of the change-covering set. The remaining tests follow in priority
order (the order :func:`~coverset.analysis.scoring.score_tests` returned), each to the
least-loaded shard; members of a fixture group that fall in the same priority decile move together
so module- and class-scoped fixtures still set up once. Every shard's list keeps assignment order,
so it stays in priority order with its wave-1 tests first. Unknown durations count as the median.

pytest-coverset re-sorts each shard by the ``--coverset-scores`` file, so the runner must write
:func:`shard_scores` (rank-derived) rather than the raw :class:`ScoredTest` scores: those know
nothing of wave 1 or of the exploration sample, and the plan's order would be lost. The plugin
still keeps a module's (and a class's) tests together behind their best-ranked member, so a
low-ranked test can move forward with its group.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field

from coverset.analysis.cover import CoverSet
from coverset.analysis.scoring import ScoredTest

__all__ = ["ShardPlan", "ShardUnit", "plan_shards", "reshard", "shard_scores"]

type GroupKey = Hashable


@dataclass(frozen=True, slots=True)
class ShardUnit:
    """Tests that are assigned to one shard together, with their predicted total duration."""

    nodeids: tuple[str, ...]
    seconds: float


@dataclass(frozen=True, slots=True)
class ShardPlan:
    """Per-shard nodeids in run order and each shard's predicted duration in seconds."""

    shards: list[list[str]]
    predicted_s: list[float]
    units: tuple[ShardUnit, ...] = field(default=(), repr=False)
    """The assignment units in dealing order, kept so :func:`reshard` can re-deal them."""


def plan_shards(
    scored: Sequence[ScoredTest],
    wave1: CoverSet,
    *,
    target_shard_seconds: float,
    max_shards: int,
    median_s: float,
    groups: Mapping[str, GroupKey] | None = None,
) -> ShardPlan:
    """Split ``scored`` (in priority order) into shards; see the module docstring."""
    if target_shard_seconds <= 0 or max_shards < 1:
        raise ValueError("target_shard_seconds must be > 0 and max_shards >= 1")
    durations = {t.nodeid: (t.p50_s if t.p50_s is not None else median_s) for t in scored}
    first = list(dict.fromkeys(wave1.tests))
    units = [ShardUnit((n,), durations.get(n, median_s)) for n in first]
    wave1_set = set(first)
    rest = [t.nodeid for t in scored if t.nodeid not in wave1_set]
    units += _group_units(rest, durations, groups or {})
    total = sum(u.seconds for u in units)
    k = min(max(math.ceil(total / target_shard_seconds), 1), max_shards, max(len(units), 1))
    return _deal(units, k)


def reshard(plan: ShardPlan, n_workers: int) -> ShardPlan:
    """Re-deal ``plan`` over ``n_workers`` shards when fewer workers were obtained than planned.

    Raises ValueError for ``n_workers < 1`` (the run fails with reason "capacity").
    """
    if n_workers < 1:
        raise ValueError("no workers available (capacity)")
    if n_workers >= len(plan.shards):
        return plan
    return _deal(list(plan.units), min(n_workers, max(len(plan.units), 1)))


def shard_scores(plan: ShardPlan) -> list[dict[str, float]]:
    """Per-shard ``{nodeid: score}`` for ``--coverset-scores`` that reproduces the plan's order.

    The score is ``len(shard) - index``: the first test of a shard scores highest.
    """
    return [
        {nodeid: float(len(shard) - index) for index, nodeid in enumerate(shard)} for shard in plan.shards
    ]


def _group_units(
    ordered: Sequence[str], durations: Mapping[str, float], groups: Mapping[str, GroupKey]
) -> list[ShardUnit]:
    """Bundle same-group tests that share a priority decile into one unit, at the first member."""
    n = len(ordered)
    members: dict[tuple[GroupKey, int], list[str]] = {}
    unit_order: list[tuple[GroupKey, int] | str] = []
    for rank, nodeid in enumerate(ordered):
        group = groups.get(nodeid)
        if group is None:
            unit_order.append(nodeid)
            continue
        key = (group, rank * 10 // n)
        if key not in members:
            members[key] = []
            unit_order.append(key)
        members[key].append(nodeid)
    units: list[ShardUnit] = []
    for entry in unit_order:
        nodeids = (entry,) if isinstance(entry, str) else tuple(members[entry])
        units.append(ShardUnit(nodeids, sum(durations[n] for n in nodeids)))
    return units


def _deal(units: Sequence[ShardUnit], k: int) -> ShardPlan:
    """Give each unit, in order, to the least-loaded shard (lowest index on ties).

    A shard can still end up empty when units have zero predicted duration (callers that need one
    worker per shard drop empty shards).
    """
    shards: list[list[str]] = [[] for _ in range(k)]
    loads = [0.0] * k
    heap = [(0.0, i) for i in range(k)]
    for unit in units:
        load, index = heapq.heappop(heap)
        shards[index].extend(unit.nodeids)
        loads[index] = load + unit.seconds
        heapq.heappush(heap, (loads[index], index))
    return ShardPlan(shards, [round(x, 6) for x in loads], tuple(units))
