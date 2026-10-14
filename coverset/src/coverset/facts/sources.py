"""The uploaded-source store: measured files of a commit, for repositories coverset cannot fetch.

Upload mode (``coverset-ci upload --with-sources``) can send the source files a run measured, so
the source view, the function pages and the verdict's function index work without a git mirror
(no GitHub App installed, or a host coverset has no credentials for). They are kept at
``<data_dir>/sources/<repo_id>/<sha>/<path>``, every directory 0700 and every file 0600, like the
mirrors they stand in for.

What is accepted is deliberately narrow, because the archive arrives over the network:

* only paths the run's own bundle lists in its ``files`` table (the paths coverage was measured
  for), plus ``pyproject.toml`` and the configured invariants file (``[tool.coverset]`` and
  ``invariants.yaml`` drive what the verdict reports) — anything else in ``sources/`` is dropped and
  counted, so a client cannot use coverset as a file drop;
* each path must be a normalised relative POSIX path (:func:`~coverset.facts.untrusted.safe_rel_path`:
  no ``..``, no absolute paths, no backslashes), and every component is created by this module with
  ``O_NOFOLLOW``/``O_EXCL`` semantics so a pre-existing symlink cannot redirect a write;
* a commit's sources are written once: the first complete upload of ``(repo, sha)`` wins and later
  uploads only add paths that are still missing (a commit's content cannot change, so a different
  later byte string is an attacker or a bug, not an update).

Reads (:func:`read_source`) re-check the path and refuse symlinks and non-regular files. The web
source view falls back here when the mirror lacks the blob (``web/loaders.py``), and only for paths
the run's facts list.

**Over an object store** (:class:`SourceStore` built by :func:`source_store` with
``COVERSET_BLOB_URL`` set) each file is the object ``sources/<repo_id>/<sha>/<path>``. An upload is
first checked and copied by :func:`store_sources` into a private staging directory (every rule
above applies unchanged), then each accepted file is PUT with ``If-None-Match: *``, so the first
upload of a path still wins across replicas (a refused PUT counts the path as kept). Reads go
through the local cache, ``<cache_dir>/sources/<repo_id>/<sha>/<path>``: a miss downloads the one
object there with a single GET (bounded by :data:`MAX_SOURCE_FILE_BYTES` from the response's size,
same 0700/0600 modes, same no-symlink rules), then reads it like any stored file. A path the bucket
does not have is remembered as missing for ``COVERSET_BLOB_SYNC_S`` seconds (the facts' sync
interval), so a source view of a file that was never uploaded does not ask the bucket on every
request. :meth:`SourceStore.checkout` downloads a whole commit for the upload pass's function
index: one listing (which carries each size, so oversized files are skipped without a request),
then the GETs in parallel on the shared transfer pool.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from coverset.facts.untrusted import safe_rel_path

if TYPE_CHECKING:
    from coverset.config import Settings
    from coverset.storage.base import BlobStore

__all__ = [
    "MAX_SOURCE_FILE_BYTES",
    "SourceStore",
    "StoreResult",
    "commit_dir",
    "has_sources",
    "list_sources",
    "read_source",
    "source_store",
    "sources_root",
    "store_sources",
]

MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
"""A larger uploaded file is not stored (the client sends at most 2 MiB per file)."""
CONFIG_PATHS = frozenset({"pyproject.toml", "invariants.yaml"})
"""Repository files accepted besides the measured ones (plus the configured invariants file)."""
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_MISSES: dict[tuple[str, int, str, str], float] = {}
"""``(cache dir, repo, sha, path) -> when`` a read found no such object (see the module docstring)."""
_MISSES_LOCK = threading.Lock()
_MISSES_MAX = 10_000


@dataclass(slots=True)
class StoreResult:
    """What :func:`store_sources` did with one upload's ``sources/`` directory."""

    stored: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    """Paths already in the store (left untouched)."""
    refused: dict[str, int] = field(default_factory=dict)
    """Counts by reason: ``not_in_bundle``, ``bad_path``, ``not_regular``, ``too_large``."""

    def refuse(self, reason: str) -> None:
        self.refused[reason] = self.refused.get(reason, 0) + 1


def sources_root(data_dir: Path) -> Path:
    """``<data_dir>/sources``."""
    return data_dir / "sources"


def commit_dir(data_dir: Path, repo_id: int, sha: str) -> Path:
    """``<data_dir>/sources/<repo_id>/<sha>`` (``sha`` must be 40 lower-case hex characters)."""
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"not a commit SHA: {sha!r}")
    return sources_root(data_dir) / str(int(repo_id)) / sha


def _ensure_dir(path: Path) -> None:
    """Create ``path`` (mode 0700) unless it is already a real directory; refuse a symlink."""
    try:
        os.mkdir(path, _DIR_MODE)
    except FileExistsError:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise ValueError(f"{path} exists and is not a directory") from None


def _ensure_parents(base: Path, rel: str) -> Path:
    """Create every directory of ``rel`` below ``base`` without following links; return the parent."""
    current = base
    for part in rel.split("/")[:-1]:
        current = current / part
        _ensure_dir(current)
    return current


def store_sources(
    data_dir: Path,
    repo_id: int,
    sha: str,
    uploaded: Path,
    *,
    allowed: Collection[str],
    extra_allowed: Iterable[str] = (),
) -> StoreResult:
    """Copy the files under ``uploaded`` (an extracted ``sources/`` directory) into the store.

    ``allowed`` is the bundle's ``files`` table; ``extra_allowed`` adds configuration paths (the
    configured invariants file). Existing files are kept as they are (see the module docstring).
    """
    result = StoreResult()
    if not uploaded.is_dir() or uploaded.is_symlink():
        return result
    permitted = set(allowed) | CONFIG_PATHS | {p for p in extra_allowed if safe_rel_path(p)}
    root = sources_root(data_dir)
    for directory in (data_dir, root, root / str(int(repo_id))):
        _ensure_dir(directory)
    target_root = commit_dir(data_dir, repo_id, sha)
    _ensure_dir(target_root)
    for file in sorted(uploaded.rglob("*")):
        if file.is_dir() and not file.is_symlink():
            continue
        rel = file.relative_to(uploaded).as_posix()
        if safe_rel_path(rel) is None:
            result.refuse("bad_path")
            continue
        if rel not in permitted:
            result.refuse("not_in_bundle")
            continue
        try:
            info = os.lstat(file)
        except OSError:
            result.refuse("not_regular")
            continue
        if not stat.S_ISREG(info.st_mode):
            result.refuse("not_regular")
            continue
        if info.st_size > MAX_SOURCE_FILE_BYTES:
            result.refuse("too_large")
            continue
        try:
            parent = _ensure_parents(target_root, rel)
        except ValueError:
            result.refuse("bad_path")
            continue
        target = parent / rel.rsplit("/", 1)[-1]
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _FILE_MODE)
        except FileExistsError:
            result.kept.append(rel)
            continue
        with os.fdopen(fd, "wb") as out, open(file, "rb") as source:
            shutil.copyfileobj(source, out)
        result.stored.append(rel)
    return result


def read_source(data_dir: Path, repo_id: int, sha: str, path: str) -> bytes | None:
    """The stored bytes of ``path`` at ``sha``, or None (never follows a symlink)."""
    if safe_rel_path(path) is None or not _SHA_RE.fullmatch(sha):
        return None
    target = commit_dir(data_dir, repo_id, sha) / path
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read(MAX_SOURCE_FILE_BYTES + 1)[:MAX_SOURCE_FILE_BYTES]


def list_sources(data_dir: Path, repo_id: int, sha: str) -> list[str]:
    """Every stored path of ``(repo_id, sha)``, sorted (empty when nothing was uploaded)."""
    try:
        root = commit_dir(data_dir, repo_id, sha)
    except ValueError:
        return []
    if not root.is_dir() or root.is_symlink():
        return []
    paths = []
    for file in root.rglob("*"):
        if file.is_file() and not file.is_symlink():
            paths.append(file.relative_to(root).as_posix())
    return sorted(paths)


def has_sources(data_dir: Path, repo_id: int, sha: str) -> bool:
    """True when at least one file of ``(repo_id, sha)`` is stored."""
    return bool(list_sources(data_dir, repo_id, sha))


def _source_prefix(repo_id: int, sha: str) -> str:
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"not a commit SHA: {sha!r}")
    return f"sources/{int(repo_id)}/{sha}/"


class SourceStore:
    """The uploaded-source store over the local data directory or an object store with a cache.

    With ``blobs=None`` every method is the module function of the same purpose on ``data_dir``,
    unchanged. With an object store, ``cache_dir`` holds the cached copies (see the module
    docstring).
    """

    def __init__(
        self, data_dir: Path, *, blobs: BlobStore | None = None, cache_dir: Path | None = None
    ) -> None:
        self.data_dir = Path(data_dir)
        self.blobs = blobs
        self.cache_dir = Path(cache_dir) if cache_dir is not None else self.data_dir

    def store(
        self,
        repo_id: int,
        sha: str,
        uploaded: Path,
        *,
        allowed: Collection[str],
        extra_allowed: Iterable[str] = (),
    ) -> StoreResult:
        """:func:`store_sources`, then (object store) one ``If-None-Match`` PUT per accepted file."""
        if self.blobs is None:
            return store_sources(
                self.data_dir, repo_id, sha, uploaded, allowed=allowed, extra_allowed=extra_allowed
            )
        from coverset.storage.base import PreconditionFailed

        prefix = _source_prefix(repo_id, sha)
        staging = Path(tempfile.mkdtemp(prefix="coverset-sources-"))
        try:
            staged = store_sources(
                staging, repo_id, sha, uploaded, allowed=allowed, extra_allowed=extra_allowed
            )
            result = StoreResult(refused=staged.refused)
            root = commit_dir(staging, repo_id, sha)
            for rel in staged.stored:
                try:
                    self.blobs.put_file(prefix + rel, root / rel, if_none_match=True)
                except PreconditionFailed:
                    result.kept.append(rel)  # the first upload of a commit's path wins
                    continue
                result.stored.append(rel)
            self._forget_misses(repo_id, sha)
            return result
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _missed(self, repo_id: int, sha: str, rel: str) -> bool:
        """Whether a read found this path absent within the last sync interval."""
        from coverset.facts.cache import sync_interval

        with _MISSES_LOCK:
            when = _MISSES.get((str(self.cache_dir), int(repo_id), sha, rel))
        return when is not None and time.monotonic() - when < sync_interval()

    def _record_miss(self, repo_id: int, sha: str, rel: str) -> None:
        with _MISSES_LOCK:
            if len(_MISSES) >= _MISSES_MAX:
                _MISSES.clear()
            _MISSES[(str(self.cache_dir), int(repo_id), sha, rel)] = time.monotonic()

    def _forget_misses(self, repo_id: int, sha: str) -> None:
        with _MISSES_LOCK:
            for key in [k for k in _MISSES if k[1] == int(repo_id) and k[2] == sha]:
                del _MISSES[key]

    def read(self, repo_id: int, sha: str, path: str) -> bytes | None:
        """:func:`read_source`; over an object store, from the cache after fetching a miss."""
        if self.blobs is None:
            return read_source(self.data_dir, repo_id, sha, path)
        if safe_rel_path(path) is None or not _SHA_RE.fullmatch(sha):
            return None
        cached = read_source(self.cache_dir, repo_id, sha, path)
        if cached is not None:
            return cached
        if self._missed(repo_id, sha, path):
            return None
        if not self._fetch(repo_id, sha, path):
            self._record_miss(repo_id, sha, path)
            return None
        return read_source(self.cache_dir, repo_id, sha, path)

    def _fetch(self, repo_id: int, sha: str, rel: str) -> bool:
        """Download one object into the cache (no-symlink parents, 0700/0600) with one GET; False
        when it is absent or larger than :data:`MAX_SOURCE_FILE_BYTES`."""
        from coverset.storage.base import BlobNotFound, BlobTooLarge

        assert self.blobs is not None
        root = sources_root(self.cache_dir)
        self.cache_dir.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        for directory in (
            self.cache_dir,
            root,
            root / str(int(repo_id)),
            commit_dir(self.cache_dir, repo_id, sha),
        ):
            _ensure_dir(directory)
        try:
            parent = _ensure_parents(commit_dir(self.cache_dir, repo_id, sha), rel)
        except ValueError:
            return False
        target = parent / rel.rsplit("/", 1)[-1]
        if target.is_symlink():
            return False
        try:
            self.blobs.get_file(_source_prefix(repo_id, sha) + rel, target, max_bytes=MAX_SOURCE_FILE_BYTES)
        except (BlobNotFound, BlobTooLarge):
            return False
        return True

    def list(self, repo_id: int, sha: str) -> list[str]:
        """:func:`list_sources`; over an object store, from the bucket's listing."""
        if self.blobs is None:
            return list_sources(self.data_dir, repo_id, sha)
        try:
            prefix = _source_prefix(repo_id, sha)
        except ValueError:
            return []
        return sorted(info.key[len(prefix) :] for info in self.blobs.list(prefix))

    def has(self, repo_id: int, sha: str) -> bool:
        """:func:`has_sources`."""
        if self.blobs is None:
            return has_sources(self.data_dir, repo_id, sha)
        try:
            prefix = _source_prefix(repo_id, sha)
        except ValueError:
            return False
        return next(iter(self.blobs.list(prefix)), None) is not None

    def checkout(self, repo_id: int, sha: str) -> Path:
        """A local directory holding every stored file of the commit (the data directory's own
        commit directory, or the cache's after downloading what it lacks)."""
        if self.blobs is None:
            return commit_dir(self.data_dir, repo_id, sha)
        from coverset.storage import transfer_pool

        prefix = _source_prefix(repo_id, sha)  # ValueError for a bad sha, like commit_dir
        target = commit_dir(self.cache_dir, repo_id, sha)
        missing = [
            rel
            for info in self.blobs.list(prefix)
            if (rel := info.key[len(prefix) :])
            and safe_rel_path(rel) is not None
            and info.size <= MAX_SOURCE_FILE_BYTES
            and not (target / rel).is_file()
        ]
        if missing:
            list(transfer_pool("io").map(lambda rel: self._fetch(repo_id, sha, rel), missing))
        return target

    def remove(self, repo_id: int, shas: Iterable[str], *, dry_run: bool = False) -> list[str]:
        """Delete the sources of each commit in ``shas`` (objects and cached copies); returns the
        commits that had any, sorted (see :func:`coverset.facts.gc.remove_sources`)."""
        from coverset.facts.gc import remove_sources

        if self.blobs is None:
            return remove_sources(self.data_dir, repo_id, shas, dry_run=dry_run)
        removed: list[str] = []
        for sha in sorted(set(shas)):
            if not _SHA_RE.fullmatch(sha):
                continue
            prefix = _source_prefix(repo_id, sha)
            if dry_run:
                found = next(iter(self.blobs.list(prefix)), None) is not None
            else:
                found = self.blobs.delete_prefix(prefix) > 0
            if found:
                removed.append(sha)
        remove_sources(self.cache_dir, repo_id, shas, dry_run=dry_run)  # the cached copies
        return removed

    def purge(self, repo_id: int) -> None:
        """Delete every stored source of the repository (``coverset repo purge``)."""
        for base in {self.data_dir, self.cache_dir}:
            root = sources_root(base) / str(int(repo_id))
            if not root.is_symlink():
                shutil.rmtree(root, ignore_errors=True)
        if self.blobs is not None:
            self.blobs.delete_prefix(f"sources/{int(repo_id)}/")


def source_store(settings: Settings) -> SourceStore:
    """The uploaded-source store ``settings`` name (local files, or the object store and its cache)."""
    from coverset.storage import remote_store

    return SourceStore(settings.data_dir, blobs=remote_store(settings), cache_dir=settings.cache_dir)
