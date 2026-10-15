"""Tests for ledger.durations.parse."""

import pytest

from ledger.durations.parse import parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90s", 90),
        ("15m", 900),
        ("1h30m", 5_400),
        ("1d2h3m4s", 93_784),
        ("0s", 0),
    ],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["never", "h", ""])
def test_parse_duration_rejects_text_without_a_token(text: str) -> None:
    with pytest.raises(ValueError, match="not a duration"):
        parse_duration(text)
