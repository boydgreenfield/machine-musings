"""The ``coverset`` command line.

Commands that later build phases implement are registered here as placeholders that exit with
"<name> arrives in phase N"; a phase replaces its placeholder by registering the real command
under the same name on the same group.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
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
    (invariants, "scan", "7", "Scan the default branch and record invariant statuses"),
):
    _placeholder(_group, _name, _phase, _summary)


def _register_modal_commands() -> None:
    """modal smoke and modal images (modal reap is registered with the orchestrator's commands)."""
    from coverset.commands.modal import register_modal_commands

    register_modal_commands(modal)


_register_modal_commands()


def _register_orchestrator_commands() -> None:
    """run, warm, seed-demo, demo, shapes, gc, facts query, invariants check, repo purge, modal reap."""
    from coverset.commands import register_commands

    register_commands(cli, facts=facts, invariants=invariants, repo=repo, modal=modal)


_register_orchestrator_commands()


DEFAULT_SERVER = "http://127.0.0.1:8000"
FIXTURES_DIR = Path("fixtures") / "webhooks"


def _fixture_path(fixture: str) -> Path:
    """``fixture`` as a path, or ``fixtures/webhooks/<fixture>[.json]`` when that exists."""
    path = Path(fixture)
    if path.exists():
        return path
    for candidate in (FIXTURES_DIR / fixture, FIXTURES_DIR / f"{fixture}.json"):
        if candidate.exists():
            return candidate
    raise click.ClickException(f"no such fixture: {fixture}")


@cli.command()
@click.argument("fixture")
@click.option("--server", default=DEFAULT_SERVER, show_default=True, help="Base URL of a running coverset.")
@click.option("--forge", "forge_name", default="github", show_default=True, help="Webhook route name.")
@click.option(
    "--resign/--as-recorded",
    default=None,
    help="Re-sign with COVERSET_GITHUB_WEBHOOK_SECRET (default: when one is set and differs from the "
    "fixtures' test secret).",
)
@click.option(
    "--new-delivery", is_flag=True, help="Send a fresh X-GitHub-Delivery id (else it is deduplicated)."
)
def replay(fixture: str, server: str, forge_name: str, resign: bool | None, new_delivery: bool) -> None:
    """Post a recorded webhook FIXTURE (headers + exact raw body) to a running server."""
    import urllib.error
    import urllib.request
    import uuid

    from coverset.forge.signing import SIGNATURE_HEADER, TEST_WEBHOOK_SECRET, sign_body

    path = _fixture_path(fixture)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        headers = {str(k): str(v) for k, v in data["headers"].items()}
        body = str(data["body"]).encode("utf-8")
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise click.ClickException(f"{path} is not a webhook fixture: {exc}") from None
    try:
        configured = load_settings().github_webhook_secret
        secret = configured.get_secret_value() if configured else ""
    except click.ClickException:
        secret = os.environ.get("COVERSET_GITHUB_WEBHOOK_SECRET", "")
    if resign is None:
        resign = bool(secret) and secret != TEST_WEBHOOK_SECRET
    if resign:
        if not secret:
            raise click.ClickException("--resign needs COVERSET_GITHUB_WEBHOOK_SECRET")
        headers[SIGNATURE_HEADER] = sign_body(secret, body)
    if new_delivery:
        headers["X-GitHub-Delivery"] = str(uuid.uuid4())
    url = f"{server.rstrip('/')}/webhooks/{forge_name}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, text = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise click.ClickException(f"cannot reach {url}: {exc}") from None
    signed = "re-signed" if resign else "as recorded"
    click.echo(f"POST {url} ({headers.get('X-GitHub-Event', '?')}, {signed}) -> HTTP {status}")
    click.echo(text.strip())
    if status >= 400:
        raise SystemExit(1)


@cli.group()
def forge() -> None:
    """Inspect the forge integration."""


@forge.command("calls")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 10_000))
@click.option("--json", "as_json", is_flag=True, help="Print JSON lines instead of a table.")
def forge_calls(limit: int, as_json: bool) -> None:
    """List recorded forge calls (the fake forge's outbox), newest first."""
    from coverset.db.models import ForgeCall
    from coverset.db.session import Database

    settings = load_settings()

    async def fetch() -> list[ForgeCall]:
        from sqlalchemy import select

        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                rows = await session.scalars(select(ForgeCall).order_by(ForgeCall.id.desc()).limit(limit))
                return list(rows)
        finally:
            await database.engine.dispose()

    for call in asyncio.run(fetch()):
        if as_json:
            record = {
                "id": call.id,
                "created_at": call.created_at.isoformat(),
                "forge": call.forge,
                "method": call.method,
                "target": call.target,
                "repo_id": call.repo_id,
                "run_id": call.run_id,
                "status_code": call.status_code,
                "request": call.request,
                "response": call.response,
            }
            click.echo(json.dumps(record, sort_keys=True))
        else:
            when = call.created_at.strftime("%Y-%m-%d %H:%M:%S")
            click.echo(
                f"{call.id:>6}  {when}  {call.method:<20} {call.target or '-':<30} {call.run_id or '-'}"
            )


def main() -> None:
    """Console-script entry point."""
    cli()
