"""``coverset users ...``: accounts, instance roles and API tokens from the server's shell.

Users appear when they sign in with GitHub; this group changes what they may do afterwards.
``promote`` and ``demote`` set the instance role (``admin`` sees and changes every repository);
``disable`` refuses the user's sign-ins and rejects their existing sessions and tokens at once,
``enable`` undoes it. ``COVERSET_ADMINS``, when set, decides the role again at each sign-in, so a
promotion of a login that is not listed there lasts until that user next signs in (the command
says so). ``tokens create`` prints the raw token exactly once; only its sha256 is stored. A
repository token with the ``upload`` scope is what a CI job uses to post bundles.

Every change is written to the audit log with the ``cli`` actor.
"""

from __future__ import annotations

from datetime import timedelta

import click
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings
from coverset.db.models import ApiToken, Repo, User, UserSession
from coverset.db.session import Database
from coverset.web.tokens import SCOPES, check_scopes

__all__ = ["users_group"]


@click.group(name="users")
def users_group() -> None:
    """Accounts, instance roles and API tokens.

    People sign in with GitHub (see docs/auth.md); these commands list them, make them instance
    admins, disable them, and mint API tokens for people and for CI jobs.
    """


def _db(settings: Settings) -> Database:
    return Database.from_url(settings.database_url)


async def _user(session: AsyncSession, login: str) -> User:
    """A user by login (case-insensitive like GitHub) or a clean error."""
    found = list(await session.scalars(select(User).where(User.login.ilike(login))))
    if not found:
        raise click.ClickException(f"no user with login {login!r} has signed in yet")
    if len(found) > 1:  # the same login under two forges: pick the exact spelling if unique
        exact = [u for u in found if u.login == login]
        if len(exact) != 1:
            raise click.ClickException(f"login {login!r} is ambiguous; use the GitHub id instead")
        return exact[0]
    return found[0]


def _audit(session: AsyncSession, action: str, **fields: object) -> None:
    from coverset.web.audit import record_audit

    details = {k: v for k, v in fields.items() if k not in ("target_kind", "target_id", "repo_id")}
    record_audit(
        session,
        None,
        action,
        target_kind=str(fields.get("target_kind")) if fields.get("target_kind") else None,
        target_id=fields.get("target_id"),  # type: ignore[arg-type]
        repo_id=fields.get("repo_id"),  # type: ignore[arg-type]
        details=details,
    )


@users_group.command("list")
def users_list() -> None:
    """List every user who has signed in, with role, state and last sign-in."""
    settings = load()

    async def run() -> list[str]:
        await prepare_db(settings)
        db = _db(settings)
        try:
            async with db.session() as session:
                users = list(await session.scalars(select(User).order_by(User.login)))
                sessions = dict(
                    (
                        await session.execute(
                            select(UserSession.user_id, UserSession.id).where(
                                UserSession.expires_at > settings.now()
                            )
                        )
                    ).all()
                )
        finally:
            await db.engine.dispose()
        if not users:
            return ["no users yet (people appear here after they sign in with GitHub)"]
        lines = [f"{'login':<24} {'role':<8} {'state':<9} {'last sign-in':<17} id"]
        for user in users:
            state = "disabled" if user.disabled else "active"
            last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "never"
            lines.append(
                f"{'@' + user.login:<24} {user.role:<8} {state:<9} {last:<17} {user.forge}:{user.external_id}"
            )
        active = sum(1 for u in users if u.id in sessions)
        lines.append(f"{len(users)} user(s), {active} with a live session")
        return lines

    for line in run_async(run):
        click.echo(line)


def _set_role(settings: Settings, login: str, role: str) -> str:
    async def run() -> str:
        await prepare_db(settings)
        db = _db(settings)
        try:
            async with db.session() as session:
                user = await _user(session, login)
                before = user.role
                if before != role:
                    user.role = role  # type: ignore[assignment]
                    _audit(
                        session,
                        "role_changed",
                        target_kind="user",
                        target_id=user.id,
                        login=user.login,
                        role=role,
                    )
                    await session.commit()
        finally:
            await db.engine.dispose()
        note = ""
        listed = {a.lower() for a in settings.admins}
        if settings.admins and (user.login.lower() in listed) != (role == "admin"):
            note = (
                f" (note: COVERSET_ADMINS decides the role again at the next sign-in; "
                f"{'add' if role == 'admin' else 'remove'} {user.login} there to make this stick)"
            )
        if before == role:
            return f"@{user.login} already has role {role}{note}"
        return f"@{user.login}: {before} -> {role}{note}"

    return run_async(run)


@users_group.command("promote")
@click.argument("login")
def users_promote(login: str) -> None:
    """Make LOGIN an instance admin (sees and changes every repository)."""
    click.echo(_set_role(load(), login, "admin"))


@users_group.command("demote")
@click.argument("login")
def users_demote(login: str) -> None:
    """Make LOGIN a member again (only the repositories GitHub grants them)."""
    click.echo(_set_role(load(), login, "member"))


def _set_disabled(settings: Settings, login: str, disabled: bool) -> str:
    async def run() -> str:
        await prepare_db(settings)
        db = _db(settings)
        try:
            async with db.session() as session:
                user = await _user(session, login)
                user.disabled = disabled
                dropped = 0
                if disabled:
                    for row in await session.scalars(
                        select(UserSession).where(UserSession.user_id == user.id)
                    ):
                        await session.delete(row)
                        dropped += 1
                _audit(
                    session,
                    "user_disabled" if disabled else "user_enabled",
                    target_kind="user",
                    target_id=user.id,
                    login=user.login,
                    sessions_dropped=dropped,
                )
                await session.commit()
        finally:
            await db.engine.dispose()
        if disabled:
            return f"@{user.login} disabled: sign-ins refused, {dropped} session(s) dropped, tokens rejected"
        return f"@{user.login} enabled"

    return run_async(run)


@users_group.command("disable")
@click.argument("login")
def users_disable(login: str) -> None:
    """Refuse LOGIN's sign-ins and reject their sessions and tokens."""
    click.echo(_set_disabled(load(), login, True))


@users_group.command("enable")
@click.argument("login")
def users_enable(login: str) -> None:
    """Let LOGIN sign in again."""
    click.echo(_set_disabled(load(), login, False))


@users_group.group("tokens")
def tokens_group() -> None:
    """API tokens: personal (act as a user) or repository (a CI job's upload credential)."""


@tokens_group.command("create")
@click.option("--name", required=True, help="What the token is for (shown in lists and the audit log).")
@click.option("--repo", "repo_spec", default=None, help="OWNER/NAME or id: a repository token bound to it.")
@click.option("--user", "login", default=None, help="LOGIN: a personal token that acts as this user.")
@click.option(
    "--scopes", default="read", show_default=True, help=f"Comma-separated: {', '.join(sorted(SCOPES))}."
)
@click.option(
    "--expires-days", type=click.IntRange(1, 3650), default=None, help="Expire after N days (default: never)."
)
def tokens_create(
    name: str, repo_spec: str | None, login: str | None, scopes: str, expires_days: int | None
) -> None:
    """Mint a token and print it once. Give --repo for a repository token or --user for a personal one."""
    from coverset.web import tokens as token_lib

    if (repo_spec is None) == (login is None):
        raise click.UsageError("pass exactly one of --repo OWNER/NAME or --user LOGIN")
    try:
        scope_list = check_scopes(scopes.split(","))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    settings = load()

    async def run() -> str:
        await prepare_db(settings)
        db = _db(settings)
        now = settings.now()
        expires = now + timedelta(days=expires_days) if expires_days else None
        try:
            async with db.session() as session:
                if repo_spec is not None:
                    repo = await find_repo(session, repo_spec)
                    raw, row = await token_lib.create(
                        session,
                        name=name,
                        kind="repo",
                        scopes=scope_list,
                        now=now,
                        repo_id=repo.id,
                        expires_at=expires,
                    )
                    bound = f"repository {repo.full_name}"
                else:
                    user = await _user(session, login or "")
                    if user.disabled:
                        raise click.ClickException(f"@{user.login} is disabled")
                    raw, row = await token_lib.create(
                        session,
                        name=name,
                        kind="personal",
                        scopes=scope_list,
                        now=now,
                        user_id=user.id,
                        expires_at=expires,
                    )
                    bound = f"user @{user.login}"
                _audit(
                    session,
                    "token_created",
                    target_kind="token",
                    target_id=row.id,
                    repo_id=row.repo_id,
                    name=row.name,
                    kind=row.kind,
                    scopes=scope_list,
                    prefix=row.prefix,
                )
                await session.commit()
        finally:
            await db.engine.dispose()
        until = f", expires {expires:%Y-%m-%d}" if expires else ", never expires"
        return (
            f"token {row.id} ({row.kind}, {bound}, scopes {','.join(scope_list)}{until})\n"
            f"{raw}\n"
            "This is the only time the token is shown. Send it as 'Authorization: Bearer <token>'."
        )

    click.echo(run_async(run))


@tokens_group.command("list")
@click.option("--all", "include_revoked", is_flag=True, help="Include revoked and expired tokens.")
def tokens_list(include_revoked: bool) -> None:
    """List tokens (live ones by default)."""
    settings = load()

    async def run() -> list[str]:
        await prepare_db(settings)
        db = _db(settings)
        now = settings.now()
        try:
            async with db.session() as session:
                rows = list(await session.scalars(select(ApiToken).order_by(ApiToken.id)))
                users = {u.id: u.login for u in await session.scalars(select(User))}
                repos = {r.id: r.full_name for r in await session.scalars(select(Repo))}
        finally:
            await db.engine.dispose()
        lines = [
            f"{'id':<5} {'prefix':<10} {'kind':<9} {'bound to':<30} {'scopes':<20} {'state':<9} {'last used':<17} name"
        ]
        shown = 0
        for row in rows:
            if row.revoked_at is not None:
                state = "revoked"
            elif row.expires_at is not None and row.expires_at <= now:
                state = "expired"
            else:
                state = "live"
            if state != "live" and not include_revoked:
                continue
            shown += 1
            bound = (
                f"@{users.get(row.user_id, '?')}" if row.kind == "personal" else repos.get(row.repo_id, "?")
            )
            used = row.last_used_at.strftime("%Y-%m-%d %H:%M") if row.last_used_at else "never"
            lines.append(
                f"{row.id:<5} {row.prefix:<10} {row.kind:<9} {bound[:30]:<30} {','.join(row.scopes or [])[:20]:<20} "
                f"{state:<9} {used:<17} {row.name}"
            )
        if not shown:
            return [
                "no tokens" + ("" if include_revoked else " (live; --all shows revoked and expired ones)")
            ]
        return lines

    for line in run_async(run):
        click.echo(line)


@tokens_group.command("revoke")
@click.argument("token_id", type=int)
def tokens_revoke(token_id: int) -> None:
    """Revoke token TOKEN_ID (its id from 'tokens list'); takes effect on the next request."""
    settings = load()

    async def run() -> str:
        await prepare_db(settings)
        db = _db(settings)
        try:
            async with db.session() as session:
                row = await session.get(ApiToken, token_id)
                if row is None:
                    raise click.ClickException(f"no token with id {token_id}")
                if row.revoked_at is not None:
                    return f"token {token_id} ({row.name}) was already revoked"
                row.revoked_at = settings.now()
                _audit(
                    session,
                    "token_revoked",
                    target_kind="token",
                    target_id=row.id,
                    repo_id=row.repo_id,
                    name=row.name,
                )
                await session.commit()
        finally:
            await db.engine.dispose()
        return f"token {token_id} ({row.name}) revoked"

    click.echo(run_async(run))
