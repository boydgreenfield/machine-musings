"""Plain functions with interesting input spaces."""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterator
from typing import Any

_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def validate_email(email: str) -> str:
    """Return the lower-cased domain of ``email``."""
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        raise ValueError(f"invalid email: {email!r}")
    return domain.lower()


def signup(email: str) -> str:
    """Validate an email on behalf of a signup flow (an in-repo caller of validate_email)."""
    return validate_email(email)


def parse_duration(text: str) -> int:
    """Parse ``1h30m``-style durations into seconds (deliberately naive)."""
    total = 0.0
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", text):
        total += float(number) * _UNIT_SECONDS[unit]
    return int(total)


def total(values: list[float]) -> float:
    """Sum ``values``."""
    return sum(values)


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping closed intervals."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def scale(x: float, factor: float = 2.0) -> float:
    """Multiply ``x`` by ``factor``."""
    return x * factor


def describe(*args: Any, **kwargs: Any) -> str:
    """Summarise a call's arguments."""
    return f"{len(args)} args, {len(kwargs)} kwargs"


def timed(func: Callable[..., Any]) -> Callable[..., Any]:
    """A pass-through decorator."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper


@timed
def double(n: int) -> int:
    """Twice ``n``."""
    return n * 2


def countdown(n: int) -> Iterator[int]:
    """Yield ``n, n-1, ..., 1``."""
    while n > 0:
        yield n
        n -= 1


def hot(i: int) -> int:
    """Called many times with few distinct shapes, so sampling settles and is capped."""
    return i + 1


def login(username: str, password: str) -> bool:
    """Accept any user with a password of at least 8 characters."""
    return bool(username) and len(password) >= 8
