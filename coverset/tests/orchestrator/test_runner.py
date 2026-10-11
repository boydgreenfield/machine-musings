"""The runner: streaming, retries and flaky tests, fail-fast, cancellation, ingest and the verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime

import pytest
from orch_fakes import FAIL_SIG, NODEIDS, FakeExecutor, Script, TestRepo, make_repo, pr_change
from orch_helpers import events, load_run, run_baseline, run_pr
from sqlalchemy import select

from coverset.db.models import ForgeCall, Shard, TestAttempt, TestCase, TestResult, TestStats
from coverset.facts.queries import patch_coverage
from coverset.orchestrator.runner import OrchestratorContext, create_run, execute_run, retry_env

ZERO = "tests/test_calc.py::test_zero_fails"


async def results(ctx: OrchestratorContext, run_id: str) -> dict[str, TestResult]:
    async with ctx.db.session() as session:
        rows = await session.execute(
            select(TestCase.nodeid, TestResult)
            .join(TestCase, TestCase.id == TestResult.test_id)
            .where(TestResult.run_id == run_id)
        )
        return {nodeid: row for nodeid, row in rows}


async def test_green_baseline_streams_events_and_ingests(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict) == ("completed", "success")
    assert run.coverage_complete
    for key in ("queued", "sandbox_ready", "first_test", "wave1_finished", "verdict", "finished"):
        assert key in run.timeline, key
    assert "first_failure" not in run.timeline
    kinds = [e.kind for e in await events(ctx, run_id)]
    assert kinds.count("test") == len(NODEIDS)
    assert {"status", "progress", "verdict", "done"} <= set(kinds)
    assert kinds[-1] == "done"
    seqs = [e.seq for e in await events(ctx, run_id)]
    assert seqs == sorted(set(seqs))
    rows = await results(ctx, run_id)
    assert {r.outcome for r in rows.values()} == {"passed"}
    assert all(r.rank is not None and r.score_explanation for r in rows.values())
    assert ctx.store.run_dir(repo_id, run_id).is_dir()
    assert run.summary_json is not None and run.summary_json["label"] in ("PASS", "PASS with gaps")


async def test_confirmed_failure_posts_verdict_first(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    script.zero_passes = False
    head = pr_change(repo)
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict) == ("completed", "failure")
    assert run.rank_coverset is not None and run.rank_file_order == 5
    assert run.t_first_failure_file_order_est is not None
    timeline = {k: datetime.fromisoformat(v) for k, v in run.timeline.items()}
    assert timeline["first_failure"] <= timeline["verdict"] <= timeline["finished"]
    log = await events(ctx, run_id)
    verdicts = [e.payload for e in log if e.kind == "verdict"]
    assert verdicts[0].get("early") is True and verdicts[-1]["conclusion"] == "failure"
    failures = [e.payload for e in log if e.kind == "failure"]
    assert failures == [
        {
            "nodeid": ZERO,
            "confirmed": True,
            "retried": True,
            "preexisting": False,
            "rank": run.rank_coverset,
            "executed": run.rank_executed,
            "note": None,
        }
    ]
    assert run.rank_executed == 5  # the one shard reported it fifth
    rows = await results(ctx, run_id)
    assert rows[ZERO].outcome == "failed" and rows[ZERO].attempts == 2 and not rows[ZERO].flaky
    async with ctx.db.session() as session:
        attempts = list(await session.scalars(select(TestAttempt).where(TestAttempt.run_id == run_id)))
        calls = [c.method for c in await session.scalars(select(ForgeCall).order_by(ForgeCall.id))]
    assert sorted(a.attempt for a in attempts if a.test_id == rows[ZERO].test_id) == [1, 2]
    assert "create_check" in calls and calls.count("upsert_pr_comment") == 2
    summary = run.summary_json
    assert summary is not None
    assert summary["failures"][0]["nodeid"] == ZERO
    assert summary["patch_total"] == 1 and summary["patch_pct"] == 100.0


async def test_retry_that_passes_marks_flaky(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    def decide(nodeid: str, env: Mapping[str, str], retry: bool) -> str:
        if nodeid == ZERO:
            return "failed" if int(env.get("COVERSET_DEMO_RUN", "0")) % 3 == 1 else "passed"
        return "passed"

    script.decide = decide
    run_id = await run_baseline(
        ctx,
        repo_id,
        repo.main_sha,
        extra_env={"COVERSET_DEMO_RUN": "4"},
        retry_increment_keys=["COVERSET_DEMO_RUN"],
    )
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict) == ("completed", "success")
    assert "1 flaky" in (run.reason or "")
    rows = await results(ctx, run_id)
    assert rows[ZERO].flaky and rows[ZERO].outcome == "passed" and rows[ZERO].attempts == 2
    retry_envs = [e for e in script.envs if e.get("COVERSET_DEMO_RUN") == "5"]
    assert len(retry_envs) == 1
    assert all(e.get("COVERSET_DEMO_RUN") in ("4", "5") for e in script.envs)
    async with ctx.db.session() as session:
        stats = await session.get(TestStats, (repo_id, rows[ZERO].test_id))
    assert stats is not None and stats.flaky_n == 1 and stats.runs_n == 1


async def test_preexisting_failure_does_not_fail_the_pr(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    base_run = await run_baseline(ctx, repo_id, repo.main_sha)
    assert (await load_run(ctx, base_run)).verdict == "failure"
    head = pr_change(repo)
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.verdict == "success"
    assert "pre-existing" in (run.reason or "")
    assert run.rank_coverset is None
    failures = [e.payload for e in await events(ctx, run_id) if e.kind == "failure"]
    assert failures and failures[0]["preexisting"] is True


async def test_retry_budget_exhausted_confirms_without_retry(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"retry_budget_per_run": 0})
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    failures = [e.payload for e in await events(ctx, run_id) if e.kind == "failure"]
    assert failures[0]["retried"] is False
    assert not any("retry-" in " ".join(c) for c in script.commands)


async def test_cancel_remaining_stops_other_shards(
    tmp_path, ctx: OrchestratorContext, script: Script
) -> None:  # type: ignore[no-untyped-def]
    from coverset.orchestrator.repos import register_local_repo

    repo = make_repo(tmp_path / "ff-repo", fail_fast="cancel_remaining")
    async with ctx.db.session() as session:
        repo_id = (await register_local_repo(session, repo.path)).id
        await session.commit()
    script.outcomes = {"tests/test_calc.py::test_add": "failed"}
    script.delay_s = 0.3
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.verdict == "failure"
    assert run.cancelled_tests > 0
    async with ctx.db.session() as session:
        shards = list(await session.scalars(select(Shard).where(Shard.run_id == run_id)))
    assert any(s.exit_code == 2 for s in shards)
    assert all(s.error is None for s in shards)


async def test_superseded_run_is_cancelled(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.delay_s = 0.5
    head = pr_change(repo)
    first = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=head,
        base_sha=repo.main_sha,
        branch="pr-1",
        trigger="pull_request",
        pr_number=1,
    )
    task = asyncio.create_task(execute_run(ctx, first))
    await asyncio.sleep(0.3)
    second = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=head,
        base_sha=repo.main_sha,
        branch="pr-1",
        trigger="pull_request",
        pr_number=1,
    )
    outcome = await asyncio.wait_for(task, timeout=30)
    assert outcome.status == "cancelled"
    assert outcome.reason == f"superseded by run {second}"
    log = await events(ctx, first)
    assert log[-1].kind == "done" and log[-1].payload["exit_code"] == 2


async def test_no_tests_collected_is_neutral(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.collected = ()
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict, run.reason) == ("completed", "neutral", "no tests collected")
    done = (await events(ctx, run_id))[-1]
    assert done.kind == "done" and done.payload["exit_code"] == 5


async def test_environment_failure_ends_in_error_with_done_event(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor
) -> None:
    executor.fail_env = RuntimeError("uv exploded")
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.status == "error"
    assert "environment" in (run.reason or "") and "uv exploded" in (run.reason or "")
    assert (await events(ctx, run_id))[-1].kind == "done"


async def test_no_capacity_fails_with_reason(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor
) -> None:
    executor.capacity = 0
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.status == "error" and (run.reason or "").startswith("capacity")


async def test_patch_report_and_facts(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    head = pr_change(repo)
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    run = await load_run(ctx, run_id)
    summary = run.summary_json
    assert summary is not None
    assert (summary["patch_covered"], summary["patch_total"], summary["patch_pct"]) == (1, 1, 100.0)
    rows = {r["qualname"]: r for r in summary["function_rows"]}
    assert rows["add"]["tests"] == 1 and rows["add"]["change_kind"] == "modified"
    assert {i["id"] for i in summary["invariants"]} == {"add_commutes"}
    coverage = patch_coverage(ctx.store, repo_id, run_id, {"src/factspkg/calc.py": {5}})
    assert coverage.complete and coverage.covered == 1


async def test_untrusted_local_run_is_skipped(ctx: OrchestratorContext, repo: TestRepo, repo_id: int) -> None:
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=repo.main_sha,
        base_sha=None,
        branch="main",
        trigger="baseline",
        trusted=False,
    )
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion) == ("skipped", "neutral")


async def test_claimed_run_is_not_executed_twice(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    commands = len(script.commands)
    outcome = await execute_run(ctx, run_id)
    assert outcome.status == "completed" and len(script.commands) == commands


def test_retry_env_increments_only_integer_keys() -> None:
    env = {"COVERSET_DEMO_RUN": "4", "OTHER": "x", "TEXT": "abc"}
    assert retry_env(env, ["COVERSET_DEMO_RUN", "TEXT", "MISSING"]) == {
        "COVERSET_DEMO_RUN": "5",
        "OTHER": "x",
        "TEXT": "abc",
    }
    assert env["COVERSET_DEMO_RUN"] == "4"


@pytest.mark.parametrize("fail_sig", [FAIL_SIG])
def test_fail_sig_constant(fail_sig: str) -> None:
    assert len(bytes.fromhex(fail_sig)) == 8
