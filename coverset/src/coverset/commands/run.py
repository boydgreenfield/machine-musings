"""``coverset run`` (one run, executed inline) and ``coverset warm`` (build the environment ahead)."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import click

from coverset.commands.common import StreamPrinter, load, prepare_db, resolve_repo, run_async
from coverset.config import Settings

__all__ = ["run_command", "warm_command"]


@click.command("run")
@click.option(
    "--repo", "repo_spec", required=True, help="Local path or URL of a git repository (or a known repo)."
)
@click.option("--head", required=True, help="Commit SHA or branch to test.")
@click.option("--base", default=None, help="Base SHA or branch (runs the change base...head).")
@click.option("--pr", "pr_number", type=int, default=None, help="Pull request number to attach the run to.")
@click.option("--executor", type=click.Choice(["local", "modal"]), default=None, help="Where shards run.")
@click.option("--shards", type=click.IntRange(1, 256), default=None, help="Force the number of shards.")
@click.option(
    "--trigger",
    type=click.Choice(["pr", "baseline"]),
    default=None,
    help="pr (needs --base) or baseline; default: pr when --base is given.",
)
@click.option("--wait/--no-wait", default=True, help="Execute inline (default) or only queue the run.")
@click.option("-v", "--verbose", is_flag=True, help="Also print the shards' own output.")
def run_command(
    repo_spec: str,
    head: str,
    base: str | None,
    pr_number: int | None,
    executor: str | None,
    shards: int | None,
    trigger: str | None,
    wait: bool,
    verbose: bool,
) -> None:
    """Run a repository's tests for HEAD (or the change BASE...HEAD) and print the verdict."""
    if executor == "modal":
        _require_modal()
    settings = load(executor=executor)
    if settings.paused and wait:
        raise click.ClickException("COVERSET_PAUSED is set: no runs start")
    kind = trigger or ("pr" if base else "baseline")
    if kind == "pr" and not base:
        raise click.ClickException("--trigger pr needs --base")
    printer = StreamPrinter(verbose=verbose)
    exit_code = run_async(
        lambda: _run(settings, repo_spec, head, base, pr_number, shards, kind, wait, printer)
    )
    sys.exit(exit_code)


def _require_modal() -> None:
    try:
        import coverset.executors.modal_  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(f"the Modal executor is not available in this build: {exc}") from None


async def _run(
    settings: Settings,
    repo_spec: str,
    head: str,
    base: str | None,
    pr_number: int | None,
    shards: int | None,
    kind: str,
    wait: bool,
    printer: StreamPrinter,
) -> int:
    from coverset.orchestrator.repos import ensure_mirror_async, resolve_commit
    from coverset.orchestrator.runner import OrchestratorContext, create_run, execute_run

    await prepare_db(settings)
    ctx = OrchestratorContext.from_settings(settings)
    try:
        async with ctx.db.session() as session:
            repo = await resolve_repo(session, repo_spec)
            await session.commit()
        mirror = await ensure_mirror_async(settings, repo)
        head_sha = await asyncio.to_thread(resolve_commit, mirror, head)
        base_sha = await asyncio.to_thread(resolve_commit, mirror, base) if base else None
        branch = head if not _is_sha(head) else (repo.default_branch if kind == "baseline" else None)
        run_id = await create_run(
            ctx,
            repo_id=repo.id,
            head_sha=head_sha,
            base_sha=base_sha,
            branch=branch,
            trigger="pull_request" if kind == "pr" else "baseline",
            executor=settings.executor,
            pr_number=pr_number,
            base_ref=base if base and not _is_sha(base) else None,
        )
        click.echo(
            f"run {run_id}: {repo.full_name} {head_sha[:12]}" + (f" vs {base_sha[:12]}" if base_sha else "")
        )
        if not wait:
            click.echo("queued (a running `coverset serve` picks it up)")
            return 0
        outcome = await execute_run(ctx, run_id, on_event=printer, shards=shards)
        click.echo(f"{outcome.status}: {outcome.conclusion} - {outcome.reason}")
        click.echo(f"details: {settings.public_url}/runs/{run_id}")
        if printer.done is not None:
            return int(printer.done.get("exit_code", 2))
        return (
            0 if outcome.conclusion in ("success", "neutral") else 1 if outcome.conclusion == "failure" else 2
        )
    finally:
        await ctx.close()


def _is_sha(ref: str) -> bool:
    return len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)


@click.command("warm")
@click.option(
    "--repo", "repo_spec", required=True, help="Local path or URL of a git repository (or a known repo)."
)
@click.option("--branch", default=None, help="Branch to warm (default: the repo's default branch).")
@click.option(
    "--executor", type=click.Choice(["local", "modal"]), default=None, help="Which executor to warm."
)
def warm_command(repo_spec: str, branch: str | None, executor: str | None) -> None:
    """Build the test environment (and, on Modal, a trusted snapshot) for a branch head."""
    if executor == "modal":
        _require_modal()
    settings = load(executor=executor)
    run_async(lambda: _warm(settings, repo_spec, branch))


async def _warm(settings: Settings, repo_spec: str, branch: str | None) -> None:
    from coverset.executors import get_executor
    from coverset.executors.env_export import build_env_spec, build_plugin_wheel
    from coverset.lang.python import PythonAdapter
    from coverset.orchestrator.repos import archive, ensure_mirror_async, extract_archive, resolve_commit
    from coverset.orchestrator.runner import OrchestratorContext

    await prepare_db(settings)
    ctx = OrchestratorContext.from_settings(settings)
    checkout = Path(tempfile.mkdtemp(prefix="coverset-warm-"))
    try:
        async with ctx.db.session() as session:
            repo = await resolve_repo(session, repo_spec)
            await session.commit()
        mirror = await ensure_mirror_async(settings, repo)
        sha = await asyncio.to_thread(resolve_commit, mirror, branch or repo.default_branch)
        await asyncio.to_thread(extract_archive, await asyncio.to_thread(archive, mirror, sha), checkout)
        adapter = PythonAdapter()
        config = adapter.load_config(checkout)
        wheel = await asyncio.to_thread(build_plugin_wheel)
        spec = await asyncio.to_thread(build_env_spec, adapter, checkout, {"python": config.python}, wheel)
        executor = get_executor(settings)
        env = await executor.ensure_env(spec)
        click.echo(f"environment {env.env_hash[:16]} (python {env.python}) ready at {env.location}")
        snapshot = await executor.ensure_snapshot(env, repo.full_name, sha)
        if snapshot is not None:
            click.echo(f"trusted snapshot of {sha[:12]}: {snapshot.image_id}")
        await executor.close()
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
        await ctx.close()
