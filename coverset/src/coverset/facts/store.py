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
"""

from __future__ import annotations

import fcntl
import functools
import re
import shutil
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from coverset.facts.atomic import atomic_write
from coverset.facts.dictionary import RepoDictionary
from coverset.facts.schema import ATTEMPT_TABLES, RUN_TABLES, SCHEMAS, TABLES

__all__ = ["FactsStore", "SqlNotAllowed", "conform"]

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HIVE_TYPES = "{'repo': BIGINT, 'run': VARCHAR}"
_LOCK_FILE = ".lock"
_LOG = "ingest_log"


class SqlNotAllowed(ValueError):
    """Ad-hoc SQL passed to :meth:`FactsStore.query` is not a single read-only SELECT."""


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
    """Immutable per-run Parquet facts under one directory."""

    def __init__(self, facts_dir: Path) -> None:
        self.root = Path(facts_dir)

    # -- layout ------------------------------------------------------------------------------

    def repo_dir(self, repo_id: int) -> Path:
        """The directory holding every run (and the dictionary) of ``repo_id``."""
        if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id < 0:
            raise ValueError(f"invalid repo id {repo_id!r}")
        return self.root / f"repo={repo_id}"

    def run_dir(self, repo_id: int, run_id: str) -> Path:
        """The directory holding one run's tables."""
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            raise ValueError(f"invalid run id {run_id!r}")
        return self.repo_dir(repo_id) / f"run={run_id}"

    def table_path(
        self, repo_id: int, run_id: str, table: str, shard_id: int | None = None, attempt: int | None = None
    ) -> Path:
        """Where one table file lives (shard tables need ``shard_id`` and, except ``ingest_log``, ``attempt``)."""
        run_dir = self.run_dir(repo_id, run_id)
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
        return self._logs(self.run_dir(repo_id, run_id), self._names(repo_id, run_id))

    def attempt_files(self, repo_id: int, run_id: str, shard_id: int) -> dict[int, list[Path]]:
        """Every shard-table file of one shard (committed or not), by attempt."""
        pattern = re.compile(rf"^(\w+)\.shard={shard_id}\.attempt=(\d+)\.parquet$")
        run_dir = self.run_dir(repo_id, run_id)
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
        run_dir = self.run_dir(repo_id, run_id)
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
        run_dir = self.run_dir(repo_id, run_id)
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
        return RepoDictionary.load(self.repo_dir(repo_id))

    @contextmanager
    def run_lock(self, repo_id: int, run_id: str) -> Iterator[None]:
        """Exclusive lock serialising writers (ingest, failure records, finalize) of one run."""
        run_dir = self.run_dir(repo_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with open(run_dir / _LOCK_FILE, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
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
        return path

    def delete_table(self, repo_id: int, run_id: str, table: str) -> bool:
        """Remove a run table's file; returns whether it existed."""
        if table not in RUN_TABLES:
            raise ValueError(f"only run tables can be deleted, not {table!r}")
        try:
            self.table_path(repo_id, run_id, table).unlink()
        except FileNotFoundError:
            return False
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
        """Every repo id with a directory in the store."""
        if not self.root.is_dir():
            return []
        ids = []
        for entry in self.root.iterdir():
            prefix, _, value = entry.name.partition("=")
            if prefix == "repo" and value.isdigit() and entry.is_dir():
                ids.append(int(value))
        return sorted(ids)

    def list_runs(self, repo_id: int) -> list[str]:
        """Every run id stored for ``repo_id``, sorted."""
        repo_dir = self.repo_dir(repo_id)
        if not repo_dir.is_dir():
            return []
        runs = []
        for entry in repo_dir.iterdir():
            prefix, _, value = entry.name.partition("=")
            if prefix == "run" and _RUN_ID.match(value) and entry.is_dir():
                runs.append(value)
        return sorted(runs)

    def delete_run(self, repo_id: int, run_id: str) -> bool:
        """Delete one run's directory (never the repo dictionary); returns whether it existed.

        The directory is first renamed to a hidden name so readers never see a half-deleted run.
        """
        run_dir = self.run_dir(repo_id, run_id)
        if not run_dir.is_dir():
            return False
        doomed = run_dir.with_name(f".deleting-{uuid.uuid4().hex}")
        run_dir.rename(doomed)
        shutil.rmtree(doomed)
        return True

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
        :meth:`query`, which accepts only a single SELECT.
        """
        wanted = TABLES if tables is None else tuple(tables)
        if unknown := set(wanted) - set(TABLES):
            raise ValueError(f"unknown facts tables {sorted(unknown)}")
        scope = self._scope(repo_id, run_id, run_ids)
        con = duckdb.connect(
            config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
        )
        try:
            root = str(self.root.resolve())
            con.execute("SET allowed_directories = ?", [[root + "/"]])
            con.execute("SET enable_external_access = false")
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
        con = self.connect(repo_id=repo_id, run_id=run_id)
        try:
            statements = con.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise SqlNotAllowed("only a single SELECT statement is allowed")
            return con.execute(statements[0], params or ()).to_arrow_table()
        finally:
            con.close()

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
        empty = f"_empty_{table}"
        con.register(empty, SCHEMAS[table].empty_table())
        con.execute(
            f"CREATE VIEW {table} AS SELECT *, NULL::BIGINT AS repo_id, NULL::VARCHAR AS run_id FROM {empty}"
        )
