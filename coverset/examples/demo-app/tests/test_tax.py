"""Tests for ledger.billing.tax."""

import pytest

from ledger.billing.tax import is_tax_exempt, rate_for, tax_cents


def test_rate_for_known_region() -> None:
    assert rate_for("GB") == 2_000


def test_rate_for_unknown_region() -> None:
    with pytest.raises(ValueError, match="no tax rate"):
        rate_for("FR")


def test_tax_cents_in_california() -> None:
    assert tax_cents(10_000, "US-CA") == 725


def test_oregon_is_tax_exempt() -> None:
    assert is_tax_exempt("US-OR")
    assert not is_tax_exempt("DE")
