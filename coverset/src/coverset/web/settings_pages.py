"""Settings pages (instance, organization, repository, your preferences) and API-token management.

Every scope page renders the same form from the catalogue in :mod:`coverset.settings_scopes`, only
with the keys that scope accepts, grouped *View* and *Policy*. Each field shows what the scope
would inherit if it set nothing ("inherited from organization acme: approve"), and an empty
choice ("Inherit") or the per-row **Reset** button deletes the scope's row. A POST validates every
field before writing any, writes the changed ones in one transaction (one ``setting_changed``
audit row per key), and re-renders the page with a status line; htmx requests get the fragment
(the form swaps itself), plain form posts the full page, so the pages work without scripts too.

Who may open what (the access guard in :mod:`coverset.web.auth` has already resolved the
:class:`~coverset.web.principal.Principal`; CSRF is checked there too):

* ``/settings`` and ``/settings/instance``: instance admins (a signed-in non-admin who opens
  ``/settings`` is sent to their preferences instead);
* ``/settings/org/<owner>``: instance admins;
* ``/repos/<rid>/settings``: write access to the repository; keys whose
  :attr:`~coverset.settings_scopes.SettingSpec.repo_edit` is ``admin`` (untrusted-PR policy, shard
  limits, public pages) are shown read-only to writers who are not repository admins;
* ``/account/settings``: the signed-in user's view preferences (scope ``user:<id>``); the local
  operator of an open-mode or admin-token instance has no user row and uses ``user:local``.

A token-authenticated caller may read these pages with any scope but needs ``write`` (or ``admin``)
to change a setting, and may never mint or revoke tokens: a leaked token must not be able to
extend its own life.

Token pages (``/settings/tokens``): the caller's personal tokens and, for repositories they may
write, those repositories' tokens. Creating one shows the raw ``cst_`` string exactly once (the
response is ``Cache-Control: no-store``); only its sha256 and prefix are stored. Revoking sets
``revoked_at``. Both are audited (``token_created``, ``token_revoked``).

The blueprint also exposes ``view_prefs`` (the reader's ``view.*`` values, resolved once per
request and cached on ``g``) plus two nav helpers to every template.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from quart import Blueprint, Response, g, has_request_context, redirect, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import Forbidden, NotFound

from coverset.db.models import ApiToken, Repo
from coverset.settings_scopes import (
    CATALOGUE,
    LOCAL_USER,
    Layers,
    RepoLike,
    Resolved,
    ScopeKind,
    SettingError,
    SettingSpec,
    ViewPrefs,
    apply_changes,
    default_view_prefs,
    keys_for_scope,
    load_layers,
    normalize_scope_id,
    repo_stub,
    view_prefs,
)
from coverset.web import tokens
from coverset.web.audit import record_audit
from coverset.web.context import app_db, app_settings
from coverset.web.pages import MAX_DB_ID, is_fragment, render_page
from coverset.web.principal import Principal, current_principal, require_instance_admin, require_repo

log = logging.getLogger(__name__)

bp = Blueprint("settings_pages", __name__)

RESET_FIELD: Final = "reset"
"""Form field naming the one key a per-row Reset button clears."""
EXPIRY_CHOICES: Final[dict[str, int | None]] = {"7": 7, "30": 30, "90": 90, "365": 365, "never": None}
"""Token lifetimes offered, in days (``never`` = no expiry)."""
DEFAULT_EXPIRY: Final = "90"
REPO_TOKEN_SCOPES: Final = frozenset({"read", "write", "upload"})
"""A repository token can never exceed write on its one repository, so ``admin`` is refused."""
MAX_TOKEN_NAME: Final = 80
MAX_LIVE_TOKENS: Final = 50
"""Live (unrevoked, unexpired) tokens one owner (user or repository) may hold."""
MAX_LISTED_REPOS: Final = 1000
_SKIP_CONTEXT: Final = frozenset({"runs.run_events", "static"})
"""Endpoints whose templates never need view preferences (SSE frames, static files)."""


# -- who is asking ---------------------------------------------------------------------------------


def user_scope_id(principal: Principal) -> str | None:
    """The user-scope id of the principal's preferences: its user id, ``local`` for the operator
    (open mode or the admin token), None for callers without preferences (repo tokens, OIDC)."""
    if principal.user_id is not None:
        return str(principal.user_id)
    if principal.kind == "admin":
        return LOCAL_USER
    return None


def _may_write(principal: Principal) -> bool:
    """False for a token principal without the ``write``/``admin`` scope (reads only)."""
    return principal.kind != "token" or bool({"write", "admin"} & principal.scopes)


def _require_write(principal: Principal) -> None:
    if request.method == "POST" and not _may_write(principal):
        raise Forbidden("This token may only read; changing settings needs the write scope.")


def _require_account(principal: Principal) -> Principal:
    """Token management is for people (and the operator), never for a token or a CI job."""
    if principal.is_public:
        raise NotFound()
    if principal.kind not in ("user", "admin"):
        raise Forbidden("API tokens cannot create or revoke tokens; sign in to manage them.")
    return principal


# -- scope pages -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopeView:
    """One scope page: which rows it stores into and how it is labelled."""

    kind: ScopeKind
    scope_id: str
    title: str
    action: str
    repo: Repo | None = None
    org: str | None = None
    user_id: str | None = None
    inherit_note: str = ""


@dataclass(frozen=True, slots=True)
class Row:
    """One key on a scope page."""

    spec: SettingSpec
    is_set: bool
    value: str
    """The field's value: the stored value at this scope, the submitted text after an error, or ""."""
    inherited: Resolved
    effective: Resolved
    locked: str | None
    """Why the field is read-only for this principal (None = editable)."""
    error: str | None

    @property
    def field_id(self) -> str:
        return "set-" + self.spec.name.replace(".", "-").replace("_", "-")


def _form_value(spec: SettingSpec, value: Any) -> str:
    """A stored value as the text its form control carries."""
    if value is None:
        return ""
    if spec.kind == "bool":
        return "true" if value else "false"
    return spec.format(value) if spec.kind in ("int", "float") else str(value)


def _locked(spec: SettingSpec, view: ScopeView, principal: Principal) -> str | None:
    if not _may_write(principal):
        return "read-only token"
    repo = view.repo if view.kind == "repo" else None
    if repo is not None and spec.repo_edit == "admin" and not principal.can(repo.id, "admin"):
        return "needs repository admin"
    return None


def _rows(
    view: ScopeView,
    layers: Layers,
    principal: Principal,
    *,
    submitted: MultiDict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = {"view": [], "policy": []}
    for spec in keys_for_scope(view.kind):
        is_set = layers.is_set(view.kind, spec.name)
        error = (errors or {}).get(spec.name)
        if error is not None and submitted is not None:
            value = submitted.get(spec.name, "")
        else:
            value = _form_value(spec, layers.at(view.kind, spec.name)) if is_set else ""
        groups[spec.group].append(
            Row(
                spec=spec,
                is_set=is_set,
                value=value,
                inherited=layers.resolve(spec.name, broader_than=view.kind),
                effective=layers.resolve(spec.name),
                locked=_locked(spec, view, principal),
                error=error,
            )
        )
    return {name: rows for name, rows in groups.items() if rows}


def _changes(
    form: MultiDict[str, str], view: ScopeView, layers: Layers, principal: Principal
) -> tuple[dict[str, object], dict[str, str]]:
    """``(changes, errors)`` from a submitted form: key -> raw value or None (clear).

    A missing field (a disabled control is not submitted) changes nothing; an empty one means
    "inherit". A locked key whose value would change is an error, never silently ignored.
    """
    reset = form.get(RESET_FIELD)
    changes: dict[str, object] = {}
    errors: dict[str, str] = {}
    specs = {spec.name: spec for spec in keys_for_scope(view.kind)}
    if reset:
        spec = specs.get(reset)
        if spec is None:
            errors[reset] = "That setting cannot be set here."
        elif (why := _locked(spec, view, principal)) is not None:
            errors[reset] = f"Read-only here ({why})."
        else:
            changes[reset] = None
        return changes, errors
    for name, spec in specs.items():
        if name not in form:
            continue
        text = form.get(name, "").strip()
        current = layers.at(view.kind, name) if layers.is_set(view.kind, name) else None
        if text == "":
            wanted = None
        else:
            try:
                wanted = spec.parse(text)
            except SettingError as exc:
                errors[name] = str(exc)
                continue
        if wanted == current and (wanted is None or type(wanted) is type(current)):
            continue
        if (why := _locked(spec, view, principal)) is not None:
            errors[name] = f"Read-only here ({why})."
            continue
        changes[name] = wanted
    return changes, errors


def _summary(written: dict[str, Any], layers: Layers) -> str:
    if not written:
        return "No changes."
    parts = []
    for key, value in written.items():
        spec = CATALOGUE[key]
        if value is None:
            inherited = layers.resolve(key)
            parts.append(f"{spec.label} now inherits {inherited.display} from {inherited.detail}")
        else:
            parts.append(f"{spec.label} set to {spec.format(value)}")
    return "Saved. " + "; ".join(parts) + "."


async def _scope_page(view: ScopeView, principal: Principal) -> Response:
    """GET renders the form; POST validates, writes, audits and re-renders (see the module doc)."""
    _require_write(principal)
    settings = app_settings()
    message: str | None = None
    errors: dict[str, str] = {}
    submitted: MultiDict[str, str] | None = None
    status = 200
    async with app_db().session() as session:
        layers = await load_layers(
            session, repo=view.repo, org=view.org, user_id=view.user_id, settings=settings
        )
        if request.method == "POST":
            submitted = await request.form
            changes, errors = _changes(submitted, view, layers, principal)
            if errors:
                status = 200 if is_fragment() else 422
                message = "Nothing was saved: fix the highlighted field."
            else:
                written = await apply_changes(
                    session,
                    changes,
                    scope=view.kind,
                    scope_id=view.scope_id,
                    actor=principal,
                    repo_id=view.repo.id if view.repo is not None else None,
                )
                layers = await load_layers(
                    session, repo=view.repo, org=view.org, user_id=view.user_id, settings=settings
                )
                message = _summary(written, layers)
                if view.kind == "user":
                    g.pop("coverset_view_prefs", None)  # the layout reflects the new theme at once
        groups = _rows(view, layers, principal, submitted=submitted, errors=errors)
        nav = await _settings_nav(session, principal)
    return await render_page(
        "settings/scope.html",
        status=status,
        view=view,
        groups=groups,
        message=message,
        failed=bool(errors),
        settings_nav=nav,
        reset_field=RESET_FIELD,
    )


async def _settings_nav(session: Any, principal: Principal) -> dict[str, Any]:
    """Links for the settings sub-navigation: orgs and repositories this principal may configure."""
    repos = list(await session.scalars(select(Repo).order_by(Repo.full_name).limit(MAX_LISTED_REPOS)))
    writable = [r for r in repos if principal.can(r.id, "write")]
    orgs: list[str] = []
    if principal.instance_admin:
        orgs = sorted({r.full_name.partition("/")[0].lower() for r in repos if "/" in r.full_name})
    return {
        "admin": principal.instance_admin,
        "orgs": orgs,
        "repos": writable,
        "preferences": user_scope_id(principal) is not None,
        "tokens": principal.kind in ("user", "admin"),
    }


@bp.route("/settings", methods=["GET", "POST"])
@bp.route("/settings/instance", methods=["GET", "POST"])  # canonical: url_for builds the first rule
async def instance_settings() -> Response:
    """Instance-wide defaults (instance admins); others are sent to their own preferences."""
    principal = current_principal()
    if not principal.instance_admin and request.method == "GET" and user_scope_id(principal):
        return redirect(url_for("settings_pages.account_settings"), code=303)
    require_instance_admin()
    view = ScopeView(
        kind="instance",
        scope_id="",
        title="Instance settings",
        action=url_for("settings_pages.instance_settings"),
        inherit_note="Values set here apply to every repository unless an organization or "
        "repository sets its own. Unset policy falls back to the COVERSET_* configuration.",
    )
    return await _scope_page(view, principal)


@bp.route("/settings/org/<owner>", methods=["GET", "POST"])
async def org_settings(owner: str) -> Response:
    """Defaults for every repository of one owner (instance admins)."""
    principal = require_instance_admin()
    try:
        org = normalize_scope_id("org", owner)
    except SettingError:
        raise NotFound() from None
    view = ScopeView(
        kind="org",
        scope_id=org,
        title=f"Organization {org}",
        action=url_for("settings_pages.org_settings", owner=org),
        org=org,
        inherit_note=f"Applies to every repository under {org}/ that does not set its own value.",
    )
    return await _scope_page(view, principal)


@bp.route("/repos/<int:rid>/settings", methods=["GET", "POST"])
async def repo_settings(rid: int) -> Response:
    """One repository's settings (write access; some policy keys need repository admin)."""
    if rid > MAX_DB_ID:
        raise NotFound()
    principal = require_repo(rid, "write")
    async with app_db().session() as session:
        repo = await session.get(Repo, rid)
    if repo is None:
        raise NotFound(f"There is no repository with id {rid}.")
    view = ScopeView(
        kind="repo",
        scope_id=str(repo.id),
        title=f"{repo.full_name} settings",
        action=url_for("settings_pages.repo_settings", rid=repo.id),
        repo=repo,
        inherit_note="Repository values win over the organization's and the instance's. "
        "Readers' own view preferences still win over the view values here.",
    )
    return await _scope_page(view, principal)


@bp.route("/account/settings", methods=["GET", "POST"])
async def account_settings() -> Response:
    """The signed-in user's view preferences (``user:local`` for the operator)."""
    principal = current_principal()
    if principal.is_public:
        raise NotFound()
    uid = user_scope_id(principal)
    if uid is None:
        raise Forbidden("Repository tokens and CI jobs have no preferences.")
    who = f"@{principal.login}" if principal.login else "the local operator"
    view = ScopeView(
        kind="user",
        scope_id=uid,
        title="Your preferences",
        action=url_for("settings_pages.account_settings"),
        user_id=uid,
        inherit_note=f"How coverset looks for {who}. These win over every repository, "
        "organization and instance default.",
    )
    return await _scope_page(view, principal)


# -- tokens ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenForm:
    """The create-token form's values (kept on a validation error so nothing is retyped)."""

    name: str = ""
    kind: str = "personal"
    repo_id: int | None = None
    scopes: tuple[str, ...] = ("read",)
    expiry: str = DEFAULT_EXPIRY


async def _token_lists(
    session: Any, principal: Principal
) -> tuple[list[ApiToken], list[ApiToken], list[Repo]]:
    """``(personal tokens, repository tokens, writable repositories)`` for the tokens page."""
    personal: list[ApiToken] = []
    if principal.user_id is not None:
        personal = list(
            await session.scalars(
                select(ApiToken)
                .where(ApiToken.kind == "personal", ApiToken.user_id == principal.user_id)
                .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
            )
        )
    repos = list(await session.scalars(select(Repo).order_by(Repo.full_name).limit(MAX_LISTED_REPOS)))
    writable = [r for r in repos if principal.can(r.id, "write")]
    repo_tokens: list[ApiToken] = []
    if writable:
        repo_tokens = list(
            await session.scalars(
                select(ApiToken)
                .where(ApiToken.kind == "repo", ApiToken.repo_id.in_([r.id for r in writable]))
                .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
            )
        )
    return personal, repo_tokens, writable


async def _tokens_page(
    principal: Principal,
    *,
    status: int = 200,
    message: str | None = None,
    error: str | None = None,
    new_token: str | None = None,
    form: TokenForm | None = None,
) -> Response:
    async with app_db().session() as session:
        personal, repo_tokens, writable = await _token_lists(session, principal)
        nav = await _settings_nav(session, principal)
    response = await render_page(
        "settings/tokens.html",
        status=status,
        personal=personal,
        repo_tokens=repo_tokens,
        repos=writable,
        repo_names={r.id: r.full_name for r in writable},
        can_personal=principal.user_id is not None,
        now=app_settings().now(),
        message=message,
        error=error,
        new_token=new_token,
        form=form or TokenForm(kind="personal" if principal.user_id is not None else "repo"),
        expiry_choices=list(EXPIRY_CHOICES),
        scope_choices=sorted(tokens.SCOPES),
        repo_token_scopes=REPO_TOKEN_SCOPES,
        settings_nav=nav,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/settings/tokens")
async def tokens_page() -> Response:
    """The caller's personal tokens and the tokens of repositories they may write."""
    return await _tokens_page(_require_account(current_principal()))


def _parse_token_form(form: MultiDict[str, str], principal: Principal, writable: set[int]) -> TokenForm:
    """Validate the create form; raises ``ValueError`` with a message safe to show."""
    name = " ".join(form.get("name", "").split())
    kind = form.get("kind", "personal")
    raw_repo = form.get("repo", "").strip()
    repo_id = int(raw_repo) if raw_repo.isascii() and raw_repo.isdigit() and len(raw_repo) < 12 else None
    expiry = form.get("expiry", DEFAULT_EXPIRY)
    picked = tuple(form.getlist("scopes"))
    draft = TokenForm(name=name, kind=kind, repo_id=repo_id, scopes=picked, expiry=expiry)
    if not name or len(name) > MAX_TOKEN_NAME or not name.isprintable():
        raise _FormError(draft, f"Give the token a name of 1 to {MAX_TOKEN_NAME} printable characters.")
    if kind not in ("personal", "repo"):
        raise _FormError(draft, "Pick a personal or a repository token.")
    if expiry not in EXPIRY_CHOICES:
        raise _FormError(draft, "Pick one of the offered lifetimes.")
    try:
        scopes = tokens.check_scopes(list(picked))
    except ValueError as exc:
        raise _FormError(draft, str(exc)) from None
    if not scopes:
        raise _FormError(draft, "Pick at least one scope.")
    if kind == "personal" and principal.user_id is None:
        raise _FormError(draft, "Personal tokens belong to a signed-in user; create a repository token.")
    if kind == "repo":
        if repo_id is None or repo_id not in writable:
            raise _FormError(draft, "Pick a repository you may write to.")
        if not set(scopes) <= REPO_TOKEN_SCOPES:
            raise _FormError(draft, "Repository tokens take only the read, write and upload scopes.")
    return TokenForm(
        name=name, kind=kind, repo_id=repo_id if kind == "repo" else None, scopes=tuple(scopes), expiry=expiry
    )


class _FormError(ValueError):
    def __init__(self, form: TokenForm, message: str) -> None:
        super().__init__(message)
        self.form = form


@bp.post("/settings/tokens")
async def create_token() -> Response:
    """Create a token and show its raw value once (the page is ``no-store``)."""
    principal = _require_account(current_principal())
    form_data = await request.form
    settings = app_settings()
    now = settings.now()
    async with app_db().session() as session:
        _, _, writable = await _token_lists(session, principal)
        try:
            form = _parse_token_form(form_data, principal, {r.id for r in writable})
        except _FormError as exc:
            return await _tokens_page(
                principal, status=200 if is_fragment() else 422, error=str(exc), form=exc.form
            )
        owner = (
            ApiToken.repo_id == form.repo_id if form.kind == "repo" else ApiToken.user_id == principal.user_id
        )
        live = list(
            await session.scalars(
                select(ApiToken.expires_at).where(
                    owner, ApiToken.kind == form.kind, ApiToken.revoked_at.is_(None)
                )
            )
        )
        if sum(1 for expires in live if expires is None or expires > now) >= MAX_LIVE_TOKENS:
            return await _tokens_page(
                principal,
                status=200 if is_fragment() else 422,
                error=f"At most {MAX_LIVE_TOKENS} live tokens; revoke one first.",
                form=form,
            )
        raw, prefix, digest = tokens.generate()
        days = EXPIRY_CHOICES[form.expiry]
        row = ApiToken(
            token_hash=digest,
            prefix=prefix,
            name=form.name,
            kind=form.kind,
            user_id=principal.user_id,
            repo_id=form.repo_id,
            scopes=list(form.scopes),
            created_at=now,
            expires_at=now + timedelta(days=days) if days is not None else None,
        )
        session.add(row)
        await session.flush()
        record_audit(
            session,
            principal,
            "token_created",
            target_kind="api_token",
            target_id=str(row.id),
            repo_id=form.repo_id,
            details={
                "name": form.name,
                "kind": form.kind,
                "prefix": prefix,
                "scopes": list(form.scopes),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            },
        )
        await session.commit()
    return await _tokens_page(
        principal,
        status=201,
        message=f"Created {'repository' if form.kind == 'repo' else 'personal'} token “{form.name}”. "
        "Copy it now: it is not shown again.",
        new_token=raw,
    )


@bp.post("/settings/tokens/<int:tid>/revoke")
async def revoke_token(tid: int) -> Response:
    """Revoke one token the caller owns (or may manage); unknown or foreign tokens are a 404."""
    principal = _require_account(current_principal())
    if tid > MAX_DB_ID:
        raise NotFound()
    now = app_settings().now()
    async with app_db().session() as session:
        row = await session.get(ApiToken, tid)
        if row is None or not _may_manage(principal, row):
            raise NotFound()
        if row.revoked_at is not None:
            message = f"“{row.name}” was already revoked."
        else:
            row.revoked_at = now
            record_audit(
                session,
                principal,
                "token_revoked",
                target_kind="api_token",
                target_id=str(row.id),
                repo_id=row.repo_id,
                details={"name": row.name, "kind": row.kind, "prefix": row.prefix},
            )
            await session.commit()
            message = f"Revoked “{row.name}” ({row.prefix}…). Requests using it now get 401."
    return await _tokens_page(principal, message=message)


def _may_manage(principal: Principal, row: ApiToken) -> bool:
    if row.kind == "repo":
        return row.repo_id is not None and principal.can(row.repo_id, "write")
    return principal.instance_admin or (principal.user_id is not None and row.user_id == principal.user_id)


# -- template context --------------------------------------------------------------------------------


async def request_view_prefs() -> ViewPrefs:
    """The current reader's view preferences: resolved once per request and cached on ``g``.

    The repository is the page's ``rid`` (if any, and only when the reader may read it).
    Resolution goes through the per-process caches of :mod:`coverset.settings_scopes`, so a warm
    request runs no query at all and a cold one at most two small ones (the repository's owner,
    then every uncached scope in one query). A database error degrades to the built-in defaults
    instead of failing the page.
    """
    cached = g.get("coverset_view_prefs")
    if cached is not None:
        return cached
    rid = (request.view_args or {}).get("rid")
    principal = current_principal()
    uid = user_scope_id(principal)
    # Only a repository the reader may see contributes its repo and org layers. Otherwise the
    # 404 that ``require_repo`` raises for an invisible repository would carry that repository's
    # (or its organisation's) preferences in the layout, and differ from the 404 for a missing id:
    # enough to enumerate private repositories and learn their owners.
    readable = isinstance(rid, int) and 0 < rid <= MAX_DB_ID and principal.can(rid, "read")
    try:
        async with app_db().session() as session:
            repo: RepoLike | None = None
            if readable:
                repo = await repo_stub(session, rid)
            prefs = await view_prefs(session, repo=repo, user_id=uid, settings=app_settings())
    except SQLAlchemyError:
        log.warning("view preferences unavailable; using defaults", exc_info=True)
        prefs = default_view_prefs()
    g.coverset_view_prefs = prefs
    return prefs


def _can_write(repo: RepoLike | None) -> bool:
    """For the nav partial: may the current principal change this repository's settings?"""
    return repo is not None and current_principal().can(repo.id, "write")


@bp.app_context_processor
async def _settings_context() -> dict[str, Any]:
    """``view_prefs``, ``settings_admin`` and ``settings_can_write(repo)`` for every template."""
    if not has_request_context() or request.endpoint in _SKIP_CONTEXT:
        return {}
    principal = current_principal()
    return {
        "view_prefs": await request_view_prefs(),
        "settings_admin": principal.instance_admin,
        "settings_can_write": _can_write,
        "settings_has_prefs": user_scope_id(principal) is not None,
    }
