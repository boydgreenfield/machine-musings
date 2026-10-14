"""Per-process caches over the immutable facts store: run stamps, leased DuckDB connections and
memoised query results.

Everything the store holds is immutable once visible: a run's files only change by an atomic
replace (a new inode under the same name), a new attempt's files, a new ``ingest_log`` row (the
commit) or the deletion of superseded attempts. So the *stamp* of a run, the sorted
``(name, inode, size, mtime_ns)`` of its ``*.parquet`` entries, changes whenever anything a reader
could see changes, and two equal stamps mean the same bytes. Every cache here is keyed by the
stamps of the runs it read, recomputed on every lookup (one ``scandir`` and one ``stat`` per file,
about 0.1 ms for a run of 40 files), so there is no invalidation message to miss: an ingest, a
finalize or a gc simply makes the next lookup miss. The inode is in the stamp because a replace
within one tick of a coarse filesystem clock keeps the mtime; size alone could collide.

:class:`ConnectionCache` keeps one in-memory DuckDB database per *scope* (a repo, its runs and the
tables a query needs) with the views :meth:`~coverset.facts.store.FactsStore.connect` creates, and
hands out cursors of it (:meth:`FactsStore.lease <coverset.facts.store.FactsStore.lease>`). A
cursor is its own connection to the same database: it sees the views, it cannot change the locked
configuration, and an Arrow table it registers is visible to it alone. Building a scope's
connection costs 5 to 10 ms (it reads every file's Parquet footer for ``union_by_name``); a lease of
a cached one costs microseconds. Each database has its own ``memory_limit`` and thread count, and
there is one database per scope, so a user query on one run can never name another run's views.
An evicted or superseded database is never closed explicitly (closing a DuckDB connection closes
its cursors, which another thread may be using); it is dropped and freed when its last cursor is.

:class:`ResultCache` memoises the results of run-scoped queries (the treemap, the overview's
gaps and changes, a function's history) under the same stamps.

Sizes come from the environment because the facts layer has no settings object of its own:
``COVERSET_FACTS_CACHE_ENTRIES`` (cached databases, default 16), ``COVERSET_FACTS_MEMORY_LIMIT``
(DuckDB ``memory_limit`` of each, default ``512MB``), ``COVERSET_FACTS_THREADS`` (DuckDB threads of
each, default ``min(4, CPUs)``) and ``COVERSET_FACTS_RESULT_ENTRIES`` (memoised results, default
256).
"""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

__all__ = [
    "CONNECTIONS",
    "RESULTS",
    "CacheStats",
    "ConnectionCache",
    "ResultCache",
    "RunStamp",
    "memory_limit",
    "run_stamp",
    "scope_key",
    "threads",
]

_SIZE = re.compile(r"\d{1,6}(\.\d{1,3})?\s?(KB|MB|GB|TB|KiB|MiB|GiB|TiB)", re.IGNORECASE)

type RunStamp = tuple[tuple[str, int, int, int], ...]
"""``(name, inode, size, mtime_ns)`` of every visible-candidate ``*.parquet`` file of a run, sorted."""


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "")
    try:
        return max(minimum, int(raw)) if raw.strip() else default
    except ValueError:
        return default


def memory_limit() -> str:
    """DuckDB ``memory_limit`` of every facts connection (``COVERSET_FACTS_MEMORY_LIMIT``)."""
    value = os.environ.get("COVERSET_FACTS_MEMORY_LIMIT", "").strip()
    # Only a plain size ("512MB", "2GB", "1.5GiB"): the value is interpolated into a SET.
    return value if _SIZE.fullmatch(value) else "512MB"


def threads() -> int:
    """DuckDB worker threads of every facts connection (``COVERSET_FACTS_THREADS``)."""
    return _env_int("COVERSET_FACTS_THREADS", min(4, os.cpu_count() or 1))


def run_stamp(run_dir: Path) -> RunStamp:
    """The stamp of one run directory (empty when it does not exist).

    Hidden files (``.<name>.<uuid>.tmp`` of a write in progress, ``.lock``) are not part of it:
    they are never visible to readers.
    """
    entries: list[tuple[str, int, int, int]] = []
    try:
        with os.scandir(run_dir) as found:
            for entry in found:
                name = entry.name
                if name.startswith(".") or not name.endswith(".parquet"):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:  # superseded and deleted while listing
                    continue
                entries.append((name, st.st_ino, st.st_size, st.st_mtime_ns))
    except (FileNotFoundError, NotADirectoryError):
        return ()
    entries.sort()
    return tuple(entries)


@dataclass(slots=True)
class CacheStats:
    """Counters of one cache since the process started (or :meth:`ConnectionCache.clear`)."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    """Lookups that found the key under an older stamp (the run changed since)."""
    evictions: int = 0


class ConnectionCache:
    """A bounded LRU of DuckDB connections, one per scope, valid while the scope's stamp holds."""

    def __init__(self, max_entries: int | None = None) -> None:
        self._max = max_entries
        self._entries: OrderedDict[Hashable, tuple[Hashable, duckdb.DuckDBPyConnection]] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = CacheStats()

    @property
    def max_entries(self) -> int:
        """The bound (``COVERSET_FACTS_CACHE_ENTRIES`` unless set at construction)."""
        return self._max if self._max is not None else _env_int("COVERSET_FACTS_CACHE_ENTRIES", 16)

    def __len__(self) -> int:
        return len(self._entries)

    def lease(
        self, key: Hashable, stamp: Hashable, build: Callable[[], duckdb.DuckDBPyConnection]
    ) -> duckdb.DuckDBPyConnection:
        """A new cursor of the connection cached for ``key`` at ``stamp`` (built on a miss).

        The caller closes the cursor; the cached connection stays. Two threads missing the same
        key may both build; the second keeps the first one's connection and drops its own.
        """
        with self._lock:
            found = self._entries.get(key)
            if found is not None and found[0] == stamp:
                self._entries.move_to_end(key)
                self.stats.hits += 1
                return found[1].cursor()
            if found is not None:
                # The scope changed (an ingest, a finalize, a gc): forget the old database now so
                # its memory goes as soon as its last cursor closes.
                del self._entries[key]
                self.stats.invalidations += 1
            self.stats.misses += 1
        con = build()
        with self._lock:
            raced = self._entries.get(key)
            if raced is not None and raced[0] == stamp:
                self._entries.move_to_end(key)
                return raced[1].cursor()
            self._entries[key] = (stamp, con)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.stats.evictions += 1
            return con.cursor()

    def clear(self) -> None:
        """Forget every cached connection and reset the counters (tests, ``coverset gc``)."""
        with self._lock:
            self._entries.clear()
            self.stats = CacheStats()


class ResultCache:
    """A bounded LRU of query results keyed by ``(key, stamp)``.

    Results are returned as stored: callers must treat them as read-only (the queries return
    frozen dataclasses; list results are copied on the way out by :meth:`get_or_compute`).
    """

    def __init__(self, max_entries: int | None = None) -> None:
        self._max = max_entries
        self._entries: OrderedDict[Hashable, tuple[Hashable, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = CacheStats()

    @property
    def max_entries(self) -> int:
        """The bound (``COVERSET_FACTS_RESULT_ENTRIES`` unless set at construction)."""
        return self._max if self._max is not None else _env_int("COVERSET_FACTS_RESULT_ENTRIES", 256)

    def __len__(self) -> int:
        return len(self._entries)

    def get_or_compute[T](self, key: Hashable, stamp: Hashable, compute: Callable[[], T]) -> T:
        """The cached result of ``compute`` for ``key`` at ``stamp`` (computed on a miss); a list
        result is returned as a new list so a caller's append or sort never reaches the cache."""
        with self._lock:
            found = self._entries.get(key)
            if found is not None and found[0] == stamp:
                self._entries.move_to_end(key)
                self.stats.hits += 1
                value = found[1]
                return list(value) if isinstance(value, list) else value  # type: ignore[return-value]
            if found is not None:
                del self._entries[key]
                self.stats.invalidations += 1
            self.stats.misses += 1
        value = compute()
        with self._lock:
            self._entries[key] = (stamp, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.stats.evictions += 1
        return list(value) if isinstance(value, list) else value  # type: ignore[return-value]

    def clear(self) -> None:
        """Forget every result and reset the counters."""
        with self._lock:
            self._entries.clear()
            self.stats = CacheStats()


def scope_key(root: Path, scope: Sequence[tuple[int, str]], extra: Hashable = ()) -> Hashable:
    """A hashable key of a store root, a scope and anything else that selects the cached object."""
    return (str(root), tuple(scope), extra)


CONNECTIONS = ConnectionCache()
"""The process's cache of facts connections (used by :meth:`FactsStore.lease`)."""

RESULTS = ResultCache()
"""The process's cache of memoised run-scoped query results (used by :mod:`coverset.facts.queries`)."""
