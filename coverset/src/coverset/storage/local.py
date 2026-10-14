"""A :class:`~coverset.storage.base.BlobStore` over a local directory: the default, under the data
directory.

Keys map one to one onto files below ``root`` (``facts/repo=1/dictionary.json`` is
``<root>/facts/repo=1/dictionary.json``). Directories are created 0700 and files 0600 (they hold or
describe private source), one path component at a time, refusing a symlink anywhere a directory is
expected, so a planted link can never redirect a write; reads open the final component with
``O_NOFOLLOW`` and accept only regular files.

Every write goes to a hidden ``.<name>.<uuid>.tmp`` sibling and is then moved into place with
``os.replace``, so a reader sees the old bytes or the new ones, never a mix. Bytes are fsynced
first. :meth:`LocalBlobStore.put_file` hard-links the file rather than copying it when both sides
are on one filesystem: the facts store's cached files are never rewritten in place (a new version
is a new file), so a published run's cache and its objects share their bytes instead of holding two
copies; across filesystems the bytes are copied and fsynced. Downloads are always copies. The
etag is ``<inode>-<size>-<mtime_ns>`` in hex, read from the temporary file before the rename (a
rename keeps all three), so it costs a ``stat`` rather than a hash of the content; because every
write creates a new inode (or links one no other key has), two versions share an etag only if a
freed inode is reused within the same clock tick for a file of the same size. Conditional writes
are emulated with the semantics of S3's ``If-Match`` and ``If-None-Match: *``: the compare and the
replace happen under an exclusive ``flock`` on ``<root>/.blobstore.lock``, which serialises them
between threads and processes on this machine, so the manifest and dictionary compare-and-swap of
:mod:`coverset.facts.store` runs unchanged on either store.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from coverset.storage.base import (
    TMP_SEGMENT,
    BlobNotFound,
    BlobTooLarge,
    ObjectInfo,
    PreconditionFailed,
    check_key,
    check_prefix,
)

__all__ = ["LocalBlobStore", "local_etag"]

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_NAME = ".blobstore.lock"
_CHUNK = 1024 * 1024
_NO_LINK = frozenset({errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP})
"""``link()`` failures that mean "copy instead": another filesystem, or one without hard links."""


def _private_etag(path: Path) -> str:
    """Make the linked file ``path`` 0600 (when it is not already) and return its etag."""
    st = os.lstat(path)
    if stat.S_IMODE(st.st_mode) != _FILE_MODE:
        os.chmod(path, _FILE_MODE)
    return local_etag(st)


def _try_link(source: Path, target: Path) -> bool:
    """Hard-link the regular file ``source`` (never a symlink) to ``target``; False when it is not a
    regular file or this filesystem pair cannot link (the caller copies)."""
    try:
        if not stat.S_ISREG(os.lstat(source).st_mode):
            return False
        os.link(source, target, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in _NO_LINK:
            return False
        raise
    return True


def local_etag(st: os.stat_result) -> str:
    """The etag of a local file version: inode, size and mtime in nanoseconds, in hex."""
    return f"{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}"


def _is_temp(name: str) -> bool:
    return TMP_SEGMENT.match(name) is not None


class LocalBlobStore:
    """Blobs as files below ``root`` (see the module docstring)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def __repr__(self) -> str:
        return f"LocalBlobStore({str(self.root)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LocalBlobStore) and other.root == self.root

    def __hash__(self) -> int:
        return hash(("local", str(self.root)))

    @property
    def url(self) -> str:
        """``file://<root>``."""
        return self.root.resolve().as_uri() if self.root.is_absolute() else f"file:{self.root}"

    # -- paths ------------------------------------------------------------------------------------

    def local_path(self, key: str) -> Path:
        """The file behind ``key`` (it may not exist)."""
        return self.root / check_key(key)

    def _parent_for_write(self, key: str) -> Path:
        """Create every directory of ``key`` below the root (0700, no symlinks); return the parent."""
        current = self.root
        self._ensure_dir(current, create_parents=True)
        for part in key.split("/")[:-1]:
            current = current / part
            self._ensure_dir(current)
        return current

    @staticmethod
    def _ensure_dir(path: Path, *, create_parents: bool = False) -> None:
        try:
            if create_parents:
                path.mkdir(mode=_DIR_MODE, parents=True)
            else:
                os.mkdir(path, _DIR_MODE)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise ValueError(f"{path} exists and is not a directory") from None

    # -- writing ----------------------------------------------------------------------------------

    @contextmanager
    def _conditional(self, key: str, if_match: str | None, if_none_match: bool) -> Iterator[None]:
        """Hold the store lock and check the condition (nothing to hold for an unconditional write)."""
        if if_match is None and not if_none_match:
            yield
            return
        self.root.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        with open(self.root / _LOCK_NAME, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = self.head(key)
                if if_none_match and current is not None:
                    raise PreconditionFailed(f"{key} already exists")
                if if_match is not None and (current is None or current.etag != if_match):
                    raise PreconditionFailed(f"{key} changed since version {if_match}")
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(
        self,
        key: str,
        fill: Callable[[BinaryIO], None],
        if_match: str | None,
        if_none_match: bool,
        *,
        link: Path | None = None,
    ) -> str:
        check_key(key)
        with self._conditional(key, if_match, if_none_match):
            parent = self._parent_for_write(key)
            name = key.rsplit("/", 1)[-1]
            if link is not None and if_match is None:
                # A new key (every facts object is one): the link itself puts the complete file in
                # place atomically, with no temporary name to rename.
                try:
                    if _try_link(link, parent / name):
                        return _private_etag(parent / name)
                except FileExistsError:
                    pass  # an overwrite after all: through a temporary name below
            tmp = parent / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                if link is not None and _try_link(link, tmp):
                    etag = _private_etag(tmp)
                else:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _FILE_MODE)
                    with os.fdopen(fd, "wb") as out:
                        fill(out)
                        out.flush()
                        os.fsync(out.fileno())
                        etag = local_etag(os.fstat(out.fileno()))
                os.replace(tmp, parent / name)
            finally:
                tmp.unlink(missing_ok=True)
        return etag

    def put_file(
        self, key: str, path: Path, *, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        """Store ``path`` under ``key`` atomically (a hard link when it can be, see the module
        docstring: the caller must not rewrite ``path`` in place afterwards); returns the etag."""

        def fill(out: BinaryIO) -> None:
            with open(path, "rb") as source:
                shutil.copyfileobj(source, out, _CHUNK)

        return self._write(key, fill, if_match, if_none_match, link=Path(path))

    def put_bytes(
        self, key: str, data: bytes, *, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        """Write ``data`` to ``key`` atomically; returns the etag."""
        return self._write(key, lambda out: out.write(data), if_match, if_none_match)

    def copy(self, source: str, target: str) -> str:
        """Copy one key to another; returns the copy's etag."""
        data, _ = self.get_bytes(source)
        return self.put_bytes(target, data)

    # -- reading ----------------------------------------------------------------------------------

    def _open(self, key: str) -> tuple[BinaryIO, os.stat_result]:
        path = self.local_path(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (FileNotFoundError, NotADirectoryError):
            raise BlobNotFound(f"no object {key} in {self.root}") from None
        except OSError as exc:  # ELOOP: a symlink in place of the file
            raise BlobNotFound(f"no object {key} in {self.root} ({exc.strerror})") from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise BlobNotFound(f"{key} in {self.root} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb"), st

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        """The bytes and etag of ``key``."""
        handle, st = self._open(key)
        with handle:
            return handle.read(), local_etag(st)

    def get_file(self, key: str, path: Path, *, max_bytes: int | None = None) -> str:
        """Copy ``key`` to ``path`` (atomically); returns the etag of the version copied. Always a
        copy: the caller may change the copy's mtime (the facts store gives it its writer's)."""
        handle, st = self._open(key)
        if max_bytes is not None and st.st_size > max_bytes:
            handle.close()
            raise BlobTooLarge(f"{key} in {self.root} is {st.st_size} bytes, more than {max_bytes}")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with handle, open(tmp, "xb") as out:
                shutil.copyfileobj(handle, out, _CHUNK)
            os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return local_etag(st)

    def head(self, key: str) -> ObjectInfo | None:
        """Size, etag and mtime of ``key``, or None."""
        try:
            st = os.lstat(self.local_path(key))
        except (FileNotFoundError, NotADirectoryError):
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return ObjectInfo(key, st.st_size, local_etag(st), datetime.fromtimestamp(st.st_mtime, UTC))

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Every regular file below ``prefix`` (hidden temporaries and the lock file left out)."""
        check_prefix(prefix)
        base = self.root / prefix if prefix else self.root
        stack = [(base, prefix)]
        while stack:
            directory, key_prefix = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except (FileNotFoundError, NotADirectoryError):
                continue
            for entry in entries:
                if _is_temp(entry.name) or entry.name == _LOCK_NAME:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append((Path(entry.path), f"{key_prefix}{entry.name}/"))
                elif stat.S_ISREG(st.st_mode):
                    yield ObjectInfo(
                        f"{key_prefix}{entry.name}",
                        st.st_size,
                        local_etag(st),
                        datetime.fromtimestamp(st.st_mtime, UTC),
                    )

    def list_dirs(self, prefix: str = "") -> list[str]:
        """The real (non-symlink, non-hidden-temporary) directories directly below ``prefix``."""
        check_prefix(prefix)
        base = self.root / prefix if prefix else self.root
        try:
            entries = list(os.scandir(base))
        except (FileNotFoundError, NotADirectoryError):
            return []
        return sorted(e.name for e in entries if not _is_temp(e.name) and e.is_dir(follow_symlinks=False))

    # -- deleting ---------------------------------------------------------------------------------

    def delete(self, keys: Iterable[str]) -> None:
        """Unlink each key, then remove directories the deletions left empty (up to the root)."""
        parents: set[Path] = set()
        for key in keys:
            path = self.local_path(key)
            try:
                path.unlink()
            except (FileNotFoundError, NotADirectoryError):
                continue
            parents.add(path.parent)
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            self._prune(parent)

    def _prune(self, directory: Path) -> None:
        root = self.root
        while directory != root and root in directory.parents:
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent

    def delete_prefix(self, prefix: str) -> int:
        """Remove the directory ``prefix`` names (renamed to a hidden name first, so a reader never
        lists half of it); returns the number of files it held."""
        if not check_prefix(prefix):
            raise ValueError("refusing to delete the whole store")
        target = self.root / prefix
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return 0
        if not stat.S_ISDIR(st.st_mode):
            return 0
        count = sum(1 for _ in self.list(prefix))
        doomed = target.with_name(f".deleting.{uuid.uuid4().hex}.tmp")
        target.rename(doomed)
        shutil.rmtree(doomed, ignore_errors=True)
        self._prune(target.parent)
        return count
