"""Expiry windows for tokens, invites and payment holds, configured as duration strings."""

from datetime import datetime, timedelta

from ledger.durations.parse import parse_duration


def expires_at(issued_at: datetime, ttl: str) -> datetime:
    """Return when something issued at ``issued_at`` with time-to-live ``ttl`` expires."""
    return issued_at + timedelta(seconds=parse_duration(ttl))


def is_expired(issued_at: datetime, ttl: str, now: datetime) -> bool:
    """Whether the window opened at ``issued_at`` has closed by ``now``."""
    return now >= expires_at(issued_at, ttl)


def remaining_seconds(issued_at: datetime, ttl: str, now: datetime) -> int:
    """Whole seconds left in the window at ``now``; zero once it has expired."""
    left = (expires_at(issued_at, ttl) - now).total_seconds()
    return max(0, int(left))
