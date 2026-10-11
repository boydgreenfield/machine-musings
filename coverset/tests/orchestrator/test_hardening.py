"""Hardening: the in-progress check, tracer warnings, untrusted priors, webhook-run gating and gc."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from orch_fakes import Script, TestRepo, pr_change
from orch_helpers import events, load_run, run_baseline, run_pr
from sqlalchemy import func, select, update

from coverset.app import create_app, schedule_gc
from coverset.config import Settings
from coverset.db.models import PullRequest, Run, RunEvent, TestStats, WebhookDelivery
from coverset.forge.fake import FakeForge
from coverset.forge.verdict import CHECK_NAME
from coverset.orchestrator import history
from coverset.orchestrator.runner import OrchestratorContext, create_run, execute_run, tracer_warning

# -- the verdict check goes in_progress when the run starts ---------------------------------------


async def test_pr_run_check_is_in_progress_before_it_completes(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    forge = ctx.forge
    assert isinstance(forge, FakeForge)
    assert forge.calls_named("create_check") == []  # baseline runs have no PR, so no check
    run_id = await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha)
    run = await load_run(ctx, run_id)
    checks = [c for c in forge.calls if c.method in ("create_check", "update_check")]
    assert [c.method for c in checks] == ["create_check", "update_check"]
    create, complete = checks
    assert create.request["status"] == "in_progress" and create.request["conclusion"] is None
    assert create.request["name"] == CHECK_NAME and create.request["external_id"] == run_id
    assert create.request["details_url"].endswith(f"/runs/{run_id}")
    assert create.run_id == run_id
    check_id = create.response["id"]
    assert run.check_run_id == check_id
    assert complete.request["check_id"] == check_id and complete.request["conclusion"] == "success"
    assert forge.checks[check_id].status == "completed"


async def test_failed_pr_run_completes_the_started_check(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    script.zero_passes = False
    run_id = await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha)
    forge = ctx.forge
    assert isinstance(forge, FakeForge)
    creates = forge.calls_named("create_check")
    assert len(creates) == 1 and creates[0].request["status"] == "in_progress"
    updates = forge.calls_named("update_check")
    check_id = creates[0].response["id"]
    assert updates and {u.request["check_id"] for u in updates} == {check_id}
    assert updates[-1].request["conclusion"] == "failure"
    assert (await load_run(ctx, run_id)).check_run_id == check_id


async def test_start_check_failure_never_fails_the_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker, fail_on={"create_check"})
    run_id = await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha)
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict) == ("completed", "success")
    assert run.check_run_id is None


# -- tracer mismatch ------------------------------------------------------------------------------


async def test_tracer_mismatch_becomes_a_log_event(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    script.meta = {"tracer": "PyTracer"}
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    warnings = [e.payload for e in await events(ctx, run_id) if e.kind == "log" and e.payload.get("warning")]
    assert warnings and warnings[0] == {
        "shard": 0,
        "text": "coverage was collected with PyTracer; per-test contexts may be incomplete",
        "warning": "tracer_mismatch",
    }
    logs = ctx.store.read_table(repo_id, run_id, "ingest_log", shard_id=0).to_pylist()
    assert '"tracer_mismatch": true' in logs[-1]["convert_json"]
    assert (await load_run(ctx, run_id)).verdict == "success"


async def test_c_tracer_gives_no_warning(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    assert not [e for e in await events(ctx, run_id) if e.kind == "log" and e.payload.get("warning")]


def test_tracer_warning_reads_the_convert_record() -> None:
    assert tracer_warning({}) is None
    assert tracer_warning({"tracer": "PyTracer"}) is None  # only the converter's verdict counts
    assert tracer_warning({"convert": {"tracer_mismatch": False}}) is None
    text = tracer_warning({"convert": {"tracer_mismatch": True, "tracer": "SysMonitor"}})
    assert text == "coverage was collected with SysMonitor; per-test contexts may be incomplete"


# -- untrusted runs and webhook runs --------------------------------------------------------------


async def _stats_rows(ctx: OrchestratorContext) -> int:
    async with ctx.db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(TestStats)) or 0)


@pytest.mark.security
async def test_untrusted_runs_never_update_test_stats(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=repo.main_sha,
        base_sha=None,
        branch="pr-9",
        trigger="pull_request",
        executor="modal",  # untrusted runs never use the local executor; the fake serves both
        pr_number=9,
        trusted=False,
    )
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion) == ("completed", "success")
    assert await _stats_rows(ctx) == 0
    await run_baseline(ctx, repo_id, repo.main_sha)
    assert await _stats_rows(ctx) > 0  # the same tests from a trusted run do update the priors


async def _webhook_run(ctx: OrchestratorContext, repo: TestRepo, repo_id: int, *, via: str) -> str:
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=repo.main_sha,
        base_sha=None,
        branch="main",
        trigger="push",
    )
    async with ctx.db.session() as session:
        if via == "timeline":
            run = await session.get(Run, run_id)
            assert run is not None
            run.timeline = {**run.timeline, "webhook_received": run.timeline["queued"]}
        else:
            session.add(WebhookDelivery(delivery_id=f"d-{run_id}", forge="fake", event="push", run_id=run_id))
        await session.commit()
    return run_id


@pytest.mark.security
@pytest.mark.parametrize("via", ["timeline", "delivery"])
async def test_local_executor_refuses_webhook_runs_without_the_flag(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, via: str
) -> None:
    assert not ctx.settings.local_allow_webhook_runs
    run_id = await _webhook_run(ctx, repo, repo_id, via=via)
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion) == ("skipped", "neutral")
    assert outcome.reason == "webhook runs need COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true locally"
    assert not script.commands  # nothing ran


@pytest.mark.security
async def test_local_executor_runs_webhook_runs_with_the_flag(
    ctx: OrchestratorContext, settings: Settings, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    ctx.settings = settings.model_copy(update={"local_allow_webhook_runs": True})
    run_id = await _webhook_run(ctx, repo, repo_id, via="delivery")
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion) == ("completed", "success")


# -- retention ------------------------------------------------------------------------------------


async def _old_pr_runs(
    ctx: OrchestratorContext, repo_id: int, sha: str, *, days: list[int]
) -> tuple[int, list[str]]:
    """A PR with one completed run created ``days`` ago each (oldest first); facts dirs for each."""
    now = ctx.settings.now()
    async with ctx.db.session() as session:
        pr = PullRequest(repo_id=repo_id, number=42, head_sha=sha, base_sha=sha, title="old", state="open")
        session.add(pr)
        await session.flush()
        ids = []
        for age in days:
            run = Run(
                repo_id=repo_id,
                head_sha=sha,
                branch="pr-42",
                pr_id=pr.id,
                trigger="pull_request",
                status="completed",
                created_at=now - timedelta(days=age),
            )
            session.add(run)
            await session.flush()
            ids.append(run.id)
            ctx.store.run_dir(repo_id, run.id).mkdir(parents=True)
        await session.commit()
        return pr.id, ids


async def _run_ids(ctx: OrchestratorContext) -> set[str]:
    async with ctx.db.session() as session:
        return set(await session.scalars(select(Run.id)))


async def test_expire_pr_runs_keeps_the_latest_and_recent_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    pr_id, (oldest, old, recent, latest) = await _old_pr_runs(
        ctx, repo_id, repo.main_sha, days=[90, 60, 3, 1]
    )
    assert await history.expire_pr_runs(ctx.db, ctx.settings, ctx.store, pr_id, dry_run=True) == sorted(
        [oldest, old]
    )
    assert {oldest, old} <= await _run_ids(ctx)
    deleted = await history.expire_pr_runs(ctx.db, ctx.settings, ctx.store, pr_id)
    assert deleted == sorted([oldest, old])
    assert await _run_ids(ctx) >= {recent, latest} and not {oldest, old} & await _run_ids(ctx)
    assert not ctx.store.run_dir(repo_id, oldest).exists() and ctx.store.run_dir(repo_id, latest).is_dir()


async def test_expire_pr_runs_never_deletes_the_only_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    pr_id, (only,) = await _old_pr_runs(ctx, repo_id, repo.main_sha, days=[90])
    assert await history.expire_pr_runs(ctx.db, ctx.settings, ctx.store, pr_id) == []
    assert only in await _run_ids(ctx)


async def test_gc_lock_makes_a_concurrent_gc_skip(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    pr_id, _ = await _old_pr_runs(ctx, repo_id, repo.main_sha, days=[90, 1])
    with history.GC_LOCK:
        report = await history.collect_garbage(ctx.db, ctx.settings, ctx.store)
        assert report.skipped and report.lines(ctx.settings) == [
            "skipped: another gc is running in this process"
        ]
        assert await history.expire_pr_runs(ctx.db, ctx.settings, ctx.store, pr_id) == []
    assert len(await _run_ids(ctx)) == 2


async def test_collect_garbage_applies_retention(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    _, (old, latest) = await _old_pr_runs(ctx, repo_id, repo.main_sha, days=[60, 50])
    stale = ctx.settings.now() - timedelta(days=60)
    os.utime(ctx.store.run_dir(repo_id, old), (stale.timestamp(), stale.timestamp()))
    async with ctx.db.session() as session:
        session.add(RunEvent(run_id=latest, seq=1, kind="log", payload={"text": "x"}, at=stale))
        session.add(
            WebhookDelivery(
                delivery_id="d-old", forge="fake", event="push", payload={"a": 1}, received_at=stale
            )
        )
        await session.commit()
    dry = await history.collect_garbage(ctx.db, ctx.settings, ctx.store, dry_run=True)
    assert (dry.expired_db, dry.expired_facts, dry.events, dry.payloads, dry.deliveries) == (
        [old],
        [old],
        1,
        1,
        1,
    )
    assert latest in dry.kept and old in await _run_ids(ctx)
    report = await history.collect_garbage(ctx.db, ctx.settings, ctx.store)
    assert report.expired_db == [old] and report.expired_facts == [old]
    assert await _run_ids(ctx) == {latest}  # the latest run of an open PR always stays
    assert not ctx.store.run_dir(repo_id, old).exists()
    async with ctx.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(RunEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0
    assert report.lines(ctx.settings)[0] == "deleted 1 run(s) from the database and 1 run(s) of facts"


async def test_gc_forever_runs_at_once_and_stops_on_the_event(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    _, (old, _latest) = await _old_pr_runs(ctx, repo_id, repo.main_sha, days=[60, 50])
    stop = asyncio.Event()
    task = asyncio.create_task(history.gc_forever(ctx.db, ctx.settings, ctx.store, stop, interval_s=3600))
    for _ in range(200):
        if old not in await _run_ids(ctx):
            break
        await asyncio.sleep(0.02)
    assert old not in await _run_ids(ctx)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_schedule_gc_runs_when_the_app_serves(
    ctx: OrchestratorContext, settings: Settings, repo: TestRepo, repo_id: int
) -> None:
    _, (old, latest) = await _old_pr_runs(ctx, repo_id, repo.main_sha, days=[60, 50])
    app = create_app(settings)
    assert settings.env == "test"  # tests never schedule gc implicitly...
    schedule_gc(app, interval_s=3600)  # ...only when asked
    async with app.test_app():
        for _ in range(200):
            if old not in await _run_ids(ctx):
                break
            await asyncio.sleep(0.02)
    assert await _run_ids(ctx) == {latest}


async def test_runs_are_not_garbage_collected_in_tests(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    first = await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha)
    async with ctx.db.session() as session:
        await session.execute(
            update(Run).where(Run.id == first).values(created_at=ctx.settings.now() - timedelta(days=90))
        )
        await session.commit()
    await run_pr(ctx, repo_id, pr_change(repo, body="return a + b + 0", hour=2), repo.main_sha)
    assert first in await _run_ids(ctx)  # env=test: the per-PR rule runs only when invoked


# -- coverset shapes --branch ----------------------------------------------------------------------


async def test_shapes_command_defaults_to_baseline_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    import click

    from coverset.commands.facts import _shapes

    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha)
    baseline = await _shapes(ctx.settings, "classify", None, 5, False, False)
    assert "over the last 5 runs on branch main" in baseline.splitlines()[0]
    pooled = await _shapes(ctx.settings, "classify", None, 5, False, False, branch="any")
    assert "on every branch" in pooled.splitlines()[0]
    markdown = await _shapes(ctx.settings, "classify", None, 5, True, False, branch="main")
    assert markdown == await _shapes(ctx.settings, "classify", None, 5, True, False)
    assert markdown.startswith("| Input class | Observed |")
    with pytest.raises(click.ClickException, match="no completed run with facts on branch nope"):
        await _shapes(ctx.settings, "classify", None, 5, False, False, branch="nope")
