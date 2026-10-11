"""Atomic file replacement: write a hidden temporary sibling, fsync, then ``os.replace``."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

__all__ = ["atomic_write", "atomic_write_bytes"]


def atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    """Call ``write(tmp_path)`` then atomically move the result to ``path``.

    The temporary file lives in the same directory (so ``os.replace`` never crosses a filesystem),
    starts with a dot and ends in ``.tmp`` (so no reader glob ever matches it), and is removed if
    ``write`` fails.
    """
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write(tmp)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``."""
    atomic_write(path, lambda tmp: tmp.write_bytes(data))
