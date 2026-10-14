"""``/setup``: connect coverset to GitHub (App manifest flow) or explain token mode.

``GET /setup`` renders the manifest form with a fresh random ``state`` kept in the session.
GitHub redirects to ``GET /setup/github/callback?code=...&state=...``. The callback and the
finish step are accepted from an admin session (cookie or bearer), or in open mode only from the
local operator: a loopback client address *and* a loopback ``Host`` *and* no ``Forwarded`` /
``X-Forwarded-*`` headers (a tunnel connects from 127.0.0.1 too). A bad ``state`` is a 400. The
callback moves the one-time ``code`` into the session and 303-redirects to the code-free
``/setup/github/finish``, which exchanges it and writes ``.env`` plus the PEM with mode 0600.
When credentials already exist it answers 409; replacing them is a ``POST`` (``overwrite=1`` plus
the CSRF token) that always needs an admin session. Templates live under ``templates/setup/``
(plain semantic HTML, no scripts).
"""

from __future__ import annotations

import hmac
import json
import secrets
from pathlib import Path
from typing import Any

from quart import Blueprint, Response, current_app, redirect, render_template, request, session, url_for
from werkzeug.exceptions import BadRequest

from coverset.config import is_loopback_host
from coverset.forge.base import ForgeError
from coverset.forge.manifest import (
    CODE_RE,
    CredentialsExist,
    build_manifest,
    exchange_code,
    existing_credentials,
    manifest_form_action,
    public_url_reachable,
    write_credentials,
)
from coverset.forge.markdown import safe_url
from coverset.forge.registry import forge_mode
from coverset.web.auth import current_auth, is_admin, request_host_is_loopback
from coverset.web.context import app_settings

bp = Blueprint("setup", __name__)

STATE_SESSION_KEY = "github_manifest_state"
CODE_SESSION_KEY = "github_manifest_code"
ENV_FILE_CONFIG = "COVERSET_SETUP_ENV_FILE"
"""``app.config`` key for the ``.env`` path the callback writes (default ``.env`` in the cwd)."""
TRANSPORT_EXTENSION = "github_transport"
"""Optional ``app.extensions`` key: an httpx transport for the manifest exchange (tests)."""


def _env_path() -> Path:
    return Path(current_app.config.get(ENV_FILE_CONFIG, ".env"))


def _key_path() -> Path:
    return Path(app_settings().github_app_private_key_path)


def _mode_label() -> str:
    try:
        return forge_mode(app_settings())
    except ValueError as exc:
        return f"misconfigured ({exc})"


@bp.get("/setup")
async def index() -> str:
    """The setup page: manifest form, token-mode instructions and the allowed owners."""
    settings = app_settings()
    state = secrets.token_urlsafe(32)
    session[STATE_SESSION_KEY] = state
    organization = request.args.get("org") or None
    try:
        action = manifest_form_action(state, organization)
    except ValueError:
        raise BadRequest("invalid organization name") from None
    manifest = build_manifest(settings.public_url)
    return await render_template(
        "setup/index.html",
        action=action,
        organization=organization,
        manifest_json=json.dumps(manifest, indent=2),
        manifest=manifest,
        public_url=settings.public_url,
        reachable=public_url_reachable(settings.public_url),
        allowed_owners=settings.allowed_owners,
        allowed_repos=settings.allowed_repos,
        mode=_mode_label(),
        existing=existing_credentials(_env_path(), _key_path()),
        key_path=str(_key_path()),
        env_path=str(_env_path()),
        oidc_audience=settings.oidc_audience,
    )


FORWARDING_HEADERS = frozenset({"via", "true-client-ip", "cf-connecting-ip"})
"""Headers a proxy or tunnel adds; their presence means the caller is not the local operator."""

FORWARDING_HEADER_PARTS = ("forwarded", "client-ip", "real-ip", "proxy")
"""Any header whose name contains one of these (``Forwarded``, ``X-Forwarded-For``,
``X-Original-Forwarded-For``, ``X-Client-IP``, ``Fastly-Client-IP``, ``X-Cluster-Client-IP``,
``X-Real-IP``, ``X-ProxyUser-Ip``, ...) is a proxy's too: proxies invent new names, so the check
matches the families rather than a list."""


def _forwarded() -> bool:
    for name, _value in request.headers.items():
        lowered = name.lower()
        if lowered in FORWARDING_HEADERS or any(part in lowered for part in FORWARDING_HEADER_PARTS):
            return True
    return False


def _admin_session() -> bool:
    """An instance admin with credentials: the admin token (bearer or cookie) or a signed-in user
    with ``role = admin`` (open mode is not a session: anyone on loopback has it)."""
    return is_admin() and current_auth() != "open"


def _local_operator() -> bool:
    """A direct loopback request: loopback client, loopback ``Host`` and no proxy headers.

    Behind a tunnel or reverse proxy the client address is the proxy's (usually 127.0.0.1), so the
    address alone proves nothing; the ``Host`` and forwarding headers give a tunnel away. A missing
    client address (Unix socket) is not accepted.
    """
    remote = request.remote_addr or ""
    return bool(remote) and is_loopback_host(remote) and request_host_is_loopback() and not _forwarded()


def _callback_allowed() -> bool:
    """Callers holding an admin session, or the local operator (see :func:`_local_operator`)."""
    return _admin_session() or _local_operator()


async def _problem(status: int, title: str, message: str, **extra: Any) -> tuple[str, int]:
    page = await render_template("setup/problem.html", title=title, message=message, **extra)
    return page, status


async def _not_allowed() -> tuple[str, int]:
    return await _problem(
        403,
        "Not allowed",
        "Finish the GitHub App setup in a browser on the machine running coverset (not through a "
        "tunnel), or sign in with the admin token first.",
    )


@bp.get("/setup/github/callback")
async def github_callback() -> Response | tuple[str, int]:
    """GitHub's redirect: check the state, move the one-time code into the session, and send the
    browser on to :func:`github_finish` so the code never sits in a URL that is logged or shared."""
    if not _callback_allowed():
        return await _not_allowed()
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected = session.get(STATE_SESSION_KEY)
    if not expected or not state or not hmac.compare_digest(state.encode(), str(expected).encode()):
        return await _problem(
            400, "Setup link expired", "The setup state did not match. Start again from /setup."
        )
    session.pop(STATE_SESSION_KEY, None)
    if not CODE_RE.fullmatch(code):
        return await _problem(
            400, "Setup link expired", "GitHub sent no usable code. Start again from /setup."
        )
    session[CODE_SESSION_KEY] = code
    response = redirect(url_for("setup.github_finish"), code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/setup/github/finish")
async def github_finish() -> str | tuple[str, int]:
    """Exchange the stored code unless credentials exist; then ask an admin to confirm (POST)."""
    if not _callback_allowed():
        return await _not_allowed()
    if not session.get(CODE_SESSION_KEY):
        return await _problem(400, "Setup link expired", "No setup is in progress. Start again from /setup.")
    existing = existing_credentials(_env_path(), _key_path())
    if existing:
        confirm = url_for("setup.github_finish_confirm") if _admin_session() else None
        return await _problem(
            409,
            "Credentials already stored",
            "coverset already has GitHub App credentials. Replacing them disconnects the old App.",
            existing=existing,
            confirm_action=confirm,
            hint=(
                "Replacing credentials needs an admin session: set COVERSET_ADMIN_TOKEN, restart, sign "
                "in and run /setup again, or remove the stored key and keys from .env yourself."
            ),
        )
    return await _exchange(overwrite=False)


@bp.post("/setup/github/finish")
async def github_finish_confirm() -> str | tuple[str, int]:
    """Replace stored credentials (admin session and CSRF token required)."""
    if not _admin_session():
        return await _not_allowed()
    form = await request.form
    if form.get("overwrite") != "1":
        raise BadRequest("missing overwrite=1")
    if not session.get(CODE_SESSION_KEY):
        return await _problem(400, "Setup link expired", "No setup is in progress. Start again from /setup.")
    return await _exchange(overwrite=True)


async def _exchange(*, overwrite: bool) -> str | tuple[str, int]:
    code = str(session.pop(CODE_SESSION_KEY, ""))
    settings = app_settings()
    try:
        credentials = await exchange_code(code, transport=current_app.extensions.get(TRANSPORT_EXTENSION))
        write_credentials(
            credentials,
            env_path=_env_path(),
            key_path=_key_path(),
            allowed_owners=settings.allowed_owners,
            overwrite=overwrite,
        )
    except ForgeError as exc:
        return await _problem(502, "GitHub did not finish the setup", str(exc))
    except CredentialsExist:
        return await _problem(409, "Credentials already stored", "Start again from /setup.")
    from coverset.web.audit import audit_now

    await audit_now(
        "credentials_written",
        target_kind="github_app",
        target_id=credentials.app_id,
        details={"slug": credentials.slug, "owner": credentials.owner, "overwrite": overwrite},
    )
    return await render_template(
        "setup/done.html",
        credentials=credentials,
        app_url=safe_url(credentials.html_url),
        env_path=str(_env_path()),
        key_path=str(_key_path()),
    )
