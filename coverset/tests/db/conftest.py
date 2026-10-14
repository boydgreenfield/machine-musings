"""Fixtures for the database layer's tests: a migrated :class:`Database` on the suite's backend,
and a skip for the tests that need Postgres (``COVERSET_TEST_DATABASE_URL``, ``make test-pg``)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from coverset.config import Settings
from coverset.db.session import Database, init_db, is_postgres


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """The ``settings`` fixture's database, migrated to head (SQLite file or Postgres clone)."""
    database = Database.from_url(settings.database_url)
    await init_db(settings, database.engine)
    yield database
    await database.engine.dispose()


@pytest.fixture
def postgres_url(settings: Settings) -> str:
    """The test's Postgres database URL; skips on SQLite."""
    if not is_postgres(settings.database_url):
        pytest.skip("needs Postgres: set COVERSET_TEST_DATABASE_URL (make test-pg)")
    return settings.database_url
