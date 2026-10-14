"""``coverset db``: the control-plane database's backend, lease and migration helpers.

* ``coverset db status`` prints the backend, server version, Alembic revision against the packaged
  head, the connection pool a process opens, and on Postgres who holds the background-task leader
  lease (:mod:`coverset.db.leader`).
* ``coverset db copy --to URL`` copies every table of the configured database (or ``--from URL``)
  into another one, typically SQLite into Postgres (:mod:`coverset.db.copy`). docs/postgres.md.
"""

from __future__ import annotations

import asyncio
import sys

import click
from sqlalchemy.engine import make_url

from coverset.db.copy import DEFAULT_BATCH_SIZE, CopyError

__all__ = ["db_group"]


def _normalise(url: str) -> str:
    """Accept the libpq form (``postgresql://``, ``postgres://``) the way the settings do."""
    url = url.strip()
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    return url


def _shown(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


@click.group(name="db")
def db_group() -> None:
    """The control-plane database (SQLite or Postgres): status and copying (docs/postgres.md)."""


@db_group.command("status")
def db_status_command() -> None:
    """Print the database backend, revision, pool and leader lease (exit 1 if it cannot connect)."""
    from coverset.cli import load_settings
    from coverset.db.leader import leader_status
    from coverset.db.session import check_database, make_engine

    settings = load_settings()
    report = check_database(settings.database_url)
    click.echo(f"database  {_shown(settings.database_url)}")
    click.echo(f"backend   {' '.join(part for part in (report.backend, report.server_version) if part)}")
    click.echo(f"revision  {', '.join(report.current) or '-'} (head {', '.join(report.heads)})")
    click.echo(f"pool      {report.pool}")
    if report.status == "fail":
        click.echo(f"error     {report.error}")
        raise SystemExit(1)

    async def lease() -> str:
        engine = make_engine(settings.database_url)
        try:
            return (await leader_status(engine)).detail
        finally:
            await engine.dispose()

    click.echo(f"leader    {asyncio.run(lease())}")
    click.echo(f"status    {report.status}: {report.detail}")


@db_group.command("copy")
@click.option("--to", "target", required=True, help="The database to copy into (migrated, must be empty).")
@click.option(
    "--from",
    "source",
    default=None,
    help="The database to copy from (default: COVERSET_DATABASE_URL, i.e. this data dir's SQLite file).",
)
@click.option("--batch-size", default=DEFAULT_BATCH_SIZE, show_default=True, type=click.IntRange(1, 100_000))
def db_copy_command(target: str, source: str | None, batch_size: int) -> None:
    """Copy every table into another database, in dependency order, verified by row counts.

    \b
    coverset db copy --to postgresql://user:pass@host/coverset
    Then point COVERSET_DATABASE_URL at the target. Stop `coverset serve` first: rows written
    during the copy would be missed.
    """
    from coverset.cli import load_settings
    from coverset.db.copy import copy_database

    source_url = _normalise(source) if source else load_settings().database_url
    target_url = _normalise(target)
    click.echo(f"copying {_shown(source_url)} -> {_shown(target_url)}")
    try:
        report = asyncio.run(copy_database(source_url, target_url, batch_size=batch_size))
    except CopyError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    for revision in report.applied:
        click.echo(f"applied migration {revision} to the target")
    width = max(len(name) for name in report.target)
    for name, count in report.target.items():
        if count:
            click.echo(f"  {name:<{width}}  {count:>9,} rows")
    tables = sum(1 for count in report.target.values() if count)
    click.echo(f"copied {report.total:,} rows in {tables} tables; row counts match the source")
