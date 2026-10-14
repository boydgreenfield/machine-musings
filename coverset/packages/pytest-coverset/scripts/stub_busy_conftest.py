"""Appended to the synthetic bench suite's ``tests/conftest.py`` by ``measure_overhead.py
--conftest-append``: ``busy(ms)`` then burns one loop round instead of ``ms`` milliseconds, so the
1,200 tests take almost no time and what remains is per-test overhead (pytest's and coverset's)."""

import benchsuite.work as _coverset_bench_work

_coverset_bench_work.ROUNDS_PER_MS = 0.0
