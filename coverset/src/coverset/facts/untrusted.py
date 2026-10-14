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
import stat
import tarfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from coverset_model.io import Limits, safe_rel_path

__all__ = [
    "BundleError",
    "BundleLimitError",
    "BundleLimits",
    "CappedReader",
    "StreamCapError",
    "extract_bundle",
    "iter_json_lines",
    "open_regular",
]

MIB = 1024 * 1024
GIB = 1024 * MIB
"""A byte cap that is never reached (the local profile's ``max_bundle_bytes``)."""
_COPY_CHUNK = 1 * MIB


class BundleError(ValueError):
    """The bundle violates a hard limit or is malformed beyond repair; nothing is ingested."""


class BundleLimitError(BundleError):
    """A :class:`BundleLimits` cap was hit: ``cap`` names the field, ``value`` is what the bundle
    has (bytes, cells, tests, ...) and ``limit`` the cap's value, so a caller can say which cap it
    was and how to lift it (``coverset report --trusted``)."""

    def __init__(
        self, message: str, *, cap: str, value: int | None, limit: int | None, profile: str = "strict"
    ) -> None:
        super().__init__(message)
        self.cap = cap
        self.value = value
        self.limit = limit
        self.profile = profile


class StreamCapError(BundleError):
    """A size-capped stream (:class:`CappedReader`) expanded beyond its cap."""


@dataclass(frozen=True, slots=True)
class BundleLimits(Limits):
    """Caps applied to an untrusted bundle: the neutral reader's :class:`coverset_model.io.Limits`
    (one set of numbers for both readers, ``docs/security.md`` section 7) plus what only the
    pytest-coverset reader needs.

    ``max_coverage_cells`` (4,000,000) keeps one shard's ingest near 300 MB of memory (read plus
    the ``test_lines`` arrays, measured in ``docs/security.md`` B14); the plan's sizing (10k tests x
    ~2k lines per run, 20M cells) fits when the run has at least five shards
    (``scripts/bench_facts.py`` uses eight, 2.5M cells each).

    These defaults are the **strict** profile, for a bundle a pull request produced (ingest, and
    ``coverset report`` of an archive or a neutral bundle). :meth:`local` is the profile for a
    directory the user's own ``pytest --coverset`` wrote; ``profile`` names which one a value is.
    """

    max_json_file_bytes: int = 64 * MIB
    """``coverage.json``, ``collect.json``, ``durations.json`` and ``meta.json`` are parsed whole;
    a larger one is skipped and counted (``<name>.oversized``)."""
    profile: str = "strict"
    """``strict`` (a pull request's bundle) or ``local`` (:meth:`local`); shown in errors and the
    report's about data."""

    @classmethod
    def local(cls) -> BundleLimits:
        """The profile for trusted input: a directory the user's own ``pytest --coverset`` wrote
        (or an archive passed with ``coverset report --trusted``): ``Limits.local()`` (no byte cap,
        no wall-clock budget, generous row and cell caps), and whole JSON files up to 1 GiB."""
        base = Limits.local()
        shared = {name: getattr(base, name) for name in Limits.__dataclass_fields__}
        return cls(**shared, max_json_file_bytes=1 * GIB, profile="local")

    def exceeded(
        self, message: str, *, cap: str, value: int | None = None, limit: int | None = None
    ) -> BundleLimitError:
        """The error for ``cap`` (a field name) hit by ``value``: ``message`` plus which cap of which
        profile it is. ``limit`` is the cap's value when it is derived (the gzip cap of a stream)."""
        return BundleLimitError(
            f"{message} ({cap} of the {self.profile} limits)",
            cap=cap,
            value=value,
            limit=getattr(self, cap) if limit is None else limit,
            profile=self.profile,
        )


def open_regular(path: Path) -> IO[bytes]:
    """Open ``path`` for reading without following a symlink; refuse anything but a regular file.

    The open is non-blocking, so a FIFO or a device in place of the file is refused at once instead
    of hanging the reader; the descriptor is switched back to blocking once it is known to be a
    regular file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise BundleError(f"cannot open {path.name}: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError(f"{path.name} is not a regular file")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
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
        raise limits.exceeded(
            f"bundle archive is {st.st_size} bytes (limit {limits.max_bundle_bytes})",
            cap="max_bundle_bytes",
            value=st.st_size,
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    if any(dest_dir.iterdir()):
        raise BundleError(f"extraction directory {dest_dir} is not empty")
    cap = min(limits.gzip_cap(st.st_size), limits.max_bundle_bytes)
    cap_name = (
        "max_bundle_bytes"
        if cap == limits.max_bundle_bytes
        else "gzip_max_bytes"
        if cap == limits.gzip_max_bytes
        else "gzip_ratio"
    )
    written = 0
    try:
        with (
            open_regular(tar_gz_path) as raw,
            gzip.GzipFile(fileobj=raw, mode="rb") as gz,
            tarfile.open(fileobj=CappedReader(gz, cap, "bundle archive"), mode="r|") as tar,
        ):
            for count, member in enumerate(tar, start=1):
                if count > limits.max_members:
                    raise limits.exceeded(
                        f"bundle has more than {limits.max_members} members", cap="max_members", value=count
                    )
                name = _member_name(member)
                if member.isdir():
                    _make_dirs(dest_dir, name)
                elif member.isreg() and not _is_sparse(member):
                    written += member.size
                    if written > cap:
                        raise limits.exceeded(
                            f"bundle members declare more than {cap} bytes",
                            cap=cap_name,
                            value=written,
                            limit=cap,
                        )
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
    except StreamCapError as exc:
        raise limits.exceeded(str(exc), cap=cap_name, limit=cap) from exc
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise BundleError(f"corrupt bundle archive: {exc}") from exc
    return dest_dir
