"""Tests for ledger.util.money."""

import pytest

from ledger.util.money import format_cents, parse_amount, percent_of


@pytest.mark.parametrize(
    ("cents", "currency", "expected"),
    [
        (123_450, "USD", "$1,234.50"),
        (-5, "USD", "-$0.05"),
        (1_000, "CHF", "CHF 10.00"),
    ],
)
def test_format_cents(cents: int, currency: str, expected: str) -> None:
    assert format_cents(cents, currency) == expected


def test_parse_amount_with_thousands_separator() -> None:
    assert parse_amount(" 1,234.56 ") == 123_456


def test_parse_amount_rejects_text() -> None:
    with pytest.raises(ValueError, match="not an amount"):
        parse_amount("twelve")


def test_percent_of_rounds_half_to_even() -> None:
    assert percent_of(250, 200) == 5
    assert percent_of(25, 200) == 0
