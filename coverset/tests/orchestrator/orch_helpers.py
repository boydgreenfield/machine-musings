"""Run helpers shared by the orchestrator tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from coverset.db.models import Repo, Run, RunEvent
from coverset.orchestrator.runner import OrchestratorContext, create_run, execute_run


async def run_baseline(ctx: OrchestratorContext, repo_id: int, sha: str, **kwargs: Any) -> str:
    """Create and execute a baseline run; return its id."""
    run_id = await create_run(
        ctx, repo_id=repo_id, head_sha=sha, base_sha=None, branch="main", trigger="baseline", **kwargs
    )
    await execute_run(ctx, run_id)
    return run_id


async def run_pr(
    ctx: OrchestratorContext,
    repo_id: int,
    head: str,
    base: str,
    *,
    number: int = 1,
    shards: int | None = None,
    **kwargs: Any,
) -> str:
    """Create and execute a pull-request run; return its id."""
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=head,
        base_sha=base,
        branch=f"pr-{number}",
        trigger="pull_request",
        pr_number=number,
        pr_title="A change",
        base_ref="main",
        **kwargs,
    )
    await execute_run(ctx, run_id, shards=shards)
    return run_id


async def load_run(ctx: OrchestratorContext, run_id: str) -> Run:
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run


async def events(ctx: OrchestratorContext, run_id: str) -> list[RunEvent]:
    async with ctx.db.session() as session:
        rows = await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))
        return list(rows)


async def repo_row(ctx: OrchestratorContext, repo_id: int) -> Repo:
    async with ctx.db.session() as session:
        row = await session.get(Repo, repo_id)
        assert row is not None
        return row
