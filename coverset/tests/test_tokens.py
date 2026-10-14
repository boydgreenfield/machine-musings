"""API tokens: generation and verification, the token principal in the access guard, and the
``coverset users`` CLI that mints, lists and revokes them."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select

from coverset.cli import cli
from coverset.config import Settings
from coverset.db.models import ApiToken, AuditEvent, Repo, RepoAccess, Run, TestCase, User, UserSession
from coverset.db.session import Database, init_db
from coverset.web import tokens
from coverset.web.api import SlidingWindowLimiter
from coverset.web.auth import (
    LOGIN_ATTEMPTS_PER_MINUTE,
    LOGIN_LIMITER_EXTENSION,
    REJECTION_LOG_EXTENSION,
    RejectionLog,
    hash_session_id,
)

pytestmark = pytest.mark.security

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings_overrides(admin_token: str) -> dict[str, Any]:
    return {"admin_token": admin_token}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- the token format ------------------------------------------------------------------------------


def test_generate_and_pattern() -> None:
    raw, prefix, digest = tokens.generate()
    assert raw.startswith(f"cst_{prefix}_") and len(prefix) == 8
    assert tokens.looks_like_token(raw)
    assert digest == tokens.hash_token(raw) and len(digest) == 64
    assert not tokens.looks_like_token("ghu_" + "x" * 40)
    assert not tokens.looks_like_token("cst_short_x")
    another, _p, other_digest = tokens.generate()
    assert another != raw and other_digest != digest


def test_check_scopes() -> None:
    assert tokens.check_scopes(["write", " read", "read", ""]) == ["read", "write"]
    with pytest.raises(ValueError, match="unknown token scope"):
        tokens.check_scopes(["read", "root"])


async def test_verify_roundtrip_revoked_expired(app: Quart) -> None:
    db: Database = app.extensions["db"]
    async with db.session() as session:
        repo = Repo(forge="github", external_id="1", full_name="acme/app")
        session.add(repo)
        await session.flush()
        raw, row = await tokens.create(
            session, name="ci", kind="repo", scopes=["upload"], now=NOW, repo_id=repo.id
        )
        expiring, _ = await tokens.create(
            session,
            name="old",
            kind="repo",
            scopes=["read"],
            now=NOW,
            repo_id=repo.id,
            expires_at=NOW + timedelta(hours=1),
        )
        await session.commit()
        assert raw not in (row.token_hash, row.prefix)
        found = await tokens.verify(session, raw, now=NOW)
        assert found is not None and found.id == row.id
        # same prefix, wrong secret: no row (compared in constant time)
        assert await tokens.verify(session, raw[:-4] + "zzzz", now=NOW) is None
        assert await tokens.verify(session, "not a token", now=NOW) is None
        assert await tokens.verify(session, expiring, now=NOW) is not None
        assert await tokens.verify(session, expiring, now=NOW + timedelta(hours=2)) is None
        row.revoked_at = NOW
        await session.commit()
        assert await tokens.verify(session, raw, now=NOW) is None
        with pytest.raises(ValueError, match="needs a repository"):
            await tokens.create(session, name="x", kind="repo", scopes=["read"], now=NOW)
        with pytest.raises(ValueError, match="needs a user"):
            await tokens.create(session, name="x", kind="personal", scopes=["read"], now=NOW)
        with pytest.raises(ValueError, match="at least one scope"):
            await tokens.create(session, name="x", kind="repo", scopes=[], now=NOW, repo_id=repo.id)


# -- the token principal ---------------------------------------------------------------------------


async def seed(app: Quart) -> tuple[Repo, Repo, TestCase, User]:
    """Two repositories, one test in the first, and a user who may write to the first."""
    async with app.extensions["db"].session() as session:
        first = Repo(forge="github", external_id="1001", full_name="acme/app", owner_id=7, installation_id=55)
        second = Repo(
            forge="github", external_id="1002", full_name="acme/lib", owner_id=7, installation_id=55
        )
        session.add_all([first, second])
        await session.flush()
        case = TestCase(
            repo_id=first.id, nodeid="tests/test_a.py::test_one", nodeid_hash=b"\0" * 16, family="a"
        )
        user = User(forge="github", external_id="4242", login="alice")
        session.add_all([case, user])
        await session.flush()
        session.add(RepoAccess(user_id=user.id, repo_id=first.id, permission="write"))
        await session.commit()
        return first, second, case, user


async def mint(app: Quart, **fields: Any) -> tuple[str, int]:
    async with app.extensions["db"].session() as session:
        raw, row = await tokens.create(session, now=app.extensions["settings"].now(), **fields)
        await session.commit()
        return raw, row.id


async def test_repo_token_acts_on_its_repository_only(app: Quart, client: Client) -> None:
    first, second, case, _user = await seed(app)
    raw, _id = await mint(app, name="ci", kind="repo", scopes=["upload"], repo_id=first.id)
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(raw))).status_code == 200
    assert (await client.get(f"/repos/{second.id}/tests", headers=bearer(raw))).status_code == 404
    assert (await client.get("/forge/outbox", headers=bearer(raw))).status_code == 403
    dashboard = await (await client.get("/", headers=bearer(raw))).get_data(as_text=True)
    assert "acme/app" in dashboard and "acme/lib" not in dashboard
    # upload implies write on the bound repository, and a bearer needs no CSRF token
    url = f"/repos/{first.id}/tests/{case.id}/quarantine"
    on = await client.post(url, headers={**bearer(raw), "HX-Request": "true"}, form={"quarantined": "1"})
    assert on.status_code == 200
    async with app.extensions["db"].session() as session:
        stored = await session.get(TestCase, case.id)
        assert stored is not None and stored.quarantined
        audit = list(await session.scalars(select(AuditEvent).where(AuditEvent.action == "quarantine")))
        assert (
            audit
            and audit[-1].actor_kind == "api_token"
            and audit[-1].actor_label.startswith("repo token ci")
        )


async def test_personal_token_is_limited_by_its_scopes(app: Quart, client: Client) -> None:
    first, second, case, user = await seed(app)
    read_only, _ = await mint(app, name="laptop", kind="personal", scopes=["read"], user_id=user.id)
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(read_only))).status_code == 200
    assert (await client.get(f"/repos/{second.id}/tests", headers=bearer(read_only))).status_code == 404
    url = f"/repos/{first.id}/tests/{case.id}/quarantine"
    assert (await client.post(url, headers=bearer(read_only), form={"quarantined": "1"})).status_code == 403
    writing, _ = await mint(app, name="agent", kind="personal", scopes=["read", "write"], user_id=user.id)
    assert (await client.post(url, headers=bearer(writing), form={"quarantined": "1"})).status_code == 303
    page = await (await client.get("/account", headers=bearer(writing))).get_data(as_text=True)
    assert "@alice" in page and "with an API token" in page and "<code>write</code>" in page


async def test_admin_scope_needs_an_admin_user(app: Quart, client: Client) -> None:
    _first, _second, _case, user = await seed(app)
    admin_scope, _ = await mint(app, name="ops", kind="personal", scopes=["admin"], user_id=user.id)
    assert (await client.get("/forge/outbox", headers=bearer(admin_scope))).status_code == 403, "a member"
    async with app.extensions["db"].session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.role = "admin"
        await session.commit()
    assert (await client.get("/forge/outbox", headers=bearer(admin_scope))).status_code == 200
    read_scope, _ = await mint(app, name="ro", kind="personal", scopes=["read"], user_id=user.id)
    assert (await client.get("/forge/outbox", headers=bearer(read_scope))).status_code == 403, (
        "scope caps the role"
    )


async def test_rejected_tokens_are_401_audited_and_never_fall_back(
    app: Quart, client: Client, admin_token: str
) -> None:
    first, _second, _case, user = await seed(app)
    raw, token_id = await mint(app, name="ci", kind="repo", scopes=["upload"], repo_id=first.id)
    login = await client.post("/login", json={"token": admin_token})
    assert login.status_code == 200
    assert (await client.get("/")).status_code == 200, "the admin cookie works"
    forged = raw[:-6] + "abcdef"
    response = await client.get("/", headers=bearer(forged))
    assert response.status_code == 401, "a bad cst_ bearer never falls back to the cookie"
    async with app.extensions["db"].session() as session:
        row = await session.get(ApiToken, token_id)
        assert row is not None
        row.revoked_at = app.extensions["settings"].now()
        await session.commit()
    assert (await client.get("/", headers=bearer(raw))).status_code == 401
    async with app.extensions["db"].session() as session:
        rows = list(await session.scalars(select(AuditEvent).where(AuditEvent.action == "token_rejected")))
    assert len(rows) == 1, "the second rejection shares the prefix within a minute: not a new row"
    (event,) = rows
    assert event.actor_kind == "system"
    assert event.details == {"prefix": raw[4:12], "path": "/", "suppressed": 0}
    assert raw not in str(event.details) and forged not in str(event.details)
    # successes are not audited
    async with app.extensions["db"].session() as session:
        assert not list(await session.scalars(select(AuditEvent).where(AuditEvent.action == "token_used")))
    # a disabled user's personal token stops working at once
    personal, _ = await mint(app, name="p", kind="personal", scopes=["read"], user_id=user.id)
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(personal))).status_code == 200
    async with app.extensions["db"].session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.disabled = True
        await session.commit()
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(personal))).status_code == 401


async def test_last_used_is_written_at_most_once_a_minute(app: Quart, client: Client) -> None:
    first, _second, _case, _user = await seed(app)
    raw, token_id = await mint(app, name="ci", kind="repo", scopes=["read"], repo_id=first.id)
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(raw))).status_code == 200
    async with app.extensions["db"].session() as session:
        row = await session.get(ApiToken, token_id)
        assert row is not None and row.last_used_at is not None
        first_use = row.last_used_at
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(raw))).status_code == 200
    async with app.extensions["db"].session() as session:
        row = await session.get(ApiToken, token_id)
        assert row is not None and row.last_used_at == first_use, "no write within a minute"
        row.last_used_at = first_use - timedelta(minutes=2)
        await session.commit()
    assert (await client.get(f"/repos/{first.id}/tests", headers=bearer(raw))).status_code == 200
    async with app.extensions["db"].session() as session:
        row = await session.get(ApiToken, token_id)
        assert row is not None and row.last_used_at is not None and row.last_used_at >= first_use


def bogus_token(index: int) -> str:
    """A well-formed ``cst_`` token that matches no row; ``index`` picks one of five prefixes."""
    return f"cst_bogus00{index % 5}_{'b' * 40}"


async def rejected_rows(app: Quart) -> list[AuditEvent]:
    async with app.extensions["db"].session() as session:
        query = select(AuditEvent).where(AuditEvent.action == "token_rejected").order_by(AuditEvent.id)
        return list(await session.scalars(query))


async def test_rejected_bearers_are_throttled_and_audited_once_a_minute(app: Quart, client: Client) -> None:
    """100 bad bearers from one address: 401 within the login budget, 429 beyond it, and at most
    one audit row per token prefix a minute (the next row carries the suppressed count)."""
    await seed(app)
    clock = [1000.0]
    app.extensions[REJECTION_LOG_EXTENSION] = RejectionLog(clock=lambda: clock[0])
    statuses = [(await client.get("/", headers=bearer(bogus_token(i)))).status_code for i in range(100)]
    assert statuses[:LOGIN_ATTEMPTS_PER_MINUTE] == [401] * LOGIN_ATTEMPTS_PER_MINUTE
    assert set(statuses[LOGIN_ATTEMPTS_PER_MINUTE:]) == {429}
    limited = await client.get("/", headers=bearer(bogus_token(0)))
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert (await client.get("/healthz", headers=bearer(bogus_token(1)))).status_code == 429, (
        "a public route throttles a bad bearer too"
    )
    rows = await rejected_rows(app)
    assert len(rows) == 5 <= LOGIN_ATTEMPTS_PER_MINUTE, "one row per (address, prefix), not per request"
    assert all(row.details["suppressed"] == 0 for row in rows)
    # A minute later (and with a fresh login budget) the next row says how many repeats were dropped.
    clock[0] += 61.0
    app.extensions[LOGIN_LIMITER_EXTENSION] = SlidingWindowLimiter(LOGIN_ATTEMPTS_PER_MINUTE)
    assert (await client.get("/", headers=bearer(bogus_token(0)))).status_code == 401
    rows = await rejected_rows(app)
    assert len(rows) == 6
    assert rows[-1].details == {"prefix": "bogus000", "path": "/", "suppressed": 3}
    # Valid traffic is never charged: a real token works regardless of the failed attempts.
    async with app.extensions["db"].session() as session:
        repo = await session.scalar(select(Repo).order_by(Repo.id))
        assert repo is not None
    good, _id = await mint(app, name="ok", kind="repo", scopes=["read"], repo_id=repo.id)
    assert (await client.get(f"/repos/{repo.id}/tests", headers=bearer(good))).status_code == 200


async def test_static_assets_resolve_no_credentials(app: Quart, client: Client) -> None:
    """``/static/*`` never looks at the bearer or the cookie: no audit row, no limiter charge."""
    await seed(app)
    for i in range(LOGIN_ATTEMPTS_PER_MINUTE + 5):
        response = await client.get("/static/app.css", headers=bearer(bogus_token(i)))
        assert response.status_code == 200
    assert await rejected_rows(app) == []
    assert (await client.get("/", headers=bearer(bogus_token(0)))).status_code == 401, "budget untouched"


async def test_tokens_cannot_end_or_refresh_browser_sessions(app: Quart, client: Client) -> None:
    """Sessions belong to browsers: a personal token, whatever its scopes, cannot sign its owner
    out, revoke a session or trigger an access refresh."""
    _first, _second, _case, user = await seed(app)
    settings = app.extensions["settings"]
    now = settings.now()
    session_id = hash_session_id("raw-browser-session-id")
    async with app.extensions["db"].session() as session:
        session.add(
            UserSession(
                id=session_id,
                user_id=user.id,
                csrf="unused",
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        await session.commit()
    for scopes in (["read"], ["read", "write"], ["admin"]):
        raw, _id = await mint(app, name="t", kind="personal", scopes=scopes, user_id=user.id)
        assert (await client.post("/logout", headers=bearer(raw))).status_code == 403
        revoke = await client.post(f"/account/sessions/{session_id}/revoke", headers=bearer(raw))
        assert revoke.status_code == 403
        assert (await client.post("/account/refresh", headers=bearer(raw))).status_code == 403
    async with app.extensions["db"].session() as session:
        assert await session.get(UserSession, session_id) is not None, "the session survived"
        actions = {
            event.action
            for event in await session.scalars(
                select(AuditEvent).where(AuditEvent.action.in_(["logout", "session_revoked"]))
            )
        }
    assert actions == set()


async def test_tokens_read_the_v1_api_for_their_repository(app: Quart, client: Client) -> None:
    """The public ``/v1`` routes answer a ``cst_`` token for the repository it may read with the
    full view; a token bound elsewhere is a 404 there and a rejected one a 401."""
    first, second, _case, user = await seed(app)
    sha = "a" * 40
    async with app.extensions["db"].session() as session:
        run = Run(
            repo_id=first.id, head_sha=sha, trigger="pull_request", status="completed", verdict="success"
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    verdict = f"/v1/verdict?repo_id={first.external_id}&sha={sha}"
    repo_token, _ = await mint(app, name="ci", kind="repo", scopes=["read"], repo_id=first.id)
    response = await client.get(f"/v1/runs/{run_id}", headers=bearer(repo_token))
    assert response.status_code == 200 and (await response.get_json())["conclusion"] == "success"
    response = await client.get(verdict, headers=bearer(repo_token))
    assert response.status_code == 200 and "summary" in await response.get_json(), "the full view"
    personal, _ = await mint(app, name="me", kind="personal", scopes=["read"], user_id=user.id)
    assert (await client.get(f"/v1/runs/{run_id}", headers=bearer(personal))).status_code == 200
    elsewhere, _ = await mint(app, name="other", kind="repo", scopes=["read"], repo_id=second.id)
    response = await client.get(f"/v1/runs/{run_id}", headers=bearer(elsewhere))
    assert response.status_code == 404 and await response.get_json() == {"error": "not_found"}
    assert (await client.get(verdict, headers=bearer(elsewhere))).status_code == 404
    response = await client.get(verdict, headers=bearer(bogus_token(0)))
    assert response.status_code == 401 and await response.get_json() == {"error": "invalid_token"}


# -- the CLI ------------------------------------------------------------------------------------


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    for name in ("COVERSET_DATABASE_URL", "COVERSET_ADMINS", "COVERSET_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    from coverset.config import get_settings

    get_settings.cache_clear()
    yield CliRunner()  # type: ignore[misc]
    get_settings.cache_clear()


async def seed_cli_db(tmp_path: Path) -> Settings:
    settings = Settings.for_tests(
        data_dir=tmp_path / "data", database_url=f"sqlite+aiosqlite:///{tmp_path / 'data' / 'coverset.db'}"
    )
    await init_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            repo = Repo(forge="github", external_id="1001", full_name="acme/app", owner_id=7)
            alice = User(forge="github", external_id="1", login="alice", last_login_at=NOW)
            bob = User(forge="github", external_id="2", login="bob", role="admin")
            session.add_all([repo, alice, bob])
            await session.commit()
    finally:
        await db.engine.dispose()
    return settings


def test_cli_users_and_tokens(runner: CliRunner, tmp_path: Path) -> None:
    """Synchronous on purpose: the commands call ``asyncio.run`` themselves."""
    settings = asyncio.run(seed_cli_db(tmp_path))
    assert "users" in cli.commands and not cli.commands["users"].hidden
    assert "Accounts, instance roles" in runner.invoke(cli, ["users", "--help"]).output

    listed = runner.invoke(cli, ["users", "list"])
    assert listed.exit_code == 0, listed.output
    assert (
        "@alice" in listed.output
        and "member" in listed.output
        and "@bob" in listed.output
        and "admin" in listed.output
    )

    promoted = runner.invoke(cli, ["users", "promote", "Alice"])
    assert promoted.exit_code == 0 and "member -> admin" in promoted.output
    assert "already has role admin" in runner.invoke(cli, ["users", "promote", "alice"]).output
    assert "admin -> member" in runner.invoke(cli, ["users", "demote", "alice"]).output
    assert runner.invoke(cli, ["users", "promote", "nobody"]).exit_code != 0

    created = runner.invoke(
        cli,
        [
            "users",
            "tokens",
            "create",
            "--name",
            "ci upload",
            "--repo",
            "acme/app",
            "--scopes",
            "upload",
            "--expires-days",
            "30",
        ],
    )
    assert created.exit_code == 0, created.output
    raw = re.search(r"^(cst_[A-Za-z0-9]{8}_[A-Za-z0-9_-]+)$", created.output, re.M).group(1)  # type: ignore[union-attr]
    assert "only time the token is shown" in created.output and "expires" in created.output
    personal = runner.invoke(
        cli, ["users", "tokens", "create", "--name", "laptop", "--user", "alice", "--scopes", "read,write"]
    )
    assert personal.exit_code == 0, personal.output
    assert runner.invoke(cli, ["users", "tokens", "create", "--name", "x"]).exit_code != 0, (
        "needs --repo or --user"
    )
    assert (
        runner.invoke(
            cli, ["users", "tokens", "create", "--name", "x", "--user", "alice", "--scopes", "root"]
        ).exit_code
        != 0
    )

    listed = runner.invoke(cli, ["users", "tokens", "list"])
    assert listed.exit_code == 0, listed.output
    assert (
        "ci upload" in listed.output
        and "acme/app" in listed.output
        and "@alice" in listed.output
        and "live" in listed.output
    )
    assert raw not in listed.output

    async def stored_token() -> int:
        db = Database.from_url(settings.database_url)
        try:
            async with db.session() as session:
                row = await tokens.verify(session, raw, now=NOW)
                assert row is not None and row.kind == "repo" and row.scopes == ["upload"]
                assert row.expires_at is not None
                return row.id
        finally:
            await db.engine.dispose()

    token_id = asyncio.run(stored_token())

    revoked = runner.invoke(cli, ["users", "tokens", "revoke", str(token_id)])
    assert revoked.exit_code == 0 and "revoked" in revoked.output
    assert "already revoked" in runner.invoke(cli, ["users", "tokens", "revoke", str(token_id)]).output
    assert "ci upload" not in runner.invoke(cli, ["users", "tokens", "list"]).output
    assert "revoked" in runner.invoke(cli, ["users", "tokens", "list", "--all"]).output

    disabled = runner.invoke(cli, ["users", "disable", "alice"])
    assert disabled.exit_code == 0 and "disabled" in disabled.output
    assert "disabled" in runner.invoke(cli, ["users", "list"]).output
    assert runner.invoke(cli, ["users", "tokens", "create", "--name", "x", "--user", "alice"]).exit_code != 0
    assert "enabled" in runner.invoke(cli, ["users", "enable", "alice"]).output

    async def audit() -> tuple[list[str], set[str]]:
        db = Database.from_url(settings.database_url)
        try:
            async with db.session() as session:
                events = list(await session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
        finally:
            await db.engine.dispose()
        return [e.action for e in events], {e.actor_label for e in events}

    actions, labels = asyncio.run(audit())
    assert actions == [
        "role_changed",
        "role_changed",
        "token_created",
        "token_created",
        "token_revoked",
        "user_disabled",
        "user_enabled",
    ]
    assert labels == {"cli"}
