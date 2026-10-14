"""The header: admin-only links, one "Settings" per kind, the economy include, and GitHub avatars.

A fresh control database per test (no demo data): two repositories, the admin token, and users
inserted with a server-side session the way ``test_rbac.sign_in_as`` does it.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any

import pytest
from quart import Quart, render_template
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select
from web_helpers import assert_csp_clean

from coverset.app import AVATAR_ORIGIN, CONTENT_SECURITY_POLICY
from coverset.db.models import Repo, RepoAccess, User, UserSession
from coverset.web.auth import hash_session_id

AVATAR = f"{AVATAR_ORIGIN}/u/4242?v=4"


@pytest.fixture
def settings_overrides(admin_token: str) -> dict[str, Any]:
    return {"admin_token": admin_token}


async def seed(app: Quart) -> tuple[int, int]:
    async with app.extensions["db"].session() as session:
        first = Repo(forge="github", external_id="1001", full_name="acme/app", owner_id=7)
        second = Repo(forge="github", external_id="1002", full_name="acme/lib", owner_id=7)
        session.add_all([first, second])
        await session.commit()
        return first.id, second.id


async def sign_in(
    app: Quart,
    client: Client,
    login: str,
    *,
    role: str = "member",
    access: dict[int, str] | None = None,
    avatar_url: str | None = None,
) -> User:
    """A user with ``access`` and a server-side session whose id ``client`` carries."""
    now = app.extensions["settings"].now()
    raw = secrets.token_urlsafe(32)
    async with app.extensions["db"].session() as session:
        user = User(forge="github", external_id=f"id-{login}", login=login, role=role, avatar_url=avatar_url)
        session.add(user)
        await session.flush()
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


def header_nav(page: str) -> str:
    """The main navigation of a full page."""
    return page.split('<nav aria-label="Main">', 1)[1].split("</nav>", 1)[0]


def link_texts(nav: str) -> list[str]:
    return re.findall(r"<a [^>]*>([^<]*)</a>", nav)


async def test_outbox_and_setup_links_are_for_instance_admins_only(
    app: Quart, client: Client, admin_token: str
) -> None:
    first, _second = await seed(app)
    admin = {"Authorization": f"Bearer {admin_token}"}
    for url in ("/", f"/repos/{first}"):
        nav = header_nav(await (await client.get(url, headers=admin)).get_data(as_text=True))
        assert 'href="/forge/outbox"' in nav and 'href="/setup"' in nav, url
    await sign_in(app, client, "mona", access={first: "write"})
    for url in ("/", f"/repos/{first}"):
        page = await (await client.get(url)).get_data(as_text=True)
        nav = header_nav(page)
        assert "Outbox" not in nav and "Setup" not in nav and "/forge/outbox" not in nav, url
        assert_csp_clean(page)
    # the pages themselves still refuse a member
    assert (await client.get("/forge/outbox")).status_code == 403
    await sign_in(app, client, "root", role="admin")
    nav = header_nav(await (await client.get("/")).get_data(as_text=True))
    assert ">Outbox</a>" in nav and ">Setup</a>" in nav, "a GitHub user who is an instance admin"


async def test_a_repo_page_has_one_repo_settings_and_one_settings_link(
    app: Quart, client: Client, admin_token: str
) -> None:
    first, _second = await seed(app)
    admin = {"Authorization": f"Bearer {admin_token}"}
    for url in (f"/repos/{first}", f"/repos/{first}/settings", "/settings/instance"):
        texts = link_texts(header_nav(await (await client.get(url, headers=admin)).get_data(as_text=True)))
        assert texts.count("Repo settings") == 1, (url, texts)
        assert texts.count("Settings") == 1, (url, texts)
    page = await (await client.get(f"/repos/{first}/settings", headers=admin)).get_data(as_text=True)
    assert re.search(r'<a href="/repos/\d+/settings" aria-current="page">Repo settings</a>', page)
    # a member who may write the repository: their preferences and the repository's settings
    await sign_in(app, client, "mona", access={first: "write"})
    texts = link_texts(header_nav(await (await client.get(f"/repos/{first}")).get_data(as_text=True)))
    assert texts.count("Repo settings") == 1 and texts.count("Settings") <= 1, texts
    # read only: no repository settings link at all
    await sign_in(app, client, "octo", access={first: "read"})
    texts = link_texts(header_nav(await (await client.get(f"/repos/{first}")).get_data(as_text=True)))
    assert "Repo settings" not in texts and texts.count("Settings") <= 1, texts


async def test_the_economy_include_is_one_anchor_without_an_id(app: Quart, admin_token: str) -> None:
    first, _second = await seed(app)
    async with app.test_request_context(f"/repos/{first}"):
        html = await render_template("_nav_repo_economy.html", nav_repo=await _repo(app, first), endpoint="")
    assert html.count("<a ") == 1 and " id=" not in html
    assert f'href="/repos/{first}/economy"' in html
    client = app.test_client()
    page = await (
        await client.get(f"/repos/{first}", headers={"Authorization": f"Bearer {admin_token}"})
    ).get_data(as_text=True)
    span = re.search(r'<span data-go="e" class="go-include">(.*?)</span>', page, re.S)
    assert span is not None
    assert span.group(1).count("<a ") == 1 and " id=" not in span.group(1)


async def _repo(app: Quart, rid: int) -> Repo:
    async with app.extensions["db"].session() as session:
        found = await session.get(Repo, rid)
        assert found is not None
        return found


async def test_the_avatar_is_githubs_image_with_the_initials_as_fallback(app: Quart, client: Client) -> None:
    assert f"img-src 'self' data: {AVATAR_ORIGIN};" in CONTENT_SECURITY_POLICY
    first, _second = await seed(app)
    await sign_in(app, client, "mona", access={first: "read"}, avatar_url=AVATAR)
    response = await client.get("/account")
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    page = await response.get_data(as_text=True)
    assert_csp_clean(page)
    escaped = AVATAR.replace("&", "&amp;")
    header = page.split('<div class="account-nav">', 1)[1].split("</div>", 1)[0]
    assert f'<img class="account-avatar" src="{escaped}" alt=""' in header
    assert 'referrerpolicy="no-referrer"' in header and '<span class="account-avatar"' not in header
    assert f'<img class="account-avatar big" src="{escaped}"' in page

    # an avatar stored from anywhere else is never rendered (the CSP would block it anyway)
    await sign_in(app, client, "octo", access={first: "read"}, avatar_url="https://evil.example/a.png")
    page = await (await client.get("/account")).get_data(as_text=True)
    assert "evil.example" not in page and "<img" not in page
    assert '<span class="account-avatar" aria-hidden="true">O</span>' in page
    assert '<span class="account-avatar big" aria-hidden="true">O</span>' in page
    # no avatar at all: the initials badge
    await sign_in(app, client, "zed", access={first: "read"})
    page = await (await client.get("/account")).get_data(as_text=True)
    assert "<img" not in page and '<span class="account-avatar" aria-hidden="true">Z</span>' in page


async def test_the_avatar_follows_the_signed_in_user(app: Quart, client: Client) -> None:
    """A lookalike prefix (``avatars.githubusercontent.com.evil``) is not the origin."""
    first, _second = await seed(app)
    await sign_in(app, client, "mona", access={first: "read"}, avatar_url=f"{AVATAR_ORIGIN}.evil.example/u/1")
    page = await (await client.get("/account")).get_data(as_text=True)
    assert "<img" not in page
    async with app.extensions["db"].session() as session:
        assert await session.scalar(select(User.avatar_url).where(User.login == "mona"))
