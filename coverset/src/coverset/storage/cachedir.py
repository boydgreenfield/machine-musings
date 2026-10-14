"""The bounded local cache in front of the object store: what it holds, and least-recently-used eviction.

With ``COVERSET_BLOB_URL`` set, ``COVERSET_CACHE_DIR`` (default ``<data_dir>/cache``) holds copies
that can always be fetched again:

``facts/repo=<id>/run=<id>/``
    a run's files as last pulled or published (``FactsStore``); used at each pull (the hidden
    ``.sync.json`` is touched), so its mtime is the run's last use;
``mirrors/<repo_id>.git``
    bare git mirrors; a missing one costs one ``git clone`` at the repository's next run
    (``orchestrator/repos.ensure_mirror`` clones when there is no mirror); last use is its newest
    fetch;
``sources/<repo_id>/<sha>/``
    uploaded sources fetched for the source view or an upload pass.

:func:`evict` keeps the total under ``COVERSET_CACHE_MAX_GB`` (:func:`coverset.facts.cache.cache_max_bytes`,
10 GB by default) by removing whole entries, least recently used first. It never removes an entry in
use: a run whose lock is held (an ingest or a pull is writing it) or that holds a change not yet
published (no ``.sync.json``, or files that differ from it), or a mirror whose fetch lock is held.
``coverset gc`` (and so ``serve`` at start-up and every 24 h, ``orchestrator/history.collect_garbage``)
calls it; ``coverset cache status|clear`` shows and empties the cache by hand. A removed run is
pulled again on its next read (a reader's freshness check sees its ``.sync.json`` gone). Repository
dictionaries are tiny and never evicted.

With the local default nothing under the data directory is a cache: :func:`evict` and
:func:`clear` do nothing and :func:`status` says so.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = ["CacheEntry", "CacheStatus", "EvictReport", "clear", "entries", "evict", "evict_dir", "status"]

Kind = Literal["run", "mirror", "sources"]


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One evictable unit of the cache."""

    kind: Kind
    path: Path
    bytes: int
    last_used: float
    """Seconds since the epoch."""
    busy: bool
    """In use (locked, or holding unpublished changes): never evicted."""


@dataclass(slots=True)
class CacheStatus:
    """What ``coverset cache status`` prints."""

    directory: Path
    remote: bool
    max_bytes: int
    total_bytes: int = 0
    by_kind: dict[str, tuple[int, int]] = field(default_factory=dict)
    """``kind -> (entries, bytes)``."""
    busy: int = 0


@dataclass(slots=True)
class EvictReport:
    """What :func:`evict` removed (or would remove, with ``dry_run``)."""

    removed: list[CacheEntry] = field(default_factory=list)
    freed_bytes: int = 0
    total_bytes: int = 0
    max_bytes: int = 0
    skipped_busy: int = 0

    def line(self) -> str:
        """One line for the gc log and the CLI."""
        if not self.removed:
            return f"cache: {_gb(self.total_bytes)} of {_gb(self.max_bytes)}, nothing evicted"
        kinds: dict[str, int] = {}
        for entry in self.removed:
            kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
        what = ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in sorted(kinds.items()))
        return f"cache: evicted {what} ({_gb(self.freed_bytes)}); {_gb(self.total_bytes - self.freed_bytes)} left"


def _gb(value: int) -> str:
    return f"{value / 1e9:.2f} GB"


def _tree_bytes(path: Path) -> int:
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except FileNotFoundError:
                continue
    return total


def _try_lock(path: Path) -> int | None:
    """An fd holding a non-blocking exclusive flock on ``path``, or None when someone holds it."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _unpublished(run_dir: Path) -> bool:
    """True when the cached run has changes its last publish does not cover (or none at all)."""
    try:
        record = json.loads((run_dir / ".sync.json").read_bytes())
    except (FileNotFoundError, ValueError):
        return any(name.endswith(".parquet") and not name.startswith(".") for name in os.listdir(run_dir))
    known = record.get("files") or {}
    present = set()
    for entry in os.scandir(run_dir):
        if entry.name.startswith(".") or not entry.name.endswith(".parquet"):
            continue
        present.add(entry.name)
        st = entry.stat(follow_symlinks=False)
        if list(known.get(entry.name, [])[:3]) != [st.st_size, st.st_mtime_ns, st.st_ino]:
            return True
    return any(name not in present for name in known)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _run_entries(facts_dir: Path) -> Iterator[CacheEntry]:
    if not facts_dir.is_dir():
        return
    for repo in os.scandir(facts_dir):
        if not repo.name.startswith("repo=") or not repo.is_dir(follow_symlinks=False):
            continue
        for run in os.scandir(repo.path):
            if not run.name.startswith("run=") or not run.is_dir(follow_symlinks=False):
                continue
            path = Path(run.path)
            record = path / ".sync.json"
            try:
                last = record.stat().st_mtime
            except FileNotFoundError:
                last = path.stat().st_mtime
            fd = _try_lock(path / ".lock")
            busy = fd is None
            if fd is not None:
                try:
                    busy = _unpublished(path)
                finally:
                    _unlock(fd)
            yield CacheEntry("run", path, _tree_bytes(path), last, busy)


def _mirror_entries(mirrors_dir: Path) -> Iterator[CacheEntry]:
    if not mirrors_dir.is_dir():
        return
    for entry in os.scandir(mirrors_dir):
        if not entry.name.endswith(".git") or not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path)
        last = max(
            (
                p.stat().st_mtime
                for p in (path, path / "FETCH_HEAD", path / "HEAD", path / "packed-refs")
                if p.exists()
            ),
            default=0.0,
        )
        fd = _try_lock(mirrors_dir / f"{entry.name}.lock")
        if fd is not None:
            _unlock(fd)
        yield CacheEntry("mirror", path, _tree_bytes(path), last, fd is None)


def _source_entries(sources_dir: Path) -> Iterator[CacheEntry]:
    if not sources_dir.is_dir():
        return
    for repo in os.scandir(sources_dir):
        if not repo.is_dir(follow_symlinks=False):
            continue
        for commit in os.scandir(repo.path):
            if commit.is_dir(follow_symlinks=False):
                path = Path(commit.path)
                yield CacheEntry("sources", path, _tree_bytes(path), path.stat().st_mtime, False)


def entries(settings: Settings) -> list[CacheEntry]:
    """Every evictable entry of the cache (empty with the local default)."""
    if not settings.blob_url or settings.cache_dir is None:
        return []
    cache_dir = settings.cache_dir
    found = list(_run_entries(settings.facts_dir)) if _is_under(settings.facts_dir, cache_dir) else []
    if _is_under(settings.mirrors_dir, cache_dir):
        found += _mirror_entries(settings.mirrors_dir)
    found += _source_entries(cache_dir / "sources")
    return found


def status(settings: Settings) -> CacheStatus:
    """The cache's size by kind, its bound and how many entries are in use."""
    from coverset.facts.cache import cache_max_bytes

    directory = settings.cache_dir or settings.data_dir / "cache"
    result = CacheStatus(directory=directory, remote=bool(settings.blob_url), max_bytes=cache_max_bytes())
    if not result.remote:
        return result
    result.total_bytes = _tree_bytes(directory) if directory.is_dir() else 0
    for entry in entries(settings):
        count, size = result.by_kind.get(entry.kind, (0, 0))
        result.by_kind[entry.kind] = (count + 1, size + entry.bytes)
        result.busy += entry.busy
    return result


def _remove(entry: CacheEntry) -> bool:
    """Delete one entry (a run under its lock, re-checked; anything else renamed away first)."""
    path = entry.path
    if entry.kind == "run":
        fd = _try_lock(path / ".lock")
        if fd is None:
            return False
        try:
            if _unpublished(path):
                return False
            hidden = path.with_name(f".evicting-{uuid.uuid4().hex}")
            path.rename(hidden)
        finally:
            _unlock(fd)
    elif entry.kind == "mirror":
        fd = _try_lock(path.parent / f"{path.name}.lock")
        if fd is None:
            return False
        try:
            hidden = path.with_name(f".evicting-{uuid.uuid4().hex}")
            path.rename(hidden)
        finally:
            _unlock(fd)
    else:
        if path.is_symlink() or not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        hidden = path.with_name(f".evicting-{uuid.uuid4().hex}")
        path.rename(hidden)
    shutil.rmtree(hidden, ignore_errors=True)
    return True


def evict(settings: Settings, *, max_bytes: int | None = None, dry_run: bool = False) -> EvictReport:
    """Remove least-recently-used entries until the cache fits ``max_bytes`` (default
    ``COVERSET_CACHE_MAX_GB``); entries in use are skipped. Does nothing with the local default."""
    from coverset.facts.cache import cache_max_bytes

    if not settings.blob_url or settings.cache_dir is None:
        return EvictReport(max_bytes=cache_max_bytes() if max_bytes is None else max_bytes)
    return _evict(settings.cache_dir, entries(settings), max_bytes=max_bytes, dry_run=dry_run)


def evict_dir(cache_dir: Path, *, max_bytes: int | None = None, dry_run: bool = False) -> EvictReport:
    """:func:`evict` for a cache directory in the standard layout (``facts/``, ``mirrors/`` and
    ``sources/`` below it, which is what ``Settings`` gives whenever a blob URL is set); used by the
    facts store, which knows its cache directory but holds no settings."""
    found = [*_run_entries(cache_dir / "facts"), *_mirror_entries(cache_dir / "mirrors")]
    found += _source_entries(cache_dir / "sources")
    return _evict(cache_dir, found, max_bytes=max_bytes, dry_run=dry_run)


def _evict(directory: Path, found: list[CacheEntry], *, max_bytes: int | None, dry_run: bool) -> EvictReport:
    from coverset.facts.cache import cache_max_bytes

    bound = cache_max_bytes() if max_bytes is None else max_bytes
    report = EvictReport(max_bytes=bound)
    if not found:
        return report
    report.total_bytes = _tree_bytes(directory)
    excess = report.total_bytes - bound
    for entry in sorted(found, key=lambda e: e.last_used):
        if excess <= 0:
            break
        if entry.busy:
            report.skipped_busy += 1
            continue
        if dry_run or _remove(entry):
            report.removed.append(entry)
            report.freed_bytes += entry.bytes
            excess -= entry.bytes
        else:
            report.skipped_busy += 1
    return report


def clear(settings: Settings, *, dry_run: bool = False) -> EvictReport:
    """Remove every entry not in use (``coverset cache clear``)."""
    return evict(settings, max_bytes=0, dry_run=dry_run)
