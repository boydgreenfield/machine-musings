"""Overlap tests and totals over half-open integer intervals."""

from itertools import pairwise

from ledger.intervals.merge import merge_intervals


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Whether the half-open intervals ``a`` and ``b`` share at least one point."""
    return a[0] < b[1] and b[0] < a[1]


def intersection(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    """Return the overlap of ``a`` and ``b``, or None when they are disjoint."""
    start, end = max(a[0], b[0]), min(a[1], b[1])
    if start >= end:
        return None
    return (start, end)


def total_length(intervals: list[tuple[int, int]]) -> int:
    """Total length covered by ``intervals`` (in start order) once overlaps are merged."""
    return sum(end - start for start, end in merge_intervals(intervals))


def gaps(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return the uncovered stretches between consecutive merged intervals."""
    merged = merge_intervals(intervals)
    return [(left[1], right[0]) for left, right in pairwise(merged)]
