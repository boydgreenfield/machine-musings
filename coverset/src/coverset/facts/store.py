"""The Parquet facts layout and its DuckDB views.

Layout under ``<facts_dir>/repo=<repo_id>/``: one ``dictionary.json`` and one ``run=<run_id>/``
directory per run holding

- run tables (see :mod:`coverset.facts.schema`) as ``<table>.parquet``;
- shard tables as ``<table>.shard=<n>.attempt=<k>.parquet``, one file per table per ingest attempt
  of shard ``n``;
- ``ingest_log.shard=<n>.parquet``, the shard's ingest history and its commit record.

An attempt's files become visible only once ``ingest_log`` records that attempt as ``ok``: readers
(:meth:`FactsStore.table_files`, :meth:`FactsStore.read_table` and the DuckDB views) see each shard's
*committed attempt* (its latest ``ok`` attempt) and nothing else, so a crash or a rejected bundle
half-way through an ingest never mixes two bundles. Files are immutable once written (zstd,
dictionary encoding, atomic replace), so any number of processes can query while one ingests: there
is no DuckDB file. Writers of one run serialise on :meth:`FactsStore.run_lock`.

Readers that query often (the web pages, :mod:`coverset.facts.queries`) take a cursor of a cached
connection with :meth:`FactsStore.lease` instead of building views per query: the cache
(:mod:`coverset.facts.cache`) is keyed by the stamps of the runs in scope, so a commit, a finalize
or a gc is seen by the next lease. Every connection gets the bounded ``memory_limit`` and thread
count of :func:`~coverset.facts.cache.memory_limit` and :func:`~coverset.facts.cache.threads`.

**Over a blob store.** The directory is a *cache* (``<cache_dir>/facts``) of a
:class:`~coverset.storage.base.BlobStore`: files under the data directory
(:class:`~coverset.storage.local.LocalBlobStore`, the default) or an S3-compatible bucket
(``COVERSET_BLOB_URL``), one code path for both. Everything above still runs on local files: DuckDB,
pyarrow, the stamps and the hidden-temp atomic writes. The store holds the truth through a per-run
``MANIFEST.json`` object (each ``*.parquet`` name with its object key, size, etag and mtime, and a
version that only grows), and two transfers around the run lock:

* entering :meth:`FactsStore.run_lock` *pulls* the run (HEAD of the manifest; when its etag differs
  from the one this cache last saw, GET it and download every file whose etag changed, delete local
  files it no longer lists), so a writer always starts from the published state;
* leaving it *publishes*: every local file that changed since the last pull or publish is uploaded
  (in parallel) as a **new object** (``<name minus .parquet>.v<version>.<random tag>.parquet``: an
  object is never rewritten in place, so every version of a file the store holds is complete),
  then the manifest is written **last**, mapping each file name to its object key, with ``If-Match``
  on the etag the writer started from (``If-None-Match: *`` for a new run), and only then are the
  objects the new manifest no longer references deleted. A reader therefore never sees a
  half-written run: until the manifest is replaced it sees the previous version, whose objects are
  all still there, whatever happened to the writer in between (a writer that dies after its uploads
  leaves unreferenced objects, which the run's deletion removes, and nothing else). A lost race
  raises :class:`RunWriteConflict` instead of overwriting another writer's update, and deletes the
  objects the loser uploaded. The manifest also carries the run's ``created_at`` (from
  ``runs_dim`` when it is published), so retention (:mod:`coverset.facts.gc`) can age a run with
  one small GET instead of pulling it.

Readers pull before they look (:meth:`FactsStore._names`, :meth:`FactsStore.stamp`), at most once per
``COVERSET_BLOB_SYNC_S`` (5 s) per run and process: within that window a lookup costs one ``stat``.
A pull that fails (the store unreachable, a manifest naming an object that is gone) is not retried
before the interval has passed either: a reader with a cached copy keeps reading it, one without
gets the error again, so a broken run costs one attempt per interval, never one per request. A
downloaded file gets the mtime its writer gave it, so the summaries' freshness rule
(``queries.summary_is_fresh``) keeps its meaning. Writers of a run on one machine (the ingest pool's
processes share the cache directory) serialise on an ``fcntl`` lock file in the run's cache
directory, and a thread that holds it never pulls under its own feet; across machines the
manifest's ``If-Match`` guards against lost updates, and the rule is that one process ingests a
given run at a time, which the runner (one claimant per run) and the upload worker (one pass per
run) already guarantee.
"""

from __future__ import annotations

import fcntl
import functools
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from coverset.facts import cache
from coverset.facts.atomic import atomic_write, atomic_write_bytes
from coverset.facts.dictionary import RepoDictionary
from coverset.facts.schema import ATTEMPT_TABLES, RUN_TABLES, SCHEMAS, TABLES
from coverset.storage import (
    BlobNotFound,
    BlobStore,
    ObjectInfo,
    PreconditionFailed,
    transfer_pool,
)

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = [
    "MANIFEST_FILE",
    "FactsStore",
    "RunWriteConflict",
    "SqlNotAllowed",
    "conform",
    "facts_store",
]

log = logging.getLogger(__name__)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HIVE_TYPES = "{'repo': BIGINT, 'run': VARCHAR}"
_LOCK_FILE = ".lock"
_LOG = "ingest_log"
MANIFEST_FILE = "MANIFEST.json"
"""A run's manifest: the object a reader trusts, and a local copy beside the run's files."""
_RECORD_FILE = ".sync.json"
"""What this cache last pulled or published for a run (hidden: never part of a stamp)."""
_MANIFEST_FORMAT = 2
"""The manifest's format: every entry names its object ``key``, and the run carries ``created_at``."""
_FILE_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.shard=\d+(\.attempt=\d+)?)?\.parquet$")
"""A run file name a manifest may list (it becomes a local path, so nothing else is accepted)."""
_OBJECT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.shard=\d+(\.attempt=\d+)?)?\.v\d+\.[0-9a-f]{8}\.parquet$")
"""The object name of one version of a run file (``runs_dim.v8.3f9a1c2e.parquet``): the file name
with the manifest version and a random tag before the extension. Disjoint from :data:`_FILE_NAME`."""
_KEY_ROOT = "facts"
_DOWNLOAD_ATTEMPTS = 3


class _Unset:
    """The type of :data:`_UNSET`."""


_UNSET = _Unset()


class SqlNotAllowed(ValueError):
    """Ad-hoc SQL passed to :meth:`FactsStore.query` is not a single read-only SELECT."""


class RunWriteConflict(RuntimeError):
    """Another writer published the run's manifest since this writer pulled it.

    Nothing is overwritten: the objects this writer uploaded are not referenced by the manifest, and
    the next pull discards its local copies. One process ingests a given run at a time, so this
    means two control planes claimed the same run.
    """


# -- blob-store bookkeeping -----------------------------------------------------------------------


@dataclass(slots=True)
class _SyncState:
    """Per process and per run (or per dictionary): when it was last checked and what was seen."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    checked: float = float("-inf")
    absent: bool = False
    """The last check found no manifest and no local record (nothing to keep in step with)."""
    error: Exception | None = None
    """The last check failed with this; not tried again before the sync interval has passed."""
    record: str = ""
    """A run's ``.sync.json`` in the cache (computed once: every lookup of the run checks it)."""


_STATES: dict[tuple[str, ...], _SyncState] = {}
_STATES_LOCK = threading.Lock()
_LISTINGS: dict[tuple[str, str], tuple[float, list[str]]] = {}
_HELD = threading.local()
EVICT_INTERVAL_S = 600.0
"""How often (at most) a process that downloads facts checks its cache's bound."""
_last_eviction = float("-inf")


def _state(key: tuple[str, ...]) -> _SyncState:
    with _STATES_LOCK:
        found = _STATES.get(key)
        if found is None:
            found = _STATES[key] = _SyncState()
        return found


def _held() -> set[tuple[str, int, str]]:
    """The runs whose lock this thread holds (a writer never pulls under its own feet)."""
    held = getattr(_HELD, "runs", None)
    if held is None:
        held = _HELD.runs = set()
    return held


def _after_fork() -> None:
    """In a forked child (the ingest pool on Linux): fresh sync bookkeeping, no inherited locks."""
    global _STATES_LOCK, _last_eviction
    _STATES_LOCK = threading.Lock()
    _STATES.clear()
    _LISTINGS.clear()
    _HELD.runs = set()  # the forking thread's run locks are the parent's, not this process's
    _last_eviction = float("-inf")


os.register_at_fork(after_in_child=_after_fork)


def _scan(run_dir: Path) -> dict[str, tuple[int, int, int]]:
    """``name -> (size, mtime_ns, inode)`` of the run's visible-candidate ``*.parquet`` files."""
    found: dict[str, tuple[int, int, int]] = {}
    try:
        with os.scandir(run_dir) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".") or not name.endswith(".parquet"):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                found[name] = (st.st_size, st.st_mtime_ns, st.st_ino)
    except (FileNotFoundError, NotADirectoryError):
        pass
    return found


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_bytes())
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _writer() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _object_name(name: str, version: int) -> str:
    """A new object name for version ``version`` of the run file ``name`` (:data:`_OBJECT_NAME`)."""
    return f"{name[: -len('.parquet')]}.v{version}.{uuid.uuid4().hex[:8]}.parquet"


def _entry_key(name: str, entry: Mapping[str, Any]) -> str:
    """The object name a manifest entry refers to."""
    key = entry.get("key")
    stem = name[: -len(".parquet")] + "."
    if not isinstance(key, str) or not _OBJECT_NAME.match(key) or not key.startswith(stem):
        raise ValueError(f"unexpected object name {key!r} for {name} in a manifest")
    return key


def _known_key(name: str, known: Mapping[str, Sequence[Any]]) -> str:
    """The object name the local record holds for ``name``."""
    return str(known[name][4])


def _created_at_of(runs_dim: Path) -> str | None:
    """``runs_dim.created_at`` of the run, ISO 8601 in UTC, or None (no row, or no such file)."""
    try:
        column = pq.read_table(runs_dim, columns=["created_at"]).column("created_at")
    except (OSError, pa.ArrowException, KeyError):
        return None
    value = column[0].as_py() if len(column) else None
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def conform(table: str, data: pa.Table) -> pa.Table:
    """Select and cast ``data`` to the schema of ``table`` (missing nullable columns become null).

    Raises ``pyarrow.ArrowInvalid`` (or ``OverflowError``) when a value does not fit its column.
    """
    schema = SCHEMAS[table]
    columns = []
    for target in schema:
        if target.name in data.column_names:
            columns.append(data.column(target.name).cast(target.type))
        else:
            columns.append(pa.nulls(data.num_rows, target.type))
    return pa.Table.from_arrays(columns, schema=schema)


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _check_shard(table: str, shard_id: object) -> int:
    if isinstance(shard_id, bool) or not isinstance(shard_id, int) or shard_id < 0:
        raise ValueError(f"{table} is a shard table: pass a shard id >= 0")
    return shard_id


def _committed(logs: Mapping[int, tuple[tuple[int, str], ...]]) -> dict[int, int]:
    result: dict[int, int] = {}
    for shard_id, rows in logs.items():
        ok = [attempt for attempt, status in rows if status == "ok"]
        if ok:
            result[shard_id] = max(ok)
    return result


@functools.lru_cache(maxsize=4096)
def _log_rows(path: str, mtime_ns: int, size: int) -> tuple[tuple[int, str], ...]:
    """``(attempt, status)`` of one ``ingest_log`` file; cached until the file is replaced."""
    table = pq.read_table(path, columns=["attempt", "status"])
    return tuple(zip(table.column("attempt").to_pylist(), table.column("status").to_pylist(), strict=True))


class FactsStore:
    """Immutable per-run Parquet facts: ``facts_dir`` is the local cache DuckDB reads, ``blobs`` the
    store that holds them (see the module docstring)."""

    def __init__(self, facts_dir: Path, blobs: BlobStore) -> None:
        self.root = Path(facts_dir)
        self.blobs = blobs
        self._root_key = str(self.root)

    def __repr__(self) -> str:
        return f"FactsStore({str(self.root)!r}, {self.blobs!r})"

    # -- layout ------------------------------------------------------------------------------

    def repo_dir(self, repo_id: int) -> Path:
        """The directory holding every run (and the dictionary) of ``repo_id``.

        The repo's ``dictionary.json`` is pulled first (rate-limited like every pull), so code that
        reads the directory finds a current copy.
        """
        path = self._repo_path(repo_id)
        self._ensure_dictionary(repo_id, path)
        return path

    def run_dir(self, repo_id: int, run_id: str) -> Path:
        """The directory holding one run's tables.

        The run is pulled into the cache first (at most once per sync interval; :meth:`_ensure`),
        so every caller that goes to the directory itself (stamps, ``is_dir`` checks, the web's
        run-facts cache) sees the published run, also on a replica whose cache has never seen it.
        """
        path = self._run_path(repo_id, run_id)
        self._ensure(repo_id, run_id)
        return path

    def _repo_path(self, repo_id: int) -> Path:
        if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id < 0:
            raise ValueError(f"invalid repo id {repo_id!r}")
        return self.root / f"repo={repo_id}"

    def _run_path(self, repo_id: int, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            raise ValueError(f"invalid run id {run_id!r}")
        return self._repo_path(repo_id) / f"run={run_id}"

    def table_path(
        self, repo_id: int, run_id: str, table: str, shard_id: int | None = None, attempt: int | None = None
    ) -> Path:
        """Where one table file lives (shard tables need ``shard_id`` and, except ``ingest_log``, ``attempt``)."""
        run_dir = self._run_path(repo_id, run_id)
        if table in RUN_TABLES:
            if shard_id is not None or attempt is not None:
                raise ValueError(f"{table} is a run table: it takes no shard id or attempt")
            return run_dir / f"{table}.parquet"
        if table == _LOG:
            if attempt is not None:
                raise ValueError("ingest_log takes no attempt")
            return run_dir / f"{table}.shard={_check_shard(table, shard_id)}.parquet"
        if table in ATTEMPT_TABLES:
            shard = _check_shard(table, shard_id)
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise ValueError(f"{table} is written per ingest attempt: pass an attempt >= 1")
            return run_dir / f"{table}.shard={shard}.attempt={attempt}.parquet"
        raise ValueError(f"unknown facts table {table!r}")

    def committed(self, repo_id: int, run_id: str) -> dict[int, int]:
        """Each shard's committed attempt (its latest ``ok`` ingest), from ``ingest_log``."""
        return _committed(self.shard_logs(repo_id, run_id))

    def shard_logs(self, repo_id: int, run_id: str) -> dict[int, tuple[tuple[int, str], ...]]:
        """``(attempt, status)`` history of every shard with an ``ingest_log``, by shard id."""
        return self._logs(self._run_path(repo_id, run_id), self._names(repo_id, run_id))

    def attempt_files(self, repo_id: int, run_id: str, shard_id: int) -> dict[int, list[Path]]:
        """Every shard-table file of one shard (committed or not), by attempt."""
        pattern = re.compile(rf"^(\w+)\.shard={shard_id}\.attempt=(\d+)\.parquet$")
        run_dir = self._run_path(repo_id, run_id)
        found: dict[int, list[Path]] = {}
        for name in self._names(repo_id, run_id):
            if (match := pattern.match(name)) and match.group(1) in ATTEMPT_TABLES:
                found.setdefault(int(match.group(2)), []).append(run_dir / name)
        return found

    def table_files(
        self, repo_id: int, run_id: str, table: str, *, shard_id: int | None = None
    ) -> list[Path]:
        """The visible files of ``table`` in one run, in shard order (committed attempts only).

        ``shard_id`` narrows a shard table to that one shard.
        """
        if table not in SCHEMAS:
            raise ValueError(f"unknown facts table {table!r}")
        if shard_id is not None and table in RUN_TABLES:
            raise ValueError(f"{table} is a run table: it takes no shard id")
        files = self._visible(repo_id, run_id, (table,))[table]
        if shard_id is None:
            return files
        suffix = f"{table}.shard={_check_shard(table, shard_id)}."
        return [f for f in files if f.name.startswith(suffix)]

    def _visible(self, repo_id: int, run_id: str, tables: Iterable[str]) -> dict[str, list[Path]]:
        """Visible files of each of ``tables`` in one run, from a single directory listing."""
        run_dir = self._run_path(repo_id, run_id)
        names = set(self._names(repo_id, run_id))
        logs = self._logs(run_dir, names)
        committed = _committed(logs)
        visible: dict[str, list[Path]] = {}
        for table in tables:
            if table in RUN_TABLES:
                candidates = [f"{table}.parquet"]
            elif table == _LOG:
                candidates = [f"{_LOG}.shard={shard}.parquet" for shard in logs]
            else:
                candidates = [f"{table}.shard={s}.attempt={a}.parquet" for s, a in committed.items()]
            visible[table] = [run_dir / name for name in candidates if name in names]
        return visible

    def _names(self, repo_id: int, run_id: str) -> list[str]:
        self._ensure(repo_id, run_id)
        run_dir = self._run_path(repo_id, run_id)
        return [entry.name for entry in run_dir.iterdir()] if run_dir.is_dir() else []

    @staticmethod
    def _logs(run_dir: Path, names: Iterable[str]) -> dict[int, tuple[tuple[int, str], ...]]:
        pattern = re.compile(r"^ingest_log\.shard=(\d+)\.parquet$")
        logs: dict[int, tuple[tuple[int, str], ...]] = {}
        for name in names:
            if match := pattern.match(name):
                path = run_dir / name
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                logs[int(match.group(1))] = _log_rows(str(path), st.st_mtime_ns, st.st_size)
        return dict(sorted(logs.items()))

    def dictionary(self, repo_id: int) -> RepoDictionary:
        """A snapshot of the repo's path, nodeid and shape-key ids."""
        repo_dir = self._repo_path(repo_id)
        self._ensure_dictionary(repo_id, repo_dir)
        return RepoDictionary.load(repo_dir)

    def extend_dictionary(
        self,
        repo_id: int,
        *,
        paths: Iterable[str] = (),
        nodeids: Iterable[str] = (),
        shapes: Iterable[str] = (),
    ) -> RepoDictionary:
        """Assign ids to any new values in the repo's dictionary (compare-and-swap on its object,
        :meth:`RepoDictionary.extend`) and return the updated dictionary."""
        return RepoDictionary.extend(
            self._repo_path(repo_id), self.blobs, paths=paths, nodeids=nodeids, shapes=shapes
        )

    @contextmanager
    def run_lock(self, repo_id: int, run_id: str) -> Iterator[None]:
        """Exclusive lock serialising writers (ingest, failure records, finalize) of one run.

        The run is pulled when the lock is taken and published when it is released (see the module
        docstring); a publish failure after the block raised is logged and the block's own error is
        the one that propagates.
        """
        run_dir = self._run_path(repo_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        key = (self._root_key, repo_id, run_id)
        held = _held()
        with open(run_dir / _LOCK_FILE, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            held.add(key)
            try:
                self._pull_run(repo_id, run_id, writer=True)
                try:
                    yield
                except BaseException:
                    try:
                        self._push_run(repo_id, run_id)
                    except Exception:
                        log.exception("publishing run %s after a failed write failed", run_id)
                    raise
                self._push_run(repo_id, run_id)
            finally:
                held.discard(key)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _after_write(self, repo_id: int, run_id: str) -> None:
        """A write outside the run lock (tools, tests) is published at once."""
        if (self._root_key, repo_id, run_id) in _held():
            return
        run_dir = self._run_path(repo_id, run_id)
        key = (self._root_key, repo_id, run_id)
        held = _held()
        with open(run_dir / _LOCK_FILE, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            held.add(key)
            try:
                self._push_run(repo_id, run_id)
            finally:
                held.discard(key)
                fcntl.flock(lock, fcntl.LOCK_UN)

    # -- reading and writing -----------------------------------------------------------------

    def write_table(
        self,
        repo_id: int,
        run_id: str,
        table: str,
        data: pa.Table,
        *,
        shard_id: int | None = None,
        attempt: int | None = None,
    ) -> Path:
        """Atomically write (or replace) one table file of a run; returns its path."""
        path = self.table_path(repo_id, run_id, table, shard_id, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        conformed = conform(table, data)
        atomic_write(
            path,
            lambda tmp: pq.write_table(conformed, tmp, compression="zstd", use_dictionary=True),
        )
        self._after_write(repo_id, run_id)
        return path

    def delete_table(self, repo_id: int, run_id: str, table: str) -> bool:
        """Remove a run table's file; returns whether it existed."""
        if table not in RUN_TABLES:
            raise ValueError(f"only run tables can be deleted, not {table!r}")
        try:
            self.table_path(repo_id, run_id, table).unlink()
        except FileNotFoundError:
            return False
        self._after_write(repo_id, run_id)
        return True

    def read_table(self, repo_id: int, run_id: str, table: str, *, shard_id: int | None = None) -> pa.Table:
        """Read one run's visible table (all shards concatenated unless ``shard_id`` is given).

        Returns an empty table with the table's schema when nothing visible has been written. A
        file superseded by a concurrent ingest between listing and reading is re-listed once.
        """
        try:
            return self._read_files(table, self.table_files(repo_id, run_id, table, shard_id=shard_id))
        except FileNotFoundError:
            return self._read_files(table, self.table_files(repo_id, run_id, table, shard_id=shard_id))

    @staticmethod
    def _read_files(table: str, files: list[Path]) -> pa.Table:
        parts = [conform(table, pq.read_table(f)) for f in files]
        return pa.concat_tables(parts) if parts else SCHEMAS[table].empty_table()

    def list_repos(self) -> list[int]:
        """Every repo id in the store's listing (at most :func:`~coverset.facts.cache.sync_interval`
        old) or in the cache."""
        ids = set(self._local_repos())
        for name in self._listing(f"{_KEY_ROOT}/"):
            prefix, _, value = name.partition("=")
            if prefix == "repo" and value.isdigit():
                ids.add(int(value))
        return sorted(ids)

    def _local_repos(self) -> list[int]:
        if not self.root.is_dir():
            return []
        ids = []
        for entry in self.root.iterdir():
            prefix, _, value = entry.name.partition("=")
            if prefix == "repo" and value.isdigit() and entry.is_dir():
                ids.append(int(value))
        return ids

    def list_runs(self, repo_id: int) -> list[str]:
        """Every run id stored for ``repo_id``, sorted: the runs in the store's listing (cached for the
        sync interval) plus runs this cache is writing that have no manifest yet; a cached run whose
        manifest is gone (deleted by another replica) is dropped from the cache and from the answer.
        """
        local = self._local_runs(repo_id)
        remote = {
            value
            for name in self._listing(f"{_KEY_ROOT}/repo={repo_id}/")
            for prefix, _, value in [name.partition("=")]
            if prefix == "run" and _RUN_ID.match(value)
        }
        runs = set(remote)
        for run_id in set(local) - remote:
            record = _read_json(self._run_path(repo_id, run_id) / _RECORD_FILE)
            if record is None or record.get("etag") is None:
                if _scan(self._run_path(repo_id, run_id)):
                    runs.add(run_id)  # being written here: not published yet
                continue
            self._ensure(repo_id, run_id)
            if self._run_path(repo_id, run_id).is_dir() and _scan(self._run_path(repo_id, run_id)):
                runs.add(run_id)  # published after the cached listing was taken
        return sorted(runs)

    def _local_runs(self, repo_id: int) -> list[str]:
        repo_dir = self._repo_path(repo_id)
        if not repo_dir.is_dir():
            return []
        runs = []
        for entry in repo_dir.iterdir():
            prefix, _, value = entry.name.partition("=")
            if prefix == "run" and _RUN_ID.match(value) and entry.is_dir():
                runs.append(value)
        return runs

    def has_run(self, repo_id: int, run_id: str) -> bool:
        """Whether the run exists (its manifest, pulled into the cache, or files being written here)."""
        run_dir = self._run_path(repo_id, run_id)
        self._ensure(repo_id, run_id)
        return run_dir.is_dir() and bool(_scan(run_dir))

    def run_modified(self, repo_id: int, run_id: str) -> datetime | None:
        """When the run last changed: its manifest's modification time (else its newest object's, for
        a run whose first publish never happened, else its cached directory's); None if absent."""
        run_dir = self._run_path(repo_id, run_id)
        info = self.blobs.head(self._run_key(repo_id, run_id, MANIFEST_FILE))
        if info is not None:
            return info.modified
        newest = max((o.modified for o in self.blobs.list(self._run_prefix(repo_id, run_id))), default=None)
        if newest is not None:
            return newest
        try:
            return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=UTC)
        except FileNotFoundError:
            return None

    def delete_run(self, repo_id: int, run_id: str) -> bool:
        """Delete one run (never the repo dictionary); returns whether it existed.

        The manifest goes first (readers stop seeing the run at once), then every object under the
        run's prefix, then the cached copy (renamed to a hidden name first, so a local reader never
        sees half of it).
        """
        manifest = self._run_key(repo_id, run_id, MANIFEST_FILE)
        existed = self.blobs.head(manifest) is not None
        self.blobs.delete([manifest])
        existed = self.blobs.delete_prefix(self._run_prefix(repo_id, run_id)) > 0 or existed
        self._forget_listing(repo_id)
        run_dir = self._run_path(repo_id, run_id)
        if not run_dir.is_dir():
            return existed
        doomed = run_dir.with_name(f".deleting-{uuid.uuid4().hex}")
        run_dir.rename(doomed)
        shutil.rmtree(doomed)
        return True

    # -- the blob store: keys, listings, pull and publish -------------------------------------

    def _run_prefix(self, repo_id: int, run_id: str) -> str:
        """``facts/repo=<id>/run=<id>/``: the run's objects."""
        self._run_path(repo_id, run_id)  # validates both ids
        return f"{_KEY_ROOT}/repo={repo_id}/run={run_id}/"

    def _run_key(self, repo_id: int, run_id: str, name: str) -> str:
        return self._run_prefix(repo_id, run_id) + name

    def _rel(self, path: Path) -> str:
        """``path`` (a directory below the root) as a POSIX path relative to it."""
        rel = Path(path).relative_to(self.root).as_posix()
        if rel in ("", ".") or rel.startswith(".."):
            raise ValueError(f"{path} is not below {self.root}")
        return rel

    def _listing(self, prefix: str) -> list[str]:
        """The store's "directories" below ``prefix``, cached for the sync interval."""
        key = (self._root_key, prefix)
        now = time.monotonic()
        found = _LISTINGS.get(key)
        if found is not None and now - found[0] < cache.sync_interval():
            return found[1]
        names = self.blobs.list_dirs(prefix)
        _LISTINGS[key] = (now, names)
        return names

    def _forget_listing(self, repo_id: int) -> None:
        _LISTINGS.pop((self._root_key, f"{_KEY_ROOT}/repo={repo_id}/"), None)
        _LISTINGS.pop((self._root_key, f"{_KEY_ROOT}/"), None)

    def _run_state(self, repo_id: int, run_id: str) -> _SyncState:
        """The run's sync bookkeeping in this process (its record path filled in on first use)."""
        state = _state(("run", self._root_key, str(repo_id), run_id))
        if not state.record:
            state.record = str(self._run_path(repo_id, run_id) / _RECORD_FILE)
        return state

    @staticmethod
    def _fresh(state: _SyncState) -> bool:
        """Within the sync interval of the last check, and the cache still holds what it saw then
        (or that check found nothing, or failed: neither is tried again before the interval)."""
        if time.monotonic() - state.checked >= cache.sync_interval():
            return False
        return state.absent or state.error is not None or os.path.exists(state.record)

    @staticmethod
    def _settled(state: _SyncState) -> None:
        """A fresh state's answer: nothing, or the last check's error again when this cache holds no
        copy to read instead (a reader with a copy keeps reading it until the next check, which
        :meth:`_ensure` applies to the failed check itself too)."""
        if state.error is not None and not os.path.exists(state.record):
            raise state.error

    def _ensure(self, repo_id: int, run_id: str) -> None:
        """Bring the cached run in step with its manifest, unless that was checked moments ago.

        A run whose lock another writer holds (here or in another process on this machine) is read
        as it is: that writer is producing the newer state, and it publishes on release. A check
        that fails is remembered for the sync interval (:meth:`_settled`).
        """
        key = (self._root_key, repo_id, run_id)
        if key in _held():
            return
        state = self._run_state(repo_id, run_id)
        if self._fresh(state):
            self._settled(state)
            return
        run_dir = self._run_path(repo_id, run_id)
        with state.lock:
            if self._fresh(state):
                self._settled(state)
                return
            try:
                head: ObjectInfo | None | _Unset = _UNSET
                if not run_dir.is_dir():
                    head = self.blobs.head(self._run_key(repo_id, run_id, MANIFEST_FILE))
                    if head is None:
                        state.absent, state.error, state.checked = True, None, time.monotonic()
                        return
                    run_dir.mkdir(parents=True, exist_ok=True)
                with open(run_dir / _LOCK_FILE, "a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return  # a writer holds the run: read the cache as it is, check again next time
                    try:
                        self._pull_run(repo_id, run_id, writer=False, head=head)
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except Exception as exc:
                state.error, state.checked = exc, time.monotonic()
                if not os.path.exists(state.record):
                    raise
                log.warning("run %s: checking its manifest failed, reading the cached copy: %s", run_id, exc)
                return
            state.error, state.checked = None, time.monotonic()

    def _ensure_many(self, scope: Sequence[tuple[int, str]]) -> None:
        """:meth:`_ensure` for every run of a scope, the stale ones in parallel."""
        stale = [(repo, run) for repo, run in scope if not self._fresh(self._run_state(repo, run))]
        if len(stale) <= 1:
            for repo, run in stale:
                self._ensure(repo, run)
            return
        list(transfer_pool("sync").map(lambda pair: self._ensure(*pair), stale))

    def _pull_run(
        self, repo_id: int, run_id: str, *, writer: bool, head: ObjectInfo | None | _Unset = _UNSET
    ) -> None:
        """Make the cached run exactly what its manifest says (the caller holds the run lock;
        ``head`` is the manifest's metadata when the caller just fetched it)."""
        run_dir = self._run_path(repo_id, run_id)
        state = _state(("run", self._root_key, str(repo_id), run_id))
        manifest_key = self._run_key(repo_id, run_id, MANIFEST_FILE)
        record = _read_json(run_dir / _RECORD_FILE)
        seen = record.get("etag") if record else None
        info = self.blobs.head(manifest_key) if isinstance(head, _Unset) else head
        if info is None:
            if seen is not None:
                # Deleted from the store (gc or purge on another replica): drop the cached copy.
                for name in _scan(run_dir):
                    (run_dir / name).unlink(missing_ok=True)
                (run_dir / _RECORD_FILE).unlink(missing_ok=True)
                (run_dir / MANIFEST_FILE).unlink(missing_ok=True)
                self._forget_listing(repo_id)
                if not writer:
                    shutil.rmtree(run_dir, ignore_errors=True)
            state.absent = True
            return
        state.absent = False
        if info.etag == seen:
            os.utime(run_dir / _RECORD_FILE)  # the cache's least-recently-used clock
            return
        for attempt in range(_DOWNLOAD_ATTEMPTS):
            data, etag = self.blobs.get_bytes(manifest_key)
            if self._apply_manifest(repo_id, run_id, data, etag, record):
                return
            time.sleep(0.25 * 2**attempt)  # the manifest was replaced mid-pull: read the new one
        # Objects are never rewritten, so after three fresh manifests this is not a race: an object
        # the current manifest names is gone or differs (deleted or replaced behind coverset's back).
        raise RunWriteConflict(
            f"run {run_id}: its manifest names an object that is missing or does not match it"
        )

    def _apply_manifest(
        self, repo_id: int, run_id: str, data: bytes, etag: str, record: Mapping[str, Any] | None
    ) -> bool:
        """Download what changed and swap it in; False (nothing changed) when an object's etag no
        longer matches the manifest (a newer publish replaced it after the manifest was read)."""
        blobs = self.blobs
        manifest = json.loads(data)
        files: dict[str, dict[str, Any]] = manifest.get("files") or {}
        keys: dict[str, str] = {}
        for name, entry in files.items():
            if not _FILE_NAME.match(name):
                raise ValueError(f"run {run_id}: unexpected file name {name!r} in its manifest")
            keys[name] = _entry_key(name, entry)
        run_dir = self._run_path(repo_id, run_id)
        local = _scan(run_dir)
        known: Mapping[str, list[Any]] = (record or {}).get("files") or {}
        need = [
            name
            for name, entry in files.items()
            if not (
                name in local
                and name in known
                and known[name][3] == entry.get("etag")
                and tuple(known[name][:3]) == local[name]
            )
        ]

        def fetch(name: str) -> tuple[str, Path, str | None]:
            tmp = run_dir / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                return name, tmp, blobs.get_file(self._run_key(repo_id, run_id, keys[name]), tmp)
            except BlobNotFound:
                return name, tmp, None  # superseded (and deleted) since this manifest was read

        fetched: list[tuple[str, Path, str | None]] = []
        try:
            for result in transfer_pool("io").map(fetch, need):
                fetched.append(result)
            if any(got != files[name].get("etag") for name, _, got in fetched):
                return False
            for name, tmp, _ in fetched:
                mtime = int(files[name].get("mtime_ns") or time.time_ns())
                os.utime(tmp, ns=(mtime, mtime))  # the writer's mtime: summary freshness compares them
                os.replace(tmp, run_dir / name)
        finally:
            for _, tmp, _ in fetched:
                tmp.unlink(missing_ok=True)
        for name in local:
            if name not in files:
                (run_dir / name).unlink(missing_ok=True)
        now = _scan(run_dir)
        new_record = {
            "etag": etag,
            "version": manifest.get("version"),
            "created_at": manifest.get("created_at"),
            "files": {
                name: [*now[name], entry.get("etag"), keys[name]]
                for name, entry in files.items()
                if name in now
            },
        }
        atomic_write_bytes(run_dir / _RECORD_FILE, json.dumps(new_record).encode())
        atomic_write_bytes(run_dir / MANIFEST_FILE, data)
        if need:
            _state(("dict", self._root_key, str(repo_id))).checked = float("-inf")  # new ids may be in use
            self._maybe_evict()
        return True

    def _maybe_evict(self) -> None:
        """Keep this host's cache within ``COVERSET_CACHE_MAX_GB`` (:mod:`coverset.storage.cachedir`):
        after a pull that downloaded, at most once per :data:`EVICT_INTERVAL_S` per process, in the
        background. The first download of a process runs it, which is the start-up pass; ``coverset
        gc`` runs it too, but only one replica runs gc, and every replica has its own cache."""
        global _last_eviction
        now = time.monotonic()
        with _STATES_LOCK:
            if now - _last_eviction < EVICT_INTERVAL_S:
                return
            _last_eviction = now
        from coverset.storage.cachedir import evict_dir

        def run() -> None:
            try:
                report = evict_dir(self.root.parent)
                if report.removed:
                    log.info("%s", report.line())
            except Exception:
                log.warning("cache eviction under %s failed", self.root.parent, exc_info=True)

        transfer_pool("sync").submit(run)

    def _push_run(self, repo_id: int, run_id: str) -> None:
        """Upload what changed since the last pull or publish as new objects, then the manifest,
        last, with ``If-Match``; then delete the objects it superseded (the caller holds the lock).

        A failure before the manifest is written changes nothing a reader can see: the objects
        already uploaded are unreferenced (a lost race deletes them at once; after any other error
        the next publish uploads new ones and the run's deletion removes them all).
        """
        blobs = self.blobs
        run_dir = self._run_path(repo_id, run_id)
        record = _read_json(run_dir / _RECORD_FILE) or {}
        known: Mapping[str, list[Any]] = record.get("files") or {}
        seen = record.get("etag")
        local = _scan(run_dir)
        changed = [name for name, st in local.items() if name not in known or tuple(known[name][:3]) != st]
        removed = [name for name in known if name not in local]
        if not changed and not removed and (seen is not None or not local):
            return
        version = int(record.get("version") or 0) + 1
        keys = {name: _known_key(name, known) for name in local if name not in changed}
        keys.update({name: _object_name(name, version) for name in changed})
        created_at = record.get("created_at")
        if "runs_dim.parquet" in changed or (created_at is None and "runs_dim.parquet" in local):
            created_at = _created_at_of(run_dir / "runs_dim.parquet") or created_at

        def upload(name: str) -> tuple[str, str]:
            return name, blobs.put_file(self._run_key(repo_id, run_id, keys[name]), run_dir / name)

        uploaded = dict(transfer_pool("io").map(upload, changed))
        files = {
            name: {
                "size": st[0],
                "mtime_ns": st[1],
                "etag": uploaded.get(name) or known[name][3],
                "key": keys[name],
            }
            for name, st in sorted(local.items())
        }
        manifest = {
            "format": _MANIFEST_FORMAT,
            "repo_id": repo_id,
            "run_id": run_id,
            "version": version,
            "created_at": created_at,
            "written_at": datetime.now(UTC).isoformat(),
            "writer": _writer(),
            "files": files,
        }
        data = json.dumps(manifest, indent=1, sort_keys=True).encode()
        manifest_key = self._run_key(repo_id, run_id, MANIFEST_FILE)
        try:
            if seen is None:
                etag = blobs.put_bytes(manifest_key, data, if_none_match=True)
            else:
                etag = blobs.put_bytes(manifest_key, data, if_match=seen)
        except PreconditionFailed as exc:
            self._delete_objects(repo_id, run_id, [keys[name] for name in changed], "a lost race's")
            raise RunWriteConflict(
                f"run {run_id} was published by another writer since this one pulled it ({exc})"
            ) from exc
        superseded = [_known_key(name, known) for name in removed]
        superseded += [_known_key(name, known) for name in changed if name in known]
        self._delete_objects(repo_id, run_id, superseded, "superseded")
        new_record = {
            "etag": etag,
            "version": version,
            "created_at": created_at,
            "files": {name: [*local[name], files[name]["etag"], keys[name]] for name in local},
        }
        atomic_write_bytes(run_dir / _RECORD_FILE, json.dumps(new_record).encode())
        atomic_write_bytes(run_dir / MANIFEST_FILE, data)
        state = _state(("run", self._root_key, str(repo_id), run_id))
        state.absent, state.error, state.checked = False, None, time.monotonic()
        if seen is None:
            self._forget_listing(repo_id)

    def _delete_objects(self, repo_id: int, run_id: str, names: list[str], what: str) -> None:
        """Best-effort deletion of run objects no manifest references (they only cost space until
        the run is deleted, whose prefix deletion removes everything)."""
        if not names:
            return
        try:
            self.blobs.delete([self._run_key(repo_id, run_id, name) for name in names])
        except Exception:
            log.warning("run %s: deleting %d %s objects failed", run_id, len(names), what, exc_info=True)

    def remote_manifest(self, repo_id: int, run_id: str) -> dict[str, Any] | None:
        """The run's manifest as the store holds it right now, read with one GET and without pulling
        the run (retention ages runs with it); None when there is none (an unpublished run, or no
        run at all)."""
        try:
            data, _ = self.blobs.get_bytes(self._run_key(repo_id, run_id, MANIFEST_FILE))
        except BlobNotFound:
            return None
        manifest = json.loads(data)
        return manifest if isinstance(manifest, dict) else None

    def manifest(self, repo_id: int, run_id: str) -> dict[str, Any] | None:
        """The run's manifest as this cache last wrote or pulled it."""
        self._ensure(repo_id, run_id)
        return _read_json(self._run_path(repo_id, run_id) / MANIFEST_FILE)

    def _ensure_dictionary(self, repo_id: int, repo_dir: Path) -> None:
        """Pull the repo's ``dictionary.json`` when it changed (at most once per sync interval)."""
        state = _state(("dict", self._root_key, str(repo_id)))
        if time.monotonic() - state.checked < cache.sync_interval():
            return
        with state.lock:
            if time.monotonic() - state.checked < cache.sync_interval():
                return
            RepoDictionary.pull(repo_dir, self.blobs)
            state.checked = time.monotonic()

    # -- immutable directories (production batches) ------------------------------------------

    def pull_batches(self, directory: Path, *, marker: str) -> None:
        """Mirror the immutable batch directories below ``directory`` from the store.

        A batch (any directory holding ``marker``, uploaded last by :meth:`push_tree`) is downloaded
        whole into a hidden staging directory and renamed into place, so a reader sees all of it or
        none; a cached batch the store no longer has is removed. At most once per sync interval.
        """
        rel = self._rel(directory)
        state = _state(("tree", self._root_key, rel))
        if time.monotonic() - state.checked < cache.sync_interval():
            return
        with state.lock:
            if time.monotonic() - state.checked < cache.sync_interval():
                return
            prefix = f"{_KEY_ROOT}/{rel}/"
            groups: dict[str, list[str]] = {}
            for info in self.blobs.list(prefix):
                sub, _, name = info.key[len(prefix) :].rpartition("/")
                if sub:
                    groups.setdefault(sub, []).append(name)
            remote = {sub for sub, names in groups.items() if marker in names}
            local_root = Path(directory)
            local = {
                found.parent.relative_to(local_root).as_posix()
                for found in local_root.rglob(marker)
                if not any(part.startswith(".") for part in found.relative_to(local_root).parts)
            }
            for sub in sorted(remote - local):
                target = local_root / sub
                staging = target.with_name(f".staging-{uuid.uuid4().hex}")
                staging.mkdir(parents=True)
                try:
                    for name in groups[sub]:
                        self.blobs.get_file(f"{prefix}{sub}/{name}", staging / name)
                    staging.rename(target)
                except BaseException:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            for sub in sorted(local - remote):
                self._drop_local(local_root / sub)
            state.checked = time.monotonic()

    def push_tree(self, directory: Path, *, last: str | None = None) -> None:
        """Upload every file below ``directory`` (``last`` after all the others)."""
        blobs = self.blobs
        rel = self._rel(directory)
        files = sorted(
            p
            for p in Path(directory).rglob("*")
            if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
        )
        first = [p for p in files if p.name != last]
        final = [p for p in files if p.name == last]

        def upload(path: Path) -> str:
            return blobs.put_file(f"{_KEY_ROOT}/{rel}/{path.relative_to(directory).as_posix()}", path)

        list(transfer_pool("io").map(upload, first))
        for path in final:
            upload(path)
        for parent in Path(rel).parents:  # the next pull of any enclosing tree lists the store again
            _state(("tree", self._root_key, parent.as_posix())).checked = float("-inf")

    def remove_tree(self, directory: Path) -> None:
        """Delete a directory below the root: its objects first, then the local copy (renamed to a
        hidden name, then removed)."""
        self.blobs.delete_prefix(f"{_KEY_ROOT}/{self._rel(directory)}/")
        self._drop_local(Path(directory))

    @staticmethod
    def _drop_local(directory: Path) -> None:
        if not directory.is_dir() or directory.is_symlink():
            return
        hidden = directory.with_name(f".deleting-{uuid.uuid4().hex}")
        directory.rename(hidden)
        shutil.rmtree(hidden, ignore_errors=True)

    # -- DuckDB ------------------------------------------------------------------------------

    def _scope(
        self, repo_id: int | None, run_id: str | None, run_ids: Sequence[str] | None
    ) -> list[tuple[int, str]]:
        if (run_id is not None or run_ids is not None) and repo_id is None:
            raise ValueError("run_id and run_ids require repo_id")
        if run_id is not None and run_ids is not None:
            raise ValueError("pass run_id or run_ids, not both")
        if repo_id is None:
            return [(repo, run) for repo in self.list_repos() for run in self.list_runs(repo)]
        if run_id is not None:
            return [(repo_id, run_id)]
        if run_ids is not None:
            return [(repo_id, run) for run in dict.fromkeys(run_ids)]
        return [(repo_id, run) for run in self.list_runs(repo_id)]

    def connect(
        self,
        *,
        repo_id: int | None = None,
        run_id: str | None = None,
        run_ids: Sequence[str] | None = None,
        tables: Iterable[str] | None = None,
    ) -> duckdb.DuckDBPyConnection:
        """A new in-memory DuckDB connection with one view per facts table.

        Views cover every repo and run, or only ``repo_id`` (and ``run_id``, or the runs in
        ``run_ids``) when given; each view reads an explicit list of the visible files (committed
        attempts only) and adds ``repo_id`` and ``run_id`` from the partition directories.
        ``tables`` limits which views are created (all by default), which keeps per-request
        connections cheap. Extension autoload/autoinstall is off, file access is limited to the
        facts directory and the configuration is locked. The connection itself can still write
        inside the facts directory (``COPY ... TO``): run user-supplied SQL through
        :meth:`query`, which accepts only a single SELECT. A scoped connection may read files only
        under the repo's directory (or the runs' directories), never another repository's.
        """
        wanted = TABLES if tables is None else tuple(tables)
        if unknown := set(wanted) - set(TABLES):
            raise ValueError(f"unknown facts tables {sorted(unknown)}")
        scope = self._scope(repo_id, run_id, run_ids)
        con = duckdb.connect(
            config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
        )
        try:
            con.execute("SET allowed_directories = ?", [self._allowed(repo_id, run_id, run_ids)])
            con.execute("SET enable_external_access = false")
            con.execute(f"SET memory_limit = '{cache.memory_limit()}'")
            con.execute(f"SET threads = {cache.threads()}")
            files: dict[str, list[Path]] = {table: [] for table in wanted}
            for repo, run in scope:
                for table, visible in self._visible(repo, run, wanted).items():
                    files[table].extend(visible)
            for table in wanted:
                self._create_view(con, table, files[table])
            con.execute("SET lock_configuration = true")
        except BaseException:
            con.close()
            raise
        return con

    def stamp(
        self, *, repo_id: int | None = None, run_id: str | None = None, run_ids: Sequence[str] | None = None
    ) -> tuple[tuple[int, str, cache.RunStamp], ...]:
        """The stamps of every run in scope (see :mod:`coverset.facts.cache`): equal stamps mean
        every file a reader of the scope could see is byte-for-byte the same."""
        scope = self._scope(repo_id, run_id, run_ids)
        self._ensure_many(scope)
        return tuple((repo, run, cache.run_stamp(self._run_path(repo, run))) for repo, run in scope)

    @contextmanager
    def lease(
        self,
        *,
        repo_id: int | None = None,
        run_id: str | None = None,
        run_ids: Sequence[str] | None = None,
        tables: Iterable[str] | None = None,
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        """A cursor of the cached connection for this scope (:meth:`connect`'s views), closed on exit.

        The connection is rebuilt when any run in scope changed since it was built (its stamp
        differs), so a reader never sees a superseded attempt for longer than one query that was
        already running. Use it like a connection: it can execute, register Arrow tables (visible
        to this cursor only) and close; it cannot change the locked configuration.
        """
        wanted = TABLES if tables is None else tuple(tables)
        stamp = self.stamp(repo_id=repo_id, run_id=run_id, run_ids=run_ids)  # before building: never stale
        # The allowed directories are part of the key: two scopes with the same runs (two run-less
        # repos, or the unscoped and the repo scope of a one-repo store) must not share a
        # connection, or one would read with the other's file access.
        allowed = tuple(self._allowed(repo_id, run_id, run_ids))
        key = cache.scope_key(self.root.resolve(), [(repo, run) for repo, run, _ in stamp], (wanted, allowed))
        cursor = cache.CONNECTIONS.lease(
            key, stamp, lambda: self.connect(repo_id=repo_id, run_id=run_id, run_ids=run_ids, tables=wanted)
        )
        try:
            yield cursor
        finally:
            cursor.close()

    def _allowed(self, repo_id: int | None, run_id: str | None, run_ids: Sequence[str] | None) -> list[str]:
        """The directories a connection may read: the facts root when it spans every repo, else
        only the repo's directory (a scoped connection, and the user SQL run on it, can never
        ``read_parquet`` another repository's files), else only the runs in scope."""
        if repo_id is None:
            return [str(self.root.resolve()) + "/"]
        if run_id is None and run_ids is None:
            return [str(self._repo_path(repo_id).resolve()) + "/"]
        runs = [run_id] if run_id is not None else list(dict.fromkeys(run_ids or ()))
        return [str(self._run_path(repo_id, run).resolve()) + "/" for run in runs] or [
            str(self._repo_path(repo_id).resolve()) + "/"
        ]

    def query(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
        *,
        repo_id: int | None = None,
        run_id: str | None = None,
    ) -> pa.Table:
        """Run one user-supplied read-only SELECT over the views (``coverset facts query``).

        Raises :class:`SqlNotAllowed` for anything but exactly one SELECT statement, so ad-hoc SQL
        can neither write files (``COPY``) nor change the connection.
        """
        with self.lease(repo_id=repo_id, run_id=run_id) as con:
            statements = con.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise SqlNotAllowed("only a single SELECT statement is allowed")
            return con.execute(statements[0], params or ()).to_arrow_table()

    def _create_view(self, con: duckdb.DuckDBPyConnection, table: str, files: list[Path]) -> None:
        if files:
            listed = ", ".join(_sql_str(str(f.resolve())) for f in files)
            source = (
                f"read_parquet([{listed}], hive_partitioning=true, "
                f"union_by_name=true, hive_types={_HIVE_TYPES})"
            )
            con.execute(
                f"CREATE VIEW {table} AS SELECT * EXCLUDE (repo, run), "
                f"repo AS repo_id, run AS run_id FROM {source}"
            )
            return
        # A real (empty) table, not a registered Arrow object: registrations are local to one
        # connection, and leased cursors of a cached connection must see this view too.
        empty = f"_empty_{table}"
        con.register(f"{empty}_arrow", SCHEMAS[table].empty_table())
        con.execute(f"CREATE TABLE {empty} AS SELECT * FROM {empty}_arrow")
        con.unregister(f"{empty}_arrow")
        con.execute(
            f"CREATE VIEW {table} AS SELECT *, NULL::BIGINT AS repo_id, NULL::VARCHAR AS run_id FROM {empty}"
        )


def facts_store(settings: Settings) -> FactsStore:
    """The facts store ``settings`` name: the cache ``Settings.facts_dir`` of the blob store
    (:func:`coverset.storage.from_settings`). Every caller that has settings builds its store here."""
    from coverset.storage import from_settings

    return FactsStore(settings.facts_dir, from_settings(settings))
