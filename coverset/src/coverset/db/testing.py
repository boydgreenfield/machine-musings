"""Throwaway Postgres databases for the test suite: one migrated template per process, one clone
per test.

``COVERSET_TEST_DATABASE_URL`` (an asyncpg URL whose user may ``CREATE DATABASE``; ``make test-pg``
sets it to the compose stack's Postgres) switches ``tests/conftest.py``'s ``settings`` fixture from a
SQLite file under ``tmp_path`` to a fresh Postgres database. Migrating costs about a second, so it
happens once per process (each pytest-xdist worker is one) into a template database, and every
test gets ``CREATE DATABASE test_<hex> TEMPLATE <template>``: a file-level copy that is already at
the Alembic head, so the app fixture's start-up migration is the one-read no-op of
:func:`coverset.db.session.migrate`. Each test's database is dropped ``WITH (FORCE)`` afterwards,
which also ends connections a test forgot to close.

The connection parameters come from the URL (any database on the server works as the
maintenance database: ``CREATE DATABASE`` runs outside it).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.engine import URL, make_url

from coverset.db.session import make_engine, migrate, postgres_connect_args

TEST_DATABASE_URL_ENV = "COVERSET_TEST_DATABASE_URL"
"""An asyncpg URL whose user may ``CREATE DATABASE``; unset, the suite uses SQLite files."""


def test_database_url() -> str | None:
    """``COVERSET_TEST_DATABASE_URL`` normalised to the asyncpg driver, or None when unset."""
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        return None
    if url.startswith(("postgresql://", "postgres://")):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError(f"{TEST_DATABASE_URL_ENV} must be a postgresql+asyncpg:// URL")
    return url


test_database_url.__test__ = False  # type: ignore[attr-defined]  # not a test, despite its name


def _quote(name: str) -> str:
    """A database name as a quoted identifier (names here are generated, never user input)."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unexpected database name {name!r}")
    return f'"{name}"'


async def _admin(url: URL, statements: list[str]) -> None:
    """Run ``statements`` one by one on a plain asyncpg connection (``CREATE DATABASE`` and
    ``DROP DATABASE`` cannot run inside a transaction, which SQLAlchemy would open)."""
    import asyncpg

    url, connect_args = postgres_connect_args(url)
    kwargs: dict[str, Any] = {
        "host": url.host,
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "database": url.database,
    }
    kwargs.update({key: value for key, value in connect_args.items() if key != "server_settings"})
    connection = await asyncpg.connect(**kwargs)
    try:
        for statement in statements:
            await connection.execute(statement)
    finally:
        await connection.close()


class PostgresTestDatabases:
    """A migrated template database, and fresh clones of it (see the module docstring)."""

    def __init__(self, admin_url: str, *, prefix: str = "cs") -> None:
        self.admin_url = make_url(admin_url)
        tag = f"{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self.prefix = f"{prefix}_{tag}"
        self.template = f"{self.prefix}_tpl"
        self._created: set[str] = set()
        self._retired: list[str] = []

    def url_for(self, name: str) -> str:
        """The asyncpg URL of database ``name`` on the same server (password included)."""
        return self.admin_url.set(database=name).render_as_string(hide_password=False)

    def start(self) -> None:
        """Create the template database and migrate it to head."""
        asyncio.run(self._start())

    async def _start(self) -> None:
        await _admin(self.admin_url, [f"CREATE DATABASE {_quote(self.template)}"])
        engine = make_engine(self.url_for(self.template))
        try:
            await migrate(engine)
        finally:
            await engine.dispose()  # a template must have no open connections to be cloned

    def create(self, *, empty: bool = False) -> str:
        """A fresh database cloned from the migrated template (``empty``: with no schema at all, for
        migration tests); returns its URL. Databases :meth:`retire` queued are dropped first."""
        name = f"{self.prefix}_{uuid.uuid4().hex[:12]}"
        # FILE_COPY: for a database this small, copying the files plus a cheap DROP beats WAL_LOG
        # (Postgres 15+'s default), whose clone is faster but whose DROP is not: 70 ms vs 140 ms.
        template = "" if empty else f" TEMPLATE {_quote(self.template)} STRATEGY FILE_COPY"
        statements = [_drop(retired) for retired in self._retired]
        self._retired.clear()
        asyncio.run(_admin(self.admin_url, [*statements, f"CREATE DATABASE {_quote(name)}{template}"]))
        self._created.add(name)
        return self.url_for(name)

    def retire(self, url: str) -> None:
        """Drop the database behind ``url`` later: just before the next :meth:`create`, or at
        :meth:`close`.

        The ``settings`` fixture is torn down before the autouse fixtures a test module adds (they
        were set up first), and some of those still write: an executor flushing its last spend rows
        when it is closed. Dropping the database under them would turn each such write into a
        retry loop; by the next test's set-up every fixture of this one has finished.
        """
        name = make_url(url).database or ""
        self._created.discard(name)
        self._retired.append(name)

    def drop(self, url: str) -> None:
        """Drop the database behind ``url`` now (ending its connections)."""
        name = make_url(url).database or ""
        self._created.discard(name)
        asyncio.run(_admin(self.admin_url, [_drop(name)]))

    def close(self) -> None:
        """Drop every database left over and the template."""
        names = [*self._retired, *sorted(self._created), self.template]
        self._created.clear()
        self._retired.clear()
        asyncio.run(_admin(self.admin_url, [_drop(name) for name in names]))


def _drop(name: str) -> str:
    return f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)"


def run_sql(url: str, statement: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
    """Run one SQL statement (``:name`` parameters) on the database behind ``url``, SQLite or
    Postgres, commit it and return its rows (empty for a statement without any).

    For tests that read or doctor a fixture's database directly (the page tests' shared demo): it
    works from synchronous code and from inside a running event loop alike, because the query runs on
    its own loop in a worker thread with a throwaway engine. JSON columns come back as text on both
    backends; write portable SQL (``false`` rather than ``0`` for a boolean, ``RETURNING id`` rather
    than a driver's last row id, ``CAST(col AS TEXT)`` before ``LIKE`` on a JSON column).
    """

    async def run() -> list[tuple[Any, ...]]:
        from sqlalchemy import text

        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), dict(params or {}))
                if not result.returns_rows:
                    return []
                # asyncpg decodes json columns; SQLite returns their text: text on both
                return [
                    tuple(json.dumps(v) if isinstance(v, dict | list) else v for v in row) for row in result
                ]
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(run())).result()
