"""Blob storage: local files by default, any S3-compatible object store when ``COVERSET_BLOB_URL`` is set.

The control database holds rows; everything else coverset keeps is a blob in one
:class:`~coverset.storage.base.BlobStore` (the key layout is in :mod:`coverset.storage.base`):

* with ``COVERSET_BLOB_URL`` empty, :class:`~coverset.storage.local.LocalBlobStore` over the data
  directory, so a laptop needs no service and the files sit exactly where they always have;
* with ``COVERSET_BLOB_URL=s3://bucket[/prefix]?endpoint_url=...&region=...``,
  :class:`~coverset.storage.s3.S3BlobStore` (AWS S3, MinIO, Cloudflare R2, Tigris), with a
  disposable local cache under ``COVERSET_CACHE_DIR`` in front of it (DuckDB and pyarrow read local
  files; the cache is what they read).

This module picks the backend (:func:`from_settings`, :func:`remote_store`), keeps one store object
per URL so every caller shares one boto3 client and its connection pool (:func:`store_from_url`),
maps a local facts directory to the bucket it caches (:func:`facts_remote`, which is how a
:class:`~coverset.facts.store.FactsStore` built from a bare path in an ingest worker process finds
its bucket), owns the thread pools transfers run on (:func:`transfer_pool`) and answers ``coverset
deploy check``'s object-store row (:func:`check_store`). The design, the consistency rules and the
measurements are in ``docs/object-store.md``.

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
    "check_key",
    "check_prefix",
    "check_store",
    "facts_remote",
    "from_settings",
    "join_key",
    "parse_blob_url",
    "register_facts_remote",
    "remote_store",
    "store_from_url",
    "transfer_pool",
]

Status = Literal["pass", "warn", "fail"]

_STORES: dict[str, BlobStore] = {}
_FACTS_REMOTES: dict[str, BlobStore] = {}
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
    for store in {*_STORES.values(), *_FACTS_REMOTES.values()}:
        forget = getattr(store, "forget_client", None)
        if forget is not None:
            forget()


os.register_at_fork(after_in_child=_after_fork)


def store_from_url(url: str) -> BlobStore:
    """The store a blob URL names, one object per URL for the process (so one client and pool).

    ``s3://...`` is :class:`S3BlobStore`; ``file:///abs/path`` is a :class:`LocalBlobStore` there
    (tests, and a shared filesystem used as the "remote"). Raises :class:`ValueError` otherwise.
    """
    with _LOCK:
        found = _STORES.get(url)
        if found is None:
            if url.startswith("s3://"):
                found = S3BlobStore.from_url(url)
            elif url.startswith("file:///"):
                found = LocalBlobStore(Path(url[len("file://") :]))
            else:
                raise ValueError(f"unsupported blob URL {url!r} (s3://bucket[/prefix] or file:///path)")
            _STORES[url] = found
        return found


def remote_store(settings: Settings) -> BlobStore | None:
    """The object store of ``settings`` (``COVERSET_BLOB_URL``), or None for local files."""
    return store_from_url(settings.blob_url) if settings.blob_url else None


def from_settings(settings: Settings) -> BlobStore:
    """The blob store ``settings`` name: the object store, else files under the data directory."""
    return remote_store(settings) or LocalBlobStore(settings.data_dir)


def register_facts_remote(root: Path, blobs: BlobStore) -> None:
    """Record that the local facts directory ``root`` is a cache of ``blobs`` (for this process)."""
    with _LOCK:
        _FACTS_REMOTES[str(Path(root).resolve())] = blobs


def facts_remote(root: Path) -> BlobStore | None:
    """The object store the facts directory ``root`` caches, or None when ``root`` is the store.

    A directory registered by :func:`register_facts_remote` (every :class:`FactsStore` built with a
    remote registers its root) answers first. Otherwise the configured knobs decide
    (:func:`coverset.facts.cache.setting`): with ``COVERSET_BLOB_URL`` set, ``root`` is a cache
    exactly when it is ``<COVERSET_CACHE_DIR>/facts``, which is what ``Settings.facts_dir`` is then,
    so any other directory (a test's, a report's) stays a plain local store.
    """
    from coverset.facts import cache

    url = cache.setting(cache.BLOB_URL_ENV).strip()
    if not _FACTS_REMOTES and not url:
        return None  # the local default: no resolve(), no lock
    resolved = str(Path(root).resolve())
    found = _FACTS_REMOTES.get(resolved)
    if found is not None or not url:
        return found
    cache_dir = cache.setting(cache.CACHE_DIR_ENV).strip()
    if not cache_dir or str((Path(cache_dir).expanduser() / "facts").resolve()) != resolved:
        return None
    blobs = store_from_url(url)
    register_facts_remote(Path(resolved), blobs)
    return blobs


def check_store(settings: Settings) -> tuple[Status, str]:
    """``coverset deploy check``'s object-store row: ``(status, detail)``.

    Local files pass with where they are. An object store is probed with a throwaway object under
    ``_coverset/probe/``: a create with ``If-None-Match: *``, a write with a wrong ``If-Match`` (it
    must be refused, or the dictionary and manifest compare-and-swap would silently overwrite), a
    read back, a listing and a delete. Missing credentials, a missing bucket or a refused request
    fail; a store that ignores conditional writes warns.
    """
    blobs = remote_store(settings)
    if blobs is None:
        return "pass", f"local files under {settings.data_dir} (COVERSET_BLOB_URL unset)"
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
