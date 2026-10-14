"""Engine and session factories, SQLite pragmas, and programmatic Alembic migrations."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, event
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

log = logging.getLogger(__name__)

LOG_LEVEL_ENV = "COVERSET_LOG_LEVEL"
"""``COVERSET_LOG_LEVEL=DEBUG`` keeps Alembic's own INFO lines (see :func:`quiet_alembic`)."""


def _is_sqlite(url: URL) -> bool:
    return url.get_backend_name() == "sqlite"


def _is_sqlite_memory(url: URL) -> bool:
    return _is_sqlite(url) and (url.database in (None, "", ":memory:") or url.query.get("mode") == "memory")


def sqlite_file_path(url: str | URL) -> Path | None:
    """The database file behind a file-backed SQLite URL, or None for other URLs and in-memory DBs."""
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


def make_engine(url: str | URL, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine; SQLite connections get WAL, ``synchronous=NORMAL``, a 5 s busy
    timeout and enforced foreign keys. In-memory SQLite shares one connection (``StaticPool``)
    so every session sees the same database.
    """
    parsed = make_url(url)
    kwargs: dict[str, Any] = {"echo": echo}
    if _is_sqlite_memory(parsed):
        kwargs["poolclass"] = StaticPool
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
    def from_url(cls, url: str | URL, *, echo: bool = False) -> Database:
        """Create the engine (see :func:`make_engine`) and a matching session factory."""
        engine = make_engine(url, echo=echo)
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
        change(connection)


async def run_schema_change(engine: AsyncEngine, change: Callable[[Connection], None]) -> None:
    """Run the synchronous schema change ``change`` in one transaction on its own connection.

    On SQLite the transaction is real for DDL too (a failed change leaves no half-built tables)
    and foreign keys are off while it runs: batch migrations copy, drop and rename tables, and with
    foreign keys on, dropping a parent table would cascade-delete or null its children's rows.
    ``PRAGMA foreign_key_check`` runs before commit and raises :class:`MigrationError` (rolling
    back) if any row is left dangling.
    """
    async with engine.connect() as connection:
        await connection.run_sync(_run_schema_change_sync, change)


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


async def migrate(engine: AsyncEngine) -> list[str]:
    """Upgrade the database behind ``engine`` to the latest Alembic revision (atomically).

    Returns the applied revisions, oldest first, as ``"<id> <first line of its message>"`` (empty
    when the database was already at head), and logs each at INFO as ``applied migration <rev>``.
    """
    quiet_alembic()
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
