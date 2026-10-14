"""Quart application factory for the coverset control plane."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quart import Quart, Request, Response, jsonify, request, session
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from coverset.config import Settings, get_settings, secure_data_dirs
from coverset.db.session import Database, init_db
from coverset.facts.cache import configure as configure_caches
from coverset.logging import configure_logging
from coverset.web import register_blueprints
from coverset.web.auth import session_lifetime
from coverset.web.proxy import ProxyHeadersMiddleware

MAX_CONTENT_LENGTH = 25 * 1024 * 1024
"""Largest accepted request body (webhook payloads are capped well below this by GitHub), except
for :data:`UPLOAD_PATH`, whose limit is ``COVERSET_MAX_UPLOAD_BYTES``."""
UPLOAD_PATH = "/v1/bundles"
"""``POST`` here is a bundle upload (``web/uploads.py``): exempt from :data:`MAX_CONTENT_LENGTH`; its
body may be as large as ``settings.max_upload_bytes``, which the view also enforces while it streams
(the client sends chunked bodies, and a declared ``Content-Length`` after a proxy's 411)."""

AVATAR_ORIGIN = "https://avatars.githubusercontent.com"
"""The one third-party image origin: GitHub avatars in the header and on ``/account``."""
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    f"img-src 'self' data: {AVATAR_ORIGIN}; connect-src 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
"""Set on every response, including errors and static files."""

HSTS_HEADER = "max-age=31536000"
"""``Strict-Transport-Security`` value sent when ``COVERSET_PUBLIC_URL`` is https (one year, this host only)."""

SECRET_KEY_FILE = "secret_key"
_SESSION_KEY_LABEL = b"coverset-session-key\0"
_JSON_ERROR_PREFIX = "/v1/"


def create_app(settings: Settings | None = None) -> Quart:
    """Build the control-plane app.

    ``app.extensions["settings"]`` holds the :class:`~coverset.config.Settings` and
    ``app.extensions["db"]`` the :class:`~coverset.db.session.Database`. The database is migrated
    when the app starts serving (unless ``settings.skip_migrations``) and the engine is disposed
    when it stops. Outside tests, serving also starts the run dispatcher, the in-process gc
    (:func:`schedule_gc`: once at startup, then every 24 h) and the executor upkeep
    (:func:`schedule_executor_upkeep`): the reaper and the warm-pool maintainer start with the app
    when ``COVERSET_EXECUTOR=modal``, and otherwise as soon as the process builds a Modal executor
    (a run queued with ``coverset run --no-wait --executor modal``).
    """
    settings = settings or get_settings()
    configure_logging(settings)
    secure_data_dirs(settings)
    configure_caches(settings.cache_overrides())  # the cache knobs: Settings (and .env) over the environment

    app = Quart("coverset", static_folder="web/static", template_folder="web/templates")
    app.request_class = _request_class(app.request_class, lambda: settings.max_upload_bytes)
    # Templates show a stored avatar URL only under the origin the CSP's img-src allows.
    app.jinja_env.globals["avatar_origin"] = AVATAR_ORIGIN + "/"
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
        # A GitHub sign-in's cookie lives exactly as long as its user_sessions row.
        PERMANENT_SESSION_LIFETIME=session_lifetime(settings.session_days),
    )
    if settings.trust_proxy:
        # Client address and scheme from the nearest proxy's X-Forwarded-For/-Proto (web/proxy.py).
        app.asgi_app = ProxyHeadersMiddleware.for_environment(app.asgi_app)  # type: ignore[method-assign]
    security_headers = dict(SECURITY_HEADERS)
    if settings.public_url.startswith("https://"):
        security_headers["Strict-Transport-Security"] = HSTS_HEADER

    @app.before_serving
    async def _startup() -> None:
        if not settings.skip_migrations:
            await init_db(settings, database.engine)
            secure_data_dirs(settings)  # the database file the migration may just have created

    @app.after_serving
    async def _shutdown() -> None:
        await database.engine.dispose()

    @app.before_request
    async def _reject_oversized_body() -> None:
        if is_upload_request(request.method, request.path):
            # The upload view authorizes first, then checks the declared length and the streamed
            # bytes against settings.max_upload_bytes (the request's own limit, _request_class).
            return
        length = request.content_length
        if length is not None and length > MAX_CONTENT_LENGTH:
            raise RequestEntityTooLarge()

    @app.after_request
    async def _security_headers(response: Response) -> Response:
        response.headers.update(security_headers)
        return response

    for code in (404, 405, 413):
        app.register_error_handler(code, _http_error)

    register_blueprints(app)
    install_response_caching(app)
    if settings.env != "test":
        # The dispatcher that turns queued runs into executions; tests drive runs explicitly.
        from coverset.orchestrator.queue import install_run_queue

        install_run_queue(app)
        schedule_gc(app)
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

    For every executor the app's runs use (shared through
    :meth:`~coverset.orchestrator.runner.OrchestratorContext.from_app`): ``settings.executor`` when it
    is Modal from the start, and any executor the process builds later, the moment it is built
    (:attr:`~coverset.orchestrator.runner.OrchestratorContext.on_executor_built`), so a ``serve`` whose
    default executor is local still reaps and pools for Modal runs queued by
    ``coverset run --no-wait --executor modal``:

    * :func:`~coverset.orchestrator.reaper.reap_forever` runs at startup and then every
      ``reap_interval_s`` (60 s), terminating workers tagged with this installation that no queued or
      running run holds (executors that do not reap skip it);
    * a :class:`~coverset.orchestrator.pool.PoolMaintainer` tops the pools of recently active repos up
      to ``COVERSET_POOL_SIZE`` every ``pool_interval_s`` (30 s) and is handed to the runner, which
      refills a repo's pool after re-snapshotting it (executors without a pool skip it);
    * when serving stops, re-snapshots in flight are cancelled, the pool is drained and the
      executors are closed, so no worker this process created is left running.

    ``create_app`` calls this outside tests; tests call it explicitly.
    """
    from coverset.executors.base import Executor, ReapingExecutor
    from coverset.orchestrator.pool import POOL_INTERVAL_S, PoolMaintainer, supports_pool
    from coverset.orchestrator.reaper import REAP_INTERVAL_S, reap_forever
    from coverset.orchestrator.runner import OrchestratorContext

    settings: Settings = app.extensions["settings"]
    ctx = OrchestratorContext.from_app(app)
    maintainers: list[PoolMaintainer] = []
    started: set[int] = set()
    serving: list[bool] = []

    def start_for(executor: Executor) -> None:
        """Start the reaper (and pool maintainer) of ``executor`` once, while serving."""
        if not serving or id(executor) in started:
            return
        started.add(id(executor))
        if isinstance(executor, ReapingExecutor):
            app.add_background_task(
                reap_forever,
                ctx.db,
                executor,
                interval_s=REAP_INTERVAL_S if reap_interval_s is None else reap_interval_s,
                stop=app.shutdown_event,
            )
        if not supports_pool(executor):
            return
        maintainer = PoolMaintainer(ctx.db, settings, executor)
        if maintainer.enabled:
            maintainers.append(maintainer)
            if ctx.pool_maintainer is None:
                ctx.pool_maintainer = maintainer
            app.add_background_task(
                maintainer.run_forever,
                app.shutdown_event,
                interval_s=POOL_INTERVAL_S if pool_interval_s is None else pool_interval_s,
            )

    @app.before_serving
    async def _start_executor_upkeep() -> None:
        serving.append(True)
        ctx.on_executor_built = lambda _kind, executor: start_for(executor)
        if settings.executor == "modal":
            ctx.executor(settings.executor)  # built now, so the reaper runs from the start
        for executor in list(ctx.executors.values()):
            start_for(executor)

    @app.after_serving
    async def _stop_executor_upkeep() -> None:
        serving.clear()
        ctx.on_executor_built = None
        ctx.pool_maintainer = None
        await ctx.finish_background(cancel=True)
        while maintainers:
            maintainer = maintainers.pop()
            try:
                drained = await maintainer.drain()
            except Exception:
                app.logger.exception("draining the warm pool failed")
            else:
                app.logger.info("warm pool drained (%d workers)", drained)
        await ctx.close()


def is_upload_request(method: str, path: str) -> bool:
    """True for ``POST`` :data:`UPLOAD_PATH`, the one request exempt from :data:`MAX_CONTENT_LENGTH`."""
    return method == "POST" and path == UPLOAD_PATH


def _request_class(base: type[Request], upload_limit: Callable[[], int]) -> type[Request]:
    """``base`` with the body limit of :data:`UPLOAD_PATH` raised to ``upload_limit()`` (read per
    request, from the app's settings).

    Quart sizes a request's body when it builds the request, from ``MAX_CONTENT_LENGTH``: a declared
    ``Content-Length`` above it makes every read of the body fail with 413, so setting
    ``request.max_content_length`` in a hook would come too late. An upload's request is therefore
    built with its own limit (``request.max_content_length`` reports it too); every other request
    keeps the app's.
    """

    class CoversetRequest(base):  # type: ignore[valid-type, misc]
        def __init__(self, method: str, scheme: str, path: str, *args: Any, **kwargs: Any) -> None:
            limit = upload_limit() if is_upload_request(method, path) else None
            if limit is not None:
                kwargs["max_content_length"] = limit
            super().__init__(method, scheme, path, *args, **kwargs)
            if limit is not None:
                self.max_content_length = limit

    CoversetRequest.__qualname__ = CoversetRequest.__name__
    return CoversetRequest


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
    """The session-signing key: derived from ``COVERSET_SECRET_KEY`` and the admin token when the key
    is set, else from the admin token, else a random key kept in data_dir.

    Deriving from the token means rotating ``COVERSET_ADMIN_TOKEN`` invalidates every login cookie,
    while the cookie signature never reveals the token itself. An explicit secret key keeps that
    property (the token is still mixed in) and adds one: the key no longer depends on a file in one
    machine's data dir, so a rebuilt container or several replicas behind one URL accept the same
    cookies, and rotating the key alone also signs everyone out.
    """
    if settings.secret_key is not None and settings.secret_key.get_secret_value():
        material = _SESSION_KEY_LABEL + settings.secret_key.get_secret_value().encode() + b"\0"
        if settings.admin_token is not None:
            material += settings.admin_token.get_secret_value().encode()
        return hashlib.sha256(material).hexdigest()
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


# -- static caching and compression -------------------------------------------------------------------

STATIC_IMMUTABLE = "public, max-age=31536000, immutable"
"""``Cache-Control`` of a static file requested at its current content version (``?v=``)."""
STATIC_REVALIDATE = "no-cache"
"""``Cache-Control`` of a static file requested without (or with a stale) version: revalidate with
the ETag each time, so a deploy is picked up at once."""
COMPRESSIBLE = frozenset(
    {
        "text/html",
        "text/css",
        "text/plain",
        "application/javascript",
        "text/javascript",
        "image/svg+xml",
        "application/json",
    }
)
"""Response types :func:`install_response_caching` gzips (never ``text/event-stream``: SSE streams)."""
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 3
"""zlib level of dynamic pages: 10x smaller HTML for 6 ms on a 3 MB treemap page (level 6 is 7 %
smaller and 3x slower); static files are compressed once at level 9 and kept."""
_GZIP_THREAD_BYTES = 256 * 1024
GZIP_FETCH_SITES = frozenset({"same-origin", "none"})
"""``Sec-Fetch-Site`` values whose dynamic responses may be gzipped (BREACH; see
:func:`install_response_caching`)."""


def install_response_caching(app: Quart) -> None:
    """Version static URLs by content and gzip text responses.

    ``url_for('static', filename=...)`` gets ``?v=<12 hex digits of the file's SHA-256>`` (an
    ``url_defaults`` hook, so no template changes), and a static response requested at the file's
    current version is ``immutable`` for a year; any other static request revalidates with its
    ETag. HTML, CSS, JavaScript, SVG and JSON bodies over 1 KiB are gzipped when the client accepts
    it (Hypercorn does not compress), dynamic ones at level 3, static ones once at level 9 and
    cached by path and mtime. Streamed bodies (the SSE event stream), ranges, 304s and responses
    that already carry a ``Content-Encoding`` pass through untouched.

    BREACH: every page carries the session's fixed CSRF token (``<body hx-headers>``) and many
    reflect query input, so a compressed page's length could leak the token to a network observer
    who makes the victim's browser request it with chosen input. Dynamic responses are therefore
    compressed only for requests the browser marks ``Sec-Fetch-Site: same-origin`` or ``none``
    (the app's own navigation, htmx requests, a typed URL or bookmark), which an attacker's page
    cannot issue; a request from another site gets the identity encoding. A client that sends no
    ``Sec-Fetch-Site`` (an old browser, curl, the CLI) gets gzip only when the body does not contain
    its session's CSRF token. Static files hold no secret and are always compressed.
    """
    import asyncio
    import gzip
    import threading

    from quart.wrappers.response import DataBody, FileBody
    from werkzeug.security import safe_join

    static_dir = Path(app.static_folder or "")
    versions: dict[str, tuple[int, int, str]] = {}
    packed: dict[str, tuple[int, int, bytes]] = {}
    lock = threading.Lock()

    def version(filename: str) -> str | None:
        joined = safe_join(str(static_dir), filename)
        if joined is None:
            return None
        try:
            st = os.stat(joined)
        except OSError:
            return None
        with lock:
            found = versions.get(filename)
        if found is not None and found[:2] == (st.st_mtime_ns, st.st_size):
            return found[2]
        digest = hashlib.sha256(Path(joined).read_bytes()).hexdigest()[:12]
        with lock:
            versions[filename] = (st.st_mtime_ns, st.st_size, digest)
        return digest

    @app.url_defaults
    def _version_static(endpoint: str, values: dict[str, object]) -> None:
        filename = values.get("filename")
        if endpoint == "static" and isinstance(filename, str) and "v" not in values:
            found = version(filename)
            if found is not None:
                values["v"] = found

    def accepts_gzip() -> bool:
        return request.accept_encodings["gzip"] > 0

    def breach_safe(body: bytes) -> bool:
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site.strip().lower() in GZIP_FETCH_SITES
        from coverset.web.auth import SESSION_CSRF

        token = session.get(SESSION_CSRF)
        return not isinstance(token, str) or not token or token.encode() not in body

    async def compressed_static(filename: str, body: bytes) -> bytes:
        joined = safe_join(str(static_dir), filename)
        st = os.stat(joined) if joined is not None else None
        key = (st.st_mtime_ns, st.st_size) if st is not None else (0, len(body))
        with lock:
            found = packed.get(filename)
        if found is not None and found[:2] == key:
            return found[2]
        data = await asyncio.to_thread(gzip.compress, body, 9, mtime=0)
        with lock:
            packed[filename] = (*key, data)
        return data

    @app.after_request
    async def _cache_and_compress(response: Response) -> Response:
        is_static = request.endpoint == "static"
        if is_static and response.status_code in (200, 304):
            filename = (request.view_args or {}).get("filename")
            wanted = request.args.get("v")
            current = version(filename) if isinstance(filename, str) else None
            response.headers["Cache-Control"] = (
                STATIC_IMMUTABLE if wanted is not None and wanted == current else STATIC_REVALIDATE
            )
            response.headers.pop("Expires", None)
        if (
            response.status_code != 200
            or response.mimetype not in COMPRESSIBLE
            or "Content-Encoding" in response.headers
            or not isinstance(response.response, DataBody | FileBody)
            or not accepts_gzip()
        ):
            return response
        body = await response.get_data()
        if len(body) < GZIP_MIN_BYTES:
            return response
        if not is_static:
            response.vary.add("Sec-Fetch-Site")  # the encoding depends on it (BREACH)
            if not breach_safe(body):
                return response
        if is_static:
            data = await compressed_static(str((request.view_args or {}).get("filename")), body)
            etag, weak = response.get_etag()
            if etag:
                response.set_etag(etag, weak=True)  # one ETag for both encodings, compared weakly
        elif len(body) >= _GZIP_THREAD_BYTES:
            data = await asyncio.to_thread(gzip.compress, body, GZIP_LEVEL, mtime=0)
        else:
            data = gzip.compress(body, GZIP_LEVEL, mtime=0)
        response.set_data(data)
        response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
        return response
