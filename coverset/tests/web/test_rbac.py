"""Repository-level access control on the UI, against the demo data.

A signed-in member sees exactly the repositories in ``repo_access`` (anything else is a 404
indistinguishable from a repository that never existed), may change only what they hold ``write``
on, and never reaches the instance-admin pages. Users and sessions are inserted directly; the
cookie is set through the test client's session transaction.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select
from test_web_pages import page_urls
from web_helpers import HX, Demo

from coverset.db.models import AuditEvent, Repo, RepoAccess, Run, TestCase, User, UserSession
from coverset.web.auth import hash_session_id
from coverset.web.principal import (
    ADMIN,
    PUBLIC,
    Principal,
    require_repo,
    set_principal,
    visible_repo_ids,
)

pytestmark = pytest.mark.security


@pytest.fixture
def settings_overrides(demo: Demo, admin_token: str) -> dict[str, Any]:
    return {**demo.overrides(), "admin_token": admin_token}


async def sign_in_as(
    app: Quart, client: Client, login: str, *, role: str = "member", access: dict[int, str] | None = None
) -> User:
    """Insert (or reuse) a user with the given repository access and give ``client`` a session."""
    settings = app.extensions["settings"]
    now = settings.now()
    raw = secrets.token_urlsafe(32)
    async with app.extensions["db"].session() as session:
        user = await session.scalar(select(User).where(User.login == login))
        if user is None:
            user = User(forge="github", external_id=f"id-{login}", login=login, role=role)
            session.add(user)
            await session.flush()
        user.role = role  # type: ignore[assignment]
        for row in await session.scalars(select(RepoAccess).where(RepoAccess.user_id == user.id)):
            await session.delete(row)
        for repo_id, permission in (access or {}).items():
            session.add(RepoAccess(user_id=user.id, repo_id=repo_id, permission=permission))  # type: ignore[arg-type]
        session.add(
            UserSession(
                id=hash_session_id(raw),
                user_id=user.id,
                csrf="unused",
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        await session.commit()
    async with client.session_transaction() as cookie:
        cookie.clear()
        cookie["sid"] = raw
        cookie["csrf"] = "test-csrf"
    return user


CSRF = {**HX, "X-CSRF-Token": "test-csrf"}


async def a_test_id(app: Quart, repo_id: int) -> int:
    async with app.extensions["db"].session() as session:
        found = await session.scalar(
            select(TestCase.id).where(TestCase.repo_id == repo_id).order_by(TestCase.id)
        )
        assert found is not None
        return found


async def test_reader_sees_pages_but_cannot_write(app: Quart, client: Client, demo: Demo) -> None:
    await sign_in_as(app, client, "reader", access={demo.repo_id: "read"})
    for url in page_urls(demo):
        if url.startswith("/forge/outbox"):
            continue
        assert (await client.get(url)).status_code == 200, url
    html = await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(as_text=True)
    assert "toggle-q" not in html, "no quarantine toggle without write"
    test_id = await a_test_id(app, demo.repo_id)
    url = f"/repos/{demo.repo_id}/tests/{test_id}/quarantine"
    assert (await client.post(url, headers=CSRF, form={"quarantined": "1"})).status_code == 403
    approve = await client.post(f"/runs/{demo.pr1_run}/approve", headers=CSRF, json={})
    assert approve.status_code == 403, "approval needs write on the run's repository"
    run_page = await (await client.get(f"/runs/{demo.pr1_run}")).get_data(as_text=True)
    assert "@reader" in run_page and 'href="/audit"' not in run_page


async def test_writer_can_quarantine_and_the_change_is_audited(
    app: Quart, client: Client, demo: Demo
) -> None:
    await sign_in_as(app, client, "writer", access={demo.repo_id: "write"})
    html = await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(as_text=True)
    assert "toggle-q" in html
    test_id = await a_test_id(app, demo.repo_id)
    url = f"/repos/{demo.repo_id}/tests/{test_id}/quarantine"
    try:
        on = await client.post(url, headers=CSRF, form={"quarantined": "1"})
        assert on.status_code == 200 and 'aria-pressed="true"' in await on.get_data(as_text=True)
    finally:
        off = await client.post(url, headers=CSRF, form={"quarantined": "0"})
        assert off.status_code == 200
    async with app.extensions["db"].session() as session:
        rows = list(await session.scalars(select(AuditEvent).where(AuditEvent.action == "quarantine")))
    assert rows and rows[-1].actor_label == "@writer" and rows[-1].repo_id == demo.repo_id
    assert rows[-1].target_id == str(test_id)
    # a writer passes the permission check on approval; the completed run is then a 409, not 403
    approve = await client.post(f"/runs/{demo.pr1_run}/approve", headers=CSRF, json={})
    assert approve.status_code == 409
    # no write without the CSRF token, even with write permission
    assert (await client.post(url, headers=HX, form={"quarantined": "1"})).status_code == 403


def normalise(body: str, rid: int) -> str:
    return body.replace(f"/repos/{rid}", "/repos/ID").replace(f"with id {rid}.", "with id ID.")


async def test_unseen_repositories_are_indistinguishable_from_missing_ones(
    app: Quart, client: Client, demo: Demo
) -> None:
    await sign_in_as(app, client, "stranger", access={})
    missing = 10**9
    pairs = [
        (f"/repos/{demo.repo_id}", f"/repos/{missing}"),
        (f"/repos/{demo.repo_id}/tests", f"/repos/{missing}/tests"),
        (f"/repos/{demo.repo_id}/map", f"/repos/{missing}/map"),
        (f"/repos/{demo.repo_id}/invariants", f"/repos/{missing}/invariants"),
        (f"/repos/{demo.repo_id}/prs/1", f"/repos/{missing}/prs/1"),
        (
            f"/repos/{demo.repo_id}/files/src/ledger/durations/parse.py",
            f"/repos/{missing}/files/src/ledger/durations/parse.py",
        ),
        (f"/repos/{demo.repo_id}/tests/1", f"/repos/{missing}/tests/1"),
        (f"/repos/{demo.repo_id}/tests/by-nodeid?nodeid=x", f"/repos/{missing}/tests/by-nodeid?nodeid=x"),
    ]
    for seen, unseen in pairs:
        first = await client.get(seen)
        second = await client.get(unseen)
        assert first.status_code == 404 == second.status_code, seen
        # the bodies differ only in the id the caller typed
        hidden = normalise(await first.get_data(as_text=True), demo.repo_id)
        absent = normalise(await second.get_data(as_text=True), missing)
        assert hidden == absent, seen
    for url in (f"/runs/{demo.pr1_run}", f"/runs/{demo.pr1_run}/events", f"/runs/{demo.pr1_run}/coverage"):
        assert (await client.get(url)).status_code == 404, url
    dashboard = await (await client.get("/")).get_data(as_text=True)
    async with app.extensions["db"].session() as session:
        repo = await session.get(Repo, demo.repo_id)
        assert repo is not None
    assert repo.full_name not in dashboard, "no repository names leak through the dashboard or the nav"
    assert demo.pr1_run not in dashboard


async def test_members_never_reach_admin_pages(app: Quart, client: Client, demo: Demo) -> None:
    await sign_in_as(app, client, "member", access={demo.repo_id: "admin"})
    for url in ("/setup", "/forge/outbox", "/audit", "/setup/github/finish"):
        response = await client.get(url)
        assert response.status_code == 403, url
        browser = await client.get(url, headers={"Accept": "text/html"})
        assert browser.status_code == 403, url
        html = await browser.get_data(as_text=True)
        assert "Instance admins only" in html and "@member" in html, url
    assert (await client.get(f"/repos/{demo.repo_id}")).status_code == 200


async def test_browsers_are_sent_to_the_login_page(client: Client, demo: Demo) -> None:
    """A browser navigating to a page without credentials gets the login page; API clients a 401."""
    browser = await client.get(f"/repos/{demo.repo_id}/tests?sort=slow", headers={"Accept": "text/html"})
    assert browser.status_code == 303
    assert browser.headers["Location"] == f"/login?next=%2Frepos%2F{demo.repo_id}%2Ftests%3Fsort%3Dslow"
    assert (await client.get("/", headers={"Accept": "text/html", **HX})).status_code == 401, "htmx stays 401"
    assert (await client.get("/")).status_code == 401
    assert (await client.get("/", headers={"Accept": "application/json"})).status_code == 401
    assert (await client.post("/logout", headers={"Accept": "text/html"})).status_code == 401


async def test_admin_role_users_see_everything(app: Quart, client: Client, demo: Demo) -> None:
    await sign_in_as(app, client, "root", role="admin", access={})
    for url in (*page_urls(demo), "/audit", "/account"):
        assert (await client.get(url)).status_code == 200, url
    html = await (await client.get("/")).get_data(as_text=True)
    assert 'href="/audit"' in html and "@root" in html
    assert (await client.get("/setup")).status_code == 200


async def test_admin_token_pages_still_work(client: Client, demo: Demo, admin_token: str) -> None:
    html = await (await client.get("/", headers={"Authorization": f"Bearer {admin_token}"})).get_data(
        as_text=True
    )
    assert "admin token" in html and 'href="/audit"' in html


async def test_expired_session_is_rejected(app: Quart, client: Client, demo: Demo) -> None:
    await sign_in_as(app, client, "late", access={demo.repo_id: "read"})
    assert (await client.get("/")).status_code == 200
    async with app.extensions["db"].session() as session:
        for row in await session.scalars(select(UserSession)):
            row.expires_at = app.extensions["settings"].now() - timedelta(seconds=1)
        await session.commit()
    assert (await client.get("/")).status_code == 401
    login = await client.get("/", headers={"Accept": "text/html"})
    assert login.status_code == 303 and login.headers["Location"] == "/login?next=%2F"


async def test_anonymous_pages_show_a_sign_in_link(client: Client, demo: Demo) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    html = await response.get_data(as_text=True)
    assert "<nav" not in html
    assert re.search(r'name="token"', html)


# -- the contract itself --------------------------------------------------------------------------


async def test_members_read_the_v1_api_for_their_repositories(app: Quart, client: Client, demo: Demo) -> None:
    """The public ``/v1`` routes answer a signed-in member for the repositories they may read with
    the full (non-public) view; a repository they cannot see stays a 404 or the public view."""
    async with app.extensions["db"].session() as session:
        run = await session.get(Run, demo.pr1_run)
        repo = await session.get(Repo, demo.repo_id)
        assert run is not None and repo is not None
        verdict = f"/v1/verdict?repo_id={repo.external_id}&sha={run.head_sha}"
        private = repo.private
    await sign_in_as(app, client, "reader", access={demo.repo_id: "read"})
    response = await client.get(verdict)
    assert response.status_code == 200 and "summary" in await response.get_json()
    response = await client.get(f"/v1/runs/{demo.pr1_run}")
    assert response.status_code == 200 and (await response.get_json())["run_id"] == demo.pr1_run
    await sign_in_as(app, client, "stranger", access={})
    response = await client.get(verdict)
    if private:
        assert response.status_code == 404
    else:
        assert response.status_code == 200 and "summary" not in await response.get_json()


async def test_principal_rules(app: Quart) -> None:
    user = Principal(kind="user", user_id=1, login="u", repo_permissions={1: "read", 2: "write", 3: "admin"})
    assert user.repo_permission(1) == "read" and user.can(2, "write") and not user.can(1, "write")
    assert user.repo_permission(9) is None
    personal_read = Principal(
        kind="token", user_id=1, scopes=frozenset({"read"}), repo_permissions={2: "write"}
    )
    assert personal_read.repo_permission(2) == "read", "a personal token never exceeds its scopes"
    personal_write = Principal(
        kind="token", user_id=1, scopes=frozenset({"write"}), repo_permissions={2: "admin", 1: "read"}
    )
    assert personal_write.repo_permission(2) == "write" and personal_write.repo_permission(1) == "read"
    personal_admin = Principal(
        kind="token", user_id=1, scopes=frozenset({"admin"}), repo_permissions={2: "admin"}
    )
    assert personal_admin.repo_permission(2) == "admin"
    repo_upload = Principal(kind="token", scopes=frozenset({"upload"}), bound_repo_id=5)
    assert repo_upload.repo_permission(5) == "write" and repo_upload.repo_permission(6) is None
    repo_read = Principal(kind="token", scopes=frozenset({"read"}), bound_repo_id=5)
    assert repo_read.repo_permission(5) == "read"
    oidc = Principal(kind="oidc", bound_repo_id=7)
    assert oidc.repo_permission(7) == "read" and not oidc.can(7, "write") and oidc.repo_permission(8) is None
    assert ADMIN.repo_permission(123) == "admin" and PUBLIC.repo_permission(1) is None

    async with app.test_request_context("/"):
        set_principal(user)
        assert visible_repo_ids([1, 2, 3, 4]) == [1, 2, 3]
        assert require_repo(2, "write").login == "u"
        with pytest.raises(Exception) as forbidden:
            require_repo(1, "write")
        assert forbidden.value.code == 403  # type: ignore[attr-defined]
        with pytest.raises(Exception) as hidden:
            require_repo(4)
        assert hidden.value.code == 404  # type: ignore[attr-defined]
        set_principal(ADMIN)
        assert visible_repo_ids([1, 2]) == [1, 2]
