"""Throwaway Postgres databases for the test suite: one migrated template per process, one clone
per test.

The suite runs on a local Postgres server by default: :data:`DEFAULT_TEST_DATABASE_URL` (Homebrew's
accepts the current user without a password), or ``COVERSET_TEST_DATABASE_URL`` (an asyncpg URL
whose user may ``CREATE DATABASE``; the database it names is only the one connected to for creating
and dropping the others, ``postgres`` by default). Migrating costs about a second, so it happens once
per process (each pytest-xdist worker is one) into a template database, and every test that takes the
``settings`` fixture gets ``CREATE DATABASE <name> TEMPLATE <template>``: a copy that is already at
the Alembic head, so the app fixture's start-up migration is the one-read no-op of
:func:`coverset.db.session.migrate`. Each test's database is dropped ``WITH (FORCE)`` afterwards,
which also ends connections a test forgot to close. Nothing here needs more than Postgres 14.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.engine import make_url

from coverset.db.session import admin_execute, make_engine, migrate, quote_database

TEST_DATABASE_URL_ENV = "COVERSET_TEST_DATABASE_URL"
"""The server the suite creates its databases on (see the module docstring)."""
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://localhost/postgres"
"""The local server, through its maintenance database."""


def test_database_url() -> str:
    """``COVERSET_TEST_DATABASE_URL`` normalised to the asyncpg driver, or
    :data:`DEFAULT_TEST_DATABASE_URL` when it is unset."""
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip() or DEFAULT_TEST_DATABASE_URL
    if url.startswith(("postgresql://", "postgres://")):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError(f"{TEST_DATABASE_URL_ENV} must be a postgresql+asyncpg:// URL")
    return url


test_database_url.__test__ = False  # type: ignore[attr-defined]  # not a test, despite its name


def server_problem(url: str, *, timeout_s: float = 5.0) -> str | None:
    """None when the server behind ``url`` accepts a connection, else one line saying how to fix it
    (the suite fails at collection with it rather than skipping anything)."""
    from coverset.db.session import shown_url

    async def probe() -> None:
        await asyncio.wait_for(admin_execute(url, ["SELECT 1"]), timeout_s)

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001 - any failure is the one-line message
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return (
            f"the test suite needs Postgres at {shown_url(url)} ({reason}): start it (brew services start "
            f"postgresql@14), or run `make services` and set {TEST_DATABASE_URL_ENV}="
            "postgresql://coverset:coverset@127.0.0.1:55432/postgres"
        )
    return None


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
        await admin_execute(self.admin_url, [f"CREATE DATABASE {quote_database(self.template)}"])
        engine = make_engine(self.url_for(self.template))
        try:
            await migrate(engine)
        finally:
            await engine.dispose()  # a template must have no open connections to be cloned

    def create(self, *, empty: bool = False) -> str:
        """A fresh database cloned from the migrated template (``empty``: with no schema at all, for
        migration tests); returns its URL. Databases :meth:`retire` queued are dropped first."""
        name = f"{self.prefix}_{uuid.uuid4().hex[:12]}"
        template = "" if empty else f" TEMPLATE {quote_database(self.template)}"
        statements = [_drop(retired) for retired in self._retired]
        self._retired.clear()
        asyncio.run(
            admin_execute(self.admin_url, [*statements, f"CREATE DATABASE {quote_database(name)}{template}"])
        )
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
        asyncio.run(admin_execute(self.admin_url, [_drop(name)]))

    def close(self) -> None:
        """Drop every database left over and the template."""
        names = [*self._retired, *sorted(self._created), self.template]
        self._created.clear()
        self._retired.clear()
        asyncio.run(admin_execute(self.admin_url, [_drop(name) for name in names]))


def _drop(name: str) -> str:
    return f"DROP DATABASE IF EXISTS {quote_database(name)} WITH (FORCE)"


def run_sql(url: str, statement: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
    """Run one SQL statement (``:name`` parameters) on the database behind ``url``, commit it and
    return its rows (empty for a statement without any).

    For tests that read or doctor a fixture's database directly (the page tests' shared demo): it
    works from synchronous code and from inside a running event loop alike, because the query runs on
    its own loop in a worker thread with a throwaway engine. JSON columns come back as their text
    (``CAST(col AS TEXT)`` before ``LIKE`` on one).
    """

    async def run() -> list[tuple[Any, ...]]:
        from sqlalchemy import text

        engine = make_engine(url)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), dict(params or {}))
                if not result.returns_rows:
                    return []
                # asyncpg decodes json columns: hand them back as text
                return [
                    tuple(json.dumps(v) if isinstance(v, dict | list) else v for v in row) for row in result
                ]
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(run())).result()
