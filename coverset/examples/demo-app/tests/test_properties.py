"""Hypothesis property tests for the rules declared in invariants.yaml."""

from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledger.billing.allocate import allocate_cents
from ledger.durations.format import format_duration
from ledger.durations.parse import parse_duration
from ledger.intervals.merge import merge_intervals

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}

totals = st.integers(min_value=0, max_value=10**7)
weight_lists = st.lists(st.integers(min_value=1, max_value=1_000), min_size=1, max_size=8)
tokens = st.lists(
    st.tuples(st.integers(min_value=0, max_value=999), st.sampled_from(sorted(UNIT_SECONDS))),
    min_size=1,
    max_size=4,
)


@st.composite
def booking_slots(draw: st.DrawFn) -> list[tuple[int, int]]:
    """Slots in start order where each one either overlaps the end of the previous slot or leaves a gap."""
    start = draw(st.integers(min_value=-1_000, max_value=1_000))
    slots = [(start, start + draw(st.integers(min_value=2, max_value=50)))]
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        prev_start, prev_end = slots[-1]
        if draw(st.booleans()):
            start = prev_end - draw(st.integers(min_value=1, max_value=prev_end - prev_start - 1))
        else:
            start = prev_end + draw(st.integers(min_value=1, max_value=20))
        slots.append((start, max(start, prev_end) + draw(st.integers(min_value=2, max_value=50))))
    return slots


@pytest.mark.invariant("cents_conserved")
@given(total_cents=totals, weights=weight_lists)
def test_allocation_conserves_cents(total_cents: int, weights: list[int]) -> None:
    assert sum(allocate_cents(total_cents, weights)) == total_cents


@given(total_cents=totals, weights=weight_lists)
def test_allocation_shares_are_within_a_cent_of_exact(total_cents: int, weights: list[int]) -> None:
    shares = allocate_cents(total_cents, weights)
    exact = [total_cents * weight / sum(weights) for weight in weights]
    assert all(abs(share - value) < 1 for share, value in zip(shares, exact, strict=True))


@pytest.mark.invariant("durations_consume_whole_input")
@given(seconds=st.integers(min_value=0, max_value=10**8))
def test_formatted_durations_parse_back(seconds: int) -> None:
    assert parse_duration(format_duration(seconds)) == seconds


@pytest.mark.invariant("durations_consume_whole_input")
@given(parts=tokens)
def test_every_token_counts(parts: list[tuple[int, str]]) -> None:
    text = "".join(f"{amount}{unit}" for amount, unit in parts)
    assert parse_duration(text) == sum(amount * UNIT_SECONDS[unit] for amount, unit in parts)


@pytest.mark.invariant("merge_idempotent")
@given(slots=booking_slots())
def test_merging_twice_changes_nothing(slots: list[tuple[int, int]]) -> None:
    merged = merge_intervals(slots)
    assert merge_intervals(merged) == merged


@given(slots=booking_slots())
def test_merged_slots_are_disjoint_and_ordered(slots: list[tuple[int, int]]) -> None:
    merged = merge_intervals(slots)
    assert all(left[1] < right[0] for left, right in pairwise(merged))
