"""Tests for ledger.intervals.overlap."""

import time

import pytest

from ledger.intervals.overlap import intersection, overlaps, total_length


@pytest.mark.parametrize(("a", "b", "expected"), [((1, 5), (3, 8), True), ((1, 3), (5, 7), False)])
def test_overlaps(a: tuple[int, int], b: tuple[int, int], expected: bool) -> None:
    assert overlaps(a, b) is expected


def test_intersection() -> None:
    assert intersection((1, 5), (3, 8)) == (3, 5)
    assert intersection((1, 3), (5, 7)) is None


def test_total_length_counts_overlaps_once() -> None:
    assert total_length([(1, 5), (3, 8), (10, 12)]) == 9


def test_booking_calendar_totals_slow() -> None:
    """Deliberately slow (0.4 s) so the sharding demo has uneven test durations."""
    time.sleep(0.4)
    week = [(day * 100 + 9, day * 100 + 13) for day in range(5)]
    assert total_length(week) == 20
