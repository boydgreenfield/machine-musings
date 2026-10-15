"""Tests for ledger.durations.format."""

import pytest

from ledger.durations.format import format_duration, humanize


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (90, "1m30s"), (93_784, "1d2h3m4s")],
)
def test_format_duration(seconds: int, text: str) -> None:
    assert format_duration(seconds) == text


def test_format_duration_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_duration(-1)


@pytest.mark.parametrize(("seconds", "text"), [(7_200, "2 hours"), (60, "1 minute")])
def test_humanize(seconds: int, text: str) -> None:
    assert humanize(seconds) == text
