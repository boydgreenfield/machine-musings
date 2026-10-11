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
    when it stops. Outside tests, serving also starts the run dispatcher and the in-process gc
    (:func:`schedule_gc`: once at startup, then every 24 h), and with ``COVERSET_EXECUTOR=modal`` the
    reaper and the warm-pool maintainer (:func:`schedule_executor_upkeep`).
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
    if settings.env != "test":
        # The dispatcher that turns queued runs into executions; tests drive runs explicitly.
        from coverset.orchestrator.queue import install_run_queue

        install_run_queue(app)
        schedule_gc(app)
        if settings.executor == "modal":
            schedule_executor_upkeep(app)
    return app


def schedule_gc(app: Quart, *, interval_s: float | None = None) -> None:
    """Run the retention pass (``coverset gc``) as a background task while the app serves.

    It runs once when serving starts and then every ``interval_s`` (24 h by default) until the app
    shuts down (``app.shutdown_event``). ``create_app`` calls this outside tests only; tests call it
    explicitly.
    """
    from coverset.facts.store import FactsStore
    from coverset.orchestrator.history import GC_INTERVAL_S, gc_forever

    settings: Settings = app.extensions["settings"]
    database: Database = app.extensions["db"]

    @app.before_serving
    async def _start_gc() -> None:
        app.add_background_task(
            gc_forever,
            database,
            settings,
            FactsStore(settings.facts_dir),
            app.shutdown_event,
            interval_s=GC_INTERVAL_S if interval_s is None else interval_s,
        )


def schedule_executor_upkeep(
    app: Quart, *, reap_interval_s: float | None = None, pool_interval_s: float | None = None
) -> None:
    """Reap and keep the warm pool filled while the app serves; drain the pool when it stops.

    For ``settings.executor`` (the executor the app's runs use, shared through
    :meth:`~coverset.orchestrator.runner.OrchestratorContext.from_app`):

    * :func:`~coverset.orchestrator.reaper.reap_forever` runs at startup and then every
      ``reap_interval_s`` (60 s), terminating workers tagged with this installation that no queued or
      running run holds (executors that do not reap skip it);
    * a :class:`~coverset.orchestrator.pool.PoolMaintainer` tops the pools of recently active repos up
      to ``COVERSET_POOL_SIZE`` every ``pool_interval_s`` (30 s) and is handed to the runner, which
      refills a repo's pool after re-snapshotting it (executors without a pool skip it);
    * when serving stops, re-snapshots in flight are cancelled, the pool is drained and the
      executors are closed, so no worker this process created is left running.

    ``create_app`` calls this outside tests when the executor is Modal; tests call it explicitly.
    """
    from coverset.orchestrator.pool import POOL_INTERVAL_S, PoolMaintainer
    from coverset.orchestrator.reaper import REAP_INTERVAL_S, reap_forever
    from coverset.orchestrator.runner import OrchestratorContext

    settings: Settings = app.extensions["settings"]
    ctx = OrchestratorContext.from_app(app)
    state: dict[str, PoolMaintainer] = {}

    @app.before_serving
    async def _start_executor_upkeep() -> None:
        executor = ctx.executor(settings.executor)
        maintainer = PoolMaintainer(ctx.db, settings, executor)
        state["maintainer"] = maintainer
        app.add_background_task(
            reap_forever,
            ctx.db,
            executor,
            interval_s=REAP_INTERVAL_S if reap_interval_s is None else reap_interval_s,
            stop=app.shutdown_event,
        )
        if maintainer.enabled:
            ctx.pool_maintainer = maintainer
            app.add_background_task(
                maintainer.run_forever,
                app.shutdown_event,
                interval_s=POOL_INTERVAL_S if pool_interval_s is None else pool_interval_s,
            )

    @app.after_serving
    async def _stop_executor_upkeep() -> None:
        ctx.pool_maintainer = None
        await ctx.finish_background(cancel=True)
        maintainer = state.pop("maintainer", None)
        if maintainer is not None and maintainer.enabled:
            try:
                drained = await maintainer.drain()
            except Exception:
                app.logger.exception("draining the warm pool failed")
            else:
                app.logger.info("warm pool drained (%d workers)", drained)
        await ctx.close()


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
