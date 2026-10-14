"""The settings pages and API-token pages: who may see and change what, validation, reset, CSRF,
the raw token shown once, and the ``view_prefs`` template context.

These tests run in open mode (no admin token, loopback host) on an empty database rather than the
shared demo. The access guard makes every request the local operator; a ``before_request`` hook
registered after it swaps in the principal a test wants (a member with write access, a repository
admin, a read-only token), which is exactly what the guard would have stored for those callers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import pytest
from quart import Quart, render_template_string
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select
from web_helpers import HX, assert_csp_clean

from coverset import settings_scopes as ss
from coverset.db.models import ApiToken, AuditEvent, Repo, ScopedSetting, User
from coverset.web.principal import Principal, set_principal

SetPrincipal = Callable[[Principal | None], None]


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """An empty database in open mode (not the shared demo)."""
    return {}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ss.clear_cache()


@pytest.fixture
def act_as(app: Quart) -> SetPrincipal:
    """Replace the guard's principal with the given one for every following request."""
    holder: dict[str, Principal | None] = {"principal": None}

    @app.before_request
    async def _override() -> None:
        if holder["principal"] is not None:
            set_principal(holder["principal"])

    return lambda principal: holder.__setitem__("principal", principal)


@pytest.fixture
async def world(app: Quart) -> dict[str, int]:
    """Two repositories under ``acme``, one elsewhere, and two users."""
    async with app.extensions["db"].session() as session:
        repos = [
            Repo(forge="github", external_id=str(n), full_name=name, private=n != 3)
            for n, name in ((1, "acme/widgets"), (2, "acme/gadgets"), (3, "other/thing"))
        ]
        users = [
            User(forge="github", external_id="501", login="mona"),
            User(forge="github", external_id="502", login="hubot"),
        ]
        session.add_all([*repos, *users])
        await session.commit()
        return {
            "widgets": repos[0].id,
            "gadgets": repos[1].id,
            "thing": repos[2].id,
            "mona": users[0].id,
            "hubot": users[1].id,
        }


def member(world: dict[str, int], login: str = "mona", **permissions: str) -> Principal:
    """A signed-in member holding ``permissions`` (``widgets="write"``...)."""
    return Principal(
        kind="user",
        user_id=world[login],
        login=login,
        label=f"@{login}",
        repo_permissions={world[name]: level for name, level in permissions.items()},
    )


async def csrf(client: Client, url: str = "/account/settings") -> str:
    page = await (await client.get(url)).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None, page[:400]
    return match.group(1)


async def rows(app: Quart) -> dict[tuple[str, str, str], Any]:
    async with app.extensions["db"].session() as session:
        found = await session.scalars(select(ScopedSetting))
        return {(r.scope_kind, r.scope_id, r.key): r.value["v"] for r in found}


async def audit_actions(app: Quart) -> list[str]:
    async with app.extensions["db"].session() as session:
        return list(await session.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))


# -- the operator (open mode) --------------------------------------------------------------------


async def test_instance_page_for_the_operator(client: Client, app: Quart, world: dict[str, int]) -> None:
    for url in ("/settings", "/settings/instance"):
        response = await client.get(url)
        assert response.status_code == 200, url
    page = await response.get_data(as_text=True)
    assert_csp_clean(page)
    assert "<legend>View</legend>" in page and "<legend>Policy</legend>" in page
    for key in ss.CATALOGUE:
        assert f'name="{key}"' in page, key
    assert "inherited from COVERSET_UNTRUSTED_PR_POLICY: <strong>wait for approval</strong>" in page
    assert 'Organization <span class="mono">acme</span>' in page, "admins see every org in the sub-nav"
    assert re.search(r'<a href="/settings/instance"[^>]*>Settings</a>', page), "the admin nav link"

    token = await csrf(client, "/settings")
    saved = await client.post(
        "/settings/instance",
        form={"csrf_token": token, "policy.max_shards": "8", "view.theme": "dark", "policy.run_drafts": ""},
    )
    body = await saved.get_data(as_text=True)
    assert saved.status_code == 200 and "Saved." in body and 'role="status"' in body
    assert await rows(app) == {
        ("instance", "", "policy.max_shards"): 8,
        ("instance", "", "view.theme"): "dark",
    }
    assert await audit_actions(app) == ["setting_changed", "setting_changed"]
    assert re.search(r'<option value="dark" selected>', body)
    assert "Set here" in body


async def test_validation_errors_write_nothing(client: Client, app: Quart, world: dict[str, int]) -> None:
    token = await csrf(client, "/settings")
    response = await client.post(
        "/settings/instance", form={"csrf_token": token, "view.theme": "dark", "policy.max_shards": "500"}
    )
    body = await response.get_data(as_text=True)
    assert response.status_code == 422
    assert "policy.max_shards must be between 1 and 128" in body and 'aria-invalid="true"' in body
    assert 'value="500"' in body, "the bad input is kept for correction"
    assert await rows(app) == {}
    fragment = await client.post(
        "/settings/instance", headers=HX, form={"csrf_token": token, "policy.max_shards": "x"}
    )
    assert fragment.status_code == 200, "htmx only swaps 2xx, so errors come back as 200 fragments"
    assert "<html" not in await fragment.get_data(as_text=True)


async def test_writes_need_csrf(client: Client, app: Quart, world: dict[str, int]) -> None:
    refused = await client.post("/settings/instance", form={"policy.max_shards": "8"})
    assert refused.status_code == 403
    assert (await client.post("/settings/tokens", form={"name": "x"})).status_code == 403
    assert await rows(app) == {}


async def test_org_and_repo_pages_show_what_they_inherit(
    client: Client, app: Quart, world: dict[str, int]
) -> None:
    token = await csrf(client, "/settings")
    await client.post("/settings/instance", form={"csrf_token": token, "policy.untrusted_pr_policy": "skip"})
    org = await client.post(
        "/settings/org/ACME", form={"csrf_token": token, "policy.untrusted_pr_policy": "run_blocked_network"}
    )
    assert org.status_code == 200
    assert "inherited from instance: <strong>skip</strong>" in await org.get_data(as_text=True)
    page = await (await client.get(f"/repos/{world['widgets']}/settings")).get_data(as_text=True)
    assert_csp_clean(page)
    assert "inherited from organization acme: <strong>run with the network blocked</strong>" in page
    assert re.search(r'<a href="/repos/\d+/settings" aria-current="page">Settings</a>', page)
    assert (await client.get("/settings/org/not%2Fan-org")).status_code == 404
    assert await rows(app) == {
        ("instance", "", "policy.untrusted_pr_policy"): "skip",
        ("org", "acme", "policy.untrusted_pr_policy"): "run_blocked_network",
    }


async def test_reset_returns_a_key_to_inheriting(client: Client, app: Quart, world: dict[str, int]) -> None:
    url = f"/repos/{world['widgets']}/settings"
    token = await csrf(client, url)
    await client.post(url, form={"csrf_token": token, "policy.max_shards": "4", "view.highlight": "missing"})
    page = await (await client.get(url)).get_data(as_text=True)
    assert 'name="reset" value="policy.max_shards"' in page
    reset = await client.post(
        url, headers=HX, form={"csrf_token": token, "reset": "policy.max_shards", "policy.max_shards": "4"}
    )
    body = await reset.get_data(as_text=True)
    assert reset.status_code == 200 and "Most shards per run now inherits" in body
    assert await rows(app) == {("repo", str(world["widgets"]), "view.highlight"): "missing"}


# -- members, repository writers and admins ------------------------------------------------------


async def test_repo_writer_edits_only_what_write_allows(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    url = f"/repos/{world['widgets']}/settings"
    token = await csrf(client, url)
    act_as(member(world, widgets="write", gadgets="read"))
    page = await (await client.get(url)).get_data(as_text=True)
    assert re.search(r'<select id="set-policy-untrusted-pr-policy"[^>]* disabled>', page)
    assert "needs repository admin" in page
    assert re.search(r'<select id="set-policy-run-drafts"(?![^>]* disabled)[^>]*>', page)
    ok = await client.post(url, form={"csrf_token": token, "policy.run_drafts": "true"})
    assert ok.status_code == 200
    locked = await client.post(url, form={"csrf_token": token, "policy.untrusted_pr_policy": "skip"})
    assert locked.status_code == 422 and "needs repository admin" in await locked.get_data(as_text=True)
    assert re.search(r'<select id="set-policy-ai-review"[^>]* disabled>', page), "code leaves the machine"
    ai = await client.post(url, form={"csrf_token": token, "policy.ai_review": "true"})
    assert ai.status_code == 422 and "needs repository admin" in await ai.get_data(as_text=True)
    assert await rows(app) == {("repo", str(world["widgets"]), "policy.run_drafts"): True}
    assert (await client.get(f"/repos/{world['gadgets']}/settings")).status_code == 403, "read is not enough"
    assert (await client.get(f"/repos/{world['thing']}/settings")).status_code == 404, "invisible repos 404"
    assert (await client.get("/settings/org/acme")).status_code == 403
    assert (await client.post("/settings/instance", form={"csrf_token": token})).status_code == 403
    landing = await client.get("/settings")
    assert landing.status_code == 303 and landing.headers["Location"].endswith("/account/settings")
    nav = await (await client.get(url)).get_data(as_text=True)
    assert not re.search(r'<a href="/settings/instance"[^>]*>Settings</a>', nav), "no admin link"
    assert re.search(r'<a href="/account/settings"[^>]*>Settings</a>', nav), "members get their preferences"
    assert "acme/widgets" in nav and "acme/gadgets" not in nav.split('<main id="main">')[1].split("</nav>")[0]


async def test_repo_admin_may_change_the_admin_keys(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    url = f"/repos/{world['widgets']}/settings"
    token = await csrf(client, url)
    act_as(member(world, widgets="admin"))
    response = await client.post(url, form={"csrf_token": token, "policy.untrusted_pr_policy": "skip"})
    assert response.status_code == 200
    assert await rows(app) == {("repo", str(world["widgets"]), "policy.untrusted_pr_policy"): "skip"}
    async with app.extensions["db"].session() as session:
        event = await session.scalar(select(AuditEvent))
    assert event is not None and (event.actor_kind, event.actor_label, event.repo_id) == (
        "user",
        "@mona",
        world["widgets"],
    )


async def test_preferences_are_per_user_and_view_only(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    token = await csrf(client)
    operator = await (await client.get("/account/settings")).get_data(as_text=True)
    assert 'name="view.theme"' in operator and "policy." not in re.sub(
        r"<nav.*?</nav>", "", operator, flags=re.S
    )
    await client.post("/account/settings", form={"csrf_token": token, "view.theme": "light"})
    act_as(member(world))
    await client.post("/account/settings", form={"csrf_token": token, "view.highlight": "missing"})
    forged = await client.post("/account/settings", form={"csrf_token": token, "policy.run_drafts": "true"})
    assert forged.status_code == 200, "a policy field on the preferences form is ignored"
    assert await rows(app) == {
        ("user", "local", "view.theme"): "light",
        ("user", str(world["mona"]), "view.highlight"): "missing",
    }


async def test_read_only_tokens_cannot_write_and_no_token_manages_tokens(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    token = await csrf(client)
    reader = Principal(
        kind="token", user_id=world["mona"], login="mona", scopes=frozenset({"read"}), token_id=1
    )
    act_as(reader)
    assert (await client.get("/account/settings")).status_code == 200
    assert (
        await client.post("/account/settings", form={"csrf_token": token, "view.theme": "dark"})
    ).status_code == 403
    assert (await client.get("/settings/tokens")).status_code == 403
    writer = Principal(kind="token", user_id=world["mona"], scopes=frozenset({"write", "admin"}), token_id=2)
    act_as(writer)
    assert (await client.post("/settings/tokens", form={"csrf_token": token, "name": "x"})).status_code == 403
    repo_token = Principal(
        kind="token", bound_repo_id=world["widgets"], scopes=frozenset({"upload"}), token_id=3
    )
    act_as(repo_token)
    assert (await client.get("/account/settings")).status_code == 403
    assert await rows(app) == {}


# -- tokens ----------------------------------------------------------------------------------------


async def test_personal_token_is_shown_once_and_stored_hashed(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    token = await csrf(client)
    act_as(member(world, widgets="write"))
    created = await client.post(
        "/settings/tokens",
        data=urlencode(
            [
                ("csrf_token", token),
                ("name", "MCP on my laptop"),
                ("kind", "personal"),
                ("scopes", "read"),
                ("scopes", "write"),
                ("expiry", "30"),
            ]
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    body = await created.get_data(as_text=True)
    assert created.status_code == 201 and created.headers["Cache-Control"] == "no-store"
    assert_csp_clean(body)
    raw = re.search(r'id="tok-raw"[^>]*value="(cst_[^"]+)"', body)
    assert raw is not None
    async with app.extensions["db"].session() as session:
        stored = await session.scalar(select(ApiToken))
    assert stored is not None
    assert stored.token_hash == hashlib.sha256(raw.group(1).encode()).hexdigest()
    assert raw.group(1) not in (stored.token_hash, stored.prefix, stored.name)
    assert (stored.kind, stored.user_id, stored.repo_id, stored.scopes) == (
        "personal",
        world["mona"],
        None,
        ["read", "write"],
    )
    assert stored.expires_at is not None
    again = await (await client.get("/settings/tokens")).get_data(as_text=True)
    assert raw.group(1) not in again and f"cst_{stored.prefix}…" in again and "MCP on my laptop" in again
    actions = await audit_actions(app)
    async with app.extensions["db"].session() as session:
        details = (await session.scalar(select(AuditEvent))).details  # type: ignore[union-attr]
    assert actions == ["token_created"] and raw.group(1) not in str(details)


async def test_operator_creates_repository_tokens_without_a_user(
    client: Client, app: Quart, world: dict[str, int]
) -> None:
    token = await csrf(client, "/settings/tokens")
    personal = await client.post(
        "/settings/tokens", form={"csrf_token": token, "name": "mine", "kind": "personal", "scopes": "read"}
    )
    assert personal.status_code == 422 and "signed-in user" in await personal.get_data(as_text=True)
    admin_scope = await client.post(
        "/settings/tokens",
        form={
            "csrf_token": token,
            "name": "ci",
            "kind": "repo",
            "repo": str(world["widgets"]),
            "scopes": "admin",
        },
    )
    assert admin_scope.status_code == 422
    ok = await client.post(
        "/settings/tokens",
        form={
            "csrf_token": token,
            "name": "CI upload",
            "kind": "repo",
            "repo": str(world["widgets"]),
            "scopes": "upload",
            "expiry": "never",
        },
    )
    assert ok.status_code == 201
    async with app.extensions["db"].session() as session:
        stored = await session.scalar(select(ApiToken))
    assert stored is not None and (stored.kind, stored.user_id, stored.repo_id, stored.expires_at) == (
        "repo",
        None,
        world["widgets"],
        None,
    )


async def test_revoke_and_foreign_tokens(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    token = await csrf(client)
    act_as(member(world, "hubot"))
    await client.post(
        "/settings/tokens",
        form={"csrf_token": token, "name": "hubot-token", "kind": "personal", "scopes": "read"},
    )
    act_as(member(world, widgets="write"))
    await client.post(
        "/settings/tokens", form={"csrf_token": token, "name": "mine", "kind": "personal", "scopes": "read"}
    )
    async with app.extensions["db"].session() as session:
        ids = {t.name: t.id for t in await session.scalars(select(ApiToken))}
    assert (
        await client.post(f"/settings/tokens/{ids['hubot-token']}/revoke", form={"csrf_token": token})
    ).status_code == 404
    revoked = await client.post(
        f"/settings/tokens/{ids['mine']}/revoke", headers=HX, form={"csrf_token": token}
    )
    assert revoked.status_code == 200 and "Revoked" in await revoked.get_data(as_text=True)
    async with app.extensions["db"].session() as session:
        states = {t.name: t.revoked_at is not None for t in await session.scalars(select(ApiToken))}
    assert states == {"hubot-token": False, "mine": True}
    assert (await audit_actions(app))[-1] == "token_revoked"
    assert (
        await client.post("/settings/tokens/99999999/revoke", form={"csrf_token": token})
    ).status_code == 404


# -- the view_prefs context ----------------------------------------------------------------------


async def test_view_prefs_reach_templates_once_per_request(
    app: Quart, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.web import settings_pages

    async with app.extensions["db"].session() as session:
        await ss.set_value(session, "view.map_module_weight", "hidden", scope="org", scope_id="acme")
        await ss.set_value(session, "view.theme", "dark", scope="user", scope_id=world["mona"])
    calls: list[int] = []
    real = settings_pages.view_prefs

    async def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(settings_pages, "view_prefs", counting)
    async with app.test_request_context(f"/repos/{world['widgets']}/settings"):
        set_principal(member(world, widgets="read"))
        from quart import request

        request.view_args = {"rid": world["widgets"]}
        first = await render_template_string(
            "{{ view_prefs.theme }}|{{ view_prefs['view.map_module_weight'] }}"
        )
        second = await render_template_string("{{ view_prefs.highlight }}")
    assert (first, second) == ("dark|hidden", "all")
    assert len(calls) == 1, "resolved once per request, then cached on g"


async def test_an_invisible_repository_404s_like_a_missing_one(
    client: Client, app: Quart, world: dict[str, int], act_as: SetPrincipal
) -> None:
    """View preferences of a repository the reader cannot see must not leak into its 404 page:
    otherwise the layout's theme/highlight attributes tell a hidden repository from a missing id."""
    async with app.extensions["db"].session() as session:
        await ss.set_value(session, "view.theme", "dark", scope="org", scope_id="acme")
        await ss.set_value(session, "view.highlight", "missing", scope="repo", scope_id=world["widgets"])
    act_as(member(world, thing="read"))
    missing_id = world["thing"] + 100
    hidden = await client.get(f"/repos/{world['widgets']}")
    missing = await client.get(f"/repos/{missing_id}")
    assert hidden.status_code == missing.status_code == 404

    def neutral(body: str, rid: int) -> str:  # the only difference allowed: the id the caller sent
        return re.sub(rf"(id |/repos/){rid}\b", r"\1N", body)

    hidden_body = neutral(await hidden.get_data(as_text=True), world["widgets"])
    missing_body = neutral(await missing.get_data(as_text=True), missing_id)
    assert 'data-theme="dark"' not in hidden_body and 'data-highlight="missing"' not in hidden_body
    assert hidden_body == missing_body
    act_as(member(world, widgets="read"))
    seen = await client.get(f"/repos/{world['widgets']}")
    assert seen.status_code == 200 and 'data-theme="dark"' in await seen.get_data(as_text=True)
