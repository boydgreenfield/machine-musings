"""Tests for ledger.billing.refund."""

import pytest

from ledger.billing.refund import refund


@pytest.mark.invariant("cents_conserved")
def test_refund_partial() -> None:
    assert sum(refund(-100, [1, 2])) == -100


def test_refund_splits_evenly_divisible_amounts() -> None:
    assert refund(-90, [1, 1, 1]) == [-30, -30, -30]


def test_refund_rejects_positive_total() -> None:
    with pytest.raises(ValueError, match="negative"):
        refund(100, [1, 2])
