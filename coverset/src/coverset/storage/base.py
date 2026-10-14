"""The blob-store contract: keys, object metadata, errors and the :class:`BlobStore` protocol.

Everything coverset keeps that is not a database row is a *blob*: a Parquet facts file, a run's
``MANIFEST.json``, a repository's ``dictionary.json``, a production batch, an uploaded source file
or an upload archive. A :class:`BlobStore` holds them under POSIX keys below one root:

=====================================================  ============================================
``facts/repo=<id>/run=<id>/<file>.v<n>.<tag>.parquet``   one version of a run file (never rewritten)
``facts/repo=<id>/run=<id>/MANIFEST.json``               what the run contains (written last)
``facts/repo=<id>/dictionary.json``                      the repo's id spaces (compare-and-swap)
``facts/repo=<id>/prod/service=<s>/batch=<b>/*.parquet`` production windows
``sources/<repo>/<sha>/<path>``                          uploaded sources of a commit
``uploads/<repo>/<sha>/<label>.tar.gz``                  an upload's shard, for the processing pass
=====================================================  ============================================

A run file (``runs_dim.parquet``, ``test_lines.shard=0.attempt=2.parquet``) keeps its local name in
the cache and in the manifest; its object key carries the manifest version and a random tag, so a
new version of a file is always a *new* object and the old one stays until the manifest that
replaces it is published (:mod:`coverset.facts.store`).

The interface is synchronous on purpose: the facts layer is synchronous and already runs in
threads (``asyncio.to_thread``) or worker processes wherever it is called from async code, so a
blocking call here never sits on the event loop.

**Keys** are checked by :func:`check_key` before any store sees them: the facts store's path rules
(:func:`coverset.facts.untrusted.safe_rel_path`: relative, normalised, no ``..``, no backslash, no
NUL) plus no control characters, no segment that looks like a local temporary file, and at most
1,024 UTF-8 bytes (S3's limit). Unicode and spaces are allowed as they are (source files may carry
them; S3 keys are UTF-8 and boto3 escapes them on the wire), never rewritten, so a key round-trips
exactly. A key derived from untrusted input (an uploaded source path, an upload label) must also
pass the caller's own check first; this one is the last line, not the only one.

**Etags** identify one version of an object: S3's ``ETag`` (an MD5 for a single-part upload,
``<md5>-<parts>`` for a multipart one, without the quotes), or the local store's
``<inode>-<size>-<mtime_ns>`` in hex. They are compared only for equality, so the two never need
to agree. Conditional writes (``if_match``, ``if_none_match``) are what make the shared state safe
with several replicas: the dictionary and the run manifests are read-modify-write cycles that fail
with :class:`PreconditionFailed` instead of overwriting a concurrent writer's update.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from coverset.facts.untrusted import safe_rel_path

__all__ = [
    "MAX_KEY_BYTES",
    "MULTIPART_THRESHOLD",
    "BlobNotFound",
    "BlobStore",
    "BlobTooLarge",
    "ObjectInfo",
    "PreconditionFailed",
    "TMP_SEGMENT",
    "StorageError",
    "check_key",
    "check_prefix",
    "join_key",
]

MAX_KEY_BYTES = 1024
"""S3's key length limit, applied to every backend so a key valid locally is valid in a bucket."""
MULTIPART_THRESHOLD = 8 * 1024 * 1024
"""Objects above this size go up in 8 MiB parts (S3); a conditional write is always one request."""
TMP_SEGMENT = re.compile(r"^\..+\.[0-9a-f]{32}\.tmp$")
"""The hidden temporary names of :func:`coverset.facts.atomic.atomic_write`: never a key."""
_RESERVED = frozenset({".blobstore.lock"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class StorageError(RuntimeError):
    """The object store failed (network, permissions, a misconfigured bucket); the message says how."""


class BlobNotFound(StorageError, FileNotFoundError):
    """The key does not exist. Also a :class:`FileNotFoundError`, so code written against local
    files (``except FileNotFoundError``) treats a missing object exactly like a missing file."""


class PreconditionFailed(StorageError):
    """A conditional write lost: the object changed (``if_match``) or exists (``if_none_match``)."""


class BlobTooLarge(StorageError):
    """The object is larger than the ``max_bytes`` a :meth:`BlobStore.get_file` caller allows; nothing
    was written (the size is known from the response before any byte of the body is read)."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """One stored object: its key (relative to the store's root), size, etag and modification time."""

    key: str
    size: int
    etag: str
    modified: datetime


def check_key(key: object) -> str:
    """``key`` if it is a valid blob key (see the module docstring), else :class:`ValueError`."""
    if not isinstance(key, str) or safe_rel_path(key) is None:
        raise ValueError(f"invalid blob key {key!r}")
    if _CONTROL.search(key) or len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ValueError(f"invalid blob key {key!r}")
    for segment in key.split("/"):
        if segment in _RESERVED or TMP_SEGMENT.match(segment):
            raise ValueError(f"invalid blob key {key!r}")
    return key


def check_prefix(prefix: str) -> str:
    """A listing or deletion prefix: empty (the whole store) or a valid key ending in ``/``.

    Prefixes always end at a segment boundary, so ``facts/repo=1/`` never also matches
    ``facts/repo=12/``.
    """
    if prefix == "":
        return prefix
    if not prefix.endswith("/"):
        raise ValueError(f"a prefix must end with '/': {prefix!r}")
    check_key(prefix[:-1])
    return prefix


def join_key(*parts: str) -> str:
    """The parts joined with ``/`` and checked (``join_key("facts", "repo=1", "dictionary.json")``)."""
    return check_key("/".join(parts))


@runtime_checkable
class BlobStore(Protocol):
    """Blobs under POSIX keys. Every method is blocking and safe to call from several threads."""

    @property
    def url(self) -> str:
        """Where the store lives, for messages (``s3://bucket/prefix`` or ``file:///path``)."""
        ...

    def put_file(
        self, key: str, path: Path, *, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        """Store ``path``'s bytes under ``key``; returns the new etag.

        ``if_match`` writes only over that exact version, ``if_none_match`` only when the key does
        not exist; either raises :class:`PreconditionFailed` otherwise.
        """
        ...

    def put_bytes(
        self, key: str, data: bytes, *, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        """Store ``data`` under ``key``; returns the new etag (conditions as :meth:`put_file`)."""
        ...

    def get_file(self, key: str, path: Path, *, max_bytes: int | None = None) -> str:
        """Write the object to ``path`` (atomically: a temporary sibling, then a rename); returns its
        etag. Raises :class:`BlobNotFound`, or :class:`BlobTooLarge` before writing anything when
        the object exceeds ``max_bytes`` (a caller's bound on untrusted content, checked here so a
        download needs no ``HEAD`` first)."""
        ...

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        """The object's bytes and etag, read in one request. Raises :class:`BlobNotFound`."""
        ...

    def head(self, key: str) -> ObjectInfo | None:
        """The object's metadata, or None when it does not exist."""
        ...

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Every object whose key starts with ``prefix`` (a :func:`check_prefix` prefix), any order."""
        ...

    def list_dirs(self, prefix: str = "") -> list[str]:
        """The names of the "directories" directly below ``prefix`` (``repo=1``, ``run=abc``), sorted."""
        ...

    def delete(self, keys: Iterable[str]) -> None:
        """Delete each key; a missing key is not an error."""
        ...

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix`` (never ``""``); returns how many there were."""
        ...

    def copy(self, source: str, target: str) -> str:
        """Copy one object to another key inside the store; returns the copy's etag."""
        ...

    def local_path(self, key: str) -> Path | None:
        """The file behind ``key`` when the store is a local directory (else None)."""
        ...
