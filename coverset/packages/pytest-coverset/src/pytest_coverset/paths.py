"""One path normalisation shared by coverage, the shape tracer and the bundle writer.

Every path that leaves the worker is git-root-relative POSIX (``src/ledger/billing.py``), so coverage
file keys, shape-tracer function keys, ``git diff`` paths and AST indexes join without translation.
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
