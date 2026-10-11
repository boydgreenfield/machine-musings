"""Helpers shared by the click commands: settings, the database, repo lookup and stream printing."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import click
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import Repo
from coverset.db.session import init_db

__all__ = [
    "StreamPrinter",
    "find_repo",
    "load",
    "prepare_db",
    "resolve_repo",
    "run_async",
]


def load(**overrides: Any) -> Settings:
    """Settings from the environment and ``.env`` plus ``overrides`` (a clean CLI error if invalid)."""
    from coverset.cli import load_settings

    clean = {k: v for k, v in overrides.items() if v is not None}
    return load_settings(**clean)


SIGTERM_EXIT_CODE = 128 + signal.SIGTERM
"""How a command stopped by SIGTERM exits (the shell convention)."""


def run_async[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to completion, turning domain errors into click errors.

    SIGTERM (and SIGINT, which ``asyncio.run`` already handles the same way) cancels the coroutine
    instead of killing the process, so its ``finally`` blocks run: inline runs are finalized and
    their Modal sandboxes terminated. The command then exits with 143.
    """
    received: list[int] = []
    try:
        return asyncio.run(_call(factory, received))
    except asyncio.CancelledError:
        if received:
            raise SystemExit(SIGTERM_EXIT_CODE) from None
        raise
    except (LookupError, ValueError, RuntimeError) as exc:
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc


async def _call[T](factory: Callable[[], Awaitable[T]], received: list[int]) -> T:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed: list[int] = []

    def stop(signum: int) -> None:
        received.append(signum)
        if task is not None:
            task.cancel()

    signals = [signal.SIGTERM]
    if signal.getsignal(signal.SIGINT) in (signal.SIG_DFL, signal.SIG_IGN):
        signals.append(signal.SIGINT)  # asyncio.run did not take SIGINT over: do it here
    for signum in signals:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signum, stop, signum)
            installed.append(signum)
    try:
        return await factory()
    finally:
        for signum in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signum)


async def prepare_db(settings: Settings) -> None:
    """Create the data directory and migrate the database (idempotent)."""
    await init_db(settings)


async def find_repo(session: AsyncSession, spec: str | None) -> Repo:
    """A known repo by id, ``full_name`` or local source path; the only repo when ``spec`` is None."""
    if spec is None:
        repos = list(await session.scalars(select(Repo).order_by(Repo.id)))
        if len(repos) == 1:
            return repos[0]
        names = ", ".join(r.full_name for r in repos) or "none"
        raise click.ClickException(f"pass --repo (known repositories: {names})")
    if spec.isdigit():
        found = await session.get(Repo, int(spec))
        if found is not None:
            return found
    found = await session.scalar(select(Repo).where(Repo.full_name == spec))
    if found is not None:
        return found
    path = Path(spec).expanduser()
    if path.exists():
        resolved = str(path.resolve())
        for repo in await session.scalars(select(Repo)):
            if (repo.settings_json or {}).get("source") == resolved:
                return repo
    raise click.ClickException(f"unknown repository {spec!r}")


async def resolve_repo(session: AsyncSession, spec: str) -> Repo:
    """A repo for ``coverset run --repo``: a local path or URL (registered on first use) or a known repo."""
    from coverset.orchestrator.repos import register_local_repo

    path = Path(spec).expanduser()
    if path.is_dir():
        known = await _by_source(session, str(path.resolve()))
        return known or await register_local_repo(session, path)
    if "://" in spec:
        return await register_local_repo(session, spec)
    return await find_repo(session, spec)


async def _by_source(session: AsyncSession, source: str) -> Repo | None:
    for repo in await session.scalars(select(Repo).order_by(Repo.id)):
        if (repo.settings_json or {}).get("source") == source:
            return repo
    return None


class StreamPrinter:
    """Prints a run's events as a pytest-style stream (``on_event`` for :func:`execute_run`)."""

    def __init__(self, *, verbose: bool = False, quiet: bool = False) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self.done: dict[str, Any] | None = None

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        line = self.format(kind, payload)
        if kind == "done":
            self.done = payload
        if line is not None and not (self.quiet and kind in ("test", "log", "shard", "progress")):
            click.echo(line)

    def format(self, kind: str, payload: dict[str, Any]) -> str | None:
        """One output line for an event, or None to stay silent."""
        if kind == "test":
            outcome = str(payload.get("outcome", "?")).upper()
            flaky = " (flaky: passed on retry)" if payload.get("flaky") else ""
            return (
                f"{outcome:<8} {payload.get('nodeid')} [{float(payload.get('duration_s') or 0):.2f}s"
                f", shard {payload.get('shard')}]{flaky}"
            )
        if kind == "failure":
            tag = "pre-existing on base" if payload.get("preexisting") else "confirmed"
            return f"FAILURE  {payload.get('nodeid')} {tag} (rank {payload.get('rank')})"
        if kind == "status" and payload.get("status") == "planned":
            return (
                f"planned  {payload.get('tests')} tests in {payload.get('shards')} shard(s); "
                f"wave 1: {payload.get('wave1')} test(s); prior: {payload.get('prior') or 'none'}"
            )
        if kind == "verdict":
            early = " (first confirmed failure)" if payload.get("early") else ""
            return f"verdict  {payload.get('label') or payload.get('conclusion')}{early}: {payload.get('reason')}"
        if kind == "done":
            counts = payload.get("counts") or {}
            parts = [f"{n} {k}" for k, n in counts.items() if n]
            return f"== {', '.join(parts) or 'no tests'} == exit {payload.get('exit_code')}"
        if kind == "log" and self.verbose:
            return f"  [shard {payload.get('shard')}] {payload.get('text')}"
        if kind == "shard" and self.verbose:
            return f"shard {payload.get('shard')}: {payload.get('status')}"
        return None
