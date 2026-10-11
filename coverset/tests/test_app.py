"""App factory: lifecycle, health check, security headers, JSON errors, body limits, secret key."""

from __future__ import annotations

import stat
from typing import Any

import pytest
from pydantic import SecretStr
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import inspect

from coverset import __version__
from coverset.app import CONTENT_SECURITY_POLICY, MAX_CONTENT_LENGTH, SECRET_KEY_FILE, create_app
from coverset.config import Settings
from coverset.db.session import Database, sqlite_file_path


async def test_healthz(client: Client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert await response.get_json() == {"status": "ok", "version": __version__}


@pytest.mark.parametrize("path", ["/healthz", "/", "/no-such-page", "/v1/nope"])
@pytest.mark.security
async def test_security_headers_on_every_response(client: Client, path: str) -> None:
    response = await client.get(path)
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_v1_errors_are_json(client: Client) -> None:
    missing = await client.get("/v1/nope")
    assert missing.status_code == 404
    assert missing.mimetype == "application/json"
    assert await missing.get_json() == {"error": "not_found"}


async def test_non_api_errors_are_not_json(client: Client) -> None:
    response = await client.get("/no-such-page")
    assert response.status_code == 404
    assert response.mimetype != "application/json"


async def test_method_not_allowed_has_allow_header(client: Client) -> None:
    response = await client.put("/healthz")
    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]


@pytest.mark.security
async def test_oversized_body_rejected(client: Client) -> None:
    size = MAX_CONTENT_LENGTH + 1
    response = await client.post("/v1/runs", data=b"x" * size, headers={"Content-Length": str(size)})
    assert response.status_code == 413
    assert await response.get_json() == {"error": "request_entity_too_large"}


async def test_serving_migrates_database(app: Quart) -> None:
    database: Database = app.extensions["db"]
    async with database.engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert {"repos", "runs", "tests", "test_results", "run_events"} <= tables


async def test_skip_migrations(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"skip_migrations": True}))
    async with app.test_app(), app.extensions["db"].engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert tables == []


def test_extensions_hold_settings_and_db(settings: Settings) -> None:
    app = create_app(settings)
    assert app.extensions["settings"] is settings
    assert isinstance(app.extensions["db"], Database)
    assert app.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024


@pytest.mark.security
def test_random_secret_key_persisted_with_private_mode(settings: Settings) -> None:
    first = create_app(settings).config["SECRET_KEY"]
    key_file = settings.data_dir / SECRET_KEY_FILE
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert len(first) == 64
    assert create_app(settings).config["SECRET_KEY"] == first


@pytest.mark.security
async def test_data_directories_are_private(settings: Settings) -> None:
    """Mirrors hold private source; the database and facts describe it: mode 0700 dirs, 0600 db."""
    for path in (settings.data_dir, settings.mirrors_dir, settings.facts_dir):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
    app = create_app(settings)
    for path in (settings.data_dir, settings.mirrors_dir, settings.facts_dir, settings.runs_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    async with app.test_app():
        pass
    db_file = sqlite_file_path(settings.database_url)
    assert db_file is not None and db_file.is_file() and stat.S_IMODE(db_file.stat().st_mode) == 0o600


@pytest.mark.security
async def test_cli_prepare_db_makes_the_data_directory_private(settings: Settings) -> None:
    from coverset.commands.common import prepare_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.chmod(0o755)
    await prepare_db(settings)
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.mirrors_dir.stat().st_mode) == 0o700
    db_file = sqlite_file_path(settings.database_url)
    assert db_file is not None and stat.S_IMODE(db_file.stat().st_mode) == 0o600


@pytest.mark.security
def test_secret_key_derived_from_admin_token(settings: Settings, admin_token: str) -> None:
    def key_for(**update: Any) -> str:
        return create_app(settings.model_copy(update=update)).config["SECRET_KEY"]

    derived = key_for(admin_token=SecretStr(admin_token))
    assert derived and admin_token not in derived
    assert not (settings.data_dir / SECRET_KEY_FILE).exists()
    other = key_for(admin_token=SecretStr(admin_token + "x"))
    assert other != derived
