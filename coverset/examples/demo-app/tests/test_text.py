"""Tests for ledger.util.text."""

import pytest

from ledger.util.text import normalize_whitespace, slugify, truncate


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Acme Corp.", "acme-corp"), ("  Café  Olé ", "cafe-ole")],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_normalize_whitespace() -> None:
    assert normalize_whitespace("  net\t30\n days ") == "net 30 days"


def test_truncate_leaves_short_text_alone() -> None:
    assert truncate("Invoice", 10) == "Invoice"


def test_truncate_cuts_long_text() -> None:
    assert truncate("Quarterly invoice summary", 12) == "Quarterly..."
