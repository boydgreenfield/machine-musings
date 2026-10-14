"""Sign-in, sessions, the account page and the audit page.

Two ways in, one login page (``templates/account/login.html``):

* **Sign in with GitHub**, offered when the GitHub App's client id and secret are configured
  (``/setup`` writes them). ``GET /auth/github`` keeps a random ``state`` and the validated
  ``next`` path in the cookie session and sends the browser to GitHub's authorize page with no
  OAuth scope (a GitHub App user token already carries the App's permissions, nothing more).
  ``GET /auth/github/callback`` checks the ``state`` in constant time, exchanges the one-time code,
  fetches ``/user``, creates or updates the ``users`` row (keyed by the numeric GitHub id, never
  the login, which can change hands), stores the user token with its refresh token and expiry,
  grants ``role = admin`` to logins listed in ``COVERSET_ADMINS``, and opens a server-side session
  (:func:`coverset.web.auth.create_user_session`). It then asks GitHub which of coverset's
  repositories the user may see (:func:`sync_repo_access`), which the guard repeats in the
  background every :data:`ACCESS_SYNC_INTERVAL` and the account page on demand.
* **the admin token**, offered when ``COVERSET_ADMIN_TOKEN`` is set: the form or JSON
  ``POST /login`` sets the admin flag in the cookie session.

With neither configured (open mode without a GitHub App) ``/login`` is a 404: there is nothing
to sign in with. In open mode a GitHub sign-in still works but changes nothing: every caller is the
local operator already.

``POST /logout`` deletes the session row and clears the cookie. ``GET /account`` shows who you
are, the repositories GitHub grants you (with a refresh button), your sessions (each revocable)
and a link to the token pages. ``GET /audit`` (instance admins) lists the newest audit events with
action and repository filters. Sign-in attempts, the callback included, share the login rate limit.

Sessions belong to browsers: ``POST /logout``, ``/account/refresh`` and ``/account/sessions/<id>/revoke``
refuse ``cst_`` bearers (403), so a leaked read-only token cannot sign its owner out everywhere.

When GitHub stops accepting the user's credentials (the person revoked the App's authorization,
or the refresh token died: :class:`~coverset.forge.oauth.OAuthRevokedError`) coverset forgets what
it knew (:func:`revoke_user_access`): the ``repo_access`` rows, every session and the stored tokens
go, an ``access_revoked`` event is recorded and the next request lands on the login page with a
note. Transient GitHub failures leave the last known access standing and are retried.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from quart import Blueprint, Response, current_app, g, jsonify, redirect, render_template, request, session
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from werkzeug.exceptions import Forbidden, NotFound

from coverset.db.models import Repo, RepoAccess, User, UserSession
from coverset.forge.oauth import (
    CODE_RE,
    GitHubOAuth,
    GitHubUser,
    OAuthError,
    OAuthRevokedError,
    OAuthTokens,
)
from coverset.web.audit import PAGE_LIMIT, known_actions, recent_events, record_audit
from coverset.web.auth import (
    SESSION_ADMIN,
    admin_token_configured,
    create_user_session,
    csrf_token,
    delete_user_session,
    rate_limited,
    safe_next,
    token_matches,
    user_principal,
)
from coverset.web.context import app_db, app_settings
from coverset.web.pages import parse_int, render_page
from coverset.web.principal import ADMIN, PUBLIC, Principal, current_principal, require_instance_admin

log = logging.getLogger(__name__)

bp = Blueprint("account", __name__)

OAUTH_EXTENSION = "github_oauth"
"""``app.extensions`` key: the :class:`GitHubOAuth` client (tests inject one with a fake transport)."""
ACCESS_SYNC_EXTENSION = "repo_access_synced"
"""``app.extensions`` key: ``user_id -> when`` the repository access was last refreshed in this process."""
ACCESS_SYNC_INTERVAL = timedelta(minutes=10)
"""How often a signed-in user's repository access is re-read from GitHub (in the background)."""
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
"""A user token this close to expiry is refreshed before it is used."""
OAUTH_STATE_KEY = "oauth_state"
OAUTH_NEXT_KEY = "oauth_next"
SESSION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_SLUG = re.compile(r"[^a-z0-9_]")
LOGIN_REASONS: dict[str, str] = {
    "revoked": (
        "GitHub no longer accepts coverset's authorization for your account, so your repositories "
        "and sessions here were cleared. Sign in again to authorize it afresh."
    ),
}
"""``/login?reason=`` slugs and the note shown for them (anything else shows nothing)."""


# -- configuration ---------------------------------------------------------------------------------


def github_oauth() -> GitHubOAuth | None:
    """The App's OAuth client, or None when the client id or secret is not configured."""
    client = current_app.extensions.get(OAUTH_EXTENSION)
    if client is not None:
        return client
    settings = app_settings()
    secret = settings.github_app_client_secret
    if not settings.github_app_client_id or secret is None or not secret.get_secret_value():
        return None
    client = GitHubOAuth(settings.github_app_client_id, secret.get_secret_value())
    current_app.extensions[OAUTH_EXTENSION] = client
    return client


def github_login_available() -> bool:
    """True when "Sign in with GitHub" can be offered."""
    return github_oauth() is not None


def login_available() -> bool:
    """True when the login page has at least one option to offer."""
    return github_login_available() or admin_token_configured()


def callback_url() -> str:
    """The OAuth redirect URI: ``COVERSET_PUBLIC_URL/auth/github/callback`` (the manifest registers it)."""
    return f"{app_settings().public_url}/auth/github/callback"


# -- the login page and the admin token --------------------------------------------------------------


async def _login_page(
    *, failed: bool, next_path: str, status: int = 200, message: str | None = None
) -> Response:
    body = await render_template(
        "account/login.html",
        failed=failed,
        next_path=next_path,
        message=message,
        github=github_login_available(),
        token=admin_token_configured(),
    )
    return Response(body, status=status, mimetype="text/html")


@bp.get("/login")
async def login_form() -> Response:
    """The sign-in page (404 when neither GitHub sign-in nor the admin token is configured)."""
    if not login_available():
        raise NotFound()
    return await _login_page(
        failed=False,
        next_path=safe_next(request.args.get("next")),
        message=LOGIN_REASONS.get(request.args.get("reason", "")),
    )


@bp.post("/login")
async def login() -> Response | tuple[Response, int]:
    """Exchange the admin token (form field or JSON ``{"token": ...}``) for a session cookie.

    JSON callers get ``{"ok": true, "csrf_token": ...}``; form callers are redirected to ``next``.
    """
    if not admin_token_configured():
        raise NotFound()
    if (limited := rate_limited()) is not None:
        return limited
    if request.is_json:
        body = await request.get_json(silent=True)
        supplied = body.get("token") if isinstance(body, dict) else None
        next_path = "/"
    else:
        form = await request.form
        supplied = form.get("token")
        next_path = safe_next(form.get("next"))
    if not isinstance(supplied, str) or not token_matches(supplied):
        await _audit("login_failed", principal=PUBLIC, details={"via": "admin_token"})
        if request.is_json:
            return jsonify({"error": "unauthorized"}), 401
        return await _login_page(failed=True, next_path=next_path, status=401)
    session.clear()
    session[SESSION_ADMIN] = True
    token = csrf_token()
    await _audit("login", principal=ADMIN, details={"via": "admin_token"})
    if request.is_json:
        return jsonify({"ok": True, "csrf_token": token})
    return redirect(next_path, code=303)  # type: ignore[return-value]


@bp.post("/logout")
async def logout() -> Response:
    """Delete the session row (if any) and drop the cookie session (CSRF-protected like every write).

    A bearer has no session to end: ``cst_`` and OIDC principals are refused, so a token can never
    sign its owner's browsers out.
    """
    principal = current_principal()
    if principal.kind in ("token", "oidc"):
        raise Forbidden("A token has no session to sign out of.")
    if principal.session_id is not None:
        async with app_db().session() as db:
            await delete_user_session(db, principal.session_id)
            record_audit(db, principal, "logout", target_kind="session", target_id=principal.session_id[:12])
            await db.commit()
    elif not principal.is_public:
        await _audit("logout", principal=principal)
    session.clear()
    if request.is_json:
        return jsonify({"ok": True})
    return redirect("/", code=303)  # type: ignore[return-value]


# -- GitHub sign-in --------------------------------------------------------------------------------


@bp.get("/auth/github")
async def github_start() -> Response:
    """Start the authorization-code flow: random ``state`` in the session, redirect to GitHub."""
    oauth = github_oauth()
    if oauth is None:
        raise NotFound()
    if (limited := rate_limited()) is not None:
        return limited
    state = secrets.token_urlsafe(32)
    session[OAUTH_STATE_KEY] = state
    session[OAUTH_NEXT_KEY] = safe_next(request.args.get("next"))
    response = redirect(oauth.authorize_url(state=state, redirect_uri=callback_url()), code=303)
    response.headers["Cache-Control"] = "no-store"
    return response  # type: ignore[return-value]


@bp.get("/auth/github/callback")
async def github_callback() -> Response:
    """GitHub's redirect back: check the state, exchange the code, sign the user in."""
    oauth = github_oauth()
    if oauth is None:
        raise NotFound()
    if (limited := rate_limited()) is not None:
        return limited
    expected = session.pop(OAUTH_STATE_KEY, None)
    next_path = safe_next(session.pop(OAUTH_NEXT_KEY, None))
    state = request.args.get("state", "")
    if not expected or not state or not hmac.compare_digest(state.encode(), str(expected).encode()):
        return await _problem(
            400, "Sign-in link expired", "The sign-in state did not match. Start again from /login."
        )
    if request.args.get("error"):
        slug = _ERROR_SLUG.sub("", request.args["error"].lower())[:64] or "error"
        await _audit("login_failed", principal=PUBLIC, details={"via": "github", "error": slug})
        return await _problem(
            400, "GitHub did not sign you in", f"GitHub answered: {slug.replace('_', ' ')}."
        )
    code = request.args.get("code", "")
    if not CODE_RE.fullmatch(code):
        return await _problem(
            400, "Sign-in link expired", "GitHub sent no usable code. Start again from /login."
        )
    try:
        tokens = await oauth.exchange_code(code, redirect_uri=callback_url())
        gh_user = await oauth.fetch_user(tokens.access_token)
    except OAuthError as exc:
        await _audit("login_failed", principal=PUBLIC, details={"via": "github", "error": str(exc)})
        return await _problem(502, "GitHub did not finish the sign-in", str(exc))
    settings = app_settings()
    now = settings.now()
    async with app_db().session() as db:
        user = await upsert_user(db, gh_user, tokens, now=now, admins=settings.admins)
        if user.disabled:
            await db.commit()  # keep the refreshed profile; the sign-in itself is refused
            await _audit("login_failed", principal=PUBLIC, details={"via": "github", "reason": "disabled"})
            return await _problem(
                403, "Account disabled", f"@{user.login} is disabled on this coverset. Ask an instance admin."
            )
        await create_user_session(db, user, now=now)
        record_audit(
            db,
            user_principal(user, {}),
            "login",
            target_kind="user",
            target_id=user.id,
            details={"via": "github"},
        )
        await db.commit()
        try:
            await sync_repo_access(db, user, oauth, now=now)
        except OAuthError as exc:
            log.warning("repository access of @%s could not be read after sign-in: %s", user.login, exc)
        _mark_synced(user.id, now)
    response = redirect(next_path, code=303)
    response.headers["Cache-Control"] = "no-store"
    return response  # type: ignore[return-value]


async def upsert_user(
    db: AsyncSession, gh_user: GitHubUser, tokens: OAuthTokens, *, now: datetime, admins: list[str]
) -> User:
    """Create or refresh the ``users`` row for a GitHub account and store its OAuth tokens.

    The row is keyed by the numeric id; login, name and avatar follow GitHub. ``COVERSET_ADMINS``,
    when set, decides the role at every sign-in (listed logins are admins, others members), so
    removing a login from it demotes that user at their next sign-in; when the list is empty the
    stored role (``coverset users promote``) stands. Matching is case-insensitive like GitHub.
    """
    user = await db.scalar(select(User).where(User.forge == "github", User.external_id == str(gh_user.id)))
    if user is None:
        user = User(forge="github", external_id=str(gh_user.id), login=gh_user.login, created_at=now)
        db.add(user)
    user.login = gh_user.login
    user.name = gh_user.name
    user.avatar_url = gh_user.avatar_url
    if admins:
        listed = {login.lower() for login in admins}
        user.role = "admin" if gh_user.login.lower() in listed else "member"
    user.oauth_token = tokens.access_token
    user.oauth_refresh_token = tokens.refresh_token
    user.oauth_expires_at = now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None
    user.last_login_at = now
    await db.flush()
    return user


# -- repository access -------------------------------------------------------------------------------


async def fresh_user_token(db: AsyncSession, user: User, oauth: GitHubOAuth, *, now: datetime) -> str:
    """The user's token, refreshed first when it is within :data:`TOKEN_REFRESH_MARGIN` of expiry.

    A refreshed token (and GitHub's rotated refresh token) is stored and committed before use, so
    a crash between the refresh and the sync cannot leave a token coverset no longer holds.
    """
    if not user.oauth_token:
        raise OAuthRevokedError("no user token stored; sign in again")
    expires = user.oauth_expires_at
    if expires is None or now < expires - TOKEN_REFRESH_MARGIN:
        return user.oauth_token
    if not user.oauth_refresh_token:
        raise OAuthRevokedError("user token expired and no refresh token stored; sign in again")
    tokens = await oauth.refresh(user.oauth_refresh_token)
    user.oauth_token = tokens.access_token
    user.oauth_refresh_token = tokens.refresh_token or user.oauth_refresh_token
    user.oauth_expires_at = now + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None
    await db.commit()
    return user.oauth_token


async def sync_repo_access(
    db: AsyncSession, user: User, oauth: GitHubOAuth, *, now: datetime
) -> dict[int, str]:
    """Recompute ``repo_access`` for ``user`` from GitHub and commit; returns ``repo_id -> permission``.

    One ``/user/installations/{id}/repositories`` listing per installation covers every repository
    that installation holds (absent from the listing = no access); repositories without an
    installation id, or whose installation the user is not part of (403/404), are asked for one by
    one with ``/repos/{owner}/{name}``, where a 404 means no access. Rows are inserted, updated or
    deleted so the table is exactly GitHub's answer; the instance role is not touched here.
    """
    token = await fresh_user_token(db, user, oauth, now=now)
    repos = list(await db.scalars(select(Repo).where(Repo.forge == "github").order_by(Repo.id)))
    found: dict[int, str] = {}
    by_installation: dict[int, list[Repo]] = {}
    loose: list[Repo] = []
    for repo in repos:
        if repo.installation_id is not None:
            by_installation.setdefault(int(repo.installation_id), []).append(repo)
        else:
            loose.append(repo)
    for installation_id, group in by_installation.items():
        listing = await oauth.installation_repositories(token, installation_id)
        if listing is None:
            loose.extend(group)
            continue
        for repo in group:
            external = int(repo.external_id) if repo.external_id.isdigit() else None
            permission = listing.get(external) if external is not None else None
            if permission is not None:
                found[repo.id] = permission
    for repo in loose:
        permission = await oauth.repo_permission(token, repo.full_name)
        if permission is not None:
            found[repo.id] = permission
    existing = {
        row.repo_id: row for row in await db.scalars(select(RepoAccess).where(RepoAccess.user_id == user.id))
    }
    for repo_id, permission in found.items():
        row = existing.get(repo_id)
        if row is None:
            db.add(RepoAccess(user_id=user.id, repo_id=repo_id, permission=permission, checked_at=now))  # type: ignore[arg-type]
        else:
            row.permission = permission  # type: ignore[assignment]
            row.checked_at = now
    for repo_id, row in existing.items():
        if repo_id not in found:
            await db.delete(row)
    await db.commit()
    return found


async def revoke_user_access(db: AsyncSession, user: User, *, reason: str) -> None:
    """Forget a user's GitHub-derived access after GitHub stopped honouring their credentials.

    Deletes their ``repo_access`` rows (the contract is "exactly what GitHub lets them see", and
    GitHub now says nothing), every ``user_sessions`` row (a browser can no longer act on access
    coverset cannot verify) and the stored tokens, records ``access_revoked`` and commits. The
    ``users`` row and its role stay: signing in again restores everything.
    """
    for row in await db.scalars(select(RepoAccess).where(RepoAccess.user_id == user.id)):
        await db.delete(row)
    for row in await db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        await db.delete(row)
    user.oauth_token = None
    user.oauth_refresh_token = None
    user.oauth_expires_at = None
    record_audit(
        db,
        user_principal(user, {}),
        "access_revoked",
        target_kind="user",
        target_id=user.id,
        details={"reason": reason},
    )
    await db.commit()
    _marks().pop(user.id, None)


def _marks() -> dict[int, datetime]:
    marks = current_app.extensions.get(ACCESS_SYNC_EXTENSION)
    if marks is None:
        marks = current_app.extensions[ACCESS_SYNC_EXTENSION] = {}
    return marks


def _mark_synced(user_id: int, when: datetime) -> None:
    _marks()[user_id] = when


def schedule_access_refresh(user: User) -> None:
    """Refresh ``user``'s repository access in the background when the last refresh in this process
    is older than :data:`ACCESS_SYNC_INTERVAL` (the guard calls this on every user request; the
    mark is set before the task runs so concurrent requests schedule it once)."""
    if github_oauth() is None or not user.oauth_token:
        return
    now = app_settings().now()
    last = _marks().get(user.id)
    if last is not None and now - last < ACCESS_SYNC_INTERVAL:
        return
    _mark_synced(user.id, now)
    current_app.add_background_task(refresh_access_task, user.id)


async def refresh_access_task(user_id: int) -> None:
    """Background: re-read one user's repository access (errors are logged, never raised)."""
    oauth = github_oauth()
    if oauth is None:
        return
    try:
        async with app_db().session() as db:
            user = await db.get(User, user_id)
            if user is None or user.disabled:
                return
            try:
                await sync_repo_access(db, user, oauth, now=app_settings().now())
            except OAuthRevokedError as exc:
                log.warning("GitHub revoked the authorization of @%s: %s", user.login, exc)
                await revoke_user_access(db, user, reason=str(exc))
    except OAuthError as exc:
        log.warning("repository access of user %d could not be refreshed: %s", user_id, exc)
    except Exception:  # noqa: BLE001 - a background task must not die silently
        log.exception("repository access of user %d could not be refreshed", user_id)


# -- the account page --------------------------------------------------------------------------------


async def _account_user(db: AsyncSession, principal: Principal) -> User | None:
    cached = g.get("coverset_user")
    if cached is not None:
        return cached
    if principal.user_id is None:
        return None
    return await db.get(User, principal.user_id)


@bp.get("/account")
async def account_page() -> Response:
    """Who you are, your repositories and permissions, your sessions, and links to the token pages."""
    principal = current_principal()
    async with app_db().session() as db:
        user = await _account_user(db, principal)
        repos: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        if user is not None:
            rows = (
                await db.execute(
                    select(RepoAccess, Repo)
                    .join(Repo, Repo.id == RepoAccess.repo_id)
                    .where(RepoAccess.user_id == user.id)
                    .order_by(Repo.full_name)
                )
            ).all()
            repos = [
                {"repo": repo, "permission": access.permission, "checked_at": access.checked_at}
                for access, repo in rows
            ]
            for row in await db.scalars(
                select(UserSession)
                .where(UserSession.user_id == user.id)
                .order_by(UserSession.created_at.desc())
            ):
                sessions.append({"row": row, "current": row.id == principal.session_id})
        elif principal.instance_admin:
            repos = [
                {"repo": repo, "permission": "admin", "checked_at": None}
                for repo in await db.scalars(select(Repo).order_by(Repo.full_name))
            ]
    synced = _marks().get(user.id) if user is not None else None
    return await render_page(
        "account/account.html",
        principal=principal,
        user=user,
        repos=repos,
        sessions=sessions,
        synced_at=synced,
        can_refresh=user is not None and principal.is_user and github_login_available(),
        refreshed=request.args.get("refreshed"),
        github_login=github_login_available(),
    )


@bp.post("/account/refresh")
async def account_refresh() -> Response:
    """Re-read the signed-in user's repository access from GitHub now."""
    principal = current_principal()
    oauth = github_oauth()
    if principal.kind in ("token", "oidc"):
        raise Forbidden("Only a browser session may refresh repository access.")
    if not principal.is_user or principal.user_id is None or oauth is None:
        raise NotFound()
    outcome = "ok"
    async with app_db().session() as db:
        user = await db.get(User, principal.user_id)
        if user is None:
            raise NotFound()
        try:
            found = await sync_repo_access(db, user, oauth, now=app_settings().now())
        except OAuthRevokedError as exc:
            log.warning("GitHub revoked the authorization of @%s: %s", user.login, exc)
            await revoke_user_access(db, user, reason=str(exc))
            session.clear()
            return redirect("/login?next=%2Faccount&reason=revoked", code=303)  # type: ignore[return-value]
        except OAuthError as exc:
            log.warning("repository access of @%s could not be refreshed: %s", user.login, exc)
            outcome = "failed"
        else:
            _mark_synced(user.id, app_settings().now())
            record_audit(
                db,
                principal,
                "access_refreshed",
                target_kind="user",
                target_id=user.id,
                details={"repos": len(found)},
            )
            await db.commit()
    return redirect(f"/account?refreshed={outcome}", code=303)  # type: ignore[return-value]


@bp.post("/account/sessions/<session_id>/revoke")
async def revoke_session(session_id: str) -> Response:
    """Delete one of your own sessions (``session_id`` is the row id, the sha256 of the cookie id).

    Only a signed-in person may: a personal token acting for the same user is refused (403).
    """
    principal = current_principal()
    if not principal.is_user:
        raise Forbidden("Only a browser session may revoke sessions.")
    if not SESSION_ID_RE.fullmatch(session_id) or principal.user_id is None:
        raise NotFound()
    async with app_db().session() as db:
        row = await db.get(UserSession, session_id)
        if row is None or row.user_id != principal.user_id:
            raise NotFound()
        await db.delete(row)
        record_audit(db, principal, "session_revoked", target_kind="session", target_id=session_id[:12])
        await db.commit()
    if session_id == principal.session_id:
        session.clear()
        return redirect("/", code=303)  # type: ignore[return-value]
    return redirect("/account", code=303)  # type: ignore[return-value]


# -- the audit page ----------------------------------------------------------------------------------


@bp.get("/audit")
async def audit_page() -> Response:
    """The newest :data:`PAGE_LIMIT` audit events; ``?action=`` and ``?repo=`` filter (admins only)."""
    require_instance_admin()
    action = (request.args.get("action") or "").strip()[:64] or None
    repo_id = parse_int(request.args.get("repo"), minimum=1)
    async with app_db().session() as db:
        events = await recent_events(db, action=action, repo_id=repo_id, limit=PAGE_LIMIT)
        actions = await known_actions(db)
        repos = {repo.id: repo for repo in await db.scalars(select(Repo).order_by(Repo.full_name))}
    return await render_page(
        "account/audit.html",
        events=events,
        actions=actions,
        repos=repos,
        action=action,
        repo_id=repo_id,
        limit=PAGE_LIMIT,
    )


# -- helpers -----------------------------------------------------------------------------------------


async def _audit(action: str, *, principal: Principal, details: dict[str, Any] | None = None) -> None:
    from coverset.web.audit import audit_now

    await audit_now(action, principal=principal, details=details)


async def _problem(status: int, heading: str, message: str) -> Response:
    return await render_page(
        "error.html",
        status=status,
        code=status,
        heading=heading,
        message=message,
        hint="Nothing was changed. Sign in again from /login.",
    )


@bp.app_context_processor
async def _account_context() -> dict[str, Any]:
    """``principal``, ``account_user`` and the login links for every template, and for callers who
    are not instance admins the header's repository list reduced to what they may read (the
    ``pages`` blueprint lists every repository; this processor runs after it and overrides)."""
    if request.endpoint == "runs.run_events":
        return {}
    principal = current_principal()
    context: dict[str, Any] = {
        "principal": principal,
        "account_user": g.get("coverset_user"),
        "login_available": login_available(),
    }
    if not principal.instance_admin:
        visible = principal.visible_repo_ids_known
        repos: list[Repo] = []
        if visible:
            async with app_db().session() as db:
                repos = list(
                    await db.scalars(select(Repo).where(Repo.id.in_(visible)).order_by(Repo.id).limit(20))
                )
        args = request.view_args or {}
        current = next((r for r in repos if r.id == args.get("rid")), None)
        context["nav_repos"] = repos
        context["nav_repo"] = current or (repos[0] if repos else None)
    return context
