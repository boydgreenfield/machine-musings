"""Shared fixtures: hermetic settings on a temp data dir, a started app, and its test client.

Override ``settings_overrides`` in a test module to change any :class:`Settings` field for the
``settings``/``app``/``client`` fixtures of that module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client

from coverset.app import create_app
from coverset.config import Settings
from coverset.logging import clear_registered_secrets

ADMIN_TOKEN = "test-admin-token-0123456789abcdefghij"
"""A valid (>= 32 characters) admin token for access-control tests."""


@pytest.fixture(autouse=True)
def _forget_registered_secrets() -> Iterator[None]:
    """Keep the process-wide redaction registry from leaking between tests."""
    yield
    clear_registered_secrets()


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
