"""Engine and session factories for Postgres, creating the database, and programmatic Alembic
migrations.

coverset's control plane runs on Postgres (``postgresql+asyncpg``), 14 or newer: a bounded
connection pool per process, ``application_name=coverset``, ``timezone=UTC`` and TCP keepalives on
every connection, and (for the web app) a per-statement timeout. :func:`init_db` creates the
database named in the URL when the server has none of that name (connecting to its maintenance
database ``postgres``) and migrates it; :class:`DatabaseUnavailable` says how to start a server that
does not answer. docs/postgres.md is the operator's view.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from coverset.config import Settings

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
"""Alembic script directory, resolved from the installed package (no ``alembic.ini`` needed)."""

MAINTENANCE_DATABASE = "postgres"
"""The database :func:`create_database` connects to for ``CREATE DATABASE`` (every server has it)."""

PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
"""Database names coverset creates itself: letters, digits and underscores, at most 63 bytes (a
longer name would be truncated by the server and then never match the URL)."""

START_HINT = (
    "start it (brew services start postgresql@14), or run `make services` and set "
    "COVERSET_DATABASE_URL=postgresql://coverset:coverset@127.0.0.1:55432/coverset"
)
"""How to get a development server, for the one line :class:`DatabaseUnavailable` prints."""

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
"""``statement_timeout`` of the web app's connections: no request (and no background task of the
web process) waits more than 30 s on one statement. CLI commands and migrations
(:func:`run_schema_change` lifts it for the migration's transaction) run without one."""

ADVISORY_MIGRATION_LOCK = "coverset:migrate"
"""Name of the transaction-scoped advisory lock a migration takes, so two replicas that start
together migrate one after the other (the second finds the schema at head)."""

_DROPPED_POSTGRES_QUERY = ("channel_binding",)
"""libpq URL parameters asyncpg does not take; hosted providers (Neon) put them in pasted URLs."""

log = logging.getLogger(__name__)

LOG_LEVEL_ENV = "COVERSET_LOG_LEVEL"
"""``COVERSET_LOG_LEVEL=DEBUG`` keeps Alembic's own INFO lines (see :func:`quiet_alembic`)."""


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
    """Create an async engine with :data:`POSTGRES_POOL`, :data:`POSTGRES_SERVER_SETTINGS` and, when
    ``statement_timeout_s`` is given, a server-side ``statement_timeout`` on every connection (the
    web app passes :data:`WEB_STATEMENT_TIMEOUT_S`); see :func:`postgres_connect_args` for the URL
    handling."""
    parsed, connect_args = postgres_connect_args(make_url(url), statement_timeout_s=statement_timeout_s)
    return create_async_engine(parsed, echo=echo, connect_args=connect_args, **POSTGRES_POOL)


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


def _run_schema_change_sync(connection: Connection, change: Callable[[Connection], None]) -> None:
    with connection.begin():
        # No web statement timeout for DDL, and one migrating process at a time (the lock is
        # released when this transaction ends).
        connection.exec_driver_sql("SET LOCAL statement_timeout = 0")
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({advisory_key(ADVISORY_MIGRATION_LOCK)})")
        change(connection)


async def run_schema_change(engine: AsyncEngine, change: Callable[[Connection], None]) -> None:
    """Run the synchronous schema change ``change`` in one transaction on its own connection.

    DDL is transactional on Postgres, so a failed change leaves nothing half-built. The transaction
    takes the advisory lock :data:`ADVISORY_MIGRATION_LOCK` first and lifts any
    ``statement_timeout``.
    """
    async with engine.connect() as connection:
        await connection.run_sync(_run_schema_change_sync, change)


async def lift_statement_timeout(session: AsyncSession) -> None:
    """``SET LOCAL statement_timeout = 0`` for ``session``'s current transaction.

    For the bulk work the web process does on its own engine, whose connections carry
    :data:`WEB_STATEMENT_TIMEOUT_S`: the retention gc deletes a day's backlog of runs and run
    events with one statement per table, and once that backlog grows past what 30 s can delete
    (after a gc outage or a retention change) every pass would be cancelled and the backlog would
    never shrink. ``SET LOCAL`` ends with the transaction, so the session's later statements, and
    the pooled connection's next user, keep the timeout. Call it inside the transaction (after its
    first statement, or it begins one).
    """
    await session.execute(text("SET LOCAL statement_timeout = 0"))


def quiet_alembic() -> None:
    """Keep Alembic's INFO lines ("Context impl PostgresqlImpl.", "Will assume transactional DDL.",
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


class DatabaseUnavailable(RuntimeError):
    """The database cannot be reached, or created; the message is one line that says what to do."""


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE of a driver error (asyncpg's, or SQLAlchemy's wrapper around it), else None."""
    for candidate in (exc, getattr(exc, "orig", None), exc.__cause__):
        state = getattr(candidate, "sqlstate", None)
        if isinstance(state, str):
            return state
    return None


def shown_url(url: str | URL) -> str:
    """``url`` with its password masked, for messages."""
    return make_url(url).render_as_string(hide_password=True)


def _unavailable(url: str | URL, exc: BaseException) -> DatabaseUnavailable:
    """The one-line :class:`DatabaseUnavailable` for a failed connection to ``url``."""
    parsed = make_url(url)
    where = f"{parsed.host or 'the local socket'}:{parsed.port or 5432}"
    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    if isinstance(exc, OSError | TimeoutError):
        return DatabaseUnavailable(f"cannot reach Postgres at {where} ({reason}): {START_HINT}")
    return DatabaseUnavailable(f"cannot connect to {shown_url(parsed)}: {reason}")


def connection_errors(url: str | URL) -> Callable[[BaseException], DatabaseUnavailable | None]:
    """A classifier for exceptions raised while connecting to ``url``: the :class:`DatabaseUnavailable`
    to raise instead (server unreachable, authentication refused, no such database), or None for
    anything else (a failing migration, a bug), which should propagate as it is."""

    def classify(exc: BaseException) -> DatabaseUnavailable | None:
        if isinstance(exc, OSError | TimeoutError):
            return _unavailable(url, exc)
        state = _sqlstate(exc)
        if state is not None and (state.startswith("28") or state in ("3D000", "08001", "08006", "57P03")):
            return _unavailable(url, exc)
        return None

    return classify


def _asyncpg_kwargs(url: URL) -> dict[str, Any]:
    """``asyncpg.connect`` arguments for ``url`` (the libpq parameters translated as for the engine)."""
    url, connect_args = postgres_connect_args(url)
    kwargs: dict[str, Any] = {
        "host": url.host,
        "port": url.port,
        "user": url.username,
        "password": url.password,
        "database": url.database,
    }
    kwargs.update({key: value for key, value in connect_args.items() if key != "server_settings"})
    return {key: value for key, value in kwargs.items() if value is not None}


async def admin_execute(url: str | URL, statements: list[str]) -> None:
    """Run ``statements`` one by one on a plain asyncpg connection to ``url`` (``CREATE DATABASE``
    and ``DROP DATABASE`` cannot run inside a transaction, which SQLAlchemy would open)."""
    import asyncpg

    connection = await asyncpg.connect(**_asyncpg_kwargs(make_url(url)))
    try:
        for statement in statements:
            await connection.execute(statement)
    finally:
        await connection.close()


def quote_database(name: str) -> str:
    """``name`` as a quoted identifier; :class:`ValueError` unless it is a plain identifier
    (:data:`PLAIN_IDENTIFIER`): coverset creates and drops only names it can quote unambiguously."""
    if not PLAIN_IDENTIFIER.fullmatch(name):
        raise ValueError(
            f"database name {name!r} is not a plain identifier (letters, digits and underscores, at "
            "most 63 characters)"
        )
    return f'"{name}"'


async def create_database(url: str | URL) -> None:
    """``CREATE DATABASE`` the database named in ``url``, from the server's maintenance database
    (:data:`MAINTENANCE_DATABASE`), as the URL's user. Raises :class:`DatabaseUnavailable` for a name
    that is not a plain identifier, a server that does not answer, or a user that may not create
    databases; a database that appeared meanwhile (another process created it) is fine.

    Replicas starting together all find the database missing and all try to create it. The loser
    sees ``duplicate_database`` (42P04) when the winner's ``CREATE DATABASE`` had already committed,
    and ``unique_violation`` (23505) on ``pg_database``'s name index when it was still running: the
    loser waits on the index until the winner commits (the name check in Postgres's dbcommands.c is
    only a friendlier message than the index gives). Both mean the database exists now.
    """
    parsed = make_url(url)
    name = parsed.database or ""
    try:
        quoted = quote_database(name)
    except ValueError as exc:
        raise DatabaseUnavailable(
            f"{shown_url(parsed)} names no database coverset can create: {exc}; create it yourself"
        ) from None
    try:
        await admin_execute(parsed.set(database=MAINTENANCE_DATABASE), [f"CREATE DATABASE {quoted}"])
    except Exception as exc:
        if _sqlstate(exc) in ("42P04", "23505"):  # someone else created it meanwhile (see above)
            return
        problem = connection_errors(parsed)(exc)
        if problem is not None:
            raise problem from None
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise DatabaseUnavailable(
            f"cannot create database {name} on {parsed.host or 'the local socket'}: {reason}; create it "
            f"yourself (createdb {name}) or use a user that may"
        ) from None
    log.info("created database %s", name)


async def migrate_url(url: str | URL, *, create: bool = True) -> list[str]:
    """:func:`migrate` the database behind ``url`` with a short-lived engine, creating it first when
    the server has no database of that name (``create``). Raises :class:`DatabaseUnavailable` when
    the server cannot be reached or refuses the connection."""
    classify = connection_errors(url)
    for attempt in (1, 2):
        engine = make_engine(url)
        try:
            return await migrate(engine)
        except Exception as exc:
            if attempt == 1 and create and _sqlstate(exc) == "3D000":  # invalid_catalog_name
                await create_database(url)
                continue
            problem = classify(exc)
            if problem is not None:
                raise problem from None
            raise
        finally:
            await engine.dispose()
    raise AssertionError("unreachable")  # pragma: no cover


async def init_db(settings: Settings, engine: AsyncEngine | None = None) -> list[str]:
    """Create ``data_dir``, then create the database when it does not exist yet and migrate it to
    head (:func:`migrate_url`).

    With ``engine`` (the app's own, at startup) the database must exist already: it is only
    migrated. Returns the applied revisions (see :func:`migrate`).
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if engine is not None:
        return await migrate(engine)
    return await migrate_url(settings.database_url)


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
    server_version: str | None = None
    current: tuple[str, ...] = ()
    heads: tuple[str, ...] = ()
    pool: str = ""
    """The connection pool a coverset process opens against this database."""
    leader: str | None = None
    """Who holds the background-task lease (:func:`coverset.db.leader.leader_status`)."""
    error: str | None = None

    def __iter__(self) -> Iterator[str]:
        return iter((self.status, self.detail))


def describe_pool() -> str:
    """The connection pool a coverset process opens, for reports."""
    return f"{POSTGRES_POOL['pool_size']} + {POSTGRES_POOL['max_overflow']} overflow connections per process"


def pooler_warning(url: str | URL) -> str | None:
    """A warning when ``url`` looks like a transaction-mode pooler endpoint, else None."""
    parsed = make_url(url)
    location = f"{parsed.host or ''}:{parsed.port or 5432}"
    if any(hint in location for hint in POOLED_HOST_HINTS):
        return (
            f"{parsed.host} looks like a transaction-mode pooler: use the direct (session) connection "
            "string (docs/postgres.md, Pooling)"
        )
    return None


async def probe_database(url: str | URL) -> DatabaseReport:
    """Connect to ``url`` and report the server version, the Alembic revision against the packaged
    head, the pool coverset uses and who holds the background-task lease. Never raises for an
    unreachable server or a missing database (the report's ``status`` is ``fail``) and never
    creates anything."""
    from coverset.db.leader import leader_status_on

    parsed = make_url(url)
    pool = describe_pool()
    heads = tuple(sorted(head_revisions()))
    engine = make_engine(parsed)
    try:
        async with engine.connect() as connection:
            version = await connection.run_sync(_server_version)
            current = await connection.run_sync(_current_heads)
            leader = (await leader_status_on(connection)).detail
    except Exception as exc:  # noqa: BLE001 - any driver error is a report, not a crash
        error = f"{type(exc).__name__}: {exc}"
        hint = " (`coverset init-db` creates it)" if _sqlstate(exc) == "3D000" else ""
        return DatabaseReport(
            "fail", f"Postgres: cannot connect: {error}{hint}", heads=heads, pool=pool, error=error
        )
    finally:
        await engine.dispose()
    where = f"Postgres {version}"
    warning = pooler_warning(parsed)
    if not current:
        status: ReportStatus = "warn"
        detail = f"{where}, not migrated yet (`coverset init-db`, or serve migrates at startup)"
    elif set(current) == set(heads):
        status, detail = "pass", f"{where}, migrated to head {', '.join(current)}"
    else:
        status = "warn"
        detail = f"{where} at {', '.join(current)}, head is {', '.join(heads)} (`coverset init-db`)"
    detail = f"{detail}; {pool}; lease {leader}"
    if warning:
        status, detail = "warn", f"{detail}; {warning}"
    return DatabaseReport(status, detail, version, tuple(sorted(current)), heads, pool, leader)


def check_database(url: str | URL) -> DatabaseReport:
    """:func:`probe_database` from synchronous code (its own event loop, as the CLI runs it)."""
    return asyncio.run(probe_database(url))


def _server_version(connection: Connection) -> str:
    return str(connection.exec_driver_sql("SHOW server_version").scalar_one()).split(" ")[0]
