"""A small async token bucket that spaces forge writes under GitHub's secondary rate limit.

GitHub's secondary limit charges 5 points per POST/PATCH against 900 points a minute and caps
content-creating requests per minute; coverset spaces its writes at about 100 a minute with a
small burst, which keeps a check run with hundreds of annotations well inside both.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

DEFAULT_WRITES_PER_MINUTE = 100.0
DEFAULT_BURST = 5
_EPSILON = 1e-9


class TokenBucket:
    """Allow ``burst`` immediate acquisitions, then one every ``60 / per_minute`` seconds."""

    def __init__(
        self,
        per_minute: float = DEFAULT_WRITES_PER_MINUTE,
        burst: int = DEFAULT_BURST,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if per_minute <= 0 or burst < 1:
            raise ValueError("per_minute must be positive and burst at least 1")
        self.interval = 60.0 / per_minute
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) / self.interval)
        self._updated = now

    async def acquire(self) -> float:
        """Take one token, sleeping until one is available; returns the seconds waited."""
        async with self._lock:
            waited = 0.0
            self._refill()
            while self._tokens < 1.0 - _EPSILON:
                delay = (1.0 - self._tokens) * self.interval
                await self._sleep(delay)
                waited += delay
                self._refill()
            self._tokens = max(0.0, self._tokens - 1.0)
            return waited
