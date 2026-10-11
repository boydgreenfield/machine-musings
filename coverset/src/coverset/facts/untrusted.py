"""Limits and safe I/O for untrusted shard bundles (anything a PR's own code could have written).

A bundle is produced inside a worker that ran the PR's code, so every byte of it is attacker
controlled. This module holds the size caps from the plan ("Secrets and config"), the path
normaliser shared by every reader, size-capped gzip/JSON-lines readers, and :func:`extract_bundle`,
which unpacks ``bundle.tar.gz`` without ever creating a link, device or out-of-tree path.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import stat
import tarfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

__all__ = [
    "BundleError",
    "BundleLimits",
    "CappedReader",
    "StreamCapError",
    "extract_bundle",
    "iter_json_lines",
    "open_regular",
    "safe_rel_path",
]

MIB = 1024 * 1024
_COPY_CHUNK = 1 * MIB
_DRIVE = re.compile(r"^[A-Za-z]:")


class BundleError(ValueError):
    """The bundle violates a hard limit or is malformed beyond repair; nothing is ingested."""


class StreamCapError(BundleError):
    """A size-capped stream (:class:`CappedReader`) expanded beyond its cap."""


@dataclass(frozen=True, slots=True)
class BundleLimits:
    """Caps applied to an untrusted bundle.

    ``gzip_cap`` bounds every gzip stream at ``gzip_ratio`` times its compressed size, but never
    below ``gzip_floor_bytes`` (SQLite pages compress far better than 10x, so a strict ratio would
    reject honest bundles) and never above ``gzip_max_bytes``.

    The remaining caps bound what a small bundle can expand into once decoded: line numbers above
    ``max_line_number`` are dropped, and more than ``max_coverage_cells`` distinct (test, file,
    line) cells, ``max_tests`` distinct nodeids, ``max_paths`` distinct paths or
    ``max_shape_keys`` distinct shape keys reject the bundle. The defaults leave room for the
    plan's sizing (10k tests x ~2k lines per run) in a single shard.
    """

    max_bundle_bytes: int = 200 * MIB
    gzip_ratio: int = 10
    gzip_floor_bytes: int = 16 * MIB
    gzip_max_bytes: int = 500 * MIB
    max_json_line_bytes: int = 1 * MIB
    max_nodeid_chars: int = 512
    max_repr_chars: int = 200
    max_longrepr_chars: int = 8 * 1024
    max_members: int = 256
    max_line_number: int = 1_000_000
    max_coverage_cells: int = 25_000_000
    max_tests: int = 50_000
    max_paths: int = 50_000
    max_shape_keys: int = 20_000

    def gzip_cap(self, compressed_bytes: int) -> int:
        """Maximum decompressed bytes allowed for a gzip stream of ``compressed_bytes``."""
        ratio_cap = max(self.gzip_floor_bytes, self.gzip_ratio * compressed_bytes)
        return min(self.gzip_max_bytes, ratio_cap)


def safe_rel_path(value: object) -> str | None:
    """Return ``value`` if it is a normalised, relative POSIX path, else None.

    Rejected: non-strings, empty strings, NUL or backslash characters, absolute paths, drive
    letters, and empty, ``.`` or ``..`` segments (so ``a//b``, ``./a`` and ``a/../b`` are refused,
    not rewritten).
    """
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        return None
    if value.startswith("/") or _DRIVE.match(value):
        return None
    if any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return value


def open_regular(path: Path) -> IO[bytes]:
    """Open ``path`` for reading without following a symlink; refuse anything but a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BundleError(f"cannot open {path.name}: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise BundleError(f"{path.name} is not a regular file")
    return os.fdopen(fd, "rb")


class CappedReader:
    """A read-only stream wrapper that raises :class:`StreamCapError` past ``cap`` bytes."""

    def __init__(self, inner: IO[bytes], cap: int, what: str) -> None:
        self._inner = inner
        self._cap = cap
        self._what = what
        self.consumed = 0

    def _account(self, data: bytes) -> bytes:
        self.consumed += len(data)
        if self.consumed > self._cap:
            raise StreamCapError(f"{self._what} expands beyond {self._cap} bytes")
        return data

    def read(self, size: int = -1) -> bytes:
        """Read up to ``size`` bytes (all remaining when negative), enforcing the cap."""
        if size is None or size < 0:
            size = self._cap - self.consumed + 1
        return self._account(self._inner.read(size))

    def readline(self, size: int = -1) -> bytes:
        """Read one line of at most ``size`` bytes, enforcing the cap."""
        return self._account(self._inner.readline(size))


def iter_json_lines(
    stream: CappedReader | IO[bytes], limits: BundleLimits, stats: Counter[str], name: str
) -> Iterator[dict[str, Any]]:
    """Yield each JSON-object line of ``stream``; oversized or malformed lines are dropped and counted.

    Lines longer than ``limits.max_json_line_bytes`` count under ``<name>.oversized_lines``;
    undecodable, too deeply nested or non-object lines under ``<name>.bad_lines``.
    """
    limit = limits.max_json_line_bytes
    while True:
        line = stream.readline(limit + 1)
        if not line:
            return
        if len(line) > limit:
            stats[f"{name}.oversized_lines"] += 1
            while line and not line.endswith(b"\n"):
                line = stream.readline(limit + 1)
            continue
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            stats[f"{name}.bad_lines"] += 1
            continue
        if isinstance(row, dict):
            yield row
        else:
            stats[f"{name}.bad_lines"] += 1


def _is_sparse(member: tarfile.TarInfo) -> bool:
    """Sparse members claim more bytes than the stream carries (tarfile fills the holes itself)."""
    return (
        member.issparse()
        or member.type == tarfile.GNUTYPE_SPARSE
        or any(key.startswith("GNU.sparse.") for key in member.pax_headers)
    )


def _member_name(member: tarfile.TarInfo) -> str:
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/") if member.isdir() else name
    checked = safe_rel_path(name)
    if checked is None:
        raise BundleError(f"refused tar member path {member.name!r}")
    return checked


def _make_dirs(dest: Path, rel_dir: str) -> None:
    current = dest
    for part in rel_dir.split("/"):
        current = current / part
        try:
            os.mkdir(current, 0o755)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise BundleError(f"tar member collides with a non-directory: {rel_dir!r}") from None


def _copy_member(source: IO[bytes], target: Path, size: int) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as out:
        remaining = size
        while remaining > 0:
            chunk = source.read(min(_COPY_CHUNK, remaining))
            if not chunk:
                raise BundleError(f"truncated tar member {target.name!r}")
            out.write(chunk)
            remaining -= len(chunk)


def extract_bundle(tar_gz_path: Path, dest_dir: Path, limits: BundleLimits | None = None) -> Path:
    """Safely extract ``bundle.tar.gz`` into ``dest_dir`` (created if missing, must be empty).

    Only regular files and directories are accepted; symlinks, hard links, devices, FIFOs and
    sparse members raise :class:`BundleError`, as do unsafe member paths, duplicate members, more
    than ``limits.max_members`` members, a compressed size above ``limits.max_bundle_bytes``, a
    decompressed stream above ``limits.gzip_cap`` and members whose declared sizes add up to more
    than that cap (checked before any byte of a member is written). Returns ``dest_dir``.
    """
    limits = limits or BundleLimits()
    tar_gz_path = Path(tar_gz_path)
    dest_dir = Path(dest_dir)
    st = os.lstat(tar_gz_path)
    if not stat.S_ISREG(st.st_mode):
        raise BundleError("bundle archive is not a regular file")
    if st.st_size > limits.max_bundle_bytes:
        raise BundleError(f"bundle archive is {st.st_size} bytes (limit {limits.max_bundle_bytes})")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if any(dest_dir.iterdir()):
        raise BundleError(f"extraction directory {dest_dir} is not empty")
    cap = min(limits.gzip_cap(st.st_size), limits.max_bundle_bytes)
    written = 0
    try:
        with (
            open_regular(tar_gz_path) as raw,
            gzip.GzipFile(fileobj=raw, mode="rb") as gz,
            tarfile.open(fileobj=CappedReader(gz, cap, "bundle archive"), mode="r|") as tar,
        ):
            for count, member in enumerate(tar, start=1):
                if count > limits.max_members:
                    raise BundleError(f"bundle has more than {limits.max_members} members")
                name = _member_name(member)
                if member.isdir():
                    _make_dirs(dest_dir, name)
                elif member.isreg() and not _is_sparse(member):
                    written += member.size
                    if written > cap:
                        raise BundleError(f"bundle members declare more than {cap} bytes")
                    parent, _, _ = name.rpartition("/")
                    if parent:
                        _make_dirs(dest_dir, parent)
                    source = tar.extractfile(member)
                    if source is None:
                        raise BundleError(f"unreadable tar member {name!r}")
                    try:
                        _copy_member(source, dest_dir / name, member.size)
                    except FileExistsError:
                        raise BundleError(f"duplicate tar member {name!r}") from None
                else:
                    raise BundleError(
                        f"refused tar member {name!r}: links, devices and sparse files are not allowed"
                    )
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise BundleError(f"corrupt bundle archive: {exc}") from exc
    return dest_dir
