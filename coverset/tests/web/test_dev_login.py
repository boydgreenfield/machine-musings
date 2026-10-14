"""The development sign-in: "Sign in as a local user" on /login (``COVERSET_DEV_LOGIN``).

It is offered only when every condition holds (dev env, open mode with no admin token, loopback
bind, private public URL, the fake forge without GitHub credentials, the flag, a loopback Host);
each test below breaks one and checks that both the form and ``POST /login/dev`` are gone. When
enabled it creates ``forge = "fake"`` users, sets a member's ``repo_access`` from the picker, opens
a server-side session like the GitHub callback and audits ``login`` with ``details.dev``; that
session replaces the local operator, so a member is refused the admin pages. Once any condition
stops holding, the sessions and API tokens of those users grant nothing, in open mode and in
admin-token mode alike.

The apps are built with ``env=test`` (no run dispatcher or gc) and switched to ``env=dev`` before
serving: the dev sign-in reads the setting per request.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select
from web_helpers import assert_csp_clean

from coverset.app import create_app
from coverset.config import Settings
from coverset.db.models import AuditEvent, Repo, RepoAccess, User, UserSession
from coverset.web.account import DEV_EXTERNAL_PREFIX, dev_login_problems
from coverset.web.auth import hash_session_id

pytestmark = pytest.mark.security

CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')
TOKEN = "dev-login-admin-token-0123456789abcdef"


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"dev_login": True}


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[Quart]:
    """The app with the development sign-in enabled (env switched to dev after construction)."""
    application = create_app(settings)
    settings.env = "dev"
    async with application.test_app():
        yield application


@pytest.fixture
async def repos(app: Quart) -> dict[str, int]:
    """Two repositories: the one the member will read, and one they will not see."""
    async with app.extensions["db"].session() as db:
        ledger = Repo(forge="fake", external_id="501", full_name="demo/ledger")
        other = Repo(forge="fake", external_id="502", full_name="demo/other")
        db.add_all([ledger, other])
        await db.commit()
        return {"ledger": ledger.id, "other": other.id}


async def form_csrf(client: Client, path: str = "/login") -> str:
    """GET the login page and return the CSRF token its forms carry."""
    response = await client.get(path)
    assert response.status_code == 200, response.status_code
    match = CSRF_FIELD.search(await response.get_data(as_text=True))
    assert match is not None, "no dev sign-in form"
    return match.group(1)


async def dev_sign_in(
    client: Client,
    login: str,
    *,
    role: str = "member",
    access: dict[int, str] | None = None,
    next_path: str = "/",
    csrf: str | None = None,
) -> Any:
    token = csrf if csrf is not None else await form_csrf(client)
    form = {"login": login, "role": role, "next": next_path, "csrf_token": token}
    for repo_id, permission in (access or {}).items():
        form[f"repo-{repo_id}"] = permission
    return await client.post("/login/dev", form=form)


# -- enabled ---------------------------------------------------------------------------------------


async def test_login_page_offers_the_form_with_a_picker_per_repository(
    client: Client, repos: dict[str, int]
) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    html = await response.get_data(as_text=True)
    assert "Sign in as a local user" in html
    assert 'action="/login/dev"' in html
    for name, repo_id in repos.items():
        assert f'name="repo-{repo_id}"' in html and f"demo/{name}" in html
    assert 'value="read" selected' in html
    assert "With the admin token" not in html and "Sign in with GitHub" not in html
    assert_csp_clean(html)


async def test_member_with_read_sees_the_repository_and_is_refused_admin_pages(
    app: Quart, client: Client, repos: dict[str, int]
) -> None:
    response = await dev_sign_in(
        client, "alice", access={repos["ledger"]: "read", repos["other"]: "none"}, next_path="/account"
    )
    assert response.status_code == 303
    assert response.headers["Location"] == "/account"
    async with app.extensions["db"].session() as db:
        user = await db.scalar(select(User).where(User.login == "alice"))
        assert user is not None
        assert (user.forge, user.external_id, user.role) == ("fake", DEV_EXTERNAL_PREFIX + "alice", "member")
        rows = list(await db.scalars(select(RepoAccess).where(RepoAccess.user_id == user.id)))
        assert [(r.repo_id, r.permission) for r in rows] == [(repos["ledger"], "read")]
        sessions = list(await db.scalars(select(UserSession).where(UserSession.user_id == user.id)))
        assert len(sessions) == 1
        event = await db.scalar(select(AuditEvent).where(AuditEvent.action == "login"))
        assert event is not None
        assert event.details == {"via": "dev", "dev": True}
        assert event.actor_label == "@alice"
    assert (await client.get("/setup")).status_code == 403
    page = await client.get("/setup", headers={"Accept": "text/html"})
    assert page.status_code == 403
    assert "Instance admins only" in await page.get_data(as_text=True)
    assert (await client.get("/audit")).status_code == 403
    assert (await client.get(f"/repos/{repos['ledger']}")).status_code == 200
    assert (await client.get(f"/repos/{repos['other']}")).status_code == 404
    account = await client.get("/account")
    assert "@alice" in await account.get_data(as_text=True)


async def test_signing_out_makes_the_browser_the_local_operator_again(
    client: Client, repos: dict[str, int]
) -> None:
    await dev_sign_in(client, "bob", access={repos["ledger"]: "read"})
    assert (await client.get("/setup")).status_code == 403
    async with client.session_transaction() as cookie:
        csrf = cookie["csrf"]
    out = await client.post("/logout", form={"csrf_token": csrf})
    assert out.status_code == 303
    assert (await client.get("/setup")).status_code == 200


async def test_admin_role_reaches_admin_pages_and_keeps_its_rows(
    app: Quart, client: Client, repos: dict[str, int]
) -> None:
    await dev_sign_in(client, "carol", access={repos["ledger"]: "write"})
    response = await dev_sign_in(client, "carol", role="admin", access={repos["ledger"]: "none"})
    assert response.status_code == 303
    assert (await client.get("/audit")).status_code == 200
    async with app.extensions["db"].session() as db:
        users = list(await db.scalars(select(User).where(User.login == "carol")))
        assert len(users) == 1 and users[0].role == "admin"
        rows = list(await db.scalars(select(RepoAccess).where(RepoAccess.user_id == users[0].id)))
        assert [(r.repo_id, r.permission) for r in rows] == [(repos["ledger"], "write")]


async def test_signing_in_again_replaces_the_permissions(
    app: Quart, client: Client, repos: dict[str, int]
) -> None:
    await dev_sign_in(client, "dave", access={repos["ledger"]: "read", repos["other"]: "admin"})
    await dev_sign_in(client, "dave", access={repos["ledger"]: "write", repos["other"]: "none"})
    async with app.extensions["db"].session() as db:
        user = await db.scalar(select(User).where(User.login == "dave"))
        assert user is not None
        rows = list(await db.scalars(select(RepoAccess).where(RepoAccess.user_id == user.id)))
        assert [(r.repo_id, r.permission) for r in rows] == [(repos["ledger"], "write")]
    assert (await client.get(f"/repos/{repos['other']}")).status_code == 404


async def test_the_form_needs_the_sessions_csrf_token(app: Quart, client: Client) -> None:
    await client.get("/login")
    response = await dev_sign_in(client, "mallory", csrf="forged-token")
    assert response.status_code == 403
    fresh = app.test_client()  # a browser that never loaded the form
    response = await fresh.post("/login/dev", form={"login": "mallory", "role": "admin"})
    assert response.status_code == 403
    assert (await client.get("/setup")).status_code == 200  # still the operator


@pytest.mark.parametrize(
    ("login", "role", "permission"),
    [
        ("", "member", "read"),
        ("-bad", "member", "read"),
        ("a" * 40, "member", "read"),
        ("ok", "owner", "read"),
        ("ok", "member", "maintain"),
    ],
)
async def test_bad_input_is_refused(
    client: Client, repos: dict[str, int], login: str, role: str, permission: str
) -> None:
    response = await dev_sign_in(client, login, role=role, access={repos["ledger"]: permission})
    assert response.status_code == 400
    assert (await client.get("/setup")).status_code == 200


async def test_a_disabled_user_is_refused_and_left_unchanged(app: Quart, client: Client) -> None:
    await dev_sign_in(client, "erin")
    async with app.extensions["db"].session() as db:
        user = await db.scalar(select(User).where(User.login == "erin"))
        assert user is not None
        user.disabled = True
        await db.commit()
        before = user.last_login_at
    other = app.test_client()
    response = await dev_sign_in(other, "erin", role="admin")
    assert response.status_code == 403
    assert (await other.get("/setup")).status_code == 200  # no session: still the operator
    async with app.extensions["db"].session() as db:
        user = await db.scalar(select(User).where(User.login == "erin"))
        assert user is not None
        assert (user.role, user.disabled, user.last_login_at) == ("member", True, before)
        failed = list(await db.scalars(select(AuditEvent).where(AuditEvent.action == "login_failed")))
        assert [e.details for e in failed] == [{"via": "dev", "reason": "disabled"}]


async def test_open_mode_ignores_a_github_session(app: Quart, client: Client) -> None:
    """Only the dev sign-in's own users replace the operator; a GitHub session changes nothing."""
    settings = app.extensions["settings"]
    raw = secrets.token_urlsafe(32)
    now = settings.now()
    async with app.extensions["db"].session() as db:
        user = User(forge="github", external_id="4242", login="gh-user", role="member")
        db.add(user)
        await db.flush()
        db.add(
            UserSession(
                id=hash_session_id(raw),
                user_id=user.id,
                csrf="x",
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        await db.commit()
    async with client.session_transaction() as cookie:
        cookie["sid"] = raw
    assert (await client.get("/setup")).status_code == 200


# Each breaks one condition after a member signed in through the form (the settings are read per
# request, so assigning them stands in for a restart with a different configuration).
BROKEN_CONDITIONS: dict[str, dict[str, Any]] = {
    "flag-off": {"dev_login": False},
    "prod-env": {"env": "prod"},
    "public-url": {"public_url": "https://coverset.example.com"},
    "github-token": {"github_token": SecretStr("ghp_" + "a" * 36)},
    "github-app": {"github_app_id": 123},
}


@pytest.mark.parametrize("broken", list(BROKEN_CONDITIONS), ids=list(BROKEN_CONDITIONS))
async def test_a_broken_condition_ends_dev_sessions_in_open_mode(
    app: Quart, client: Client, repos: dict[str, int], broken: str
) -> None:
    await dev_sign_in(client, "frank", access={repos["ledger"]: "read"})
    assert (await client.get("/setup")).status_code == 403
    settings = app.extensions["settings"]
    for name, value in BROKEN_CONDITIONS[broken].items():
        setattr(settings, name, value)
    assert (await client.get("/setup")).status_code == 200  # the operator again, not @frank
    assert (await client.post("/login/dev", form={"login": "x"})).status_code == 404


async def test_a_dev_session_on_a_foreign_host_name_is_ignored(
    app: Quart, client: Client, repos: dict[str, int]
) -> None:
    """The bind host is accepted by open mode but is not a loopback name: the session counts for
    nothing there (the operator answers)."""
    app.extensions["settings"].bind = "127.0.0.1:8000"
    await dev_sign_in(client, "fern", access={repos["ledger"]: "read"})
    assert (await client.get("/setup")).status_code == 403
    async with app.test_request_context("/setup", headers={"Host": "coverset.example.com"}):
        from coverset.web.account import dev_login_enabled

        assert dev_login_enabled() is False


@pytest.mark.parametrize(
    "also_broken",
    ["none", "flag-off", "prod-env", "non-loopback-bind"],
)
async def test_an_admin_dev_session_grants_nothing_once_an_admin_token_is_set(
    app: Quart, client: Client, also_broken: str
) -> None:
    """The review's reproduction: an admin minted by the form must not survive into token mode,
    whatever else changed (the generic session path asks :func:`dev_user_refused` too)."""
    await dev_sign_in(client, "mallory", role="admin")
    assert (await client.get("/audit")).status_code == 200
    settings = app.extensions["settings"]
    settings.admin_token = SecretStr(TOKEN)
    headers: dict[str, str] = {}
    if also_broken == "flag-off":
        settings.dev_login = False
    elif also_broken == "prod-env":
        settings.env = "prod"
    elif also_broken == "non-loopback-bind":
        settings.bind = "0.0.0.0:8000"
        headers = {"Host": "coverset.example.com"}
    assert (await client.get("/setup", headers=headers)).status_code == 401
    assert (await client.get("/audit", headers=headers)).status_code == 401
    if not headers:  # under another Host the test client keeps that name's cookies apart
        async with client.session_transaction() as cookie:
            assert "sid" not in cookie  # the dead session id was dropped
    assert (
        await client.get("/setup", headers={"Authorization": f"Bearer {TOKEN}", **headers})
    ).status_code == 200


async def test_a_dev_users_api_token_is_refused_in_admin_token_mode(app: Quart, client: Client) -> None:
    """A personal ``cst_`` token of a dev user (created while it was the principal) grants nothing
    once the dev sign-in is off; a GitHub user's token keeps working."""
    from coverset.web import tokens

    await dev_sign_in(client, "tess", role="admin")
    now = app.extensions["settings"].now()
    async with app.extensions["db"].session() as db:
        dev_user = await db.scalar(select(User).where(User.login == "tess"))
        assert dev_user is not None
        github_user = User(forge="github", external_id="77", login="octo", role="admin")
        db.add(github_user)
        await db.flush()
        dev_raw, _ = await tokens.create(
            db, name="t", kind="personal", scopes=["admin"], now=now, user_id=dev_user.id
        )
        github_raw, _ = await tokens.create(
            db, name="g", kind="personal", scopes=["admin"], now=now, user_id=github_user.id
        )
        await db.commit()
    app.extensions["settings"].admin_token = SecretStr(TOKEN)
    bearer = app.test_client()
    assert (await bearer.get("/audit", headers={"Authorization": f"Bearer {dev_raw}"})).status_code == 401
    assert (await bearer.get("/audit", headers={"Authorization": f"Bearer {github_raw}"})).status_code == 200


async def test_off_in_admin_token_mode(tmp_path: Path) -> None:
    """With an admin token the form would make any local caller an instance admin without it."""
    async with dev_app(tmp_path, admin_token=TOKEN) as application:
        problems = dev_login_problems(application.extensions["settings"])
        assert len(problems) == 1 and "COVERSET_ADMIN_TOKEN" in problems[0], problems
        client = application.test_client()
        html = await (await client.get("/login")).get_data(as_text=True)
        assert "With the admin token" in html and "Sign in as a local user" not in html
        await assert_no_dev_login(application)


# -- disabled: every condition on its own -----------------------------------------------------------


class dev_app:  # noqa: N801 - used as ``async with dev_app(...)``
    """An app with the development sign-in flag on and ``env`` (default dev) set after construction."""

    def __init__(self, tmp_path: Path, *, env: str = "dev", **overrides: Any) -> None:
        values: dict[str, Any] = {
            "data_dir": tmp_path / "data",
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'coverset.db'}",
            "dev_login": True,
        }
        values.update(overrides)
        self.settings = Settings.for_tests(**values)
        self.env = env
        self.app = create_app(self.settings)
        self._ctx: Any = None

    async def __aenter__(self) -> Quart:
        self.settings.env = self.env  # type: ignore[assignment]
        self._ctx = self.app.test_app()
        await self._ctx.__aenter__()
        return self.app

    async def __aexit__(self, *exc: object) -> None:
        await self._ctx.__aexit__(*exc)


async def assert_no_dev_login(application: Quart, *, headers: dict[str, str] | None = None) -> None:
    client = application.test_client()
    page = await client.get("/login", headers=headers or {})
    if page.status_code == 200:
        assert "Sign in as a local user" not in await page.get_data(as_text=True)
    else:
        assert page.status_code in (401, 404, 421), page.status_code
    async with client.session_transaction() as cookie:
        cookie["csrf"] = "known"
    response = await client.post(
        "/login/dev",
        form={"login": "intruder", "role": "admin", "csrf_token": "known"},
        headers=headers or {},
    )
    assert response.status_code in (404, 421), response.status_code
    async with application.extensions["db"].session() as db:
        assert await db.scalar(select(User).where(User.login == "intruder")) is None


async def test_enabled_baseline(tmp_path: Path) -> None:
    async with dev_app(tmp_path) as application:
        assert dev_login_problems(application.extensions["settings"]) == []
        response = await dev_sign_in(application.test_client(), "baseline")
        assert response.status_code == 303


async def test_off_without_the_flag(tmp_path: Path) -> None:
    async with dev_app(tmp_path, dev_login=False) as application:
        await assert_no_dev_login(application)


@pytest.mark.parametrize("env", ["prod", "test"])
async def test_off_outside_the_dev_env(tmp_path: Path, env: str) -> None:
    async with dev_app(tmp_path, env=env) as application:
        await assert_no_dev_login(application)


async def test_off_on_a_non_loopback_bind(tmp_path: Path) -> None:
    """Configuration refuses a non-loopback bind without a token, so the bind is changed after
    construction: this condition alone turns the sign-in off."""
    async with dev_app(tmp_path) as application:
        application.extensions["settings"].bind = "0.0.0.0:8000"
        assert dev_login_problems(application.extensions["settings"]) == [
            "COVERSET_BIND=0.0.0.0:8000 is not a loopback address"
        ]
        await assert_no_dev_login(application)


async def test_off_behind_a_public_url(tmp_path: Path) -> None:
    async with dev_app(tmp_path) as application:
        application.extensions["settings"].public_url = "https://coverset.example.com"
        problems = dev_login_problems(application.extensions["settings"])
        assert len(problems) == 1 and "COVERSET_PUBLIC_URL" in problems[0], problems
        await assert_no_dev_login(application)


@pytest.mark.parametrize(
    "credentials",
    [
        {"github_token": SecretStr("ghp_" + "a" * 36)},
        {"github_token": SecretStr("ghp_" + "a" * 36), "forge": "fake"},
        {"github_app_client_id": "Iv1.abc", "github_app_client_secret": SecretStr("s" * 40)},
        {"github_app_id": 123, "allowed_owners": ["acme"]},
    ],
    ids=["token", "token-with-fake-forge", "oauth-client", "app-id"],
)
async def test_off_with_real_forge_credentials(tmp_path: Path, credentials: dict[str, Any]) -> None:
    async with dev_app(tmp_path, **credentials) as application:
        await assert_no_dev_login(application)


async def test_off_for_a_non_loopback_host_header(tmp_path: Path) -> None:
    """Reached under another name (a tunnel or proxy): no form. Open mode answers only its own host
    names, so the request-level check is also asserted on its own."""
    async with dev_app(tmp_path) as application:
        await assert_no_dev_login(application, headers={"Host": "coverset.example.com"})
        from coverset.web.account import dev_login_enabled

        async with application.test_request_context("/login", headers={"Host": "coverset.example.com"}):
            assert dev_login_enabled() is False
        async with application.test_request_context("/login", headers={"Host": "127.0.0.1:8000"}):
            assert dev_login_enabled() is True
