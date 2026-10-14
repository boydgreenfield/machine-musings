"""``coverset dev`` and ``coverset serve --reload``: the local development loop.

``coverset dev`` seeds the demo into the data directory when it holds no repository yet (the demo
command's own :func:`~coverset.commands.demo.seed_demo_data`, run in a child process by
:func:`seed_in_child`; ``--no-seed`` skips it), then serves with reload. ``coverset serve
--reload`` is the same reloader without the seeding.

The reloader is this process; the server is a child process (:func:`serve_child`) that
:class:`Reloader` replaces whenever a watched file changes:

* :class:`Watcher` polls the modification times of every ``.py``, ``.html``, ``.css`` and ``.js``
  file under ``src/coverset`` and ``packages/*/src`` every :data:`POLL_INTERVAL_S` with
  :func:`os.scandir` (no dependency), skipping caches, virtualenvs and ``node_modules``
  (:data:`IGNORED_DIRS`). A change is reported once no further change has been seen for
  :data:`DEBOUNCE_S`, so an editor's save-all or a ``git checkout`` restarts once.
* Python changes restart the child (SIGTERM, a short graceful shutdown, SIGKILL after
  :data:`STOP_TIMEOUT_S`); one line per restart names the changed path, and another says when the
  new server answers ``/healthz``.
* Template and static-file changes need no restart (:func:`live_reloadable`): the child turns on
  Quart's ``TEMPLATES_AUTO_RELOAD`` and Jinja's ``auto_reload`` (:func:`enable_dev_reload`), so the
  next request re-reads a changed template, and static URLs carry a content hash (``?v=``) that
  :func:`coverset.app.install_response_caching` recomputes when a file's mtime or size changes,
  so a reloaded page fetches the edited stylesheet instead of its year-long cached copy.
* A child that exits on its own (a syntax error, the port in use) is reported and restarted on
  the next change; Ctrl-C stops both processes.

The child runs in its own session, so a terminal's Ctrl-C reaches only the reloader, which stops
the child with SIGTERM (its worker processes then shut down cleanly instead of each printing a
``KeyboardInterrupt`` traceback); the child also exits by itself when the reloader disappears
(:data:`PARENT_CHECK_S`). It serves with a 1-second graceful timeout instead of ``serve``'s 10 s, so
an open event stream (the live run page) does not hold a restart up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from quart import Quart

    from coverset.config import Settings

__all__ = [
    "DEBOUNCE_S",
    "POLL_INTERVAL_S",
    "Reloader",
    "Watcher",
    "dev_command",
    "enable_dev_reload",
    "live_reloadable",
    "run_reloader",
    "scan_mtimes",
    "seed_command",
    "seed_in_child",
    "serve_child",
    "watch_roots",
]

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
"""How often the watcher re-reads modification times."""
DEBOUNCE_S = 0.3
"""Quiet time after the last change before the change is acted on."""
STOP_TIMEOUT_S = 3.0
"""How long a stopping child may take before it is killed."""
CHILD_GRACEFUL_TIMEOUT_S = 1.0
"""Hypercorn's graceful timeout in the child (open event streams are cut after this)."""
READY_TIMEOUT_S = 60.0
"""How long the reloader waits for a new child to answer ``/healthz`` before saying so."""
PARENT_CHECK_S = 1.0
"""How often the child checks that its reloader is still alive (else it shuts down)."""
WATCHED_SUFFIXES = frozenset({".py", ".html", ".css", ".js"})
IGNORED_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".hypothesis",
        "build",
        "dist",
        "target",
    }
)
"""Directory names never descended into (besides any name starting with a dot)."""
DEFAULT_DEV_BIND = "127.0.0.1:8000"
_CLOCK_SLACK_S = 1e-6
"""Float slack when comparing elapsed time with the debounce (sleeping exactly the rest counts)."""

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
"""``src/coverset`` (or the installed package directory)."""
LIVE_DIRS = (_PACKAGE_DIR / "web" / "templates", _PACKAGE_DIR / "web" / "static")
"""Changes below these directories are served fresh by the running child (no restart)."""


# -- watching --------------------------------------------------------------------------------------


def watch_roots(package_dir: Path = _PACKAGE_DIR) -> list[Path]:
    """``src/coverset`` and, in a source checkout, every ``packages/*/src``."""
    roots = [package_dir]
    project = package_dir.parent.parent
    if (project / "pyproject.toml").is_file() and (project / "packages").is_dir():
        roots.extend(sorted(p for p in (project / "packages").glob("*/src") if p.is_dir()))
    return roots


def scan_mtimes(roots: Iterable[Path], suffixes: frozenset[str] = WATCHED_SUFFIXES) -> dict[str, int]:
    """``path -> st_mtime_ns`` of every watched file below ``roots`` (one :func:`os.scandir` walk).

    Hidden entries and :data:`IGNORED_DIRS` are skipped, symlinked directories are not followed,
    and files that vanish mid-walk are left out.
    """
    found: dict[str, int] = {}
    stack = [str(root) for root in roots]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if name not in IGNORED_DIRS:
                            stack.append(entry.path)
                        continue
                    if os.path.splitext(name)[1] not in suffixes:
                        continue
                    found[entry.path] = entry.stat().st_mtime_ns
                except OSError:
                    continue
    return found


def changed_paths(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """Paths added, removed or modified between two :func:`scan_mtimes` results, sorted."""
    changed = {path for path, mtime in after.items() if before.get(path) != mtime}
    changed.update(path for path in before if path not in after)
    return sorted(changed)


def live_reloadable(path: str | Path, live_dirs: Iterable[Path] = LIVE_DIRS) -> bool:
    """True when the running server picks ``path`` up by itself: a web template or static file."""
    resolved = Path(path)
    return resolved.suffix != ".py" and any(resolved.is_relative_to(d) for d in live_dirs)


@dataclass
class Watcher:
    """Polls ``scan`` and reports settled changes (see the module docstring).

    ``clock`` and ``scan`` are injectable so the debounce is testable without sleeping. Call
    :meth:`poll` every :data:`POLL_INTERVAL_S` (or sooner, :meth:`next_wait` says how long to
    sleep); it returns the changed paths once they have been quiet for ``debounce_s``.
    """

    scan: Callable[[], dict[str, int]]
    clock: Callable[[], float] = time.monotonic
    debounce_s: float = DEBOUNCE_S
    poll_interval_s: float = POLL_INTERVAL_S
    _state: dict[str, int] = field(default_factory=dict, init=False)
    _pending: set[str] = field(default_factory=set, init=False)
    _last_change: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._state = self.scan()

    def poll(self) -> list[str]:
        """Rescan; the settled changes (sorted), or ``[]`` while nothing changed or it is still settling."""
        current = self.scan()
        fresh = changed_paths(self._state, current)
        self._state = current
        now = self.clock()
        if fresh:
            self._pending.update(fresh)
            self._last_change = now
            return []
        quiet = now - self._last_change if self._last_change is not None else 0.0
        if self._pending and quiet >= self.debounce_s - _CLOCK_SLACK_S:
            settled = sorted(self._pending)
            self._pending.clear()
            self._last_change = None
            return settled
        return []

    def next_wait(self) -> float:
        """Seconds until the next useful :meth:`poll`: the rest of the debounce, else the interval."""
        if self._last_change is None:
            return self.poll_interval_s
        remaining = self.debounce_s - (self.clock() - self._last_change)
        return max(0.01, min(self.poll_interval_s, remaining))


# -- the reloader ----------------------------------------------------------------------------------


def _relative(path: str) -> str:
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path


def describe_change(paths: list[str]) -> str:
    """``"src/x.py"`` or ``"src/x.py (and 2 more)"``."""
    first = _relative(paths[0])
    return first if len(paths) == 1 else f"{first} (and {len(paths) - 1} more)"


def health_url(bind: str) -> str:
    """``http://<host>:<port>/healthz`` for a bind (a wildcard host is asked on loopback)."""
    from coverset.config import split_bind

    host, port = split_bind(bind)
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    netloc = f"[{host}]" if ":" in host else host
    return f"http://{netloc}:{port}/healthz"


def child_command(bind: str) -> list[str]:
    """The child's argv: this interpreter running :func:`serve_child`."""
    return [
        sys.executable,
        "-c",
        "import sys; from coverset.commands.dev import serve_child; serve_child(sys.argv[1])",
        bind,
    ]


def seed_command() -> list[str]:
    """The seeding child's argv: this interpreter running the demo's seeding without its report."""
    return [
        sys.executable,
        "-c",
        "from coverset.commands.demo import demo_settings, seed_demo_data; "
        "seed_demo_data(demo_settings('local'), report=False)",
    ]


def seed_in_child() -> None:
    """Seed the demo into the configured data directory in a child process (it inherits the
    environment and working directory, so the same settings apply).

    In this process the local executor's process pool and multiprocessing's resource tracker would
    outlive the seeding for the whole session, in the terminal's process group, and a Ctrl-C would
    then make each worker print a ``KeyboardInterrupt`` traceback; in a child they exit with it.
    Raises :class:`click.ClickException` when the child fails.
    """
    result = subprocess.run(seed_command(), check=False)  # noqa: S603 - our own interpreter and code
    if result.returncode != 0:
        raise click.ClickException(f"seeding the demo failed (exit {result.returncode})")


class Reloader:
    """Runs the server child and replaces it when the :class:`Watcher` reports a Python change."""

    def __init__(
        self,
        bind: str,
        *,
        watcher: Watcher | None = None,
        open_browser: bool = False,
        echo: Callable[[str], None] = click.echo,
        spawn: Callable[[list[str]], subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.bind = bind
        self.watcher = watcher or Watcher(scan=lambda: scan_mtimes(watch_roots()))
        self.open_browser = open_browser
        self.echo = echo
        self._spawn = spawn or (lambda argv: subprocess.Popen(argv, start_new_session=True))
        self.child: subprocess.Popen[bytes] | None = None
        self.restarts = 0
        self._reported_exit = False

    def start(self, reason: str | None = None, *, started: float | None = None) -> None:
        """Start a child and report (from a thread) when it answers ``/healthz``, timed from
        ``started`` (default now; a restart passes the moment it began stopping the old child)."""
        started = time.monotonic() if started is None else started
        self.child = self._spawn(child_command(self.bind))
        self._reported_exit = False
        first = self.restarts == 0 and reason is None
        thread = threading.Thread(target=self._announce_ready, args=(self.child, started, first), daemon=True)
        thread.start()

    def _announce_ready(self, child: subprocess.Popen[bytes], started: float, first: bool) -> None:
        url = health_url(self.bind)
        deadline = started + READY_TIMEOUT_S
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
        while time.monotonic() < deadline and child.poll() is None:
            try:
                with opener.open(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)
        else:
            return
        elapsed = time.monotonic() - started
        site = url.removesuffix("/healthz")
        if first:
            self.echo(f"coverset dev: serving on {site} (ready in {elapsed:.1f} s; Ctrl-C to stop)")
            if self.open_browser:
                webbrowser.open(site + "/")
        else:
            self.echo(f"coverset dev: ready again in {elapsed:.1f} s")

    def stop(self) -> None:
        """Stop the child: SIGTERM, then SIGKILL after :data:`STOP_TIMEOUT_S`."""
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def handle(self, paths: list[str]) -> None:
        """React to settled changes: restart for code, a note for templates and static files."""
        code = [p for p in paths if not live_reloadable(p)]
        if not code:
            self.echo(f"coverset dev: {describe_change(paths)} changed; served fresh, no restart")
            return
        self.restarts += 1
        self.echo(f"coverset dev: {describe_change(code)} changed; restarting the server")
        began = time.monotonic()
        self.stop()
        self.start(reason="change", started=began)

    def check_child(self) -> None:
        """Report a child that exited on its own (once); the next change starts a new one."""
        child = self.child
        if child is None or self._reported_exit:
            return
        code = child.poll()
        if code is not None:
            self._reported_exit = True
            self.echo(f"coverset dev: the server exited with code {code}; save a file to restart it")

    def steps(self) -> Iterator[float]:
        """One watch iteration per item (yields how long to sleep); for tests and :meth:`run`."""
        while True:
            self.check_child()
            settled = self.watcher.poll()
            if settled:
                if self.child is not None and self.child.poll() is not None:
                    self.child = None  # it died; start afresh below
                    self.restarts += 1
                    self.echo(f"coverset dev: {describe_change(settled)} changed; starting the server")
                    self.start(reason="change")
                else:
                    self.handle(settled)
            yield self.watcher.next_wait()

    def run(self) -> None:
        """Serve until Ctrl-C or SIGTERM, then stop the child."""

        def terminate(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        # SIGINT explicitly too: a shell starts background jobs with SIGINT ignored, and Python then
        # never raises KeyboardInterrupt.
        stops = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        previous = {signum: signal.signal(signum, terminate) for signum in stops}
        try:
            self.start()
            for wait in self.steps():
                time.sleep(wait)
        except KeyboardInterrupt:
            self.echo("coverset dev: stopping")
        finally:
            with contextlib.suppress(KeyboardInterrupt):
                self.stop()
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run_reloader(bind: str, *, open_browser: bool = False) -> None:
    """Watch the sources and serve on ``bind`` in a child process, restarting it on code changes."""
    roots = watch_roots()
    click.echo(
        "coverset dev: watching "
        + ", ".join(_relative(str(root)) for root in roots)
        + f" (every {POLL_INTERVAL_S:g} s; templates and static files reload without a restart)"
    )
    Reloader(bind, open_browser=open_browser).run()


# -- the child -------------------------------------------------------------------------------------


def enable_dev_reload(app: Quart) -> None:
    """Re-read changed templates on the next request (Quart's flag and the live Jinja environment,
    which may already exist when the flag is set)."""
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True


def serve_child(bind: str) -> None:
    """The server process of the reloader: ``coverset serve`` with template auto-reload."""
    from coverset.app import create_app
    from coverset.cli import load_settings

    try:
        settings = load_settings(bind=bind)
    except click.ClickException as exc:
        click.echo(f"Error: {exc.format_message()}", err=True)
        raise SystemExit(2) from None
    app = create_app(settings)
    enable_dev_reload(app)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve_child(app, settings.bind))


async def _serve_child(app: Quart, bind: str) -> None:
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config as HypercornConfig

    config = HypercornConfig()
    config.bind = [bind]
    config.accesslog = logging.getLogger("hypercorn.access")
    config.errorlog = logging.getLogger("hypercorn.error")
    config.graceful_timeout = CHILD_GRACEFUL_TIMEOUT_S
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, shutdown.set)
    watchdog = loop.create_task(_exit_with_parent(shutdown, os.getppid()))
    try:
        await hypercorn_serve(app, config, shutdown_trigger=shutdown.wait)
    finally:
        watchdog.cancel()


async def _exit_with_parent(shutdown: asyncio.Event, parent: int) -> None:
    """Shut down when the reloader is gone (it was killed, so nobody else will stop this server)."""
    while not shutdown.is_set():
        await asyncio.sleep(PARENT_CHECK_S)
        if os.getppid() != parent:
            log.warning("coverset dev: the reloader exited; stopping the server")
            shutdown.set()


# -- the command -----------------------------------------------------------------------------------


def data_dir_has_repository(settings: Settings) -> bool:
    """True when the database exists and holds at least one repository (the demo is then not seeded)."""
    from sqlalchemy import select

    from coverset.db.models import Repo
    from coverset.db.session import Database, sqlite_file_path

    database_file = sqlite_file_path(settings.database_url)
    if database_file is not None and not database_file.is_file():
        return False

    async def probe() -> bool:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                return await session.scalar(select(Repo.id).limit(1)) is not None
        except Exception:  # noqa: BLE001 - no schema yet: treat as empty
            return False
        finally:
            await database.engine.dispose()

    return asyncio.run(probe())


@click.command("dev")
@click.option(
    "--bind",
    default=DEFAULT_DEV_BIND,
    show_default=True,
    help="host:port to serve on (a loopback address unless COVERSET_ADMIN_TOKEN is set).",
)
@click.option("--no-seed", is_flag=True, help="Do not seed the demo, even into an empty data directory.")
@click.option("--open/--no-open", "open_browser", default=False, help="Open the browser once serving.")
def dev_command(bind: str, no_seed: bool, open_browser: bool) -> None:
    """Seed the demo (first time only) and serve with reload: the local development loop.

    Python changes under src/coverset and packages/*/src restart the server; template and static
    file changes are served fresh without one. See docs/dev.md.
    """
    from coverset.cli import load_settings

    settings = load_settings(bind=bind)
    click.echo(f"coverset dev: data directory {settings.data_dir}")
    if settings.dev_login:
        from coverset.web.account import dev_login_problems

        problems = dev_login_problems(settings)
        state = "off: " + "; ".join(problems) if problems else f"on at {settings.public_url}/login"
        click.echo(f"coverset dev: development sign-in {state}")
    if no_seed:
        click.echo("coverset dev: --no-seed, not seeding")
    elif data_dir_has_repository(settings):
        click.echo("coverset dev: the data directory has repositories already, not seeding")
    else:
        click.echo("coverset dev: no repository yet, seeding the demo (about 25 s, once)")
        started = time.monotonic()
        seed_in_child()
        click.echo(f"coverset dev: demo seeded in {time.monotonic() - started:.1f} s")
    run_reloader(settings.bind, open_browser=open_browser)
