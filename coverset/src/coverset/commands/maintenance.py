"""Maintenance commands: ``gc``, ``repo purge``, ``invariants check`` and ``modal reap``."""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path

import click
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings

__all__ = ["gc_command", "invariants_check_command", "modal_reap_command", "repo_purge_command"]

DELIVERY_KEEP_DAYS = 30
"""Delivery rows stay this long (payload nulled earlier) so redeliveries are still deduplicated."""


@click.command("gc")
@click.option("--dry-run", is_flag=True, help="Report what would be deleted without deleting it.")
def gc_command(dry_run: bool) -> None:
    """Delete expired runs (and their facts), old run events and old webhook payloads."""
    settings = load()
    for line in run_async(lambda: _gc(settings, dry_run=dry_run)):
        click.echo(line)


async def _gc(settings: Settings, *, dry_run: bool) -> list[str]:
    from coverset.db.models import Run, RunEvent, WebhookDelivery
    from coverset.db.session import Database
    from coverset.facts.gc import expire_runs
    from coverset.facts.store import FactsStore

    await prepare_db(settings)
    now = settings.now()
    run_cutoff = now - timedelta(days=settings.retention_runs_days)
    event_cutoff = now - timedelta(days=settings.retention_events_days)
    payload_cutoff = now - timedelta(days=settings.retention_deliveries_days)
    delivery_cutoff = now - timedelta(days=max(DELIVERY_KEEP_DAYS, settings.retention_deliveries_days))
    db = Database.from_url(settings.database_url)
    store = FactsStore(settings.facts_dir)
    try:
        async with db.session() as session:
            keep = await _runs_to_keep(session)
            expired_db = list(
                await session.scalars(
                    select(Run.id).where(
                        Run.created_at < run_cutoff,
                        Run.id.not_in(keep or {""}),
                        Run.status.not_in(("queued", "running")),
                    )
                )
            )
            events = await session.scalar(select(func.count(RunEvent.id)).where(RunEvent.at < event_cutoff))
            payloads = await session.scalar(
                select(func.count(WebhookDelivery.id)).where(
                    WebhookDelivery.received_at < payload_cutoff, WebhookDelivery.payload.is_not(None)
                )
            )
            deliveries = await session.scalar(
                select(func.count(WebhookDelivery.id)).where(WebhookDelivery.received_at < delivery_cutoff)
            )
            if not dry_run:
                if expired_db:
                    await session.execute(delete(Run).where(Run.id.in_(expired_db)))
                await session.execute(delete(RunEvent).where(RunEvent.at < event_cutoff))
                await session.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.received_at < payload_cutoff)
                    .values(payload=None)
                )
                await session.execute(
                    delete(WebhookDelivery).where(WebhookDelivery.received_at < delivery_cutoff)
                )
                await session.commit()
    finally:
        await db.engine.dispose()
    expired_facts = await asyncio.to_thread(
        expire_runs, store, keep_run_ids=keep, older_than=run_cutoff, dry_run=dry_run
    )
    verb = "would delete" if dry_run else "deleted"
    return [
        f"{verb} {len(expired_db)} run(s) from the database and {len(expired_facts)} run(s) of facts",
        f"{verb} {events or 0} run event(s) older than {settings.retention_events_days} d",
        f"{'would clear' if dry_run else 'cleared'} {payloads or 0} webhook payload(s) older than "
        f"{settings.retention_deliveries_days} d; {verb} {deliveries or 0} delivery row(s)",
        f"kept {len(keep)} run(s): the latest baseline per repo and the latest run per open PR",
    ]


async def _runs_to_keep(session: AsyncSession) -> set[str]:
    """The latest completed baseline per repo and the latest run per open PR."""
    from coverset.db.models import PullRequest, Run
    from coverset.orchestrator.history import BASELINE_TRIGGERS

    keep: set[str] = set()
    baselines = await session.execute(
        select(Run.repo_id, Run.id)
        .where(Run.trigger.in_(BASELINE_TRIGGERS), Run.status == "completed")
        .order_by(Run.created_at.desc())
    )
    seen: set[int] = set()
    for repo_id, run_id in baselines:
        if repo_id not in seen:
            seen.add(repo_id)
            keep.add(run_id)
    for pr_id in await session.scalars(select(PullRequest.id).where(PullRequest.state == "open")):
        latest = await session.scalar(
            select(Run.id).where(Run.pr_id == pr_id).order_by(Run.created_at.desc()).limit(1)
        )
        if latest is not None:
            keep.add(latest)
    return keep


@click.command("purge")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def repo_purge_command(name: str, yes: bool) -> None:
    """Delete repository NAME (id or full name) with its runs, facts and mirror."""
    settings = load()
    run_async(lambda: _purge(settings, name, yes=yes))


async def _purge(settings: Settings, name: str, *, yes: bool) -> None:
    from coverset.db.models import Repo
    from coverset.db.session import Database
    from coverset.facts.store import FactsStore
    from coverset.orchestrator.repos import mirror_path

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
            await session.execute(delete(Repo).where(Repo.id == repo_id))
            await session.commit()
    finally:
        await db.engine.dispose()
    store = FactsStore(settings.facts_dir)
    await asyncio.to_thread(shutil.rmtree, store.repo_dir(repo_id), True)
    await asyncio.to_thread(shutil.rmtree, mirror_path(settings, repo_id), True)
    click.echo(f"purged {label} (repo {repo_id}): database rows, facts and mirror")


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
    "--all", "reap_all", is_flag=True, help="Terminate every coverset sandbox, active runs included."
)
def modal_reap_command(reap_all: bool) -> None:
    """Terminate coverset sandboxes not tied to an active run."""
    settings = load()
    click.echo(run_async(lambda: _reap(settings, reap_all=reap_all)))


async def _reap(settings: Settings, *, reap_all: bool) -> str:
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
        report = await reap(executor, all=reap_all, active=active)
    finally:
        await db.engine.dispose()
        await executor.close()
    errors = f"; {len(report.errors)} error(s)" if report.errors else ""
    return f"found {report.found} sandbox(es), terminated {report.terminated}, kept {report.kept}{errors}"
