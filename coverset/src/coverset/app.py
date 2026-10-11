"""Quart application factory for the coverset control plane."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from quart import Quart, Response, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from coverset.config import Settings, get_settings
from coverset.db.session import Database, init_db
from coverset.logging import configure_logging
from coverset.web import register_blueprints

MAX_CONTENT_LENGTH = 25 * 1024 * 1024
"""Largest accepted request body (webhook payloads are capped well below this by GitHub)."""

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
"""Set on every response, including errors and static files."""

SECRET_KEY_FILE = "secret_key"
_SESSION_KEY_LABEL = b"coverset-session-key\0"
_JSON_ERROR_PREFIX = "/v1/"


def create_app(settings: Settings | None = None) -> Quart:
    """Build the control-plane app.

    ``app.extensions["settings"]`` holds the :class:`~coverset.config.Settings` and
    ``app.extensions["db"]`` the :class:`~coverset.db.session.Database`. The database is migrated
    when the app starts serving (unless ``settings.skip_migrations``) and the engine is disposed
    when it stops.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    app = Quart("coverset", static_folder="web/static", template_folder="web/templates")
    app.extensions["settings"] = settings
    database = Database.from_url(settings.database_url)
    app.extensions["db"] = database
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        SECRET_KEY=_secret_key(settings),
        SESSION_COOKIE_NAME="coverset_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.public_url.startswith("https://"),
    )

    @app.before_serving
    async def _startup() -> None:
        if not settings.skip_migrations:
            await init_db(settings, database.engine)

    @app.after_serving
    async def _shutdown() -> None:
        await database.engine.dispose()

    @app.before_request
    async def _reject_oversized_body() -> None:
        length = request.content_length
        if length is not None and length > MAX_CONTENT_LENGTH:
            raise RequestEntityTooLarge()

    @app.after_request
    async def _security_headers(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    for code in (404, 405, 413):
        app.register_error_handler(code, _http_error)

    register_blueprints(app)
    return app


async def _http_error(error: HTTPException) -> HTTPException | tuple[Response, int]:
    """Answer ``/v1/*`` errors as ``{"error": <slug>}`` JSON; leave other paths to Quart."""
    if not request.path.startswith(_JSON_ERROR_PREFIX):
        return error
    code = error.code or 500
    slug = (error.name or "error").lower().replace(" ", "_")
    response = jsonify({"error": slug})
    valid_methods = getattr(error, "valid_methods", None)
    if valid_methods:
        response.headers["Allow"] = ", ".join(valid_methods)
    return response, code


def _secret_key(settings: Settings) -> str:
    """The session-signing key: derived from the admin token, else a random key kept in data_dir.

    Deriving from the token means rotating ``COVERSET_ADMIN_TOKEN`` invalidates every login cookie,
    while the cookie signature never reveals the token itself.
    """
    if settings.admin_token is not None:
        token = settings.admin_token.get_secret_value().encode()
        return hashlib.sha256(_SESSION_KEY_LABEL + token).hexdigest()
    return _persistent_random_key(settings.data_dir / SECRET_KEY_FILE)


def _persistent_random_key(path: Path) -> str:
    """Read the key in ``path``; if absent or empty, write a fresh random one (mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key)
    return key
