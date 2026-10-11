"""The ``coverset`` command line.

Commands that later build phases implement are registered here as placeholders that exit with
"<name> arrives in phase N"; a phase replaces its placeholder by registering the real command
under the same name on the same group.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

import click
from hypercorn.asyncio import serve as hypercorn_serve
from hypercorn.config import Config as HypercornConfig
from pydantic import ValidationError
from quart import Quart
from sqlalchemy.engine import make_url

from coverset import __version__
from coverset.config import Settings, get_settings

GRACEFUL_TIMEOUT_S = 10.0
"""How long ``serve`` waits for in-flight requests after SIGINT/SIGTERM."""


def load_settings(**overrides: Any) -> Settings:
    """Settings from the environment and ``.env`` (plus ``overrides``), as a clean CLI error."""
    try:
        return Settings(**overrides) if overrides else get_settings()
    except ValidationError as exc:
        problems = "; ".join(_describe(error) for error in exc.errors())
        raise click.ClickException(f"invalid configuration: {problems}") from None


def _describe(error: Any) -> str:
    location = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


@click.group(name="coverset")
@click.version_option(__version__, prog_name="coverset")
def cli() -> None:
    """Anticipatory CI and semantic coverage for agent-written Python."""


@cli.command("init-db")
def init_db_command() -> None:
    """Create the data directory and migrate the control-plane database to the latest revision."""
    from coverset.db.session import init_db

    settings = load_settings()
    asyncio.run(init_db(settings))
    url = make_url(settings.database_url).render_as_string(hide_password=True)
    click.echo(f"database ready: {url}")


@cli.command()
@click.option("--bind", help="host:port to listen on (overrides COVERSET_BIND).")
def serve(bind: str | None) -> None:
    """Serve the web UI, webhooks and API with Hypercorn until SIGINT/SIGTERM."""
    from coverset.app import create_app

    settings = load_settings(bind=bind) if bind else load_settings()
    app = create_app(settings)
    click.echo(f"coverset {__version__} serving on {settings.public_url} (bind {settings.bind})")
    asyncio.run(_serve(app, settings.bind))


async def _serve(app: Quart, bind: str) -> None:
    config = HypercornConfig()
    config.bind = [bind]
    # Hand hypercorn our loggers so its output goes through the redacting root handler only once.
    config.accesslog = logging.getLogger("hypercorn.access")
    config.errorlog = logging.getLogger("hypercorn.error")
    config.graceful_timeout = GRACEFUL_TIMEOUT_S
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, shutdown.set)
    await hypercorn_serve(app, config, shutdown_trigger=shutdown.wait)


@cli.group()
def modal() -> None:
    """Modal sandbox maintenance."""


@cli.group()
def facts() -> None:
    """Query the Parquet facts store."""


@cli.group()
def invariants() -> None:
    """Check declared invariants."""


@cli.group()
def repo() -> None:
    """Manage repositories known to coverset."""


def _placeholder(group: click.Group, name: str, phase: str, summary: str) -> None:
    """Register ``name`` on ``group`` as a command that reports which phase delivers it."""
    full_name = name if group is cli else f"{group.name} {name}"

    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def command(args: tuple[str, ...]) -> None:
        raise click.ClickException(f"{full_name} arrives in phase {phase}")

    group.command(
        name,
        help=f"{summary} (arrives in phase {phase}).",
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )(command)


for _group, _name, _phase, _summary in (
    (cli, "seed-demo", "2", "Create the deterministic demo repository and its branches"),
    (facts, "query", "3", "Run a SQL query over the facts views"),
    (cli, "gc", "3", "Delete expired runs, deliveries, events and orphaned facts"),
    (cli, "shapes", "4", "Show a function's observed and missing input classes"),
    (invariants, "check", "4", "Evaluate invariants for a repository checkout"),
    (cli, "run", "5a", "Run a repository's tests for a base..head change"),
    (cli, "demo", "5a", "Seed, run and serve the end-to-end demo"),
    (cli, "warm", "5b", "Build the environment image and trusted snapshot for a branch"),
    (modal, "reap", "5b", "Terminate coverset sandboxes not tied to an active run"),
    (modal, "smoke", "5b", "Create one Modal sandbox, run python -V and terminate it"),
    (cli, "replay", "6", "Replay a recorded webhook fixture through the forge pipeline"),
    (repo, "purge", "6", "Delete a repository and everything recorded for it"),
    (invariants, "scan", "7", "Scan the default branch and record invariant statuses"),
):
    _placeholder(_group, _name, _phase, _summary)


def main() -> None:
    """Console-script entry point."""
    cli()
