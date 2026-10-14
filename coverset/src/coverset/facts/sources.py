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
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from coverset.facts.untrusted import safe_rel_path

__all__ = [
    "MAX_SOURCE_FILE_BYTES",
    "StoreResult",
    "commit_dir",
    "has_sources",
    "list_sources",
    "read_source",
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
