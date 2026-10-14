"""Maintenance commands: ``gc``, ``repo purge``, ``invariants check`` and ``modal reap``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
from sqlalchemy import delete

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings

__all__ = ["gc_command", "invariants_check_command", "modal_reap_command", "repo_purge_command"]


@click.command("gc")
@click.option("--dry-run", is_flag=True, help="Report what would be deleted without deleting it.")
def gc_command(dry_run: bool) -> None:
    """Delete expired runs (and their facts), old run events and old webhook payloads."""
    settings = load()
    for line in run_async(lambda: _gc(settings, dry_run=dry_run)):
        click.echo(line)


async def _gc(settings: Settings, *, dry_run: bool) -> list[str]:
    """``serve`` runs the same pass daily in-process (``orchestrator.history.gc_forever``)."""
    from coverset.db.session import Database
    from coverset.facts.store import facts_store
    from coverset.orchestrator.history import collect_garbage

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        report = await collect_garbage(db, settings, facts_store(settings), dry_run=dry_run)
    finally:
        await db.engine.dispose()
    return report.lines(settings)


@click.command("purge")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def repo_purge_command(name: str, yes: bool) -> None:
    """Delete repository NAME (id or full name) with its runs, facts, mirror and uploads."""
    settings = load()
    run_async(lambda: _purge(settings, name, yes=yes))


async def _purge(settings: Settings, name: str, *, yes: bool) -> None:
    """Delete the repository's rows and scoped settings in one transaction, then its files.

    ``forget_scope`` runs in the Repo delete's transaction: repo-scope settings have no foreign key
    (docs/settings.md). The files are the
    facts, the mirror, the uploaded sources (``<data_dir>/sources/<repo_id>``) and the upload
    archives of the repository's runs (``<runs_dir>/<run_id>/upload``).
    """
    from sqlalchemy import select

    from coverset import settings_scopes
    from coverset.db.models import Repo, Run
    from coverset.db.session import Database
    from coverset.facts.gc import purge_repo_files

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            repo = await find_repo(session, name)
            label, repo_id = repo.full_name, repo.id
            if not yes and not click.confirm(
                f"Delete {label} and everything recorded for it?", default=False
            ):
                raise click.Abort()
            run_ids = list(await session.scalars(select(Run.id).where(Run.repo_id == repo_id)))
            await session.execute(delete(Repo).where(Repo.id == repo_id))
            await settings_scopes.forget_scope(session, "repo", repo_id)
            await session.commit()
    finally:
        await db.engine.dispose()
    # Facts, mirror, uploaded sources, upload archives: the blobs and their cached copies.
    await asyncio.to_thread(purge_repo_files, settings, repo_id, run_ids)
    click.echo(f"purged {label} (repo {repo_id}): database rows, scoped settings, facts, mirror and uploads")


@click.command("check")
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
)
def invariants_check_command(repo_path: Path) -> None:
    """Load and validate a checkout's invariants.yaml and print its invariants."""
    from coverset.analysis.invariants import InvariantError, load_invariants
    from coverset.lang.python import ConfigError, PythonAdapter

    root = repo_path.resolve()
    try:
        config = PythonAdapter().load_config(root)
    except ConfigError as exc:
        raise click.ClickException(f"[tool.coverset] is invalid: {exc}") from None
    path = root / config.invariants
    if not path.is_file():
        click.echo(f"no invariants file at {path} (no invariants)")
        return
    try:
        invariants = load_invariants(path.read_text(encoding="utf-8"))
    except (InvariantError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"{path}: {exc}") from None
    rows = [("id", "severity", "scope", "verify", "statement")]
    for inv in invariants:
        verify = ", ".join(v.kind + (f"({v.marker})" if v.marker else "") for v in inv.verify)
        statement = " ".join(inv.statement.split())
        rows.append(
            (
                inv.id,
                inv.severity,
                f"{inv.scope.kind} {inv.scope.target}",
                verify,
                statement[:70] + ("..." if len(statement) > 70 else ""),
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for index, row in enumerate(rows):
        click.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            click.echo("  ".join("-" * w for w in widths))
    click.echo(f"{len(invariants)} invariant(s) valid in {path}")


@click.command("reap")
@click.option(
    "--all",
    "reap_all",
    is_flag=True,
    help="Terminate every coverset sandbox, active runs and other processes' young pool, snapshot "
    "and smoke sandboxes included.",
)
@click.option(
    "--any-instance",
    is_flag=True,
    help="Look at every coverset sandbox in the App (any data dir, e.g. benchmarks), not only this "
    "installation's; their shards are terminated because their runs are unknown here.",
)
def modal_reap_command(reap_all: bool, any_instance: bool) -> None:
    """Terminate coverset sandboxes not tied to an active run.

    Without --all, pool, snapshot and smoke sandboxes younger than their natural lifetime (pool:
    COVERSET_POOL_TTL_S + 60 s; snapshot and smoke: COVERSET_RUN_TIMEOUT_S) are kept, because a
    running `coverset serve`, `coverset warm` or `modal smoke` may own them.
    """
    settings = load()
    click.echo(run_async(lambda: _reap(settings, reap_all=reap_all, any_instance=any_instance)))


async def _reap(settings: Settings, *, reap_all: bool, any_instance: bool = False) -> str:
    from coverset.db.session import Database
    from coverset.executors import ExecutorError, get_executor
    from coverset.orchestrator.reaper import active_run_ids, reap

    try:
        executor = get_executor(settings, "modal")
    except ExecutorError as exc:
        raise click.ClickException(str(exc)) from None
    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            active = await active_run_ids(session)
        report = await reap(executor, all=reap_all, active=active, any_instance=any_instance)
    finally:
        await db.engine.dispose()
        await executor.close()
    errors = f"; {len(report.errors)} error(s)" if report.errors else ""
    return f"found {report.found} sandbox(es), terminated {report.terminated}, kept {report.kept}{errors}"
