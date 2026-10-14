"""Access control: who is calling, and whether this request may proceed at all.

The access guard (:func:`access_guard`, a ``before_app_request`` hook) resolves one
:class:`~coverset.web.principal.Principal` for every request and stores it with
:func:`~coverset.web.principal.set_principal`; views then ask :func:`~coverset.web.principal.require_repo`
and never look at credentials themselves. The credentials it understands, in the order tried:

* **open mode** (no ``COVERSET_ADMIN_TOKEN``, only allowed on a loopback bind): every caller is the
  local operator, an instance admin. Open mode answers only requests whose ``Host`` names this
  server (loopback names or the bind host): a page that rebinds its own DNS name to ``127.0.0.1``
  would otherwise be same-origin with coverset and could read pages and CSRF tokens. The
  ``COVERSET_PUBLIC_URL`` host is deliberately not accepted (configuration refuses a public URL
  without a token): a tunnel forwarding that host would make every internet caller the operator.
* ``Authorization: Bearer <admin token>``: the instance admin.
* ``Authorization: Bearer cst_...``: an API token (:mod:`coverset.web.tokens`); a personal token
  acts as its user within its scopes, a repository token on its one repository. A ``cst_`` token
  that does not verify is refused outright and never falls back to a cookie. Each rejection
  counts as a failed sign-in attempt for the client address (the login limiter: 429 beyond
  :data:`LOGIN_ATTEMPTS_PER_MINUTE` a minute) and is audited at most once a minute per address and
  token prefix (:class:`RejectionLog`), so a bogus bearer replayed at line rate cannot grow the
  database or bury the audit page.
* any other bearer (a GitHub Actions OIDC token): no credentials for the guard. On the public
  ``/v1`` routes the view verifies it and binds it to a repository; elsewhere it is a 401.
* the session cookie: either the admin flag set by ``POST /login`` with the token, or the id of a
  server-side ``user_sessions`` row created by GitHub sign-in (:mod:`coverset.web.account`). The
  cookie holds the raw id, the row its sha256; the row must be unexpired and its user enabled.
  ``last_seen_at`` is touched at most every :data:`SESSION_TOUCH_INTERVAL`.
* otherwise ``public``.

Bearer requests are exempt from CSRF: browsers never attach that header cross-site on their own.
Every other state-changing request (open mode, the admin cookie, a user session) needs the
session's CSRF token in the ``X-CSRF-Token`` header, or for plain HTML forms on non-public routes
in a ``csrf_token`` form field.

Public routes: ``POST /webhooks/*``, ``GET|HEAD /v1/verdict``, ``GET|HEAD /v1/runs/<id>`` and
``/v1/runs/<id>/events``, ``GET|HEAD /healthz``, ``GET|POST /login``, ``GET /auth/github`` and its
callback, and ``/static/*``. A public route still learns about credentials: :func:`current_auth` is
``open``, ``bearer``, ``cookie``, ``user`` or ``token`` when the request carries valid ones, and
``public`` otherwise. Every view on a public route must authorize ``public`` callers itself:
webhooks check their HMAC, and the ``/v1`` views verify the OIDC token. ``/static/*`` is the one
exception: assets are served to everyone without resolving credentials at all, so a page load's
handful of asset requests costs no session or token queries.

Signed-in non-admins are admitted to every other route except the instance-admin ones
(:data:`ADMIN_ONLY_PREFIXES`: setup, the outbox, instance settings, the facts SQL API, the audit
log), which answer 403; repository-scoped views then apply their own ``require_repo``.

``POST /login`` and the GitHub sign-in routes accept at most :data:`LOGIN_ATTEMPTS_PER_MINUTE`
attempts per client address in any 60 s window (429 with ``Retry-After`` beyond that).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlencode, urlsplit

from quart import Blueprint, Response, current_app, g, jsonify, redirect, request, session
from quart.blueprints import BlueprintSetupState
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from werkzeug.exceptions import Forbidden, MisdirectedRequest, Unauthorized

from coverset.config import is_loopback_host
from coverset.db.models import ApiToken, RepoAccess, User, UserSession
from coverset.web import tokens
from coverset.web.context import app_db, app_settings
from coverset.web.principal import ADMIN, OPEN, PUBLIC, Principal, current_principal, set_principal

if TYPE_CHECKING:
    from coverset.web.api import SlidingWindowLimiter

log = logging.getLogger(__name__)

AuthMethod = Literal["open", "bearer", "cookie", "user", "token", "public"]
"""How the current request was admitted (``public`` = a route that needs no auth).

``bearer`` and ``cookie`` are the admin token; ``user`` is a GitHub session; ``token`` a ``cst_``
API token.
"""

ADMIN_METHODS: frozenset[AuthMethod] = frozenset({"open", "bearer", "cookie"})
"""Auth methods that are the admin token itself (``is_admin`` also counts admin-role users)."""

BEARER_METHODS: frozenset[AuthMethod] = frozenset({"bearer", "token"})
"""Methods carried in an ``Authorization`` header, hence exempt from CSRF."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
LOOPBACK_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
"""Host names always accepted in open mode (``*.localhost`` names are accepted too)."""

LOGIN_ATTEMPTS_PER_MINUTE = 20
"""Sign-in attempts allowed per client address per minute (the admin token is 32+ characters, so
this only blunts online guessing and log flooding; for the OAuth routes it caps redirect churn;
a ``cst_`` bearer that does not verify is charged as an attempt too)."""
LOGIN_LIMITER_EXTENSION = "login_limiter"
REJECTION_LOG_EXTENSION = "token_rejection_log"
REJECTION_AUDIT_INTERVAL_S = 60.0
"""A ``token_rejected`` row is written at most this often per client address and token prefix."""

SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
"""``user_sessions.last_seen_at`` is written at most this often per session."""
TOKEN_TOUCH_INTERVAL = timedelta(minutes=1)
"""``api_tokens.last_used_at`` is written at most this often per token."""

ADMIN_ONLY_PREFIXES: tuple[str, ...] = (
    "/setup",
    "/forge/outbox",
    "/settings/instance",
    "/v1/facts",
    "/audit",
    "/modal/",
    "/admin",
)
"""Paths only instance admins may reach, whatever their repository permissions."""

SESSION_ADMIN = "admin"
SESSION_CSRF = "csrf"
SESSION_ID = "sid"
"""Cookie-session keys: the admin flag, the CSRF token and the raw user-session id."""

_PUBLIC_RUN_PATH = re.compile(r"/v1/runs/[^/]+(?:/events)?")
_OAUTH_PATHS = frozenset({"/auth/github", "/auth/github/callback"})

bp = Blueprint("auth", __name__)


def is_public(method: str, path: str) -> bool:
    """True for the routes the guard admits without credentials (their views authorize)."""
    reading = method in ("GET", "HEAD")
    return (
        (method == "POST" and path.startswith("/webhooks/"))
        or (reading and path in ("/v1/verdict", "/healthz"))
        or (reading and _PUBLIC_RUN_PATH.fullmatch(path) is not None)
        or (path == "/login" and (reading or method == "POST"))
        or (reading and path in _OAUTH_PATHS)
        or (path == "/v1/bundles" and method == "POST")
        or (reading and path.startswith("/v1/bundles/"))
        or path.startswith("/static/")
    )


def is_admin_only(path: str) -> bool:
    """True for paths reserved to instance admins (see :data:`ADMIN_ONLY_PREFIXES`)."""
    for prefix in ADMIN_ONLY_PREFIXES:
        base = prefix.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _request_host_name() -> str:
    """The request's ``Host`` without port or IPv6 brackets, lower-cased, no trailing dot."""
    host = request.host.strip().lower()
    if host.startswith("["):
        name = host[1 : host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        name = host.partition(":")[0]
    else:
        name = host
    return name.rstrip(".")


def request_host_is_loopback() -> bool:
    """True when the request's ``Host`` is a loopback name (``localhost``, ``*.localhost``, 127/8, ::1)."""
    name = _request_host_name()
    return name in LOOPBACK_HOST_NAMES or name.endswith(".localhost") or is_loopback_host(name)


def _host_allowed() -> bool:
    """True when ``Host`` names this server (see the module docstring on DNS rebinding)."""
    name = _request_host_name()
    return request_host_is_loopback() or name == app_settings().bind_host.lower()


def _expected_token() -> bytes | None:
    token = app_settings().admin_token
    return token.get_secret_value().encode() if token is not None else None


def admin_token_configured() -> bool:
    """True when ``COVERSET_ADMIN_TOKEN`` is set (the token form is offered on the login page)."""
    return _expected_token() is not None


def token_matches(candidate: str) -> bool:
    """Constant-time comparison of ``candidate`` with the configured admin token."""
    expected = _expected_token()
    return expected is not None and hmac.compare_digest(candidate.encode(), expected)


def _bearer_token() -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _wants_json() -> bool:
    if request.path.startswith("/v1/") or request.is_json:
        return True
    accept = request.accept_mimetypes
    return accept.best_match(["application/json", "text/html"]) == "application/json"


def _wants_html_page() -> bool:
    """A browser navigating to a page (``Accept: text/html``, a safe method, not htmx)."""
    return (
        request.method in ("GET", "HEAD")
        and "text/html" in request.headers.get("Accept", "")
        and request.headers.get("HX-Request") != "true"
    )


async def _deny_page(error: type[Unauthorized | Forbidden], slug: str) -> Response:
    """What a browser sees: the login page for a 401 (when there is one), a styled 403 otherwise."""
    from coverset.web.account import login_available
    from coverset.web.pages import render_page

    if error is Unauthorized and login_available():
        target = request.full_path.rstrip("?") if request.query_string else request.path
        response = redirect(f"/login?{urlencode({'next': target})}", code=303)
        response.headers["Cache-Control"] = "no-store"
        return response  # type: ignore[return-value]
    if error is Unauthorized:
        return _deny(error, slug)
    heading = "Instance admins only" if slug == "admin_only" else "Not allowed"
    message = (
        "This page is reserved to instance admins (the admin token, or a GitHub user with the admin role)."
        if slug == "admin_only"
        else f"The request was refused: {slug.replace('_', ' ')}."
    )
    return await render_page(
        "error.html",
        status=403,
        code=403,
        heading=heading,
        message=message,
        hint="Your repositories are on the dashboard.",
    )


def _deny(error: type[Unauthorized | Forbidden | MisdirectedRequest], slug: str) -> Response:
    if _wants_json():
        response = jsonify({"error": slug})
    else:
        response = Response(f"{error.code} {slug.replace('_', ' ')}\n", mimetype="text/plain")
    response.status_code = error.code
    if error is Unauthorized:
        response.headers["WWW-Authenticate"] = 'Bearer realm="coverset"'
    return response


def current_auth() -> AuthMethod | None:
    """How the current request was admitted, or None if the access guard has not admitted it."""
    return g.get("coverset_auth")


def is_admin() -> bool:
    """True when the current request acts as an instance admin: open mode, the admin token (bearer
    or cookie), a signed-in user with ``role = admin`` or a personal token of one with the ``admin``
    scope."""
    return current_principal().instance_admin


def csrf_token() -> str:
    """The session's CSRF token, created on first use; send it back in the ``X-CSRF-Token`` header.

    Templates get it as ``csrf_token()`` (e.g. ``hx-headers`` on ``<body>``).
    """
    token = session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_CSRF] = token
    return token


def _csrf_ok(supplied: str | None = None) -> bool:
    expected = session.get(SESSION_CSRF)
    if supplied is None:
        supplied = request.headers.get(CSRF_HEADER, "")
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


async def _csrf_ok_with_form() -> bool:
    """The header, else a ``csrf_token`` field of a urlencoded or multipart form (plain HTML forms)."""
    if _csrf_ok():
        return True
    if request.mimetype not in ("application/x-www-form-urlencoded", "multipart/form-data"):
        return False
    form = await request.form
    field = form.get(CSRF_FORM_FIELD)
    return isinstance(field, str) and _csrf_ok(field)


def _csrf_required(method: AuthMethod) -> bool:
    return method not in BEARER_METHODS and request.method not in SAFE_METHODS


# -- resolving credentials -----------------------------------------------------------------------


def hash_session_id(raw: str) -> str:
    """The ``user_sessions.id`` of a raw cookie session id (sha256, so a database read-out cannot
    be replayed as a cookie)."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def load_repo_permissions(db: AsyncSession, user_id: int) -> dict[int, str]:
    """``repo_id -> permission`` from ``repo_access`` for one user (one query)."""
    rows = await db.execute(
        select(RepoAccess.repo_id, RepoAccess.permission).where(RepoAccess.user_id == user_id)
    )
    return {int(repo_id): str(permission) for repo_id, permission in rows.all()}


def user_principal(user: User, permissions: dict[int, str], *, session_id: str | None = None) -> Principal:
    """The principal of a signed-in user."""
    return Principal(
        kind="user",
        user_id=user.id,
        login=user.login,
        label=f"@{user.login}",
        instance_admin=user.role == "admin",
        repo_permissions=permissions,
        session_id=session_id,
    )


async def _session_principal(raw_sid: str) -> Principal | None:
    """The principal of a ``user_sessions`` row, or None when the row is gone, expired or its user
    is disabled. Caches the user row on ``g`` for the account page."""
    if not isinstance(raw_sid, str) or not raw_sid:
        return None
    digest = hash_session_id(raw_sid)
    now = app_settings().now()
    async with app_db().session() as db:
        pair = (
            await db.execute(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .where(UserSession.id == digest)
            )
        ).first()
        if pair is None:
            return None
        row, user = pair
        if row.expires_at <= now or user.disabled:
            return None
        if now - row.last_seen_at >= SESSION_TOUCH_INTERVAL:
            row.last_seen_at = now
            await db.commit()
        permissions = await load_repo_permissions(db, user.id)
    g.coverset_user = user
    from coverset.web.account import schedule_access_refresh  # the account module imports this one

    schedule_access_refresh(user)
    return user_principal(user, permissions, session_id=digest)


async def _token_principal(raw: str) -> Principal | None:
    """The principal of a ``cst_`` bearer, or None when it does not verify."""
    now = app_settings().now()
    async with app_db().session() as db:
        row = await tokens.verify(db, raw, now=now)
        if row is None:
            return None
        principal = await _principal_for_token(db, row)
        if principal is None:
            return None
        if row.last_used_at is None or now - row.last_used_at >= TOKEN_TOUCH_INTERVAL:
            row.last_used_at = now
            await db.commit()
    return principal


async def _principal_for_token(db: AsyncSession, row: ApiToken) -> Principal | None:
    scopes = frozenset(str(s) for s in (row.scopes or []))
    short = f"{row.name} ({row.prefix}…)"
    if row.kind == "repo":
        if row.repo_id is None:
            return None
        return Principal(
            kind="token",
            label=f"repo token {short}",
            scopes=scopes,
            bound_repo_id=row.repo_id,
            token_id=row.id,
        )
    user = await db.get(User, row.user_id) if row.user_id is not None else None
    if user is None or user.disabled:
        return None
    permissions = await load_repo_permissions(db, user.id)
    return Principal(
        kind="token",
        user_id=user.id,
        login=user.login,
        label=f"token {short} of @{user.login}",
        instance_admin=user.role == "admin" and "admin" in scopes,
        scopes=scopes,
        repo_permissions=permissions,
        token_id=row.id,
    )


class Throttled(Exception):
    """Raised inside the guard when the client has used up its failed-credential budget."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class RejectionLog:
    """Which ``(client address, token prefix)`` rejections were audited in the last minute.

    Without it a bogus bearer replayed at line rate would write one ``token_rejected`` row per
    request. :meth:`note` answers how many identical rejections were dropped since the last row
    for that pair (so nothing is lost in aggregate) or None when this one is dropped too. Entries
    older than :data:`REJECTION_AUDIT_INTERVAL_S` are pruned once the map grows past 10,000 keys.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._seen: dict[tuple[str, str], tuple[float, int]] = {}

    def note(self, key: tuple[str, str]) -> int | None:
        now = self._clock()
        entry = self._seen.get(key)
        if entry is not None and now - entry[0] < REJECTION_AUDIT_INTERVAL_S:
            self._seen[key] = (entry[0], entry[1] + 1)
            return None
        if len(self._seen) > 10_000:
            self._seen = {k: v for k, v in self._seen.items() if now - v[0] < REJECTION_AUDIT_INTERVAL_S}
        self._seen[key] = (now, 0)
        return entry[1] if entry is not None else 0


def rejection_log() -> RejectionLog:
    """The per-app :class:`RejectionLog`."""
    found = current_app.extensions.get(REJECTION_LOG_EXTENSION)
    if found is None:
        found = current_app.extensions[REJECTION_LOG_EXTENSION] = RejectionLog()
    return found


async def _resolve() -> tuple[AuthMethod, Principal] | None:
    """The method and principal the request's credentials grant, or None (see the module docstring).

    A bearer header that is neither the admin token nor a valid ``cst_`` token grants nothing and
    never falls back to the cookie. A ``cst_`` rejection is charged to the login limiter first:
    over budget it raises :class:`Throttled` (a 429, nothing audited), within it the rejection is
    audited through :class:`RejectionLog`.
    """
    if _expected_token() is None:
        return "open", OPEN
    if (bearer := _bearer_token()) is not None:
        if token_matches(bearer):
            return "bearer", ADMIN
        if tokens.looks_like_token(bearer):
            principal = await _token_principal(bearer)
            if principal is None:
                retry_after = login_limiter().hit(request.remote_addr or "unknown")
                if retry_after is not None:
                    raise Throttled(retry_after)
                await _audit_rejected_token(bearer)
                return None
            return "token", principal
        return None
    if session.get(SESSION_ADMIN) is True:
        return "cookie", ADMIN
    if (sid := session.get(SESSION_ID)) is not None:
        principal = await _session_principal(sid)
        if principal is not None:
            return "user", principal
        session.pop(SESSION_ID, None)  # a dead session: drop the id so the cookie stops naming it
    return None


async def _audit_rejected_token(raw: str) -> None:
    from coverset.web.audit import audit_now

    match = tokens.TOKEN_PATTERN.match(raw)
    prefix = match.group(1) if match else "?"  # the public 8-character prefix, never the secret
    suppressed = rejection_log().note((request.remote_addr or "unknown", prefix))
    if suppressed is None:
        return
    await audit_now(
        "token_rejected",
        principal=PUBLIC,
        details={"prefix": prefix, "path": request.path, "suppressed": suppressed},
    )


@bp.before_app_request
async def access_guard() -> Response | None:
    """Admit or reject every request before it reaches its view (see the module docstring).

    Requests that match no route (or no method of a route) pass through untouched so Quart
    answers them with a plain 404/405; no view runs for them.
    """
    if _expected_token() is None and not _host_allowed():
        return _deny(MisdirectedRequest, "bad_host")
    if request.routing_exception is not None:
        return None
    if request.path.startswith("/static/"):
        g.coverset_auth = "public"
        set_principal(PUBLIC)
        return None
    try:
        resolved = await _resolve()
    except Throttled as exc:
        return too_many_requests(exc.retry_after)
    if is_public(request.method, request.path):
        granted = resolved is not None and not (_csrf_required(resolved[0]) and not _csrf_ok())
        if granted and resolved is not None:
            g.coverset_auth, principal = resolved
            set_principal(principal)
        else:
            g.coverset_auth = "public"
            set_principal(PUBLIC)
        return None
    if resolved is None:
        if _wants_html_page():
            return await _deny_page(Unauthorized, "unauthorized")
        return _deny(Unauthorized, "unauthorized")
    method, principal = resolved
    if _csrf_required(method) and not await _csrf_ok_with_form():
        return _deny(Forbidden, "csrf_failed")
    if not principal.instance_admin and is_admin_only(request.path):
        set_principal(principal)  # the error page's header shows who is signed in
        if _wants_html_page():
            return await _deny_page(Forbidden, "admin_only")
        return _deny(Forbidden, "admin_only")
    g.coverset_auth = method
    set_principal(principal)
    return None


def require_admin[**P, R](view: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorate a view that must never be reachable as a public route.

    The access guard already protects every non-public path; this is defence in depth for views
    whose path could fall under a public prefix, e.g. under ``/webhooks/``.
    """

    @wraps(view)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if not is_admin():
            raise Unauthorized()
        return await view(*args, **kwargs)

    return wrapper


def safe_next(value: str | None) -> str:
    """A same-site path to redirect to after login, else ``/``.

    Rejects absolute and scheme-relative URLs, backslashes, spaces and every control character:
    browsers drop tabs and newlines from URLs, so ``/\\t/evil.example`` would leave the site.
    """
    if not value or any(ord(char) < 0x20 or char in "\\ \x7f" for char in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def login_limiter() -> SlidingWindowLimiter:
    """The per-app limiter shared by ``POST /login`` and the GitHub sign-in routes."""
    from coverset.web.api import SlidingWindowLimiter

    limiter = current_app.extensions.get(LOGIN_LIMITER_EXTENSION)
    if limiter is None:
        limiter = current_app.extensions[LOGIN_LIMITER_EXTENSION] = SlidingWindowLimiter(
            LOGIN_ATTEMPTS_PER_MINUTE
        )
    return limiter


def too_many_requests(retry_after: float) -> Response:
    """The 429 answer with its ``Retry-After`` (whole seconds, rounded up)."""
    response = jsonify({"error": "rate_limited"})
    response.status_code = 429
    response.headers["Retry-After"] = str(int(retry_after + 0.999))
    return response


def rate_limited() -> Response | None:
    """Charge the login limiter for this client; a 429 response when it is over its budget."""
    retry_after = login_limiter().hit(request.remote_addr or "unknown")
    if retry_after is None:
        return None
    return too_many_requests(retry_after)


# -- server-side sessions ------------------------------------------------------------------------


async def create_user_session(db: AsyncSession, user: User, *, now: datetime) -> str:
    """Insert a ``user_sessions`` row for ``user`` and return the raw id for the cookie.

    The caller commits. The cookie session is reset too (session fixation: a session id chosen
    before sign-in never survives it) and gets a fresh CSRF token, which the row also stores. The
    cookie is made permanent (``Max-Age``/``Expires`` from ``PERMANENT_SESSION_LIFETIME``, which
    the app sets from ``COVERSET_SESSION_DAYS``) so it outlives the browser like the row does;
    the admin-token cookie stays a browser-session cookie on purpose, it has no row to revoke.
    """
    raw = secrets.token_urlsafe(32)
    session.clear()
    session.permanent = True
    session[SESSION_ID] = raw
    csrf = csrf_token()
    db.add(
        UserSession(
            id=hash_session_id(raw),
            user_id=user.id,
            csrf=csrf,
            ip=(request.remote_addr or None) and request.remote_addr[:64],
            user_agent=(request.headers.get("User-Agent") or "")[:255] or None,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=app_settings().session_days),
        )
    )
    return raw


async def delete_user_session(db: AsyncSession, session_id: str) -> bool:
    """Delete one ``user_sessions`` row by its hashed id; True when it existed (the caller commits)."""
    row = await db.get(UserSession, session_id)
    if row is None:
        return False
    await db.delete(row)
    return True


@bp.app_context_processor
def _inject_csrf() -> dict[str, Callable[[], str]]:
    return {"csrf_token": csrf_token}


@bp.record_once
def _align_cookie_lifetime(state: BlueprintSetupState) -> None:
    """Make the permanent cookie last exactly as long as a ``user_sessions`` row
    (``COVERSET_SESSION_DAYS``) unless the app configured a lifetime itself (Quart's own default
    is 31 days, which is what "not configured" looks like here)."""
    settings = state.app.extensions.get("settings")
    configured = state.app.config.get("PERMANENT_SESSION_LIFETIME")
    if settings is not None and configured in (None, timedelta(days=31)):
        state.app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=settings.session_days)
