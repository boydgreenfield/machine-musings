"""``/setup``: connect coverset to GitHub (App manifest flow) or explain token mode.

``GET /setup`` renders the manifest form with a fresh random ``state`` kept in the session, and the
GitHub credentials in force: an App stored by this page, credentials from the environment (the page
is then read-only), or none. GitHub redirects to ``GET /setup/github/callback?code=...&state=...``.
The callback, the finish step and "Forget the App" are accepted from an admin session (cookie or
bearer), or in open mode only from the local operator: a loopback client address *and* a loopback
``Host`` *and* no ``Forwarded`` / ``X-Forwarded-*`` headers (a tunnel connects from 127.0.0.1 too).
A bad ``state`` is a 400. The callback moves the one-time ``code`` into the session and
303-redirects to the code-free ``/setup/github/finish``, which exchanges it and seals the App into
the database (:func:`coverset.forge.credentials.store_app`, under ``COVERSET_SECRET_KEY``). Every
replica uses it within seconds; nothing is restarted. When an App is already stored the finish step
answers 409; replacing it is a ``POST`` (``overwrite=1`` plus the CSRF token) that always needs an
admin session. ``POST /setup/github/forget`` (CSRF token) deletes the stored App; both are audited.
Templates live under ``templates/setup/`` (plain semantic HTML, no scripts).
"""

from __future__ import annotations

import hmac
import json
import secrets
from typing import Any

from quart import Blueprint, Response, current_app, redirect, render_template, request, session, url_for
from werkzeug.exceptions import BadRequest

from coverset.config import is_loopback_host
from coverset.db.vault import KEY_MISSING, VaultError
from coverset.forge.base import ForgeError
from coverset.forge.credentials import (
    AppCredentials,
    CredentialsExist,
    CredentialsInEnvironment,
    GitHubCredentials,
    effective_owners,
    forget_app,
    store_app,
)
from coverset.forge.manifest import (
    CODE_RE,
    build_manifest,
    exchange_code,
    manifest_form_action,
    public_url_reachable,
)
from coverset.forge.markdown import safe_url
from coverset.forge.registry import credential_cache, forge_mode
from coverset.web.auth import current_auth, is_admin, request_host_is_loopback
from coverset.web.context import app_db, app_settings

bp = Blueprint("setup", __name__)

STATE_SESSION_KEY = "github_manifest_state"
CODE_SESSION_KEY = "github_manifest_code"
TRANSPORT_EXTENSION = "github_transport"
"""Optional ``app.extensions`` key: an httpx transport for the manifest exchange (tests)."""


def _credentials() -> GitHubCredentials:
    """The credentials in force (the app refreshed them before this request)."""
    return credential_cache(current_app).snapshot


def _mode_label(credentials: GitHubCredentials) -> str:
    try:
        return forge_mode(app_settings(), credentials=credentials)
    except ValueError as exc:
        return f"misconfigured ({exc})"


def _key_set() -> bool:
    key = app_settings().secret_key
    return key is not None and bool(key.get_secret_value())


@bp.get("/setup")
async def index() -> str:
    """The setup page: what is connected, the manifest form, token-mode instructions."""
    settings = app_settings()
    credentials = _credentials()
    state = secrets.token_urlsafe(32)
    session[STATE_SESSION_KEY] = state
    organization = request.args.get("org") or None
    try:
        action = manifest_form_action(state, organization)
    except ValueError:
        raise BadRequest("invalid organization name") from None
    manifest = build_manifest(settings.public_url)
    reachable = public_url_reachable(settings.public_url)
    environment = credentials.source == "environment"
    return await render_template(
        "setup/index.html",
        action=action,
        organization=organization,
        manifest_json=json.dumps(manifest, indent=2),
        manifest=manifest,
        public_url=settings.public_url,
        reachable=reachable,
        allowed_owners=effective_owners(settings, credentials),
        owners_from_app=not settings.allowed_owners and bool(effective_owners(settings, credentials)),
        allowed_repos=settings.allowed_repos,
        mode=_mode_label(credentials),
        credentials=credentials,
        stored=credentials if credentials.source == "database" else None,
        environment_names=credentials.names if environment else (),
        key_set=_key_set(),
        can_create=reachable and not environment and _key_set(),
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


async def _environment_owned(credentials: GitHubCredentials) -> tuple[str, int]:
    return await _problem(
        409,
        "GitHub credentials come from the environment",
        f"{', '.join(credentials.names)} {'is' if len(credentials.names) == 1 else 'are'} set, so "
        "coverset uses those and /setup stores nothing. Remove them from the environment (and "
        "restart) to let /setup store a GitHub App instead.",
    )


async def _cannot_store() -> tuple[str, int] | None:
    """The page refusing to store an App (environment credentials, no secret key), or None."""
    credentials = _credentials()
    if credentials.source == "environment":
        return await _environment_owned(credentials)
    if not _key_set():
        return await _problem(503, "COVERSET_SECRET_KEY is not set", KEY_MISSING)
    return None


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
    """Exchange the stored code unless an App is stored; then ask an admin to confirm (POST)."""
    if not _callback_allowed():
        return await _not_allowed()
    if not session.get(CODE_SESSION_KEY):
        return await _problem(400, "Setup link expired", "No setup is in progress. Start again from /setup.")
    refusal = await _cannot_store()
    if refusal is not None:
        session.pop(CODE_SESSION_KEY, None)
        return refusal
    stored = _credentials()
    if stored.source == "database":
        confirm = url_for("setup.github_finish_confirm") if _admin_session() else None
        return await _problem(
            409,
            "Credentials already stored",
            "coverset already has a GitHub App. Replacing it disconnects the old App.",
            stored=stored,
            confirm_action=confirm,
            hint=(
                "Replacing the stored App needs an admin session: set COVERSET_ADMIN_TOKEN, restart, "
                "sign in and run /setup again. Or press Forget the App on /setup first."
            ),
        )
    return await _exchange(overwrite=False)


@bp.post("/setup/github/finish")
async def github_finish_confirm() -> str | tuple[str, int]:
    """Replace the stored App (admin session and CSRF token required)."""
    if not _admin_session():
        return await _not_allowed()
    form = await request.form
    if form.get("overwrite") != "1":
        raise BadRequest("missing overwrite=1")
    if not session.get(CODE_SESSION_KEY):
        return await _problem(400, "Setup link expired", "No setup is in progress. Start again from /setup.")
    refusal = await _cannot_store()
    if refusal is not None:
        session.pop(CODE_SESSION_KEY, None)
        return refusal
    return await _exchange(overwrite=True)


async def _exchange(*, overwrite: bool) -> str | tuple[str, int]:
    code = str(session.pop(CODE_SESSION_KEY, ""))
    settings = app_settings()
    try:
        credentials = await exchange_code(code, transport=current_app.extensions.get(TRANSPORT_EXTENSION))
    except ForgeError as exc:
        return await _problem(502, "GitHub did not finish the setup", str(exc))
    try:
        async with app_db().session() as db:
            stored = await store_app(db, settings, credentials, overwrite=overwrite)
            await db.commit()
    except CredentialsExist:
        return await _orphan(409, "Credentials already stored", credentials)
    except (CredentialsInEnvironment, VaultError) as exc:  # the checks before the exchange raced
        return await _orphan(503, "The GitHub App was not stored", credentials, str(exc))
    credential_cache(current_app).invalidate()
    from coverset.web.audit import audit_now

    await audit_now(
        "github_app_stored",
        target_kind="github_app",
        target_id=stored.app_id,
        details={"slug": stored.slug, "owner": stored.owner, "overwrite": overwrite},
    )
    return await render_template(
        "setup/done.html",
        credentials=stored,
        app_url=safe_url(stored.html_url),
        owners=effective_owners(settings, (await credential_cache(current_app).refresh())),
        owners_configured=bool(settings.allowed_owners),
    )


async def _orphan(
    status: int, title: str, credentials: AppCredentials, reason: str | None = None
) -> tuple[str, int]:
    """GitHub created an App that coverset did not keep: say so, and where to delete it."""
    where = safe_url(credentials.html_url) or "your GitHub settings (Developer settings, GitHub Apps)"
    message = f"{reason} " if reason else "Another setup stored an App first. "
    message += f"GitHub created the App {credentials.slug} anyway: delete it at {where}, then start again from /setup."
    return await _problem(status, title, message)


@bp.post("/setup/github/forget")
async def github_forget() -> str | tuple[str, int]:
    """Delete the stored App (admin session or the local operator; the access guard checks the CSRF
    token). The App itself stays on GitHub until its owner deletes it there."""
    if not _callback_allowed():
        return await _not_allowed()
    credentials = await credential_cache(current_app).refresh()
    if credentials.source == "environment":
        return await _environment_owned(credentials)
    async with app_db().session() as db:
        deleted = await forget_app(db)
        await db.commit()
    credential_cache(current_app).invalidate()
    if not deleted:
        return await _problem(404, "No App stored", "There is no stored GitHub App to forget.")
    app = credentials.app
    from coverset.web.audit import audit_now

    await audit_now(
        "github_app_forgotten",
        target_kind="github_app",
        target_id=app.app_id if app is not None else None,
        details={"slug": app.slug if app is not None else None},
    )
    where = safe_url(app.html_url) if app is not None else None
    return await _problem(
        200,
        "GitHub App forgotten",
        "coverset no longer has GitHub App credentials; webhooks answer 503 until an App is stored "
        "again. The App still exists on GitHub"
        + (f": delete it at {where} if you no longer need it." if where else "."),
    )
