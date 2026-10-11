"""The report's output directory: fresh or previously ours, and nothing written outside it.

:func:`prepare_out_dir` creates the directory, or reuses it when it is empty, or empties it when a
previous report left its :data:`MARKER` file there; any other non-empty directory is refused, so a
mistyped ``--out`` never wipes someone's files. :class:`Writer` writes pages only at relative paths
built by :func:`file_page` / :func:`hashed_name` and checks every target resolves inside the
directory. Page names come from untrusted bundle paths and test ids: a path that is not a plain
relative POSIX path of simple segments (``../x.py``, ``/etc/passwd``, ``a\\b``, a dot segment, a
character outside ``[A-Za-z0-9._~-]``) gets a hashed name instead, and so does a path whose
lower-cased page name collides with an earlier one (case-insensitive file systems).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

__all__ = ["MARKER", "OutDirError", "PageNames", "Writer", "hashed_name", "neutralize", "prepare_out_dir"]

MARKER = ".coverset-report"
"""File a report writes at the top of its output directory; only such a directory is ever emptied."""
MARKER_TEXT = "coverset report output: this directory is emptied and rewritten by `coverset report`.\n"
_SEGMENT = re.compile(r"^[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,127}$")
_MAX_PATH = 400
_BIDI = re.compile("[؜‎‏‪-‮⁦-⁩]")


class OutDirError(Exception):
    """The output directory cannot be used (not ours, not a directory, or not writable)."""


def neutralize(text: str) -> str:
    """``text`` with bidirectional control characters replaced by a visible ``<U+202E>`` marker.

    Applied to every page and data file the report writes, after escaping, so a Trojan-Source
    string in a nodeid, a repr or the source text shows what it is instead of reordering the line.
    """
    return _BIDI.sub(lambda m: f"&lt;U+{ord(m.group()):04X}&gt;", text)


def neutralize_js(text: str) -> str:
    """Like :func:`neutralize` for JavaScript and JSON data (no HTML entities there)."""
    return _BIDI.sub(lambda m: f"<U+{ord(m.group()):04X}>".translate(_JS_ESCAPES), text)


_JS_ESCAPES = {ord("<"): "\\u003c", ord(">"): "\\u003e"}


def hashed_name(value: str, prefix: str = "") -> str:
    """A 16-hex-digit blake2b name for ``value`` (test pages, unsafe file paths)."""
    return prefix + hashlib.blake2b(value.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _plain_path(path: str) -> bool:
    if not path or len(path) > _MAX_PATH or path.startswith("/"):
        return False
    return all(_SEGMENT.match(part) for part in path.split("/"))


class PageNames:
    """Assigns each source path its page under ``files/`` (``files/<path>.html`` or a hash)."""

    def __init__(self) -> None:
        self._by_path: dict[str, str] = {}
        self._taken: set[str] = set()

    def file_page(self, path: str) -> str:
        """The page of ``path`` relative to the report root, stable for one report."""
        found = self._by_path.get(path)
        if found is not None:
            return found
        page = f"files/{path}.html" if _plain_path(path) else f"files/_/{hashed_name(path)}.html"
        if page.lower() in self._taken:
            page = f"files/_/{hashed_name(path)}.html"
        self._taken.add(page.lower())
        self._by_path[path] = page
        return page


def prepare_out_dir(out: Path) -> Path:
    """Create ``out`` (or empty it when a previous report made it) and write the marker.

    Raises :class:`OutDirError` when ``out`` exists and is a symlink or not a directory, or is a
    non-empty directory without :data:`MARKER`.
    """
    out = Path(out).absolute()
    try:
        st = os.lstat(out)
    except FileNotFoundError:
        out.mkdir(parents=True)
    else:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise OutDirError(f"{out} exists and is not a directory")
        entries = list(out.iterdir())
        if entries:
            marker = out / MARKER
            if not marker.is_file() or marker.is_symlink():
                raise OutDirError(
                    f"{out} is not empty and was not written by `coverset report` (no {MARKER} file): "
                    "pass an empty or new --out directory"
                )
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
    (out / MARKER).write_text(MARKER_TEXT, encoding="utf-8")
    return out.resolve()


class Writer:
    """Writes report files at checked relative paths under one root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.written: list[str] = []

    def target(self, rel: str) -> Path:
        """The absolute path of ``rel``, refusing anything that would leave the root."""
        parts = rel.split("/")
        if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\\" in rel:
            raise OutDirError(f"refusing to write {rel!r}")
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise OutDirError(f"refusing to write {rel!r} outside {self.root}")
        return target

    def text(self, rel: str, content: str) -> Path:
        """Write ``content`` (UTF-8, ``\\n`` newlines) at ``rel``."""
        target = self.target(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        self.written.append(rel)
        return target
