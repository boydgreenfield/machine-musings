"""Shared fixtures: hermetic settings on a temp data dir, a started app, and its test client.

Override ``settings_overrides`` in a test module to change any :class:`Settings` field for the
``settings``/``app``/``client`` fixtures of that module.

Databases: the ``settings`` fixture's database is a fresh Postgres database cloned from a template
migrated once per worker (:mod:`coverset.db.testing`), on the local server
(``postgresql+asyncpg://localhost/postgres``) or the one ``COVERSET_TEST_DATABASE_URL`` names, and
dropped after the test. When that server does not answer, the run stops before collecting anything,
with one line saying how to start it. ``COVERSET_DATABASE_URL`` points at a scratch database of this
process on the same server (created on first use like any database, dropped at the end): code that
reads the process environment (CLI commands under ``CliRunner``, subprocesses) and
``Settings.for_tests`` without a ``database_url`` (the one environment key it reads) both land there,
never in the developer's own ``coverset`` database. Blobs: files under the test's data directory by default; with
``COVERSET_TEST_BLOB_URL`` set, ``blob_url`` is a fresh key prefix of that store (deleted after the
test) unless the module overrides ``blob_url`` or ``data_dir`` (a module that brings its own data
directory brings its own facts too).

:func:`pytest_configure` groups the command line's test paths by directory first: pytest 9 binds a
``conftest.py``'s fixtures to the one ``Dir`` node it was loaded for, and paths that leave a
directory and come back to it (``tests/web/a.py tests/b.py tests/web/c.py``) give that directory a
second ``Dir`` node whose tests cannot see its conftest fixtures ("fixture 'demo' not found").
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client

from coverset.app import create_app
from coverset.config import Settings
from coverset.db.session import admin_execute, quote_database
from coverset.db.testing import PostgresTestDatabases, server_problem, test_database_url
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
    """Group the command line's test paths by directory (see the module docstring), and stop at
    once, with the one-line fix, when the Postgres server the suite needs does not answer (checked
    by the controlling process only, before any worker starts)."""
    config.args = grouped_by_directory(config.args, config.invocation_params.dir)
    if config.option.help or config.option.version:
        return
    _point_the_environment_at_a_scratch_database(config)
    if hasattr(config, "workerinput"):
        return
    problem = server_problem(test_database_url())
    if problem is not None:
        raise pytest.UsageError(problem)


_ENV_DATABASE = "_coverset_env_database"


def _point_the_environment_at_a_scratch_database(config: pytest.Config) -> None:
    """Set ``COVERSET_DATABASE_URL`` for this process to a database of its own on the test server
    (see the module docstring); :func:`pytest_unconfigure` drops it."""
    from sqlalchemy.engine import make_url

    name = f"cs_env_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    url = make_url(test_database_url()).set(database=name).render_as_string(hide_password=False)
    os.environ["COVERSET_DATABASE_URL"] = url
    setattr(config, _ENV_DATABASE, url)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Drop this process's scratch database, if anything created it."""
    url = getattr(config, _ENV_DATABASE, None)
    if url is None:
        return
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    drop = f"DROP DATABASE IF EXISTS {quote_database(parsed.database or '')} WITH (FORCE)"
    with contextlib.suppress(Exception):  # the server may be gone; nothing else to clean up
        asyncio.run(admin_execute(parsed.set(database=make_url(test_database_url()).database), [drop]))


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
def postgres_databases() -> Iterator[PostgresTestDatabases]:
    """This worker's migrated template database on the test server, and the clones made from it
    (all dropped at the end of the session)."""
    databases = PostgresTestDatabases(test_database_url())
    databases.start()
    try:
        yield databases
    finally:
        databases.close()


@pytest.fixture
def settings(
    tmp_path: Path, settings_overrides: dict[str, Any], postgres_databases: PostgresTestDatabases
) -> Iterator[Settings]:
    """Test settings with ``data_dir`` under ``tmp_path``, a fresh Postgres database already at head,
    and blobs as local files or under a fresh object-store prefix (see the module docstring)."""
    values: dict[str, Any] = {"data_dir": tmp_path / "data"}
    created = None
    if "database_url" not in settings_overrides:
        created = values["database_url"] = postgres_databases.create()
    blob_url = None
    if test_blob_url() is not None and not {"blob_url", "data_dir"} & settings_overrides.keys():
        blob_url = values["blob_url"] = fresh_blob_url("app")
    values.update(settings_overrides)
    yield Settings.for_tests(**values)
    if created is not None:
        postgres_databases.retire(created)  # dropped before the next test's
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
