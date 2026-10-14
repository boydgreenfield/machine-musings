"""Fixtures for the database layer's tests: a migrated :class:`Database` on the suite's server."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from coverset.config import Settings
from coverset.db.session import Database, init_db


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """The ``settings`` fixture's database, migrated to head."""
    database = Database.from_url(settings.database_url)
    await init_db(settings, database.engine)
    yield database
    await database.engine.dispose()
