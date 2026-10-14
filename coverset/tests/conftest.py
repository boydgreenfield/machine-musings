"""Shared fixtures: hermetic settings on a temp data dir, a started app, and its test client.

Override ``settings_overrides`` in a test module to change any :class:`Settings` field for the
``settings``/``app``/``client`` fixtures of that module.

Backend switch: with ``COVERSET_TEST_DATABASE_URL`` set (``make test-pg``), the ``settings``
fixture's database is a fresh Postgres database cloned from a template migrated once per worker
(:mod:`coverset.db.testing`) instead of a SQLite file under ``tmp_path``; with
``COVERSET_TEST_BLOB_URL`` set, its ``blob_url`` is a fresh key prefix of that store (deleted after
the test) unless the module overrides ``blob_url`` or ``data_dir`` (a module that brings its own data
directory brings its own facts too). Unset, nothing changes: SQLite and local files.

:func:`pytest_configure` groups the command line's test paths by directory first: pytest 9 binds a
``conftest.py``'s fixtures to the one ``Dir`` node it was loaded for, and paths that leave a
directory and come back to it (``tests/web/a.py tests/b.py tests/web/c.py``) give that directory a
second ``Dir`` node whose tests cannot see its conftest fixtures ("fixture 'demo' not found").
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client

from coverset.app import create_app
from coverset.config import Settings
from coverset.db.testing import PostgresTestDatabases, test_database_url
from coverset.facts.cache import configure as configure_caches
from coverset.logging import clear_registered_secrets
from coverset.storage import S3BlobStore
from coverset.storage.testing import fresh_blob_url, test_blob_url

ADMIN_TOKEN = "test-admin-token-0123456789abcdefghij"
"""A valid (>= 32 characters) admin token for access-control tests."""


def grouped_by_directory(args: Sequence[str], invocation_dir: Path) -> list[str]:
    """``args`` (pytest's positional test paths, ``path::name`` included) reordered so that every
    directory's paths are contiguous: a stable sort by the path's directory components, so a
    directory's tests, then its subdirectories', follow each other. An argument that is not an
    existing path keeps its place relative to the others with the same key (its own text)."""

    def key(arg: str) -> tuple[str, ...]:
        path = Path(arg.split("::", 1)[0])
        absolute = path if path.is_absolute() else invocation_dir / path
        if not absolute.exists():
            return (arg,)
        directory = absolute if absolute.is_dir() else absolute.parent
        return directory.resolve().parts

    return sorted(args, key=key)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Group the command line's test paths by directory (see the module docstring)."""
    config.args = grouped_by_directory(config.args, config.invocation_params.dir)
    config.addinivalue_line(
        "markers",
        "sqlite_only: needs the SQLite backend of the settings fixture (file modes, pragmas, rowid "
        "reuse, an empty unmigrated database); skipped when COVERSET_TEST_DATABASE_URL is set",
    )
    config.addinivalue_line(
        "markers",
        "local_files_only: plants or ages facts directly in the local facts layout (an empty run "
        "directory, a file's mtime); skipped when COVERSET_TEST_BLOB_URL selects an object store",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``sqlite_only`` tests while the suite runs against Postgres, and ``local_files_only``
    tests while it runs against an object store."""
    skips: dict[str, pytest.MarkDecorator] = {}
    if test_database_url() is not None:
        skips["sqlite_only"] = pytest.mark.skip(
            reason="SQLite-specific; COVERSET_TEST_DATABASE_URL selects Postgres"
        )
    if test_blob_url() is not None:
        skips["local_files_only"] = pytest.mark.skip(
            reason="local facts layout; COVERSET_TEST_BLOB_URL selects an object store"
        )
    for item in items:
        for marker, skip in skips.items():
            if item.get_closest_marker(marker) is not None:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _forget_registered_secrets() -> Iterator[None]:
    """Keep the process-wide redaction registry from leaking between tests."""
    yield
    clear_registered_secrets()


@pytest.fixture(autouse=True)
def _forget_cache_overrides() -> Iterator[None]:
    """Keep the cache knobs an app configured (``facts.cache.configure``) from leaking between tests."""
    yield
    configure_caches(None)


@pytest.fixture
def admin_token() -> str:
    """:data:`ADMIN_TOKEN`, for tests that configure or present the admin token."""
    return ADMIN_TOKEN


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """Extra :class:`Settings` fields for the ``settings`` fixture (override per module)."""
    return {}


@pytest.fixture(scope="session")
def postgres_databases() -> Iterator[PostgresTestDatabases | None]:
    """The per-worker Postgres template when ``COVERSET_TEST_DATABASE_URL`` is set, else None."""
    url = test_database_url()
    if url is None:
        yield None
        return
    databases = PostgresTestDatabases(url)
    databases.start()
    try:
        yield databases
    finally:
        databases.close()


@pytest.fixture
def settings(
    tmp_path: Path, settings_overrides: dict[str, Any], postgres_databases: PostgresTestDatabases | None
) -> Iterator[Settings]:
    """Test settings with ``data_dir`` under ``tmp_path`` and a file-backed SQLite database there,
    or a fresh Postgres database already at head, and local facts or a fresh object-store prefix
    (see the module docstring)."""
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'coverset.db'}",
    }
    created = None
    if postgres_databases is not None and "database_url" not in settings_overrides:
        created = values["database_url"] = postgres_databases.create()
    blob_url = None
    if test_blob_url() is not None and not {"blob_url", "data_dir"} & settings_overrides.keys():
        blob_url = values["blob_url"] = fresh_blob_url("app")
    values.update(settings_overrides)
    yield Settings.for_tests(**values)
    if created is not None:
        postgres_databases.retire(created)  # type: ignore[union-attr]  # dropped before the next test's
    if blob_url is not None:
        blobs = S3BlobStore.from_url(blob_url)
        blobs.delete([info.key for info in blobs.list()])  # this test's prefix only


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[Quart]:
    """The app with its serving lifecycle running (migrations applied, engine disposed after)."""
    application = create_app(settings)
    async with application.test_app():
        yield application


@pytest.fixture
def client(app: Quart) -> Client:
    """A test client for :func:`app` (keeps cookies between requests)."""
    return app.test_client()
