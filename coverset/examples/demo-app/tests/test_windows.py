"""Tests for ledger.durations.windows."""

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from ledger.durations.windows import expires_at, is_expired, remaining_seconds


@pytest.fixture
def issued_at() -> datetime:
    """A fixed issue time for invitation tokens."""
    return datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def test_expires_at(issued_at: datetime) -> None:
    assert expires_at(issued_at, "2d") == datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(("elapsed", "expired"), [(timedelta(minutes=14), False), (timedelta(hours=1), True)])
def test_is_expired(issued_at: datetime, elapsed: timedelta, expired: bool) -> None:
    assert is_expired(issued_at, "15m", issued_at + elapsed) is expired


def test_remaining_seconds_is_zero_after_expiry(issued_at: datetime) -> None:
    assert remaining_seconds(issued_at, "1h", issued_at + timedelta(hours=2)) == 0


def test_remaining_seconds_rounds_down_partial_seconds_slow() -> None:
    """Deliberately slow (0.5 s) so the sharding demo has uneven test durations."""
    issued = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    time.sleep(0.5)
    assert remaining_seconds(issued, "1m", issued + timedelta(seconds=0.5)) == 59


def test_hold_survives_clock_skew(issued_at: datetime) -> None:
    """Deliberately flaky: fails when COVERSET_DEMO_RUN % 3 == 1, so flake detection has a signal.

    Each demo run sets COVERSET_DEMO_RUN; on every third one the simulated payment
    processor clock runs two seconds fast and the hold looks expired a second early. The
    demo runner retries a failure with the next run number, so the retry passes and the
    failure is recorded as flaky (see tests/README.md).
    """
    skew = 2 if int(os.environ.get("COVERSET_DEMO_RUN", "0")) % 3 == 1 else 0
    processor_now = issued_at + timedelta(minutes=15, seconds=skew - 1)
    assert not is_expired(issued_at, "15m", processor_now)
