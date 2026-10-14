"""``coverset settings ...``: read and change scoped settings from the server's shell.

The same catalogue, validation and audit trail as the settings pages
(:mod:`coverset.settings_scopes`); the shell is trusted like an instance admin, and every change is
written to the audit log with the ``cli`` actor. Without ``--repo``/``--org``/``--user`` a write
goes to the instance scope. A running ``coverset serve`` picks a change up within
:data:`~coverset.settings_scopes.CACHE_TTL_S` seconds (its cache is per process).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import click
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings
from coverset.db.models import Repo, User
from coverset.db.session import Database
from coverset.settings_scopes import (
    CATALOGUE,
    LOCAL_USER,
    ScopeKind,
    SettingError,
    apply_changes,
    catalogue_markdown,
    describe_sources,
    load_layers,
    spec_for,
)

__all__ = ["settings_group"]


@click.group(name="settings")
def settings_group() -> None:
    """Scoped settings: view preferences and policy per instance, organization, repository, user.

    Precedence for view.* keys: user > repo > org > instance > default. For policy.* keys:
    repo > org > instance > COVERSET_* > default. See docs/settings.md.
    """


async def _user_scope(session: AsyncSession, login: str | None) -> str | None:
    """The user-scope id for ``--user`` (``local`` is the operator without a user row)."""
    if login is None:
        return None
    if login == LOCAL_USER:
        return LOCAL_USER
    users = list(await session.scalars(select(User).where(User.login.ilike(login.lstrip("@")))))
    if len(users) != 1:
        raise click.ClickException(f"no single user with login {login!r} (users appear after they sign in)")
    return str(users[0].id)


def _scope(repo: str | None, org: str | None, user: str | None) -> ScopeKind:
    chosen = [name for name, value in (("repo", repo), ("org", org), ("user", user)) if value is not None]
    if len(chosen) > 1:
        raise click.UsageError("pass at most one of --repo, --org and --user")
    return chosen[0] if chosen else "instance"  # type: ignore[return-value]


async def _with_session[T](settings: Settings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            return await work(session)
    finally:
        await db.engine.dispose()


@settings_group.command("get")
@click.argument("key")
@click.option("--repo", "repo_spec", help="Repository (OWNER/NAME, id or local path).")
@click.option("--user", "login", help="Resolve as this user (GitHub login, or 'local').")
@click.option("--value-only", is_flag=True, help="Print only the value, as JSON.")
def settings_get(key: str, repo_spec: str | None, login: str | None, value_only: bool) -> None:
    """Print KEY's effective value and where it comes from."""
    try:
        spec_for(key)
    except SettingError as exc:
        raise click.BadParameter(str(exc), param_hint="KEY") from None
    settings = load()

    async def work(session: AsyncSession) -> str:
        repo = await find_repo(session, repo_spec) if repo_spec else None
        uid = await _user_scope(session, login)
        layers = await load_layers(session, repo=repo, user_id=uid, settings=settings)
        resolved = layers.resolve(key)
        if value_only:
            return json.dumps(resolved.value)
        return describe_sources([resolved])[0]

    click.echo(run_async(lambda: _with_session(settings, work)))


@settings_group.command("list")
@click.option("--repo", "repo_spec", help="Repository (OWNER/NAME, id or local path).")
@click.option("--user", "login", help="Resolve as this user (GitHub login, or 'local').")
def settings_list(repo_spec: str | None, login: str | None) -> None:
    """Print every key's effective value and its source."""
    settings = load()

    async def work(session: AsyncSession) -> list[str]:
        repo = await find_repo(session, repo_spec) if repo_spec else None
        uid = await _user_scope(session, login)
        layers = await load_layers(session, repo=repo, user_id=uid, settings=settings)
        return describe_sources([layers.resolve(key) for key in CATALOGUE])

    for line in run_async(lambda: _with_session(settings, work)):
        click.echo(line)


async def _write(
    settings: Settings,
    key: str,
    raw: object,
    *,
    repo_spec: str | None,
    org: str | None,
    login: str | None,
) -> str:
    scope = _scope(repo_spec, org, login)

    async def work(session: AsyncSession) -> str:
        repo: Repo | None = await find_repo(session, repo_spec) if repo_spec else None
        scope_id: str | None = None
        if repo is not None:
            scope_id = str(repo.id)
        elif org is not None:
            scope_id = org
        elif login is not None:
            scope_id = await _user_scope(session, login)
        written = await apply_changes(session, {key: raw}, scope=scope, scope_id=scope_id, actor=None)
        where = {"instance": "instance", "repo": repo.full_name if repo else "", "org": f"org {org}"}.get(
            scope, f"user {login}"
        )
        if key not in written:
            return f"{key}: unchanged at {where}"
        if raw is None:
            layers = await load_layers(session, repo=repo, org=org, settings=settings)
            inherited = layers.resolve(key)
            return f"{key}: cleared at {where}; now {inherited.display} ({inherited.detail})"
        return f"{key} = {spec_for(key).format(written[key])} at {where}"

    try:
        return await _with_session(settings, work)
    except SettingError as exc:
        raise click.ClickException(str(exc)) from None


@settings_group.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--repo", "repo_spec", help="Set it for this repository (OWNER/NAME, id or local path).")
@click.option("--org", help="Set it for every repository of this owner.")
@click.option("--user", "login", help="Set a view preference for this user (GitHub login, or 'local').")
def settings_set(key: str, value: str, repo_spec: str | None, org: str | None, login: str | None) -> None:
    """Set KEY to VALUE at one scope (the instance unless --repo, --org or --user)."""
    settings = load()
    click.echo(run_async(lambda: _write(settings, key, value, repo_spec=repo_spec, org=org, login=login)))


@settings_group.command("clear")
@click.argument("key")
@click.option("--repo", "repo_spec", help="Clear it for this repository.")
@click.option("--org", help="Clear it for this owner.")
@click.option("--user", "login", help="Clear this user's preference.")
def settings_clear(key: str, repo_spec: str | None, org: str | None, login: str | None) -> None:
    """Delete KEY at one scope so it inherits again."""
    settings = load()
    click.echo(run_async(lambda: _write(settings, key, None, repo_spec=repo_spec, org=org, login=login)))


@settings_group.command("keys")
@click.option("--markdown", is_flag=True, help="Print the table docs/settings.md carries.")
def settings_keys(markdown: bool) -> None:
    """List every key with its values, default and scopes."""
    if markdown:
        click.echo(catalogue_markdown())
        return
    for spec in CATALOGUE.values():
        scopes = ", ".join(sorted(spec.scopes))
        values = " | ".join(spec.choices) if spec.choices else spec.kind
        click.echo(f"{spec.name:<30} {values:<44} [{scopes}]")
        click.echo(f"    {spec.description}")
