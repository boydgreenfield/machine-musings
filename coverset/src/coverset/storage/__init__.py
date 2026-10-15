"""Blob storage: files under the data directory by default, any S3-compatible object store when
``COVERSET_BLOB_URL`` is set; one code path above either.

The database holds rows; everything else coverset keeps is a blob in one
:class:`~coverset.storage.base.BlobStore` (the key layout is in :mod:`coverset.storage.base`):

* with ``COVERSET_BLOB_URL`` empty, :class:`~coverset.storage.local.LocalBlobStore` over the data
  directory, so a laptop needs no service besides Postgres;
* with ``COVERSET_BLOB_URL=s3://bucket[/prefix]?endpoint_url=...&region=...``,
  :class:`~coverset.storage.s3.S3BlobStore` (AWS S3, MinIO, Cloudflare R2, Tigris).

Either way a disposable local cache under ``COVERSET_CACHE_DIR`` sits in front of it (DuckDB and
pyarrow read local files; the cache is what they read), and the facts, the uploaded sources and the
upload archives go through the same manifests, conditional writes and pulls.

This module picks the store (:func:`from_settings`, :func:`blob_url_of`), keeps one store object
per URL so every caller shares one boto3 client and its connection pool (:func:`store_from_url`,
which is also how an ingest worker process, handed only the URL, finds its store), owns the thread
pools transfers run on (:func:`transfer_pool`) and answers ``coverset deploy check``'s object-store
row (:func:`check_store`). The design, the consistency rules and the measurements are in
``docs/object-store.md``.

**Forking.** The ingest pool is a ``ProcessPoolExecutor``, which forks on Linux, so a worker starts
with copies of this module's stores (their boto3 clients and keep-alive sockets), the pools (whose
threads do not exist in the child) and the facts store's sync bookkeeping (locks that may be held
by a parent thread that is not there). :func:`os.register_at_fork` hooks (:func:`_after_fork`, and
:func:`coverset.facts.store._after_fork`) reset all of that in the child before it runs anything, so
a worker builds its own client and never shares a socket with the web process.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from coverset.storage.base import (
    MULTIPART_THRESHOLD,
    BlobNotFound,
    BlobStore,
    BlobTooLarge,
    ObjectInfo,
    PreconditionFailed,
    StorageError,
    check_key,
    check_prefix,
    join_key,
)
from coverset.storage.local import LocalBlobStore
from coverset.storage.s3 import S3BlobStore, parse_blob_url

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = [
    "MULTIPART_THRESHOLD",
    "BlobNotFound",
    "BlobStore",
    "BlobTooLarge",
    "LocalBlobStore",
    "ObjectInfo",
    "PreconditionFailed",
    "S3BlobStore",
    "StorageError",
    "blob_url_of",
    "check_key",
    "check_prefix",
    "check_store",
    "from_settings",
    "join_key",
    "parse_blob_url",
    "store_from_url",
    "transfer_pool",
]

Status = Literal["pass", "warn", "fail"]

_STORES: dict[str, BlobStore] = {}
_POOLS: dict[str, ThreadPoolExecutor] = {}
_LOCK = threading.Lock()
TRANSFER_WORKERS = 16
"""Threads per transfer pool: how many objects go up or down at once."""


def transfer_pool(name: str) -> ThreadPoolExecutor:
    """A shared thread pool for blob transfers: ``io`` for the objects themselves (uploads, downloads,
    sources), ``sync`` for pulling many runs at once (two pools, so a pull waiting on its downloads
    never starves them). One per process; reset in a forked child."""
    with _LOCK:
        pool = _POOLS.get(name)
        if pool is None:
            pool = _POOLS[name] = ThreadPoolExecutor(
                max_workers=TRANSFER_WORKERS, thread_name_prefix=f"coverset-blob-{name}"
            )
        return pool


def _after_fork() -> None:
    """In a forked child: new lock, no pools, no clients (see the module docstring)."""
    global _LOCK
    _LOCK = threading.Lock()
    _POOLS.clear()
    for store in _STORES.values():
        forget = getattr(store, "forget_client", None)
        if forget is not None:
            forget()


os.register_at_fork(after_in_child=_after_fork)


def store_from_url(url: str) -> BlobStore:
    """The store a blob URL names, one object per URL for the process (so one client and pool).

    ``s3://...`` is :class:`S3BlobStore`; ``file:///abs/path`` is a :class:`LocalBlobStore` there
    (the default under the data directory, :func:`blob_url_of`). Raises :class:`ValueError`
    otherwise.
    """
    with _LOCK:
        found = _STORES.get(url)
        if found is None:
            if url.startswith("s3://"):
                found = S3BlobStore.from_url(url)
            elif url.startswith("file:///"):
                found = LocalBlobStore(Path.from_uri(url))
            else:
                raise ValueError(f"unsupported blob URL {url!r} (s3://bucket[/prefix] or file:///path)")
            _STORES[url] = found
        return found


def blob_url_of(settings: Settings) -> str:
    """The URL of the blob store ``settings`` name: ``COVERSET_BLOB_URL``, or ``file://<data_dir>``
    (picklable, for the ingest pool's worker processes)."""
    return settings.blob_url or settings.data_dir.as_uri()


def from_settings(settings: Settings) -> BlobStore:
    """The blob store ``settings`` name: the object store, else files under the data directory."""
    return store_from_url(blob_url_of(settings))


def check_store(settings: Settings) -> tuple[Status, str]:
    """``coverset deploy check``'s object-store row: ``(status, detail)``.

    The store (local files or a bucket alike) is probed with a throwaway object under
    ``_coverset/probe/``: a create with ``If-None-Match: *``, a write with a wrong ``If-Match`` (it
    must be refused, or the dictionary and manifest compare-and-swap would silently overwrite), a
    read back, a listing and a delete. Missing credentials, a missing bucket or a refused request
    fail; a store that ignores conditional writes warns.
    """
    blobs = from_settings(settings)
    key = f"_coverset/probe/{uuid.uuid4().hex}"
    started = time.perf_counter()
    try:
        etag = blobs.put_bytes(key, b"coverset probe", if_none_match=True)
        try:
            try:
                blobs.put_bytes(key, b"overwrite", if_match=etag + "x")
                ignored = True
            except PreconditionFailed:
                ignored = False
            data, _ = blobs.get_bytes(key)
            listed = any(info.key == key for info in blobs.list("_coverset/probe/"))
        finally:
            blobs.delete([key])
    except StorageError as exc:
        return "fail", f"{blobs.url}: {exc}"
    except Exception as exc:  # noqa: BLE001 - credentials missing, DNS, TLS: report, never raise
        return "fail", f"{blobs.url}: {type(exc).__name__}: {exc}"
    elapsed = (time.perf_counter() - started) * 1000
    if ignored:
        return "warn", (
            f"{blobs.url}: the store ignores If-Match, so two replicas could overwrite each other's "
            "dictionary or manifest updates; run one replica (docs/object-store.md)"
        )
    if data != b"coverset probe" or not listed:
        return "fail", f"{blobs.url}: an object written could not be read back or listed"
    return "pass", f"{blobs.url}: read, write, list, delete and conditional writes ok ({elapsed:.0f} ms)"
