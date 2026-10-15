"""Tests for ledger.billing.allocate."""

import pytest

from ledger.billing.allocate import allocate_cents, split_evenly


@pytest.mark.invariant("cents_conserved")
@pytest.mark.parametrize(
    ("total_cents", "weights", "expected"),
    [
        (100, [1, 1, 1], [34, 33, 33]),
        (10, [1, 2, 3], [2, 3, 5]),
        (1_000, [0.5, 0.25, 0.25], [500, 250, 250]),
        (7, [1, 1, 1, 1], [2, 2, 2, 1]),
        (0, [1, 2], [0, 0]),
    ],
)
def test_allocate_cents_examples(total_cents: int, weights: list[float], expected: list[int]) -> None:
    shares = allocate_cents(total_cents, weights)
    assert shares == expected
    assert sum(shares) == total_cents


def test_leftover_cent_goes_to_largest_fraction() -> None:
    assert allocate_cents(5, [1, 3]) == [1, 4]


def test_shares_stay_within_a_cent_of_exact() -> None:
    weights = [2.0, 3.0, 5.0, 7.0]
    shares = allocate_cents(1_234, weights)
    exact = [1_234 * weight / sum(weights) for weight in weights]
    assert all(abs(share - value) < 1 for share, value in zip(shares, exact, strict=True))


@pytest.mark.parametrize(
    ("total_cents", "parts", "expected"),
    [(10, 3, [4, 3, 3]), (5, 1, [5])],
)
def test_split_evenly(total_cents: int, parts: int, expected: list[int]) -> None:
    assert split_evenly(total_cents, parts) == expected
