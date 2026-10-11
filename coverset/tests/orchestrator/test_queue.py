"""The run queue: concurrency cap, the paused kill switch, boot handling, the budget guard."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from orch_fakes import Script, TestRepo
from orch_helpers import events, load_run
from sqlalchemy import select

from coverset.db.models import Run
from coverset.orchestrator import budget
from coverset.orchestrator import queue as queue_module
from coverset.orchestrator.queue import RunDispatcher, install_run_queue
from coverset.orchestrator.runner import OrchestratorContext, RunOutcome, create_run


async def _queued(ctx: OrchestratorContext, repo_id: int, sha: str, n: int) -> list[str]:
    return [
        await create_run(ctx, repo_id=repo_id, head_sha=sha, base_sha=None, branch="main", trigger="baseline")
        for _ in range(n)
    ]


async def test_concurrency_cap(
    monkeypatch: pytest.MonkeyPatch, ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    running = 0
    peak = 0

    async def fake_execute(context: OrchestratorContext, run_id: str) -> RunOutcome:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        async with context.db.session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            run.status = "completed"
            await session.commit()
        running -= 1
        return RunOutcome(run_id, "completed", "success", "ok")

    monkeypatch.setattr(queue_module, "execute_run", fake_execute)
    ids = await _queued(ctx, repo_id, repo.main_sha, 5)
    dispatcher = RunDispatcher(ctx, max_concurrent=2)
    outcomes = await dispatcher.drain()
    assert sorted(o.run_id for o in outcomes) == sorted(ids)
    assert peak == 2


async def test_paused_dispatches_nothing(ctx: OrchestratorContext, repo: TestRepo, repo_id: int) -> None:
    await _queued(ctx, repo_id, repo.main_sha, 2)
    ctx.settings = ctx.settings.model_copy(update={"paused": True})
    dispatcher = RunDispatcher(ctx)
    assert await dispatcher.dispatch() == []
    assert await dispatcher.run_once() == []
    async with ctx.db.session() as session:
        statuses = set(await session.scalars(select(Run.status)))
    assert statuses == {"queued"}


async def test_boot_abandons_leftovers(ctx: OrchestratorContext, repo: TestRepo, repo_id: int) -> None:
    queued, running = await _queued(ctx, repo_id, repo.main_sha, 2)
    async with ctx.db.session() as session:
        row = await session.get(Run, running)
        assert row is not None
        row.status = "running"
        await session.commit()
    touched = await RunDispatcher(ctx).on_boot()
    assert sorted(touched) == sorted([queued, running])
    for run_id in (queued, running):
        run = await load_run(ctx, run_id)
        assert run.status == "abandoned" and run.verdict == "neutral"
        log = await events(ctx, run_id)
        assert [e.kind for e in log][-2:] == ["verdict", "done"]


async def test_requeue_on_boot(ctx: OrchestratorContext, repo: TestRepo, repo_id: int) -> None:
    (run_id,) = await _queued(ctx, repo_id, repo.main_sha, 1)
    async with ctx.db.session() as session:
        row = await session.get(Run, run_id)
        assert row is not None
        row.status = "running"
        await session.commit()
    ctx.settings = ctx.settings.model_copy(update={"requeue_on_boot": True})
    await RunDispatcher(ctx).on_boot()
    old = await load_run(ctx, run_id)
    assert old.status == "abandoned" and "re-queued as run" in (old.reason or "")
    assert [e.kind for e in await events(ctx, run_id)][-2:] == ["verdict", "done"]
    async with ctx.db.session() as session:
        (fresh,) = list(await session.scalars(select(Run).where(Run.status == "queued")))
    assert fresh.id != run_id and fresh.timeline["requeued_from"] == run_id
    assert (fresh.head_sha, fresh.trigger, fresh.extra_env) == (old.head_sha, old.trigger, old.extra_env)


async def test_dispatcher_runs_real_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    ids = await _queued(ctx, repo_id, repo.main_sha, 2)
    outcomes = await RunDispatcher(ctx, max_concurrent=1).drain()
    assert sorted(o.run_id for o in outcomes) == sorted(ids)
    assert {o.status for o in outcomes} == {"completed"}


async def test_install_on_app_starts_and_stops(
    settings, ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:  # type: ignore[no-untyped-def]
    from coverset.app import create_app

    script.zero_passes = True
    app = create_app(settings)
    dispatcher = install_run_queue(app, ctx=ctx)
    assert app.extensions["run_queue"] is dispatcher
    async with app.test_app():
        assert dispatcher.started
        (run_id,) = await _queued(ctx, repo_id, repo.main_sha, 1)
        dispatcher.enqueue(run_id)
        for _ in range(200):
            if (await load_run(ctx, run_id)).status == "completed":
                break
            await asyncio.sleep(0.05)
    assert (await load_run(ctx, run_id)).status == "completed"
    assert not dispatcher.started


async def test_budget_counts_only_todays_modal_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    now = ctx.settings.now()
    async with ctx.db.session() as session:
        session.add_all(
            [
                Run(
                    repo_id=repo_id,
                    head_sha=repo.main_sha,
                    trigger="baseline",
                    executor="modal",
                    cost_estimate=3.0,
                    created_at=now,
                ),
                Run(
                    repo_id=repo_id,
                    head_sha=repo.main_sha,
                    trigger="baseline",
                    executor="modal",
                    cost_estimate=4.0,
                    created_at=now - timedelta(days=2),
                ),
                Run(
                    repo_id=repo_id,
                    head_sha=repo.main_sha,
                    trigger="baseline",
                    executor="local",
                    cost_estimate=9.0,
                    created_at=now,
                ),
            ]
        )
        await session.commit()
        assert await budget.spent_today(session, ctx.settings) == pytest.approx(3.0)
        assert not await budget.budget_exhausted(session, ctx.settings, "modal")
        assert not await budget.budget_exhausted(session, ctx.settings, "local")
        tight = ctx.settings.model_copy(update={"modal_daily_budget_usd": 2.0})
        assert await budget.budget_exhausted(session, tight, "modal")


async def test_modal_run_over_budget_is_neutral(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    from coverset.orchestrator.runner import execute_run

    ctx.settings = ctx.settings.model_copy(update={"modal_daily_budget_usd": 0.0})
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=repo.main_sha,
        base_sha=None,
        branch="main",
        trigger="baseline",
        executor="modal",
    )
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion, outcome.reason) == (
        "completed",
        "neutral",
        "budget exhausted",
    )
