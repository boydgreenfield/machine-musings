"""``coverset seed-demo`` and ``coverset demo``: the end-to-end demo on the seeded ledger repository."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import signal
from pathlib import Path
from typing import Any

import click

from coverset.commands.common import StreamPrinter, load, prepare_db, run_async
from coverset.config import Settings

__all__ = ["demo_command", "seed_demo_command"]

log = logging.getLogger(__name__)

STARTUP_WAIT_S = 30.0


@click.command("seed-demo")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Bare repository to (re)create.")
def seed_demo_command(out: Path | None) -> None:
    """Create the deterministic demo repository (six main commits, two PR branches) and print its SHAs."""
    from coverset.demo.seed import seed_demo_repo

    target = out or load().data_dir / "demo.git"
    try:
        shas = seed_demo_repo(target)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from None
    for name, sha in {**shas["commits"], **shas["branches"]}.items():
        click.echo(f"{sha}  {name}")
    click.echo(f"wrote {target.with_name(target.name + '.shas.json')}")


@click.command("demo")
@click.option("--executor", type=click.Choice(["local", "modal"]), default="local", show_default=True)
@click.option("--port", type=click.IntRange(1, 65535), default=8000, show_default=True)
@click.option("--no-serve", is_flag=True, help="Seed and run the demo, replay the webhook, then exit.")
@click.option("-v", "--verbose", is_flag=True, help="Print every test result of every run.")
def demo_command(executor: str, port: int, no_serve: bool, verbose: bool) -> None:
    """Seed the demo repo, run its history and both PRs, replay the pr_opened webhook, then serve."""
    if executor == "modal":
        try:
            import coverset.executors.modal_  # noqa: F401
        except ImportError as exc:
            raise click.ClickException(f"the Modal executor is not available in this build: {exc}") from None
    base = load()
    overrides: dict[str, Any] = {
        "executor": executor,
        "local_allow_webhook_runs": True,
        "bind": f"{base.bind_host}:{port}" if ":" not in base.bind_host else f"[{base.bind_host}]:{port}",
    }
    if base.github_webhook_secret is None or not base.github_webhook_secret.get_secret_value():
        overrides["github_webhook_secret"] = secrets.token_hex(24)
    settings = load(**overrides)
    printer = StreamPrinter(verbose=verbose, quiet=not verbose)
    run_async(lambda: _demo(settings, executor, serve=not no_serve, printer=printer))


async def _demo(settings: Settings, executor: str, *, serve: bool, printer: StreamPrinter) -> None:
    from coverset.app import create_app
    from coverset.demo.flow import format_table, run_demo
    from coverset.demo.seed import PR_OPENED_FIXTURE
    from coverset.orchestrator.queue import install_run_queue
    from coverset.orchestrator.runner import OrchestratorContext, finalize_crashed

    await prepare_db(settings)
    ctx = OrchestratorContext.from_settings(settings)
    current: list[str] = []

    def on_run(name: str, run_id: str) -> None:
        current[:] = [run_id]
        click.echo(f"\n--- {name}: run {run_id}")

    try:
        result = await run_demo(
            ctx, out=settings.data_dir / "demo.git", executor=executor, on_event=printer, on_run=on_run
        )
    except BaseException as exc:  # crashed or interrupted: never leave the current run "running"
        for run_id in current:
            with contextlib.suppress(Exception):
                await finalize_crashed(ctx.db, settings, run_id, f"interrupted: {type(exc).__name__}")
        raise
    finally:
        await ctx.close()
    click.echo("")
    click.echo(format_table(result.runs))
    app = create_app(settings)
    install_run_queue(app)
    if not serve:
        if PR_OPENED_FIXTURE.is_file():
            async with app.test_app():
                await _replay(app, settings)
        return
    await _serve_with_replay(app, settings, replay=PR_OPENED_FIXTURE.is_file())


async def _replay(app: Any, settings: Settings) -> None:
    from coverset.demo.flow import replay_fixture

    payload = await replay_fixture(app, settings)
    status = payload.get("status")
    detail = payload.get("reason") or payload.get("run_status") or ""
    click.echo(
        f"\nreplayed fixtures/webhooks/pr_opened.json: HTTP {payload.get('http_status')} {status} {detail}".rstrip()
    )


async def _serve_with_replay(app: Any, settings: Settings, *, replay: bool) -> None:
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config as HypercornConfig

    config = HypercornConfig()
    config.bind = [settings.bind]
    config.accesslog = logging.getLogger("hypercorn.access")
    config.errorlog = logging.getLogger("hypercorn.error")
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, shutdown.set)
    server = loop.create_task(hypercorn_serve(app, config, shutdown_trigger=shutdown.wait))
    click.echo(f"\ncoverset demo serving on {settings.public_url} (Ctrl-C to stop)")
    if replay:
        dispatcher = app.extensions["run_queue"]
        waited = 0.0
        while not dispatcher.started and waited < STARTUP_WAIT_S and not server.done():
            await asyncio.sleep(0.1)
            waited += 0.1
        if dispatcher.started:
            await _replay(app, settings)
    await server
