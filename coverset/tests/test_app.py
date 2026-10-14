"""App factory: lifecycle, health check, security headers, JSON errors, body limits, secret key."""

from __future__ import annotations

import asyncio
import stat
from typing import Any

import pytest
from pydantic import SecretStr
from quart import Quart, request
from quart.typing import TestClientProtocol as Client
from sqlalchemy import inspect

from coverset import __version__
from coverset.app import (
    AVATAR_ORIGIN,
    CONTENT_SECURITY_POLICY,
    MAX_CONTENT_LENGTH,
    SECRET_KEY_FILE,
    UPLOAD_PATH,
    create_app,
    is_upload_request,
)
from coverset.config import Settings
from coverset.db.session import Database, sqlite_file_path
from coverset.db.testing import PostgresTestDatabases


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


async def test_skip_migrations(settings: Settings, postgres_databases: PostgresTestDatabases | None) -> None:
    """``skip_migrations`` leaves an empty database empty (on Postgres a database with no schema,
    since the ``settings`` fixture's clone is already at head)."""
    update: dict[str, Any] = {"skip_migrations": True}
    if postgres_databases is not None:  # its helpers run their own event loop: off this one
        update["database_url"] = await asyncio.to_thread(postgres_databases.create, empty=True)
    try:
        app = create_app(settings.model_copy(update=update))
        async with app.test_app(), app.extensions["db"].engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    finally:
        if postgres_databases is not None:
            postgres_databases.retire(update["database_url"])
    assert tables == []


def test_extensions_hold_settings_and_db(settings: Settings) -> None:
    app = create_app(settings)
    assert app.extensions["settings"] is settings
    assert isinstance(app.extensions["db"], Database)
    assert app.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024


@pytest.mark.parametrize("days", [1, 30, 31, 365])
def test_the_session_lifetime_is_set_with_the_cookie_keys(settings: Settings, days: int) -> None:
    """``PERMANENT_SESSION_LIFETIME`` comes from ``COVERSET_SESSION_DAYS`` in ``create_app`` (31 days,
    Quart's own default, included); the auth blueprint's fallback hook agrees with it."""
    from datetime import timedelta

    from coverset.web.auth import session_lifetime

    app = create_app(settings.model_copy(update={"session_days": days}))
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=days) == session_lifetime(days)
    assert app.config["SESSION_COOKIE_NAME"] == "coverset_session"


@pytest.mark.security
def test_the_csp_allows_github_avatars_and_nothing_else_third_party() -> None:
    directives = dict(d.strip().split(" ", 1) for d in CONTENT_SECURITY_POLICY.split(";"))
    assert directives["img-src"] == f"'self' data: {AVATAR_ORIGIN}"
    assert AVATAR_ORIGIN == "https://avatars.githubusercontent.com"
    others = {k: v for k, v in directives.items() if k != "img-src"}
    assert not any("http" in v for v in others.values()), others


@pytest.mark.security
async def test_only_bundle_uploads_skip_the_global_body_limit(app: Quart, client: Client) -> None:
    """``POST /v1/bundles`` is exempt (its request carries ``max_upload_bytes``); every other path
    and method keeps the 25 MiB check."""
    assert is_upload_request("POST", UPLOAD_PATH)
    assert not is_upload_request("GET", UPLOAD_PATH)
    assert not is_upload_request("POST", UPLOAD_PATH + "/x")
    assert not is_upload_request("POST", "/webhooks/github")
    size = MAX_CONTENT_LENGTH + 1
    for path in ("/v1/bundles/run_1", "/webhooks/github", "/login"):
        response = await client.post(path, data=b"x" * size, headers={"Content-Length": str(size)})
        assert response.status_code == 413, path
    limit = app.extensions["settings"].max_upload_bytes
    async with app.test_request_context(UPLOAD_PATH, method="POST"):
        assert request.max_content_length == limit
    async with app.test_request_context("/webhooks/github", method="POST"):
        assert request.max_content_length == MAX_CONTENT_LENGTH
    request_class = app.request_class
    upload = _request(request_class, "POST", UPLOAD_PATH, size)
    assert upload.max_content_length == limit and upload.body._must_raise is None
    other = _request(request_class, "POST", "/webhooks/github", size)
    assert other.body._must_raise is not None, "Quart refuses the declared length up front"


def _request(request_class: Any, method: str, path: str, length: int) -> Any:
    from functools import partial

    from werkzeug.datastructures import Headers

    async def push(*_args: Any) -> None:
        return None

    headers = Headers({"Content-Length": str(length)})
    return request_class(
        method,
        "http",
        path,
        b"",
        headers,
        "",
        "1.1",
        {"type": "http"},
        max_content_length=MAX_CONTENT_LENGTH,
        body_timeout=None,
        send_push_promise=partial(push),
    )


@pytest.mark.security
def test_random_secret_key_persisted_with_private_mode(settings: Settings) -> None:
    first = create_app(settings).config["SECRET_KEY"]
    key_file = settings.data_dir / SECRET_KEY_FILE
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert len(first) == 64
    assert create_app(settings).config["SECRET_KEY"] == first


@pytest.mark.security
async def test_data_directories_are_private(
    settings: Settings, postgres_databases: PostgresTestDatabases | None
) -> None:
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
    if postgres_databases is None:
        assert db_file is not None and db_file.is_file() and stat.S_IMODE(db_file.stat().st_mode) == 0o600
    else:
        assert db_file is None  # Postgres: no database file under the data dir to protect


@pytest.mark.security
async def test_cli_prepare_db_makes_the_data_directory_private(
    settings: Settings, postgres_databases: PostgresTestDatabases | None
) -> None:
    from coverset.commands.common import prepare_db

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.chmod(0o755)
    await prepare_db(settings)
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.mirrors_dir.stat().st_mode) == 0o700
    db_file = sqlite_file_path(settings.database_url)
    if postgres_databases is None:
        assert db_file is not None and stat.S_IMODE(db_file.stat().st_mode) == 0o600
    else:
        assert db_file is None  # Postgres: no database file under the data dir to protect


@pytest.mark.security
def test_secret_key_derived_from_admin_token(settings: Settings, admin_token: str) -> None:
    def key_for(**update: Any) -> str:
        return create_app(settings.model_copy(update=update)).config["SECRET_KEY"]

    derived = key_for(admin_token=SecretStr(admin_token))
    assert derived and admin_token not in derived
    assert not (settings.data_dir / SECRET_KEY_FILE).exists()
    other = key_for(admin_token=SecretStr(admin_token + "x"))
    assert other != derived
