"""The report's output directory: fresh or previously ours, and nothing written outside it.

:func:`check_out_dir` accepts a new directory, an empty one, or one a previous report left its
:data:`MARKER` file in; any other non-empty directory is refused, so a mistyped ``--out`` never wipes
someone's files. The report is built in a staging directory next to it (:func:`staging_dir`) and
swapped in only when the build succeeded (:func:`swap_in`), so a failed build leaves the previous
report as it was. :class:`Writer` writes pages only at relative paths built by
:meth:`PageNames.file_page` / :func:`hashed_name` and checks every target resolves inside the
directory. Page names come from untrusted bundle paths and test ids: a path that is not a plain
relative POSIX path of simple segments (``../x.py``, ``/etc/passwd``, ``a\\b``, a dot segment, a
character outside ``[A-Za-z0-9._~-]``, a first segment ``_``) gets a hashed name under ``files/_/``
instead, and so does a path whose page or any of whose directories would collide, compared
case-insensitively, with an earlier page or directory (case-insensitive file systems, ``x`` next to
``x.html/y``).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from coverset.web.render.html import mark_bidi  # noqa: F401  (re-exported for the report's callers)

__all__ = [
    "MARKER",
    "OutDirError",
    "PageNames",
    "Writer",
    "check_out_dir",
    "hashed_name",
    "mark_bidi",
    "neutralize",
    "prepare_out_dir",
    "staging_dir",
    "swap_in",
]

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
    parts = path.split("/")
    # ``files/_/`` holds the hashed pages: no plain path may land there
    return parts[0] != "_" and all(_SEGMENT.match(part) for part in parts)


class PageNames:
    """Assigns each source path its page under ``files/`` (``files/<path>.html`` or a hash).

    Every page name and every directory a page creates is kept case-folded in one namespace, so no
    two paths share a page, no page is also a directory (``x`` → ``files/x.html`` next to
    ``x.html/y`` → ``files/x.html/y.html``), and no directory is spelled two ways (``Src/a.ts`` next
    to ``src/b.ts``, which a case-insensitive file system merges into one directory while the links
    keep both spellings). A path that would collide gets ``files/_/<hash>.html``, re-salted until it
    is free.
    """

    def __init__(self) -> None:
        self._by_path: dict[str, str] = {}
        self._files: set[str] = set()
        self._dirs: dict[str, str] = {}
        """case-folded directory → its spelling."""

    def file_page(self, path: str) -> str:
        """The page of ``path`` relative to the report root, stable for one report."""
        found = self._by_path.get(path)
        if found is not None:
            return found
        page = f"files/{path}.html" if _plain_path(path) else None
        if page is None or not self._free(page):
            salt = 0
            page = f"files/_/{hashed_name(path)}.html"
            while not self._free(page):
                salt += 1
                page = f"files/_/{hashed_name(f'{salt}:{path}')}.html"
        self._claim(page)
        self._by_path[path] = page
        return page

    def _free(self, page: str) -> bool:
        folded = page.casefold()
        if folded in self._files or folded in self._dirs:
            return False
        parts = page.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            key = prefix.casefold()
            if key in self._files:
                return False
            spelled = self._dirs.get(key)
            if spelled is not None and spelled != prefix:
                return False
        return True

    def _claim(self, page: str) -> None:
        self._files.add(page.casefold())
        parts = page.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            self._dirs.setdefault(prefix.casefold(), prefix)


def check_out_dir(out: Path) -> Path:
    """``out`` as an absolute path, when it may be (re)written: it does not exist, or it is an empty
    directory, or a directory holding :data:`MARKER`.

    Raises :class:`OutDirError` when ``out`` exists and is a symlink or not a directory, or is a
    non-empty directory without :data:`MARKER`.
    """
    out = Path(out).absolute()
    try:
        st = os.lstat(out)
    except FileNotFoundError:
        return out
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OutDirError(f"{out} exists and is not a directory")
    if any(out.iterdir()):
        marker = out / MARKER
        if not marker.is_file() or marker.is_symlink():
            raise OutDirError(
                f"{out} is not empty and was not written by `coverset report` (no {MARKER} file): "
                "pass an empty or new --out directory"
            )
    return out


def staging_dir(out: Path) -> Path:
    """A new, empty directory next to ``out`` (same file system) holding :data:`MARKER`: the build
    writes there and :func:`swap_in` moves it into place."""
    out = check_out_dir(out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.coverset-build-", dir=out.parent))
    except OSError as exc:
        raise OutDirError(f"cannot create a build directory next to {out}: {exc}") from exc
    (staging / MARKER).write_text(MARKER_TEXT, encoding="utf-8")
    return staging


def swap_in(staging: Path, out: Path) -> Path:
    """Replace ``out`` (checked again with :func:`check_out_dir`) by the finished ``staging``
    directory; the previous report is removed only once the new one is in place."""
    out = check_out_dir(out)
    old: Path | None = None
    if out.exists():
        old = Path(tempfile.mkdtemp(prefix=f".{out.name}.coverset-old-", dir=out.parent))
        os.replace(out, old / "report")
    try:
        os.replace(staging, out)
    except OSError:
        if old is not None:
            os.replace(old / "report", out)
            old.rmdir()
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return out.resolve()


def prepare_out_dir(out: Path) -> Path:
    """Create ``out`` (or empty it when a previous report made it) and write the marker, in place.

    The report build itself stages into :func:`staging_dir` instead; this stays for callers that
    write into the directory directly. Raises :class:`OutDirError` like :func:`check_out_dir`.
    """
    out = check_out_dir(out)
    if out.exists():
        for entry in list(out.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        out.mkdir(parents=True)
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
