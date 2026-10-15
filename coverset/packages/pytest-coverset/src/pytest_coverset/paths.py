"""One path normalisation shared by coverage, the shape tracer and the bundle writer.

Every path that leaves the worker is git-root-relative POSIX (``src/ledger/billing.py``), so coverage
file keys, shape-tracer function keys, ``git diff`` paths and AST indexes join without translation,
whichever directory pytest ran in: coverage.py names files relative to the working directory, and
:func:`coverage_name` turns those names into repository paths (``meta.cwd`` and ``meta.rootdir``,
from :func:`rel_dir`, say where the run happened).
"""

from __future__ import annotations

import os
from pathlib import Path

_EXCLUDED_TOP_DIRS = frozenset({".venv", "venv", ".tox", ".nox", ".git"})
_EXCLUDED_ANY_DIRS = frozenset({"site-packages", "dist-packages", "__pypackages__"})


def find_git_root(start: str | os.PathLike[str]) -> Path | None:
    """Return the nearest directory at or above ``start`` that contains ``.git`` (a dir or a worktree file)."""
    current = Path(os.path.realpath(start))
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def norm_path(path: str | os.PathLike[str], git_root: str | os.PathLike[str]) -> str | None:
    """Return ``path`` relative to ``git_root`` as a POSIX string, or None when it lies outside the repo.

    Relative paths are resolved against the current directory and symlinks are resolved on both sides
    (macOS ``/var`` is ``/private/var``). Pseudo-filenames such as ``<string>`` or ``<frozen os>``
    return None.
    """
    raw = os.fspath(path)
    if not raw or raw.startswith("<"):
        return None
    root = os.path.realpath(git_root)
    full = os.path.realpath(raw)
    if full == root:
        return None
    prefix = root if root.endswith(os.sep) else root + os.sep
    if not full.startswith(prefix):
        return None
    rel = full[len(prefix) :]
    return rel if os.sep == "/" else rel.replace(os.sep, "/")


def rel_dir(path: str | os.PathLike[str], git_root: str | os.PathLike[str]) -> str | None:
    """Directory ``path`` relative to ``git_root`` as POSIX (``.`` for the root), None when outside it."""
    root = os.path.realpath(git_root)
    full = os.path.realpath(path)
    if full == root:
        return "."
    return norm_path(full, root)


def coverage_name(name: str, cwd: str, git_root: str | os.PathLike[str]) -> str:
    """A ``coverage.sqlite``/``coverage.json`` file name as a git-root-relative path.

    coverage.py (``relative_files``) writes a file under the working directory ``cwd`` relative to
    it and any other file absolute. Both are resolved against ``cwd`` and made relative to
    ``git_root``; a file outside the repository keeps an absolute name, which every reader drops.
    """
    if not name or name.startswith("<"):
        return name
    full = os.path.join(cwd, name)
    return norm_path(full, git_root) or os.path.normpath(full)


def is_test_file(rel_path: str) -> bool:
    """True for pytest test modules and conftest files (``test_*.py``, ``*_test.py``, ``conftest.py``)."""
    name = rel_path.rpartition("/")[2]
    return name == "conftest.py" or (
        name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
    )


def is_environment_path(rel_path: str) -> bool:
    """True for files inside a virtualenv or installed-packages directory that happens to sit in the repo."""
    parts = rel_path.split("/")
    return parts[0] in _EXCLUDED_TOP_DIRS or any(part in _EXCLUDED_ANY_DIRS for part in parts[:-1])
