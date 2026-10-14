"""GitHub sign-in, server-side sessions, the account page and the audit page.

GitHub is a fake HTTP layer (:class:`httpx.MockTransport`) behind :class:`GitHubOAuth`: the token
endpoint, ``/user``, the installation listing and ``/repos/{owner}/{name}``. No network.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select
from web_helpers import assert_csp_clean

from coverset.config import Settings
from coverset.db.models import AuditEvent, Repo, RepoAccess, User, UserSession
from coverset.forge.oauth import GitHubOAuth, permission_from_github
from coverset.logging import redact
from coverset.web.account import ACCESS_SYNC_EXTENSION, OAUTH_EXTENSION, refresh_access_task
from coverset.web.auth import LOGIN_ATTEMPTS_PER_MINUTE
from coverset.web.tokens import hash_token

pytestmark = pytest.mark.security

CLIENT_ID = "Iv1.0123456789abcdef"
CLIENT_SECRET = "client-secret-0123456789abcdef0123456789"
USER_TOKEN = "ghu_" + "A1b2C3d4" * 5
REFRESH_TOKEN = "ghr_" + "Z9y8X7w6" * 10
NEW_TOKEN = "ghu_" + "N3wT0k3n" * 5
ALICE = {"id": 4242, "login": "Alice", "name": "Alice Example", "avatar_url": "https://avatars.example/4242"}


@pytest.fixture
def settings_overrides(admin_token: str) -> dict[str, Any]:
    """Not the demo data: these tests need no runs, just an admin token and a public URL."""
    return {"admin_token": admin_token, "public_url": "https://coverset.example"}


class FakeGitHub:
    """What the fake answers; tests mutate the attributes between requests."""

    def __init__(self) -> None:
        self.user = dict(ALICE)
        self.installations: dict[int, dict[int, dict[str, bool]]] = {}
        """installation id -> repository id -> GitHub permissions object."""
        self.repos: dict[str, dict[str, bool] | None] = {}
        """full name -> permissions (None = 404) for the per-repository fallback."""
        self.token_response: dict[str, Any] = {
            "access_token": USER_TOKEN,
            "expires_in": 28800,
            "refresh_token": REFRESH_TOKEN,
            "refresh_token_expires_in": 15811200,
            "token_type": "bearer",
            "scope": "",
        }
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_user = False
        self.revoked = False
        """The user revoked the App: every REST call is 401 and a refresh is ``bad_refresh_token``."""
        self.rest_down = False
        """GitHub is having a bad day: every REST call is 502 (a transient failure)."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        body: dict[str, Any] = {}
        if request.method == "POST" and request.content:
            body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((request.method, url.path, body))
        auth = request.headers.get("Authorization", "")
        if url.netloc == "github.com" and url.path == "/login/oauth/access_token":
            assert request.headers["Accept"] == "application/json"
            assert body["client_id"] == CLIENT_ID and body["client_secret"] == CLIENT_SECRET
            if body.get("grant_type") == "refresh_token":
                if self.revoked or body["refresh_token"] != REFRESH_TOKEN:
                    return httpx.Response(200, json={"error": "bad_refresh_token"})
                return httpx.Response(200, json={**self.token_response, "access_token": NEW_TOKEN})
            if body.get("code") != "good-code":
                return httpx.Response(200, json={"error": "bad_verification_code"})
            assert body["redirect_uri"] == "https://coverset.example/auth/github/callback"
            return httpx.Response(200, json=self.token_response)
        if url.netloc != "api.github.com":
            return httpx.Response(500, json={"message": f"unexpected host {url.netloc}"})
        if self.rest_down:
            return httpx.Response(502, json={"message": "bad gateway"})
        if self.revoked:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if auth not in (
            f"token {USER_TOKEN}",
            f"token {NEW_TOKEN}",
            f"Bearer {USER_TOKEN}",
            f"Bearer {NEW_TOKEN}",
        ):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if url.path == "/user":
            if self.fail_user:
                return httpx.Response(502, json={"message": "bad gateway"})
            return httpx.Response(200, json=self.user)
        match = re.fullmatch(r"/user/installations/(\d+)/repositories", url.path)
        if match:
            listing = self.installations.get(int(match.group(1)))
            if listing is None:
                return httpx.Response(404, json={"message": "Not Found"})
            repos = [{"id": rid, "permissions": perms} for rid, perms in listing.items()]
            return httpx.Response(200, json={"total_count": len(repos), "repositories": repos})
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)", url.path)
        if match:
            perms = self.repos.get(match.group(1))
            if perms is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": 1, "full_name": match.group(1), "permissions": perms})
        return httpx.Response(404, json={"message": f"no fake for {url.path}"})


@pytest.fixture
def github(app: Quart) -> FakeGitHub:
    """Install the fake GitHub behind the app's OAuth client."""
    fake = FakeGitHub()
    app.extensions[OAUTH_EXTENSION] = GitHubOAuth(
        CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(fake.handler)
    )
    return fake


async def seed_repos(app: Quart) -> dict[str, Repo]:
    """Three GitHub repos: two under installation 55, one without an installation id."""
    async with app.extensions["db"].session() as session:
        repos = {
            "app": Repo(
                forge="github", external_id="1001", full_name="acme/app", installation_id=55, owner_id=7
            ),
            "lib": Repo(
                forge="github", external_id="1002", full_name="acme/lib", installation_id=55, owner_id=7
            ),
            "old": Repo(
                forge="github", external_id="1003", full_name="acme/old", installation_id=None, owner_id=7
            ),
        }
        session.add_all(repos.values())
        await session.commit()
        return {name: repo for name, repo in repos.items()}


async def sign_in(client: Client, *, next_path: str | None = None) -> httpx.URL:
    """Drive the whole flow; returns the authorize URL GitHub would have shown."""
    query = {"next": next_path} if next_path else {}
    start = await client.get("/auth/github", query_string=query)
    assert start.status_code == 303
    authorize = httpx.URL(start.headers["Location"])
    state = authorize.params["state"]
    done = await client.get("/auth/github/callback", query_string={"code": "good-code", "state": state})
    assert done.status_code == 303, await done.get_data(as_text=True)
    return authorize


async def audit_rows(app: Quart) -> list[AuditEvent]:
    async with app.extensions["db"].session() as session:
        return list(await session.scalars(select(AuditEvent).order_by(AuditEvent.id)))


# -- the login page -----------------------------------------------------------------------------------


async def test_login_page_offers_the_token_only_without_github(client: Client) -> None:
    page = await (await client.get("/login")).get_data(as_text=True)
    assert 'name="token"' in page
    assert "/auth/github" not in page
    assert_csp_clean(page)
    assert (await client.get("/auth/github")).status_code == 404


async def test_login_page_offers_both_with_github(client: Client, github: FakeGitHub) -> None:
    page = await (await client.get("/login", query_string={"next": "/repos/1"})).get_data(as_text=True)
    assert "Sign in with GitHub" in page and 'name="token"' in page
    assert re.search(r'href="/auth/github\?next=(?:/|%2F)repos(?:/|%2F)1"', page)
    assert "nav_repos" not in page and "<nav" not in page  # nothing about the instance before sign-in
    assert_csp_clean(page)


async def test_login_page_offers_github_from_settings(settings: Settings) -> None:
    from coverset.app import create_app

    configured = settings.model_copy(
        update={"github_app_client_id": CLIENT_ID, "github_app_client_secret": SecretStr(CLIENT_SECRET)}
    )
    app = create_app(configured)
    async with app.test_app():
        page = await (await app.test_client().get("/login")).get_data(as_text=True)
    assert "Sign in with GitHub" in page


class TestOpenMode:
    @pytest.fixture
    def settings_overrides(self) -> dict[str, Any]:
        return {}

    async def test_login_is_404_with_nothing_configured(self, client: Client) -> None:
        assert (await client.get("/login")).status_code == 404
        assert (await client.get("/auth/github")).status_code == 404
        assert (await client.get("/account")).status_code == 200  # the local operator


# -- the flow ---------------------------------------------------------------------------------------


async def test_authorize_redirect_carries_state_and_no_scope(client: Client, github: FakeGitHub) -> None:
    response = await client.get("/auth/github", query_string={"next": "/repos/1"})
    assert response.status_code == 303
    url = httpx.URL(response.headers["Location"])
    assert url.host == "github.com" and url.path == "/login/oauth/authorize"
    assert url.params["client_id"] == CLIENT_ID
    assert url.params["redirect_uri"] == "https://coverset.example/auth/github/callback"
    assert len(url.params["state"]) >= 32
    assert "scope" not in url.params
    assert response.headers["Cache-Control"] == "no-store"


async def test_callback_rejects_a_bad_or_missing_state(client: Client, github: FakeGitHub) -> None:
    assert (
        await client.get("/auth/github/callback", query_string={"code": "good-code", "state": "x"})
    ).status_code == 400
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    wrong = await client.get(
        "/auth/github/callback", query_string={"code": "good-code", "state": state + "x"}
    )
    assert wrong.status_code == 400
    # the state is single use: the right one no longer works either
    assert (
        await client.get("/auth/github/callback", query_string={"code": "good-code", "state": state})
    ).status_code == 400
    assert github.calls == []


async def test_callback_rejects_githubs_error_and_bad_codes(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    denied = await client.get(
        "/auth/github/callback", query_string={"error": "access_denied", "state": state}
    )
    assert denied.status_code == 400 and "access denied" in await denied.get_data(as_text=True)
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    bad = await client.get("/auth/github/callback", query_string={"code": "bad code!", "state": state})
    assert bad.status_code == 400
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    refused = await client.get("/auth/github/callback", query_string={"code": "stale-code", "state": state})
    assert refused.status_code == 502
    assert "bad_verification_code" in await refused.get_data(as_text=True)
    actions = [e.action for e in await audit_rows(app)]
    assert actions.count("login_failed") == 2
    assert (await client.get("/")).status_code == 401


async def test_sign_in_creates_user_session_and_access(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    repos = await seed_repos(app)
    github.installations[55] = {1001: {"admin": True, "push": True, "pull": True}, 1002: {"pull": True}}
    github.repos["acme/old"] = {"push": True, "pull": True}
    authorize = await sign_in(client, next_path="/account")
    assert authorize.params["redirect_uri"].endswith("/auth/github/callback")

    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None
        assert (user.forge, user.external_id, user.login, user.name) == (
            "github",
            "4242",
            "Alice",
            "Alice Example",
        )
        assert user.role == "member" and not user.disabled
        assert user.oauth_token == USER_TOKEN and user.oauth_refresh_token == REFRESH_TOKEN
        assert user.oauth_expires_at is not None and user.last_login_at is not None
        rows = list(await session.scalars(select(UserSession)))
        assert len(rows) == 1
        assert re.fullmatch(r"[0-9a-f]{64}", rows[0].id)
        assert rows[0].expires_at - rows[0].created_at == timedelta(days=30)
        access = {row.repo_id: row.permission for row in await session.scalars(select(RepoAccess))}
    assert access == {repos["app"].id: "admin", repos["lib"].id: "read", repos["old"].id: "write"}
    paths = [path for _m, path, _b in github.calls]
    assert paths == [
        "/login/oauth/access_token",
        "/user",
        "/user/installations/55/repositories",
        "/repos/acme/old",
    ]

    # the cookie names the session: the raw id is in the cookie, its sha256 in the row
    page = await client.get("/account")
    assert page.status_code == 200
    html = await page.get_data(as_text=True)
    assert "@Alice" in html and "acme/app" in html and "acme/old" in html
    assert "this session" in html and "/settings/tokens" in html
    assert_csp_clean(html)
    async with client.session_transaction() as cookie:
        assert hash_token(cookie["sid"]) == rows[0].id
        assert cookie.get("admin") is None
    assert (await client.get("/")).status_code == 200


async def test_sign_in_rotates_the_session(client: Client, github: FakeGitHub) -> None:
    """A session id planted before sign-in never survives it (session fixation)."""
    async with client.session_transaction() as cookie:
        cookie["sid"] = "planted-by-an-attacker"
        cookie["csrf"] = "planted-csrf"
    await sign_in(client)
    async with client.session_transaction() as cookie:
        assert cookie["sid"] != "planted-by-an-attacker"
        assert cookie["csrf"] != "planted-csrf"


async def test_next_is_validated_through_the_flow(client: Client, github: FakeGitHub) -> None:
    start = await client.get("/auth/github", query_string={"next": "https://evil.example/"})
    state = httpx.URL(start.headers["Location"]).params["state"]
    done = await client.get("/auth/github/callback", query_string={"code": "good-code", "state": state})
    assert done.headers["Location"] == "/"


async def test_logout_deletes_the_session_row(client: Client, github: FakeGitHub, app: Quart) -> None:
    await sign_in(client)
    page = await (await client.get("/account")).get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    assert (await client.post("/logout")).status_code == 403, "a cookie write needs the CSRF token"
    out = await client.post("/logout", form={"csrf_token": csrf})
    assert out.status_code == 303
    async with app.extensions["db"].session() as session:
        assert list(await session.scalars(select(UserSession))) == []
    assert (await client.get("/")).status_code == 401
    actions = [e.action for e in await audit_rows(app)]
    assert actions[-1] == "logout" and "login" in actions


async def test_disabled_user_cannot_sign_in_and_loses_sessions(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    await sign_in(client)
    assert (await client.get("/")).status_code == 200
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None
        user.disabled = True
        await session.commit()
    assert (await client.get("/")).status_code == 401, "existing sessions are rejected at once"
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    refused = await client.get("/auth/github/callback", query_string={"code": "good-code", "state": state})
    assert refused.status_code == 403
    assert "disabled" in await refused.get_data(as_text=True)
    assert (await client.get("/")).status_code == 401


async def test_admins_list_grants_and_removes_the_role(
    client: Client, github: FakeGitHub, app: Quart, settings: Settings
) -> None:
    settings.admins = ["alice"]  # matched case-insensitively, like GitHub logins
    await sign_in(client)
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None and user.role == "admin"
    assert (await client.get("/forge/outbox")).status_code == 200
    assert (await client.get("/audit")).status_code == 200
    html = await (await client.get("/")).get_data(as_text=True)
    assert 'href="/audit"' in html and "@Alice" in html
    settings.admins = ["someone-else"]
    await sign_in(client)
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None and user.role == "member"
    assert (await client.get("/forge/outbox")).status_code == 403
    assert (await client.get("/audit")).status_code == 403
    assert (await client.get("/setup")).status_code == 403
    html = await (await client.get("/")).get_data(as_text=True)
    assert 'href="/audit"' not in html


async def test_second_sign_in_updates_the_profile_not_the_identity(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    await sign_in(client)
    github.user = {**ALICE, "login": "alice-renamed", "name": None, "avatar_url": None}
    await sign_in(client)
    async with app.extensions["db"].session() as session:
        users = list(await session.scalars(select(User)))
        assert len(users) == 1
        assert users[0].login == "alice-renamed" and users[0].external_id == "4242"
        assert len(list(await session.scalars(select(UserSession)))) == 2


# -- repository access -------------------------------------------------------------------------------


async def test_refresh_reads_access_again_and_drops_lost_repos(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    repos = await seed_repos(app)
    github.installations[55] = {1001: {"pull": True}, 1002: {"pull": True}}
    github.repos["acme/old"] = {"pull": True}
    await sign_in(client)
    assert (await client.get(f"/repos/{repos['lib'].id}/tests")).status_code == 200
    github.installations[55] = {1001: {"push": True, "pull": True}}
    github.repos["acme/old"] = None
    page = await (await client.get("/account")).get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    response = await client.post("/account/refresh", form={"csrf_token": csrf})
    assert response.status_code == 303 and response.headers["Location"] == "/account?refreshed=ok"
    async with app.extensions["db"].session() as session:
        access = {row.repo_id: row.permission for row in await session.scalars(select(RepoAccess))}
    assert access == {repos["app"].id: "write"}
    assert (await client.get(f"/repos/{repos['lib'].id}/tests")).status_code == 404
    assert (await client.get(f"/repos/{repos['old'].id}/tests")).status_code == 404
    html = await (await client.get("/account?refreshed=ok")).get_data(as_text=True)
    assert "refreshed from GitHub" in html and "acme/lib" not in html
    actions = [e.action for e in await audit_rows(app)]
    assert "access_refreshed" in actions


async def csrf_of(client: Client, path: str = "/account") -> str:
    page = await (await client.get(path)).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]


async def test_revoked_authorization_clears_access_and_sessions(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    """A transient GitHub failure keeps the last known access; GitHub refusing the credentials
    (401, ``bad_refresh_token``) clears ``repo_access``, every session and the stored tokens, is
    audited, and sends the person back to the login page with a note."""
    repos = await seed_repos(app)
    github.installations[55] = {1001: {"pull": True}}
    github.repos["acme/old"] = None
    await sign_in(client)
    csrf = await csrf_of(client)
    github.rest_down = True
    response = await client.post("/account/refresh", form={"csrf_token": csrf})
    assert response.status_code == 303 and response.headers["Location"] == "/account?refreshed=failed"
    async with app.extensions["db"].session() as session:
        assert len(list(await session.scalars(select(RepoAccess)))) == 1, "a 502 changes nothing"
    github.rest_down = False
    github.revoked = True
    response = await client.post("/account/refresh", form={"csrf_token": csrf})
    assert response.status_code == 303
    assert response.headers["Location"] == "/login?next=%2Faccount&reason=revoked"
    async with app.extensions["db"].session() as session:
        assert list(await session.scalars(select(RepoAccess))) == []
        assert list(await session.scalars(select(UserSession))) == []
        user = await session.scalar(select(User))
        assert user is not None and user.role == "member" and not user.disabled
        assert user.oauth_token is None and user.oauth_refresh_token is None
    rows = await audit_rows(app)
    assert rows[-1].action == "access_revoked" and rows[-1].actor_label == "@Alice"
    assert "401" in rows[-1].details["reason"] and USER_TOKEN not in json.dumps(rows[-1].details)
    note = await (await client.get("/login", query_string={"reason": "revoked"})).get_data(as_text=True)
    assert "no longer accepts" in note
    assert (await client.get("/account", headers={"Accept": "text/html"})).status_code == 303, "signed out"
    assert (await client.get(f"/repos/{repos['app'].id}/tests")).status_code == 401
    # The background refresh takes the same path when the refresh token has died.
    github.revoked = False
    await sign_in(client)
    github.revoked = True
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None
        user.oauth_expires_at = user.oauth_expires_at - timedelta(days=1)  # type: ignore[operator]
        await session.commit()
        user_id = user.id
    async with app.app_context():
        await refresh_access_task(user_id)
    async with app.extensions["db"].session() as session:
        assert list(await session.scalars(select(RepoAccess))) == []
        assert list(await session.scalars(select(UserSession))) == []
    assert [r.action for r in await audit_rows(app)].count("access_revoked") == 2
    assert "bad_refresh_token" in (await audit_rows(app))[-1].details["reason"]


async def test_sign_in_cookie_is_permanent(client: Client, github: FakeGitHub, app: Quart) -> None:
    """The cookie lasts as long as the row (``COVERSET_SESSION_DAYS``), not just the browser run;
    the admin-token cookie stays a browser-session cookie."""
    settings: Settings = app.extensions["settings"]
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=settings.session_days)
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    done = await client.get("/auth/github/callback", query_string={"code": "good-code", "state": state})
    cookies = [c for c in done.headers.getlist("Set-Cookie") if c.startswith("coverset_session=")]
    assert len(cookies) == 1 and "Expires=" in cookies[0] and "HttpOnly" in cookies[0]
    assert "Secure" in cookies[0] and "SameSite=Lax" in cookies[0]
    other = app.test_client()
    login = await other.post("/login", form={"token": settings.admin_token.get_secret_value(), "next": "/"})  # type: ignore[union-attr]
    cookies = [c for c in login.headers.getlist("Set-Cookie") if c.startswith("coverset_session=")]
    assert len(cookies) == 1 and "Expires=" not in cookies[0] and "Max-Age=" not in cookies[0]


async def test_installation_fallback_asks_per_repo(client: Client, github: FakeGitHub, app: Quart) -> None:
    """A user outside the installation (404 on the listing) is checked repository by repository."""
    repos = await seed_repos(app)
    github.repos["acme/app"] = {"pull": True}
    github.repos["acme/lib"] = None
    github.repos["acme/old"] = None
    await sign_in(client)
    async with app.extensions["db"].session() as session:
        access = {row.repo_id: row.permission for row in await session.scalars(select(RepoAccess))}
    assert access == {repos["app"].id: "read"}
    paths = [path for _m, path, _b in github.calls]
    assert paths[2:] == [
        "/user/installations/55/repositories",
        "/repos/acme/old",  # never under an installation: asked first
        "/repos/acme/app",  # the installation refused the listing: one call per repository
        "/repos/acme/lib",
    ]


async def test_expired_user_token_is_refreshed_before_use(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    await seed_repos(app)
    github.installations[55] = {1001: {"pull": True}}
    github.repos["acme/old"] = None
    await sign_in(client)
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None
        user.oauth_expires_at = user.oauth_expires_at - timedelta(days=1)  # type: ignore[operator]
        await session.commit()
    page = await (await client.get("/account")).get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    github.calls.clear()
    assert (await client.post("/account/refresh", form={"csrf_token": csrf})).status_code == 303
    assert github.calls[0][1] == "/login/oauth/access_token"
    assert github.calls[0][2]["grant_type"] == "refresh_token"
    assert github.calls[0][2]["refresh_token"] == REFRESH_TOKEN
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User))
        assert user is not None and user.oauth_token == NEW_TOKEN
        assert user.oauth_expires_at is not None and user.oauth_expires_at > app.extensions["settings"].now()


async def test_background_refresh_is_scheduled_every_ten_minutes(
    client: Client, github: FakeGitHub, app: Quart
) -> None:
    await seed_repos(app)
    github.installations[55] = {1001: {"pull": True}}
    github.repos["acme/old"] = None
    await sign_in(client)
    marks = app.extensions[ACCESS_SYNC_EXTENSION]
    (user_id,) = marks
    before = len(github.calls)
    await client.get("/")
    assert len(github.calls) == before, "just synced: no new calls"
    marks[user_id] = marks[user_id] - timedelta(minutes=11)
    await client.get("/")
    # the background task runs on the app's loop; a second request lets it finish
    await client.get("/")
    assert [p for _m, p, _b in github.calls[before:]] == [
        "/user/installations/55/repositories",
        "/repos/acme/old",
    ]


async def test_session_touch_is_throttled(client: Client, github: FakeGitHub, app: Quart) -> None:
    await sign_in(client)
    async with app.extensions["db"].session() as session:
        row = await session.scalar(select(UserSession))
        assert row is not None
        created = row.last_seen_at
    await client.get("/")
    async with app.extensions["db"].session() as session:
        row = await session.scalar(select(UserSession))
        assert row is not None and row.last_seen_at == created, "within five minutes: no write"
        row.last_seen_at = created - timedelta(minutes=6)
        await session.commit()
    await client.get("/")
    async with app.extensions["db"].session() as session:
        row = await session.scalar(select(UserSession))
        assert row is not None and row.last_seen_at > created


async def test_revoke_another_session(client: Client, github: FakeGitHub, app: Quart) -> None:
    await sign_in(client)
    other = app.test_client()  # the same app, a separate cookie jar
    await sign_in(other)
    page = await (await client.get("/account")).get_data(as_text=True)
    assert page.count("Revoke") == 1 and page.count(">Sign out<") >= 1
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    async with app.extensions["db"].session() as session:
        rows = list(await session.scalars(select(UserSession)))
    async with client.session_transaction() as cookie:
        mine = hash_token(cookie["sid"])
    theirs = next(r.id for r in rows if r.id != mine)
    assert (
        await client.post(f"/account/sessions/{theirs}/revoke", form={"csrf_token": csrf})
    ).status_code == 303
    assert (await other.get("/")).status_code == 401
    assert (await client.get("/")).status_code == 200
    assert (
        await client.post(f"/account/sessions/{'0' * 64}/revoke", form={"csrf_token": csrf})
    ).status_code == 404


async def test_sign_in_routes_are_rate_limited(client: Client, github: FakeGitHub) -> None:
    for _ in range(LOGIN_ATTEMPTS_PER_MINUTE):
        assert (await client.get("/auth/github")).status_code == 303
    limited = await client.get("/auth/github")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert (
        await client.get("/auth/github/callback", query_string={"code": "x", "state": "y"})
    ).status_code == 429


# -- audit and redaction -----------------------------------------------------------------------------


async def test_audit_rows_never_contain_tokens(client: Client, github: FakeGitHub, app: Quart) -> None:
    await seed_repos(app)
    github.installations[55] = {1001: {"pull": True}}
    github.repos["acme/old"] = None
    github.fail_user = True
    start = await client.get("/auth/github")
    state = httpx.URL(start.headers["Location"]).params["state"]
    assert (
        await client.get("/auth/github/callback", query_string={"code": "good-code", "state": state})
    ).status_code == 502
    github.fail_user = False
    await sign_in(client)
    rows = await audit_rows(app)
    assert [r.action for r in rows] == ["login_failed", "login"]
    dumped = json.dumps([[r.actor_kind, r.actor_label, r.target_kind, r.target_id, r.details] for r in rows])
    for secret in (USER_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, "good-code"):
        assert secret not in dumped
    assert rows[1].actor_kind == "user" and rows[1].actor_label == "@Alice" and rows[1].ip is not None

    from coverset.web.audit import record_audit
    from coverset.web.principal import ADMIN

    async with app.app_context(), app.extensions["db"].session() as session:
        event = record_audit(
            session, ADMIN, "test", details={"token": USER_TOKEN, "nested": [f"Bearer {USER_TOKEN}"], "n": 1}
        )
        await session.commit()
    assert event.details == {"token": "ghu_[REDACTED]", "nested": ["Bearer ghu_[REDACTED]"], "n": 1}


def test_user_and_refresh_tokens_are_redacted_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    text = f"exchange gave {USER_TOKEN} and {REFRESH_TOKEN}"
    out = redact(text)
    assert USER_TOKEN not in out and REFRESH_TOKEN not in out
    assert out == "exchange gave ghu_[REDACTED] and ghr_[REDACTED]"
    from coverset.logging import RedactingFilter

    logger = logging.getLogger("coverset.test.oauth")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="coverset.test.oauth"):
        logger.info("token %s", USER_TOKEN)
    logger.filters.clear()
    assert USER_TOKEN not in caplog.text and "ghu_[REDACTED]" in caplog.text


async def test_audit_page_filters(client: Client, github: FakeGitHub, app: Quart, settings: Settings) -> None:
    settings.admins = ["Alice"]
    repos = await seed_repos(app)
    github.installations[55] = {1001: {"admin": True, "pull": True}}
    github.repos["acme/old"] = None
    await sign_in(client)
    from coverset.web.audit import record_audit
    from coverset.web.principal import ADMIN

    async with app.app_context(), app.extensions["db"].session() as session:
        record_audit(session, ADMIN, "approve", target_kind="run", target_id="run_1", repo_id=repos["app"].id)
        await session.commit()
    html = await (await client.get("/audit")).get_data(as_text=True)
    assert "<code>login</code>" in html and "<code>approve</code>" in html and "acme/app" in html
    assert_csp_clean(html)
    only = await (await client.get("/audit", query_string={"action": "approve"})).get_data(as_text=True)
    assert "<code>approve</code>" in only and "<code>login</code>" not in only
    by_repo = await (await client.get("/audit", query_string={"repo": repos["lib"].id})).get_data(
        as_text=True
    )
    assert "Nothing recorded yet" in by_repo


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ({"admin": True, "maintain": True, "push": True, "triage": True, "pull": True}, "admin"),
        ({"admin": False, "maintain": True, "push": True, "pull": True}, "write"),
        ({"push": True, "pull": True}, "write"),
        ({"triage": True, "pull": True}, "read"),
        ({"pull": True}, "read"),
        ({"pull": False}, None),
        ({}, None),
        (None, None),
    ],
)
def test_permission_mapping(permissions: dict[str, bool] | None, expected: str | None) -> None:
    assert permission_from_github(permissions) == expected
