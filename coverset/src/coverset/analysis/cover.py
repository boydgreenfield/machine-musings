"""Wave 1: the change-covering set every shard runs first.

A greedy weighted set cover picks, until every coverable changed function body is covered, the test
with the most newly covered bodies per second of p50 duration (unknown durations count as the
median). Every test in a changed file and every test without coverage history in the prior run
joins as well. Ties break by nodeid, so the result is deterministic.

Without a prior run (the first PR of a repo, before any baseline) there is no history at all:
wave 1 is then only the tests in changed files, and score order decides everything else, rather
than every test joining wave 1 as "no coverage history".
"""

from __future__ import annotations

import heapq
import statistics
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from coverset.analysis.functions import FunctionChange
from coverset.analysis.impact import ImpactIndex
from coverset.lang.base import CollectedItem, PriorRun

__all__ = [
    "CoverSet",
    "change_covering_set",
    "greedy_cover_csr",
    "greedy_weighted_cover",
    "prune_csr",
    "prune_redundant",
]

_MIN_WEIGHT_S: Final = 1e-3


@dataclass(frozen=True, slots=True)
class CoverSet:
    """Wave 1 in run order, with a one-line reason per test."""

    tests: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)
    """Qualnames of changed prior regions that no collected test touched."""


def change_covering_set(
    items: Sequence[CollectedItem],
    changed: Sequence[FunctionChange],
    prior: PriorRun | None,
    durations: Mapping[str, float],
    *,
    changed_files: Collection[str] = (),
) -> CoverSet:
    """Return wave 1 for ``changed`` given the prior run's per-test coverage.

    ``durations`` holds p50 seconds by nodeid. ``changed_files`` (head paths) adds the tests
    defined in those files; the paths of ``changed`` regions are always included.
    """
    collected = {item.nodeid for item in items}
    impact = ImpactIndex.build(prior, changed, ())
    known = [d for d in durations.values() if d > 0]
    median = statistics.median(known) if known else 1.0

    def weight(nodeid: str) -> float:
        return max(durations.get(nodeid, median), _MIN_WEIGHT_S)

    universe = {key: tests & collected for key, tests in impact.tests_by_region.items()}
    uncovered = sorted(impact.regions[k].qualname for k, tests in universe.items() if not tests)
    remaining = {key for key, tests in universe.items() if tests}
    covers: dict[str, set[str]] = {}
    for key, tests in universe.items():
        for nodeid in tests:
            covers.setdefault(nodeid, set()).add(key)
    tests: list[str] = []
    reasons: dict[str, str] = {}
    while remaining:
        best = min(
            (n for n, keys in covers.items() if keys & remaining),
            key=lambda n: (-len(covers[n] & remaining) / weight(n), n),
        )
        gained = sorted(impact.regions[k].qualname for k in covers[best] & remaining)
        tests.append(best)
        reasons[best] = f"covers {', '.join(gained)} (changed)"
        remaining -= covers[best]

    files = set(changed_files)
    for change in changed:
        files.update(r.path for r in (change.region, change.prev) if r is not None)
    for item in sorted(items, key=lambda i: i.nodeid):
        if item.nodeid not in reasons and item.file in files:
            tests.append(item.nodeid)
            reasons[item.nodeid] = f"in changed file {item.file}"
    if prior is None:
        return CoverSet(tests, reasons, uncovered)
    history = prior.durations()
    for item in sorted(items, key=lambda i: i.nodeid):
        if item.nodeid not in reasons and item.nodeid not in history:
            tests.append(item.nodeid)
            reasons[item.nodeid] = "no coverage history"
    return CoverSet(tests, reasons, uncovered)


def greedy_weighted_cover[K: str, F](sets: Mapping[K, Collection[F]], weights: Mapping[K, float]) -> list[K]:
    """A weighted set cover of every element of ``sets``: the greedy pick order.

    The same rule as wave 1 (most newly covered elements per unit of weight, ties by name), over
    arbitrary keys and elements, so the test-economy analysis (``analysis/economy.py``) can ask for
    a minimal covering set of a whole run. Weights below 1 ms count as 1 ms; a key missing from
    ``weights`` weighs the median. Evaluation is lazy: a key's gain only shrinks as elements get
    covered, so a stale heap entry is re-scored only when it reaches the top, which keeps a
    1,000-test run with 100,000 facts well under a second. Keys whose set is empty are never picked.
    """
    known = [w for w in weights.values() if w > 0]
    median = statistics.median(known) if known else 1.0

    def weight(key: K) -> float:
        return max(weights.get(key, median), _MIN_WEIGHT_S)

    members = {key: frozenset(elements) for key, elements in sets.items() if elements}
    remaining: set[F] = set().union(*members.values()) if members else set()
    heap = [(-len(elems) / weight(key), key) for key, elems in members.items()]
    heapq.heapify(heap)
    chosen: list[K] = []
    while remaining and heap:
        _, key = heapq.heappop(heap)
        gain = len(members[key] & remaining)
        if gain == 0:
            continue
        entry = (-gain / weight(key), key)
        if heap and entry > heap[0]:
            heapq.heappush(heap, entry)  # stale score: re-queue it and let a better key go first
            continue
        chosen.append(key)
        remaining -= members[key]
    return chosen


def prune_redundant[K: str, F](
    chosen: Sequence[K], sets: Mapping[K, Collection[F]], weights: Mapping[K, float]
) -> list[K]:
    """``chosen`` minus every key whose elements the other chosen keys still cover.

    Greedy cover can pick a key early that later picks make redundant; dropping the heaviest such
    keys first (ties by name) keeps the union intact and makes the set irredundant (no single key
    can be removed). The order of the survivors is the order of ``chosen``.
    """
    holders: Counter[F] = Counter()
    for key in chosen:
        holders.update(set(sets[key]))
    dropped: set[K] = set()
    for key in sorted(chosen, key=lambda k: (-weights.get(k, 0.0), k)):
        elements = set(sets[key])
        if all(holders[e] >= 2 for e in elements):
            holders.subtract(elements)
            dropped.add(key)
    return [key for key in chosen if key not in dropped]


def greedy_cover_csr(offsets: np.ndarray, elements: np.ndarray, weights: np.ndarray) -> list[int]:
    """:func:`greedy_weighted_cover` over a CSR matrix of sets (numpy, for whole-run sizes).

    Set ``i`` is ``elements[offsets[i]:offsets[i + 1]]``: sorted, unique, non-negative ints.
    ``weights[i]`` is its cost (floored at 1 ms like wave 1). Ties break by the lower index, so
    callers that number their sets in name order get the same picks as the set-based version.
    Coverage is a boolean mask over the elements, so re-scoring a set costs one gather.
    """
    n_sets = len(offsets) - 1
    size = int(elements.max()) + 1 if elements.size else 0
    covered = np.zeros(size, dtype=bool)
    remaining = int(np.unique(elements).size) if elements.size else 0
    w = np.maximum(np.asarray(weights, dtype=np.float64), _MIN_WEIGHT_S)
    lengths = np.diff(offsets)
    heap = [(-int(lengths[i]) / float(w[i]), i) for i in range(n_sets) if lengths[i] > 0]
    heapq.heapify(heap)
    chosen: list[int] = []
    while remaining and heap:
        _, i = heapq.heappop(heap)
        segment = elements[offsets[i] : offsets[i + 1]]
        gain = int(segment.size - np.count_nonzero(covered[segment]))
        if gain == 0:
            continue
        entry = (-gain / float(w[i]), i)
        if heap and entry > heap[0]:
            heapq.heappush(heap, entry)
            continue
        chosen.append(i)
        covered[segment] = True
        remaining -= gain
    return chosen


def prune_csr(
    chosen: Sequence[int], offsets: np.ndarray, elements: np.ndarray, weights: np.ndarray
) -> list[int]:
    """:func:`prune_redundant` over the CSR sets of :func:`greedy_cover_csr` (heaviest dropped first)."""
    size = int(elements.max()) + 1 if elements.size else 0
    holders = np.zeros(size, dtype=np.int32)
    for i in chosen:
        holders[elements[offsets[i] : offsets[i + 1]]] += 1
    dropped: set[int] = set()
    for i in sorted(chosen, key=lambda k: (-float(weights[k]), k)):
        segment = elements[offsets[i] : offsets[i + 1]]
        if segment.size and bool(np.all(holders[segment] >= 2)):
            holders[segment] -= 1
            dropped.add(i)
    return [i for i in chosen if i not in dropped]
