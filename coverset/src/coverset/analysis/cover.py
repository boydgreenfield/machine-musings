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

import statistics
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from coverset.analysis.functions import FunctionChange
from coverset.analysis.impact import ImpactIndex
from coverset.lang.base import CollectedItem, PriorRun

__all__ = ["CoverSet", "change_covering_set"]

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
