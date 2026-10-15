"""Tests for ledger.intervals.merge."""

import pytest

from ledger.intervals.merge import merge_intervals


@pytest.mark.parametrize(
    ("intervals", "merged"),
    [
        ([(1, 3), (5, 7)], [(1, 3), (5, 7)]),
        ([(1, 5), (3, 8)], [(1, 8)]),
        ([(1, 4), (2, 6), (5, 9)], [(1, 9)]),
        ([(1, 3), (2, 4), (10, 12)], [(1, 4), (10, 12)]),
        ([(-5, -1), (-3, 2)], [(-5, 2)]),
    ],
)
def test_merge_intervals(intervals: list[tuple[int, int]], merged: list[tuple[int, int]]) -> None:
    assert merge_intervals(intervals) == merged


def test_merge_intervals_leaves_input_unchanged() -> None:
    intervals = [(9, 12), (10, 15)]
    merge_intervals(intervals)
    assert intervals == [(9, 12), (10, 15)]
