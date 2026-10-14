"""Shared fixtures: hermetic settings on a temp data dir, a started app, and its test client.

Override ``settings_overrides`` in a test module to change any :class:`Settings` field for the
``settings``/``app``/``client`` fixtures of that module.

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
from coverset.facts.cache import configure as configure_caches
from coverset.logging import clear_registered_secrets

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


@pytest.fixture
def settings(tmp_path: Path, settings_overrides: dict[str, Any]) -> Settings:
    """Test settings with ``data_dir`` and a file-backed SQLite database under ``tmp_path``."""
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'coverset.db'}",
    }
    values.update(settings_overrides)
    return Settings.for_tests(**values)


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
