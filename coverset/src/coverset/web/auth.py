"""Access control: admin bearer token or login cookie, plus CSRF for cookie-authenticated writes.

With ``COVERSET_ADMIN_TOKEN`` unset (only allowed on a loopback bind) every route is open, but
state-changing requests still need the CSRF header, because any web page the operator visits can
make their browser POST to ``127.0.0.1``. Open mode also answers only requests whose ``Host`` names
this server (loopback names or the bind host): otherwise a page that rebinds its own DNS name to
``127.0.0.1`` would be same-origin with coverset and could read pages and CSRF tokens. The
``COVERSET_PUBLIC_URL`` host is deliberately not accepted in open mode (configuration refuses a
public URL without a token): a tunnel forwarding that host would make every internet caller the
operator. With a token, every request except the public routes below needs
``Authorization: Bearer <token>`` or the session cookie issued by ``POST /login``. Bearer requests
are exempt from CSRF: browsers never attach that header cross-site on their own. The CSRF token
travels in the ``X-CSRF-Token`` header, or for plain HTML forms on non-public routes in a
``csrf_token`` form field.

Public routes: ``POST /webhooks/*``, ``GET|HEAD /v1/verdict``, ``GET|HEAD /v1/runs/<id>`` and
``/v1/runs/<id>/events``, ``GET|HEAD /healthz``, ``GET|POST /login`` and ``/static/*``. A public
route still learns about admin credentials: :func:`current_auth` is ``open``, ``bearer`` or
``cookie`` when the request carries valid ones, and ``public`` otherwise (including when it carries
some other bearer, such as a GitHub Actions OIDC token). Every view on a public route must authorize
``public`` callers itself: webhooks check their HMAC, and the ``/v1`` views verify the OIDC token
and bind its ``repository_id`` to the requested repo or run. :func:`is_admin` tells the two apart.

``POST /login`` accepts at most :data:`LOGIN_ATTEMPTS_PER_MINUTE` attempts per client address in any
60 s window (429 with ``Retry-After`` beyond that), checked before the token is compared.
"""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from quart import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
)
from werkzeug.exceptions import Forbidden, MisdirectedRequest, NotFound, Unauthorized

from coverset.config import is_loopback_host
from coverset.web.context import app_settings

if TYPE_CHECKING:
    from coverset.web.api import SlidingWindowLimiter

AuthMethod = Literal["open", "bearer", "cookie", "public"]
"""How the current request was admitted (``public`` = a route that needs no auth)."""

ADMIN_METHODS: frozenset[AuthMethod] = frozenset({"open", "bearer", "cookie"})
"""Auth methods that grant admin access."""

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
LOOPBACK_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
"""Host names always accepted in open mode (``*.localhost`` names are accepted too)."""

LOGIN_ATTEMPTS_PER_MINUTE = 20
"""``POST /login`` attempts allowed per client address per minute (the admin token is 32+ characters,
so this only blunts online guessing and log flooding)."""
LOGIN_LIMITER_EXTENSION = "login_limiter"

_SESSION_ADMIN = "admin"
_SESSION_CSRF = "csrf"
_PUBLIC_RUN_PATH = re.compile(r"/v1/runs/[^/]+(?:/events)?")

bp = Blueprint("auth", __name__)

_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>coverset · sign in</title></head>
<body><main>
<h1>coverset</h1>
{% if failed %}<p role="alert">That token was not accepted.</p>{% endif %}
<form method="post" action="{{ url_for('auth.login') }}">
<input type="hidden" name="next" value="{{ next_path }}">
<label>Admin token <input type="password" name="token" autocomplete="current-password" required></label>
<button type="submit">Sign in</button>
</form>
</main></body></html>
"""


def is_public(method: str, path: str) -> bool:
    """True for the routes the guard admits without admin credentials (their views authorize)."""
    reading = method in ("GET", "HEAD")
    return (
        (method == "POST" and path.startswith("/webhooks/"))
        or (reading and path in ("/v1/verdict", "/healthz"))
        or (reading and _PUBLIC_RUN_PATH.fullmatch(path) is not None)
        or (path == "/login" and (reading or method == "POST"))
        or path.startswith("/static/")
    )


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


def _token_matches(candidate: str) -> bool:
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
    """True when the current request was admitted with admin access (open mode, bearer or cookie)."""
    return current_auth() in ADMIN_METHODS


def csrf_token() -> str:
    """The session's CSRF token, created on first use; send it back in the ``X-CSRF-Token`` header.

    Templates get it as ``csrf_token()`` (e.g. ``hx-headers`` on ``<body>``).
    """
    token = session.get(_SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_SESSION_CSRF] = token
    return token


def _csrf_ok(supplied: str | None = None) -> bool:
    expected = session.get(_SESSION_CSRF)
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


def _admin_credentials() -> AuthMethod | None:
    """The admin access the request's credentials grant, or None.

    A bearer header that is not the admin token grants nothing and never falls back to the cookie.
    """
    if _expected_token() is None:
        return "open"
    if (bearer := _bearer_token()) is not None:
        return "bearer" if _token_matches(bearer) else None
    if session.get(_SESSION_ADMIN) is True:
        return "cookie"
    return None


def _csrf_required(method: AuthMethod) -> bool:
    return method != "bearer" and request.method not in SAFE_METHODS


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
    admin = _admin_credentials()
    if is_public(request.method, request.path):
        granted = admin is not None and not (_csrf_required(admin) and not _csrf_ok())
        g.coverset_auth = admin if granted else "public"
        return None
    if admin is None:
        return _deny(Unauthorized, "unauthorized")
    if _csrf_required(admin) and not await _csrf_ok_with_form():
        return _deny(Forbidden, "csrf_failed")
    g.coverset_auth = admin
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


def _safe_next(value: str | None) -> str:
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


@bp.get("/login")
async def login_form() -> str | Response:
    """A minimal sign-in form (only meaningful when an admin token is configured)."""
    if _expected_token() is None:
        raise NotFound()
    return await render_template_string(
        _LOGIN_PAGE, failed=False, next_path=_safe_next(request.args.get("next"))
    )


@bp.post("/login")
async def login() -> Response | tuple[str | Response, int]:
    """Exchange the admin token (form field or JSON ``{"token": ...}``) for a session cookie.

    JSON callers get ``{"ok": true, "csrf_token": ...}``; form callers are redirected to ``next``.
    """
    if _expected_token() is None:
        raise NotFound()
    retry_after = _login_limiter().hit(request.remote_addr or "unknown")
    if retry_after is not None:
        response = jsonify({"error": "rate_limited"})
        response.status_code = 429
        response.headers["Retry-After"] = str(int(retry_after + 0.999))
        return response
    if request.is_json:
        body = await request.get_json(silent=True)
        supplied = body.get("token") if isinstance(body, dict) else None
        next_path = "/"
    else:
        form = await request.form
        supplied = form.get("token")
        next_path = _safe_next(form.get("next"))
    if not isinstance(supplied, str) or not _token_matches(supplied):
        if request.is_json:
            return jsonify({"error": "unauthorized"}), 401
        page = await render_template_string(_LOGIN_PAGE, failed=True, next_path=next_path)
        return page, 401
    session.clear()
    session[_SESSION_ADMIN] = True
    token = csrf_token()
    if request.is_json:
        return jsonify({"ok": True, "csrf_token": token})
    return redirect(next_path, code=303)


def _login_limiter() -> SlidingWindowLimiter:
    from coverset.web.api import SlidingWindowLimiter

    limiter = current_app.extensions.get(LOGIN_LIMITER_EXTENSION)
    if limiter is None:
        limiter = current_app.extensions[LOGIN_LIMITER_EXTENSION] = SlidingWindowLimiter(
            LOGIN_ATTEMPTS_PER_MINUTE
        )
    return limiter


@bp.post("/logout")
async def logout() -> Response:
    """Drop the session cookie (CSRF-protected like every other write)."""
    session.clear()
    if request.is_json:
        return jsonify({"ok": True})
    return redirect("/", code=303)


@bp.app_context_processor
def _inject_csrf() -> dict[str, Callable[[], str]]:
    return {"csrf_token": csrf_token}
