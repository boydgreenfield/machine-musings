"""``coverset seed-demo`` and ``coverset demo``: the end-to-end demo on the seeded ledger repository.

``coverset demo`` is idempotent: run twice, it adds runs to the same database. ``--fresh`` first
empties the data directory (:func:`wipe_data_dir`, which refuses anything that does not look like
a coverset data directory). ``coverset dev`` seeds through :func:`demo_settings` and
:func:`seed_demo_data` when its data directory holds no repository.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import signal
from pathlib import Path
from typing import Any

import click

from coverset.commands.common import StreamPrinter, load, prepare_db, run_async
from coverset.config import Settings

__all__ = [
    "DATA_DIR_MARKER",
    "demo_command",
    "demo_settings",
    "seed_demo_command",
    "seed_demo_data",
    "wipe_data_dir",
]

log = logging.getLogger(__name__)

STARTUP_WAIT_S = 30.0
DATA_DIR_MARKER = "coverset.db"
"""A directory holding this file (or nothing at all) is one ``--fresh`` may empty."""


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
@click.option(
    "--no-report", is_flag=True, help="Skip building the static report of PR #2 under <data dir>/report."
)
@click.option("-v", "--verbose", is_flag=True, help="Print every test result of every run.")
@click.option(
    "--fresh",
    is_flag=True,
    help="Empty the data directory first (only one holding coverset.db, or nothing), then rebuild.",
)
def demo_command(
    executor: str, port: int, no_serve: bool, no_report: bool, verbose: bool, fresh: bool
) -> None:
    """Seed the demo repo, run its history and both PRs, replay the pr_opened webhook, then serve."""
    if executor == "modal":
        try:
            import coverset.executors.modal_  # noqa: F401
        except ImportError as exc:
            raise click.ClickException(f"the Modal executor is not available in this build: {exc}") from None
    settings = demo_settings(executor, port=port)
    if fresh:
        removed = wipe_data_dir(settings)
        click.echo(f"emptied {settings.data_dir} ({removed} entries removed)")
    printer = StreamPrinter(verbose=verbose, quiet=not verbose)
    run_async(lambda: _demo(settings, executor, serve=not no_serve, report=not no_report, printer=printer))


def demo_settings(executor: str = "local", *, port: int | None = None) -> Settings:
    """The demo's settings: the environment's, with ``executor``, webhook runs allowed, ``port`` on
    the configured bind host (when given) and a random webhook secret when none is configured."""
    base = load()
    overrides: dict[str, Any] = {"executor": executor, "local_allow_webhook_runs": True}
    if port is not None:
        host = base.bind_host
        overrides["bind"] = f"{host}:{port}" if ":" not in host else f"[{host}]:{port}"
    if base.github_webhook_secret is None or not base.github_webhook_secret.get_secret_value():
        overrides["github_webhook_secret"] = secrets.token_hex(24)
    return load(**overrides)


def seed_demo_data(settings: Settings, *, report: bool = True, verbose: bool = False) -> None:
    """Seed and run the demo into ``settings.data_dir`` and replay its webhook, without serving."""
    printer = StreamPrinter(verbose=verbose, quiet=not verbose)
    run_async(lambda: _demo(settings, settings.executor, serve=False, report=report, printer=printer))


def wipe_data_dir(settings: Settings) -> int:
    """Remove everything inside ``settings.data_dir`` (the directory itself stays); returns how
    many top-level entries went.

    Refuses (:class:`click.ClickException`) a directory that is neither empty nor holds
    :data:`DATA_DIR_MARKER`, a data directory that is a symlink, and a SQLite database configured
    outside the data directory (``COVERSET_DATABASE_URL``), which emptying the directory would
    leave behind with rows pointing at deleted mirrors and facts. Symlinks inside are unlinked,
    never followed. A missing directory is fine (nothing to remove).
    """
    from coverset.db.session import sqlite_file_path

    data_dir = settings.data_dir
    if data_dir.is_symlink():
        raise click.ClickException(f"--fresh refuses {data_dir}: it is a symlink")
    if not data_dir.exists():
        return 0
    if not data_dir.is_dir():
        raise click.ClickException(f"--fresh refuses {data_dir}: not a directory")
    entries = list(data_dir.iterdir())
    if entries and not (data_dir / DATA_DIR_MARKER).is_file():
        raise click.ClickException(
            f"--fresh refuses {data_dir}: it is not empty and holds no {DATA_DIR_MARKER}, so it does "
            "not look like a coverset data directory (check COVERSET_DATA_DIR)"
        )
    database = sqlite_file_path(settings.database_url)
    if database is None or not database.resolve().is_relative_to(data_dir.resolve()):
        raise click.ClickException(
            f"--fresh refuses to run: the database ({settings.database_url.split('://')[0]}) is not a "
            f"SQLite file inside {data_dir}; empty it yourself or unset COVERSET_DATABASE_URL"
        )
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)
    return len(entries)


async def _demo(
    settings: Settings, executor: str, *, serve: bool, report: bool = True, printer: StreamPrinter
) -> None:
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
    if report:
        await _static_report(settings, result.runs)
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


async def _static_report(settings: Settings, runs: list[Any]) -> None:
    """Build the static report of PR #2's run under ``<data dir>/report`` (never fails the demo)."""
    import asyncio

    from coverset.report import build_report
    from coverset.report.load import load_run

    run_id = next((r.run_id for r in runs if r.name == "pr-2"), None)
    if run_id is None:
        return
    out = settings.data_dir / "report"
    try:
        inp = await load_run(settings, run_id, None, title="coverset demo · PR #2")
        built = await asyncio.to_thread(build_report, inp, out)
    except Exception as exc:  # the report is a convenience; the runs above are the demo
        click.echo(f"static report not built: {exc}", err=True)
        return
    pages = getattr(built, "pages", None)
    total = sum(pages.values()) if isinstance(pages, dict) else None
    note = f" ({total} pages)" if total is not None else ""
    click.echo(f"static report of PR #2{note}: {out / 'index.html'}")
