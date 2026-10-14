"""Engine and session factories for SQLite and Postgres, and programmatic Alembic migrations.

Two backends, one schema. SQLite (``sqlite+aiosqlite``) is the zero-service default for laptops and
the demo: WAL, a 5 s busy timeout, enforced foreign keys. Postgres (``postgresql+asyncpg``) is the
hosted backend: a bounded connection pool per process, ``application_name=coverset`` and
``timezone=UTC`` on every connection, and (for the web app) a per-statement timeout. Everything above
this module is backend-neutral; the differences live here, in :mod:`coverset.db.upsert` and in
:mod:`coverset.db.leader`. docs/postgres.md is the operator's view.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from coverset.config import Settings

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
"""Alembic script directory, resolved from the installed package (no ``alembic.ini`` needed)."""

SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)
"""Applied to every new SQLite connection, in order."""

POSTGRES_POOL: dict[str, Any] = {
    "pool_size": 10,
    "max_overflow": 10,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}
"""The Postgres connection pool of each process: 10 kept open, up to 10 more under load (so at most
20 connections per replica), each checked with a ping before use (a server restart or an idle
timeout on a proxy is noticed before a request fails) and replaced after 30 minutes."""

POSTGRES_SERVER_SETTINGS = {
    "application_name": "coverset",
    "timezone": "UTC",
    "tcp_keepalives_idle": "60",
    "tcp_keepalives_interval": "10",
    "tcp_keepalives_count": "3",
}
"""Session settings sent with every Postgres connection: the name ``pg_stat_activity`` shows, UTC so
that anything the server renders as text (logs, ``now()::text``) matches the app, and server-side
TCP keepalives so that a client host that vanishes (VM crash, power loss, a network partition) is
noticed within 60 + 3 x 10 = 90 s. Without them the server keeps the dead session, and with it the
leader lease (:mod:`coverset.db.leader`) and any row locks, for the OS default: on Linux 7200 s idle
plus 9 probes 75 s apart, about 2 h 11 min. The three are user-settable GUCs on every supported
Postgres; where the transport has no keepalives (a Unix socket, Windows) the server logs and ignores
them. A session-mode PgBouncer must list them in ``ignore_startup_parameters`` (docs/postgres.md)."""

WEB_STATEMENT_TIMEOUT_S = 30.0
"""``statement_timeout`` of the web app's Postgres connections: no request (and no background task
of the web process) waits more than 30 s on one statement. CLI commands, migrations
(:func:`run_schema_change` lifts it for the migration's transaction) and ``coverset db copy`` run
without one."""

ADVISORY_MIGRATION_LOCK = "coverset:migrate"
"""Name of the transaction-scoped advisory lock a Postgres migration takes, so two replicas that
start together migrate one after the other (the second finds the schema at head)."""

_DROPPED_POSTGRES_QUERY = ("channel_binding",)
"""libpq URL parameters asyncpg does not take; hosted providers (Neon) put them in pasted URLs."""

log = logging.getLogger(__name__)

LOG_LEVEL_ENV = "COVERSET_LOG_LEVEL"
"""``COVERSET_LOG_LEVEL=DEBUG`` keeps Alembic's own INFO lines (see :func:`quiet_alembic`)."""

Backend = Literal["sqlite", "postgresql"]


def _is_sqlite(url: URL) -> bool:
    return url.get_backend_name() == "sqlite"


def _is_sqlite_memory(url: URL) -> bool:
    return _is_sqlite(url) and (url.database in (None, "", ":memory:") or url.query.get("mode") == "memory")


def backend_of(url: str | URL) -> str:
    """The backend name of ``url``: ``"sqlite"``, ``"postgresql"`` (or another dialect's name)."""
    return make_url(url).get_backend_name()


def is_postgres(url: str | URL) -> bool:
    """True for a Postgres URL (any driver; the settings normalise it to asyncpg)."""
    return backend_of(url) == "postgresql"


def sqlite_file_path(url: str | URL) -> Path | None:
    """The database file behind a file-backed SQLite URL, or None for other URLs and in-memory DBs.

    None for a Postgres URL too: callers that chmod, back up, wipe or probe the file skip it then.
    """
    parsed = make_url(url)
    if not _is_sqlite(parsed) or _is_sqlite_memory(parsed) or not parsed.database:
        return None
    return Path(parsed.database)


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in SQLITE_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def postgres_connect_args(
    url: URL, *, statement_timeout_s: float | None = None
) -> tuple[URL, dict[str, Any]]:
    """``url`` made acceptable to asyncpg, and the ``connect_args`` for it.

    Hosted providers hand out libpq URLs (``?sslmode=require&channel_binding=require``), which
    asyncpg refuses as unknown keywords: ``sslmode`` becomes asyncpg's ``ssl`` (same values),
    ``connect_timeout`` its ``timeout``, and ``channel_binding`` is dropped (asyncpg negotiates
    SCRAM itself). An ``application_name`` in the URL wins over coverset's. The server settings
    (:data:`POSTGRES_SERVER_SETTINGS`, plus ``statement_timeout`` when given) go in
    ``server_settings``, so they apply from the connection's first statement.
    """
    query = dict(url.query)
    server_settings = dict(POSTGRES_SERVER_SETTINGS)
    if statement_timeout_s:
        server_settings["statement_timeout"] = str(int(statement_timeout_s * 1000))
    connect_args: dict[str, Any] = {}
    if "sslmode" in query:
        connect_args["ssl"] = query.pop("sslmode")
    if "connect_timeout" in query:
        connect_args["timeout"] = float(str(query.pop("connect_timeout")))
    if "application_name" in query:
        server_settings["application_name"] = str(query.pop("application_name"))
    for name in _DROPPED_POSTGRES_QUERY:
        query.pop(name, None)
    connect_args["server_settings"] = server_settings
    return url.set(query=query), connect_args


def make_engine(
    url: str | URL, *, echo: bool = False, statement_timeout_s: float | None = None
) -> AsyncEngine:
    """Create an async engine for SQLite or Postgres.

    SQLite connections get WAL, ``synchronous=NORMAL``, a 5 s busy timeout and enforced foreign
    keys; in-memory SQLite shares one connection (``StaticPool``) so every session sees the same
    database; ``statement_timeout_s`` does not apply (SQLite has none). Postgres gets
    :data:`POSTGRES_POOL`, :data:`POSTGRES_SERVER_SETTINGS` and, when ``statement_timeout_s`` is
    given, a server-side ``statement_timeout`` on every connection (the web app passes
    :data:`WEB_STATEMENT_TIMEOUT_S`); see :func:`postgres_connect_args` for the URL handling.
    """
    parsed = make_url(url)
    kwargs: dict[str, Any] = {"echo": echo}
    if _is_sqlite_memory(parsed):
        kwargs["poolclass"] = StaticPool
    if parsed.get_backend_name() == "postgresql":
        parsed, connect_args = postgres_connect_args(parsed, statement_timeout_s=statement_timeout_s)
        kwargs.update(POSTGRES_POOL)
        kwargs["connect_args"] = connect_args
    engine = create_async_engine(parsed, **kwargs)
    if _is_sqlite(parsed):
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory whose objects stay usable after commit (``expire_on_commit=False``)."""
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(frozen=True, slots=True)
class Database:
    """An engine and its session factory, as stored in ``app.extensions["db"]``."""

    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    @classmethod
    def from_url(
        cls, url: str | URL, *, echo: bool = False, statement_timeout_s: float | None = None
    ) -> Database:
        """Create the engine (see :func:`make_engine`) and a matching session factory."""
        engine = make_engine(url, echo=echo, statement_timeout_s=statement_timeout_s)
        return cls(engine=engine, sessionmaker=make_sessionmaker(engine))

    @property
    def backend(self) -> str:
        """``"sqlite"`` or ``"postgresql"``."""
        return self.engine.dialect.name

    def session(self) -> AsyncSession:
        """A new session; use as ``async with db.session() as session``."""
        return self.sessionmaker()


def alembic_config(connection: Connection | None = None) -> Config:
    """An Alembic config pointing at the packaged migrations, optionally bound to ``connection``."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


@functools.cache
def head_revisions() -> frozenset[str]:
    """The packaged migrations' head revision(s), read once per process (the dev server restarts
    on any ``.py`` change, so a new revision file is seen)."""
    return frozenset(ScriptDirectory.from_config(alembic_config()).get_heads())


def advisory_key(name: str) -> int:
    """A stable signed 64-bit key for ``name`` (Postgres advisory locks take a ``bigint``)."""
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)


class MigrationError(RuntimeError):
    """A schema change left rows that violate a foreign key; it was rolled back."""


def _driver_execute(connection: Connection, statement: str) -> None:
    """Run ``statement`` on the DBAPI connection, bypassing SQLAlchemy's transaction tracking."""
    cursor = connection.connection.cursor()
    try:
        cursor.execute(statement)
    finally:
        cursor.close()


def _run_sqlite_schema_change(connection: Connection, change: Callable[[Connection], None]) -> None:
    """Run ``change`` atomically with foreign keys off (see :func:`run_schema_change`)."""
    dbapi_connection = connection.connection.dbapi_connection
    if dbapi_connection is None:
        raise RuntimeError("the connection has no live DBAPI connection")
    previous_isolation = dbapi_connection.isolation_level
    # The driver's legacy mode never BEGINs before DDL; manage the transaction explicitly instead.
    dbapi_connection.isolation_level = None
    # foreign_keys can only change outside a transaction.
    _driver_execute(connection, "PRAGMA foreign_keys=OFF")
    try:
        with connection.begin():
            _driver_execute(connection, "BEGIN")
            change(connection)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                tables = sorted({str(row[0]) for row in violations})
                raise MigrationError(f"foreign key violations after the schema change in: {tables}")
    finally:
        _driver_execute(connection, "PRAGMA foreign_keys=ON")
        dbapi_connection.isolation_level = previous_isolation


def _run_schema_change_sync(connection: Connection, change: Callable[[Connection], None]) -> None:
    if connection.dialect.name == "sqlite":
        _run_sqlite_schema_change(connection, change)
        return
    with connection.begin():
        if connection.dialect.name == "postgresql":
            # No web statement timeout for DDL, and one migrating process at a time (the lock is
            # released when this transaction ends).
            connection.exec_driver_sql("SET LOCAL statement_timeout = 0")
            connection.exec_driver_sql(
                f"SELECT pg_advisory_xact_lock({advisory_key(ADVISORY_MIGRATION_LOCK)})"
            )
        change(connection)


async def run_schema_change(engine: AsyncEngine, change: Callable[[Connection], None]) -> None:
    """Run the synchronous schema change ``change`` in one transaction on its own connection.

    On SQLite the transaction is real for DDL too (a failed change leaves no half-built tables)
    and foreign keys are off while it runs: batch migrations copy, drop and rename tables, and with
    foreign keys on, dropping a parent table would cascade-delete or null its children's rows.
    ``PRAGMA foreign_key_check`` runs before commit and raises :class:`MigrationError` (rolling
    back) if any row is left dangling.

    On Postgres DDL is transactional anyway and batch operations are plain ``ALTER TABLE``s
    (Alembic's batch mode passes them through); the transaction takes the advisory lock
    :data:`ADVISORY_MIGRATION_LOCK` first and lifts any ``statement_timeout``.
    """
    async with engine.connect() as connection:
        await connection.run_sync(_run_schema_change_sync, change)


async def lift_statement_timeout(session: AsyncSession) -> None:
    """``SET LOCAL statement_timeout = 0`` for ``session``'s current transaction (Postgres only).

    For the bulk work the web process does on its own engine, whose connections carry
    :data:`WEB_STATEMENT_TIMEOUT_S`: the retention gc deletes a day's backlog of runs and run
    events with one statement per table, and once that backlog grows past what 30 s can delete
    (after a gc outage or a retention change) every pass would be cancelled and the backlog would
    never shrink. ``SET LOCAL`` ends with the transaction, so the session's later statements, and
    the pooled connection's next user, keep the timeout. Call it inside the transaction (after its
    first statement, or it begins one); a no-op on SQLite, which has no statement timeout.
    """
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL statement_timeout = 0"))


def quiet_alembic() -> None:
    """Keep Alembic's INFO lines ("Context impl SQLiteImpl.", "Will assume non-transactional DDL.",
    "Running upgrade ...") out of every command's output.

    Its logger is set to WARNING unless ``COVERSET_LOG_LEVEL=DEBUG``; real migration steps are not
    lost: :func:`migrate` logs each revision it applies at INFO on coverset's own logger (so the
    server's startup migration shows in its log) and returns them, and ``coverset init-db`` prints
    them.
    """
    debug = os.environ.get(LOG_LEVEL_ENV, "").strip().upper() == "DEBUG"
    logging.getLogger("alembic").setLevel(logging.DEBUG if debug else logging.WARNING)


def _upgrade_head(connection: Connection, applied: list[str]) -> None:
    config = alembic_config(connection)
    before = MigrationContext.configure(connection).get_current_heads()
    command.upgrade(config, "head")
    after = MigrationContext.configure(connection).get_current_heads()
    if tuple(after) == tuple(before):
        return
    script = ScriptDirectory.from_config(config)
    steps = script.iterate_revisions(tuple(after), tuple(before) or "base")
    for revision in reversed(list(steps)):
        doc = (revision.doc or "").strip().splitlines()
        applied.append(f"{revision.revision} {doc[0]}" if doc else revision.revision)


def _current_heads(connection: Connection) -> tuple[str, ...]:
    return tuple(MigrationContext.configure(connection).get_current_heads())


async def current_revisions(engine: AsyncEngine) -> tuple[str, ...]:
    """The revision(s) recorded in the database's ``alembic_version`` (empty before the first
    migration). Read-only: never creates a table."""
    async with engine.connect() as connection:
        return await connection.run_sync(_current_heads)


async def migrate(engine: AsyncEngine) -> list[str]:
    """Upgrade the database behind ``engine`` to the latest Alembic revision (atomically).

    Returns the applied revisions, oldest first, as ``"<id> <first line of its message>"`` (empty
    when the database was already at head), and logs each at INFO as ``applied migration <rev>``.

    A database already at head is recognised from one read of ``alembic_version`` against the
    packaged heads (:func:`head_revisions`), without loading the revision scripts or opening a
    schema-change transaction: every app start and every test's app fixture pays only that read.
    """
    quiet_alembic()
    if set(await current_revisions(engine)) == head_revisions():
        return []
    applied: list[str] = []
    await run_schema_change(engine, lambda connection: _upgrade_head(connection, applied))
    for revision in applied:
        log.info("applied migration %s", revision)
    return applied


async def init_db(settings: Settings, engine: AsyncEngine | None = None) -> list[str]:
    """Create ``data_dir`` (and the SQLite file's directory) and migrate the database to head.

    Uses ``engine`` when given; otherwise a temporary engine for ``settings.database_url`` is
    created and disposed. Returns the applied revisions (see :func:`migrate`).
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db_file = sqlite_file_path(settings.database_url)
    if db_file is not None:
        db_file.parent.mkdir(parents=True, exist_ok=True)
    if engine is not None:
        return await migrate(engine)
    owned = make_engine(settings.database_url)
    try:
        return await migrate(owned)
    finally:
        await owned.dispose()


# -- probing a database (coverset init-db, coverset deploy check) ----------------------------------

POOLED_HOST_HINTS = ("-pooler.", ".pooler.supabase.com:6543")
"""Host markers of transaction-mode poolers (Neon's ``-pooler`` endpoint, Supabase's pooler on port
6543). coverset needs a session-mode connection: its leader lease is a session advisory lock, and
asyncpg prepares statements per connection. docs/postgres.md, "Pooling"."""

ReportStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class DatabaseReport:
    """What :func:`check_database` found. Unpacks as ``(status, detail)``; the other fields are for
    callers that format their own row (``coverset deploy check``)."""

    status: ReportStatus
    detail: str
    backend: str
    """``"SQLite"`` or ``"Postgres"``."""
    server_version: str | None = None
    current: tuple[str, ...] = ()
    heads: tuple[str, ...] = ()
    pool: str = ""
    """The connection pool a coverset process opens against this database."""
    error: str | None = None

    def __iter__(self) -> Iterator[str]:
        return iter((self.status, self.detail))


def describe_pool(url: str | URL) -> str:
    """The connection pool a coverset process uses for ``url``, for reports."""
    if is_postgres(url):
        return (
            f"{POSTGRES_POOL['pool_size']} + {POSTGRES_POOL['max_overflow']} overflow connections per process"
        )
    return "SQLite: one file, WAL, one writer at a time"


def pooler_warning(url: str | URL) -> str | None:
    """A warning when ``url`` looks like a transaction-mode pooler endpoint, else None."""
    parsed = make_url(url)
    if not is_postgres(parsed):
        return None
    location = f"{parsed.host or ''}:{parsed.port or 5432}"
    if any(hint in location for hint in POOLED_HOST_HINTS):
        return (
            f"{parsed.host} looks like a transaction-mode pooler: use the direct (session) connection "
            "string (docs/postgres.md, Pooling)"
        )
    return None


async def probe_database(url: str | URL) -> DatabaseReport:
    """Connect to ``url`` and report the backend, server version, Alembic revision against the
    packaged head and the pool coverset uses. Never raises for an unreachable database (the
    report's ``status`` is ``fail``) and never creates a SQLite file (a missing one is ``warn``)."""
    parsed = make_url(url)
    backend = (
        "Postgres" if is_postgres(parsed) else "SQLite" if _is_sqlite(parsed) else parsed.get_backend_name()
    )
    pool = describe_pool(parsed)
    heads = tuple(sorted(head_revisions()))
    db_file = sqlite_file_path(parsed)
    if db_file is not None and not db_file.exists():
        return DatabaseReport(
            "warn",
            f"{db_file} does not exist yet (serve creates and migrates it)",
            backend,
            heads=heads,
            pool=pool,
        )
    engine = make_engine(parsed)
    try:
        async with engine.connect() as connection:
            version = await connection.run_sync(_server_version)
            current = await connection.run_sync(_current_heads)
    except Exception as exc:  # noqa: BLE001 - any driver error is a report, not a crash
        error = f"{type(exc).__name__}: {exc}"
        return DatabaseReport(
            "fail", f"{backend}: cannot connect: {error}", backend, heads=heads, pool=pool, error=error
        )
    finally:
        await engine.dispose()
    where = f"{backend} {version}"
    warning = pooler_warning(parsed)
    if not current:
        status: ReportStatus = "warn"
        detail = f"{where}, not migrated yet (`coverset init-db`, or serve migrates at startup)"
    elif set(current) == set(heads):
        status, detail = "pass", f"{where}, migrated to head {', '.join(current)}"
    else:
        status = "warn"
        detail = f"{where} at {', '.join(current)}, head is {', '.join(heads)} (`coverset init-db`)"
    detail = f"{detail}; {pool}"
    if warning:
        status, detail = "warn", f"{detail}; {warning}"
    return DatabaseReport(status, detail, backend, version, tuple(sorted(current)), heads, pool)


def check_database(url: str | URL) -> DatabaseReport:
    """:func:`probe_database` from synchronous code (its own event loop, as the CLI runs it)."""
    return asyncio.run(probe_database(url))


def _server_version(connection: Connection) -> str:
    if connection.dialect.name == "postgresql":
        return str(connection.exec_driver_sql("SHOW server_version").scalar_one()).split(" ")[0]
    if connection.dialect.name == "sqlite":
        return str(connection.exec_driver_sql("SELECT sqlite_version()").scalar_one())
    info = connection.dialect.server_version_info or ()
    return ".".join(str(part) for part in info)
