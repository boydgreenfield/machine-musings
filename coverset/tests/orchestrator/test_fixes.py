"""Regression tests for the phase 5a review: locking, empty shards, crashes, cost, boot, cancellation."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from orch_fakes import NODEIDS, FakeExecutor, Script, TestRepo, fake_env_spec, make_repo, pr_change
from orch_helpers import events, load_run, run_baseline
from sqlalchemy import event, select

from coverset.analysis.sharding import ShardPlan
from coverset.config import Settings
from coverset.db.models import Run, Shard, TestResult
from coverset.facts.ingest import RunContext
from coverset.facts.untrusted import BundleLimits
from coverset.orchestrator import planner as planner_module
from coverset.orchestrator import queue as queue_module
from coverset.orchestrator import runner as runner_module
from coverset.orchestrator import verdict as verdict_module
from coverset.orchestrator.bundles import IngestJob, IngestPool
from coverset.orchestrator.planner import LAYOUT, plan_run
from coverset.orchestrator.queue import RunDispatcher
from coverset.orchestrator.repos import ensure_mirror, register_local_repo
from coverset.orchestrator.runner import (
    EventSink,
    OrchestratorContext,
    create_run,
    execute_run,
    process_claimant,
)

ZERO = "tests/test_calc.py::test_zero_fails"


async def _baseline_run(ctx: OrchestratorContext, repo_id: int, sha: str, **kwargs: Any) -> str:
    return await create_run(
        ctx, repo_id=repo_id, head_sha=sha, base_sha=None, branch="main", trigger="baseline", **kwargs
    )


# -- blocker: planning holds no write transaction ----------------------------------------------------


async def test_planning_never_blocks_other_writers(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    script.collect_delay_s = 2.0
    slow = await _baseline_run(ctx, repo_id, repo.main_sha)
    task = asyncio.create_task(execute_run(ctx, slow))
    await asyncio.sleep(0.8)  # planning: env recorded, workers acquired, collection running
    started = time.monotonic()
    other = await _baseline_run(ctx, repo_id, repo.main_sha)  # a concurrent writer (intake)
    assert time.monotonic() - started < 1.0
    statuses = [e.payload.get("status") for e in await events(ctx, slow) if e.kind == "status"]
    assert statuses == ["running"]  # the run's own events flushed while it planned
    outcome = await asyncio.wait_for(task, timeout=30)
    assert (outcome.status, outcome.conclusion) == ("completed", "success")
    assert (await load_run(ctx, other)).status == "queued"


async def test_a_slow_plan_does_not_break_a_concurrent_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    script.collect_delay_s = 1.5
    ids = [await _baseline_run(ctx, repo_id, repo.main_sha) for _ in range(2)]
    outcomes = await RunDispatcher(ctx, max_concurrent=2).drain()
    assert sorted(o.run_id for o in outcomes) == sorted(ids)
    assert {(o.status, o.conclusion) for o in outcomes} == {("completed", "success")}


# -- empty shards ---------------------------------------------------------------------------------


MANY = tuple(f"tests/test_calc.py::test_many[{i:02d}]" for i in range(12))


async def test_one_file_with_many_tests_and_a_high_k_plans_no_empty_shard(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.collected = MANY
    executor.capacity = 16
    run_id = await _baseline_run(ctx, repo_id, repo.main_sha)
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
    assert run is not None
    plan = await plan_run(
        ctx.db,
        ctx.settings,
        ctx.store,
        run,
        executor=executor,
        env_spec_builder=fake_env_spec,
        shards_override=16,
    )
    try:
        assert all(shard.nodeids for shard in plan.shards)
        assert len(plan.shards) == 10  # one unit per priority decile of the single file
        for shard in plan.shards:
            args = Path(shard.worker.path(LAYOUT.args_file)).read_text().splitlines()
            assert args == shard.nodeids
        assert sorted(n for s in plan.shards for n in s.nodeids) == sorted(MANY)
        assert sum(1 for w in executor.workers if not w.released) == len(plan.shards)
    finally:
        await plan.cleanup()


async def test_each_test_runs_once_with_a_high_k(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.collected = MANY
    executor.capacity = 16
    run_id = await _baseline_run(ctx, repo_id, repo.main_sha)
    await execute_run(ctx, run_id, shards=16)
    tests = [e.payload["nodeid"] for e in await events(ctx, run_id) if e.kind == "test"]
    assert sorted(tests) == sorted(MANY)
    async with ctx.db.session() as session:
        shards = list(await session.scalars(select(Shard).where(Shard.run_id == run_id)))
    assert shards and all(s.test_count > 0 for s in shards)


async def test_planner_drops_empty_shards(
    monkeypatch: pytest.MonkeyPatch,
    ctx: OrchestratorContext,
    repo: TestRepo,
    repo_id: int,
    executor: FakeExecutor,
    script: Script,
) -> None:
    def with_empties(self: Any, max_shards: int, *, forced: bool = False) -> ShardPlan:
        nodeids = [t.nodeid for t in self.scored_list]
        return ShardPlan(shards=[[], nodeids[:2], [], nodeids[2:]], predicted_s=[0.0, 1.0, 0.0, 1.0])

    monkeypatch.setattr(planner_module._Analysis, "shard_plan", with_empties)
    script.zero_passes = True
    run_id = await _baseline_run(ctx, repo_id, repo.main_sha)
    outcome = await execute_run(ctx, run_id)
    assert outcome.conclusion == "success"
    run = await load_run(ctx, run_id)
    assert run.shard_count == 2 and run.coverage_complete
    tests = [e.payload["nodeid"] for e in await events(ctx, run_id) if e.kind == "test"]
    assert sorted(tests) == sorted(NODEIDS)


# -- the ingest pool survives a dead worker process -------------------------------------------------


def test_broken_process_pool_is_recreated(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    job = IngestJob(
        facts_dir=str(tmp_path / "facts"),
        ctx=RunContext(1, "r" * 16, "a" * 40, None, "main", None, datetime(2026, 10, 1, tzinfo=UTC), None),
        shard_id=0,
        tarball=str(tmp_path / "missing.tar.gz"),
        limits=BundleLimits(),
        source_root=str(tmp_path),
        head_paths=frozenset(),
        functions=(),
    )
    pool = IngestPool("process", max_workers=1)

    async def scenario() -> None:
        broken = pool._pool().submit(os._exit, 9)
        with pytest.raises(Exception):  # noqa: B017 - BrokenProcessPool
            await asyncio.wrap_future(broken)
        (outcome,) = await pool.run([job])  # used to raise BrokenProcessPool out of run()
        assert outcome.shard_id == 0 and not outcome.ok and "ingest crashed" not in (outcome.error or "")
        (again,) = await pool.run([job])
        assert again.shard_id == 0 and not again.ok

    try:
        asyncio.run(scenario())
    finally:
        pool.close()


# -- mirrors ---------------------------------------------------------------------------------------


def test_concurrent_mirror_clones_and_fetches_never_collide(settings: Settings, tmp_path: Path) -> None:
    source = make_repo(tmp_path / "race-src")
    errors: list[BaseException] = []
    for trial in range(4):
        row = SimpleNamespace(
            id=1000 + trial, forge="local", full_name="local/race", settings_json={"source": str(source.path)}
        )

        def refresh(repo_row: Any = row) -> None:
            try:
                ensure_mirror(settings, None, repo_row)  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=refresh) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        mirror = settings.mirrors_dir / f"{row.id}.git"
        assert (mirror / "HEAD").is_file()
    assert errors == []


# -- crashes always end with verdict and done -----------------------------------------------------


async def test_a_failing_terminal_write_still_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(runner_module, "build_summary", broken)
    script.zero_passes = True
    run_id = await _baseline_run(ctx, repo_id, repo.main_sha)
    outcome = await execute_run(ctx, run_id)
    assert outcome.status == "error" and "summary exploded" in (outcome.reason or "")
    log = await events(ctx, run_id)
    assert [e.kind for e in log][-2:] == ["verdict", "done"]
    assert log[-1].payload["exit_code"] == 2
    seqs = [e.seq for e in log]
    assert seqs == sorted(set(seqs))


async def test_dispatcher_finalizes_a_crashed_task(
    monkeypatch: pytest.MonkeyPatch, ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    async def crash(context: OrchestratorContext, run_id: str) -> Any:
        async with context.db.session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            run.status = "running"
            await session.commit()
        raise RuntimeError("boom")

    monkeypatch.setattr(queue_module, "execute_run", crash)
    run_id = await _baseline_run(ctx, repo_id, repo.main_sha)
    await RunDispatcher(ctx).run_once()
    run = await load_run(ctx, run_id)
    assert run.status == "error" and "boom" in (run.reason or "")
    assert [e.kind for e in await events(ctx, run_id)][-2:] == ["verdict", "done"]


# -- cost and budget -------------------------------------------------------------------------------


async def test_cost_is_worker_lifetime_on_every_path(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    executor.rate_per_s = 1.0
    script.zero_passes = True
    script.collect_delay_s = 0.3
    ok = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, ok)
    assert run.cost_estimate >= 0.3  # collection happened on a held worker; exec seconds alone are ~0
    script.collected = ("tests/test_calc.py::test_add",)
    script.collect_delay_s = 0.2
    executor.fail_env = None

    async def failing_collect(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        raise planner_module.PlanError("collection", "pytest --collect-only exited 3")

    original = planner_module._collect
    planner_module._collect = failing_collect  # type: ignore[assignment]
    try:
        failed = await run_baseline(ctx, repo_id, repo.main_sha)
    finally:
        planner_module._collect = original  # type: ignore[assignment]
    run = await load_run(ctx, failed)
    assert run.status == "error" and run.cost_estimate >= 0.2  # the _finish path records cost too


async def test_concurrent_modal_runs_see_each_others_reservation(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    # The pre-plan reservation (600 s) is $3 and the planned one min($9 per worker, cap $5) = $5:
    # either way a second run's $3 no longer fits next to the first run's reservation.
    executor.rate_per_s = 0.005
    ctx.settings = ctx.settings.model_copy(update={"modal_daily_budget_usd": 5.0, "max_run_cost_usd": 5.0})
    script.zero_passes = True
    script.delay_s = 0.2
    first = await _baseline_run(ctx, repo_id, repo.main_sha, executor="modal")
    second = await _baseline_run(ctx, repo_id, repo.main_sha, executor="modal")
    task = asyncio.create_task(execute_run(ctx, first))
    deadline = asyncio.get_running_loop().time() + 30  # generous: a loaded machine plans slowly
    while (await load_run(ctx, first)).cost_estimate <= 0:
        assert asyncio.get_running_loop().time() < deadline, "the first run never reserved its budget"
        await asyncio.sleep(0.02)
    refused = await execute_run(ctx, second)
    assert (refused.status, refused.conclusion, refused.reason) == (
        "completed",
        "neutral",
        "budget exhausted",
    )
    done = (await events(ctx, second))[-1]
    assert done.kind == "done" and done.payload["exit_code"] == 2  # neutral exits 2
    assert (await asyncio.wait_for(task, 30)).conclusion == "success"
    final = await load_run(ctx, first)
    assert 0 < final.cost_estimate < 6.0  # the reservation was replaced by the real cost


# -- boot, inline runs and claims -----------------------------------------------------------------


async def test_inline_runs_are_claimed_and_left_alone_by_serve(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    inline = await _baseline_run(ctx, repo_id, repo.main_sha, claim=True)
    run = await load_run(ctx, inline)
    assert run.status == "running" and run.claimed_by == ctx.claimant
    serve = OrchestratorContext(
        settings=ctx.settings, db=ctx.db, store=ctx.store, claimant=process_claimant("serve")
    )
    dispatcher = RunDispatcher(serve)
    assert await dispatcher.dispatch() == []  # never claimed by the dispatcher
    assert inline not in await dispatcher.on_boot()  # this process is alive
    outcome = await execute_run(ctx, inline)
    assert outcome.status == "completed"
    commands = len(script.commands)
    assert (await execute_run(ctx, inline)).status == "completed" and len(script.commands) == commands


async def test_boot_takes_over_only_dead_or_stale_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    host = process_claimant("cli").split("@", 1)[1].split(":", 1)[0]
    alive, dead, stale, old_serve = [await _baseline_run(ctx, repo_id, repo.main_sha) for _ in range(4)]
    now = ctx.settings.now()
    claims = {
        alive: (f"cli@{host}:{os.getpid()}:aaaa", now),
        dead: (f"cli@{host}:999999:bbbb", now),
        stale: (f"cli@{host}:{os.getpid()}:cccc", now - timedelta(hours=1)),
        old_serve: (f"serve@{host}:{os.getpid()}:dddd", now),
    }
    async with ctx.db.session() as session:
        for run_id, (claimed_by, heartbeat) in claims.items():
            row = await session.get(Run, run_id)
            assert row is not None
            row.status, row.claimed_by, row.heartbeat_at = "running", claimed_by, heartbeat
        await session.commit()
    serve = OrchestratorContext(
        settings=ctx.settings, db=ctx.db, store=ctx.store, claimant=process_claimant("serve")
    )
    touched = await RunDispatcher(serve).on_boot()
    assert sorted(touched) == sorted([dead, stale, old_serve])
    assert (await load_run(ctx, alive)).status == "running"
    for run_id in (dead, stale, old_serve):
        assert (await load_run(ctx, run_id)).status == "abandoned"


# -- cancellation ----------------------------------------------------------------------------------


async def test_cancel_during_planning_stops_the_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.collect_delay_s = 30.0
    run_id = await _baseline_run(ctx, repo_id, repo.main_sha)
    task = asyncio.create_task(execute_run(ctx, run_id))
    await asyncio.sleep(0.5)
    async with ctx.db.session() as session:
        row = await session.get(Run, run_id)
        assert row is not None
        row.status, row.reason = "cancelled", "cancelled by an admin"
        await session.commit()
    outcome = await asyncio.wait_for(task, timeout=10)
    assert (outcome.status, outcome.reason) == ("cancelled", "cancelled by an admin")
    assert executor.workers and all(w.released for w in executor.workers)
    assert (await events(ctx, run_id))[-1].kind == "done"


async def test_dispatch_cancels_superseded_queued_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    head = pr_change(repo)
    kwargs = {"base_sha": repo.main_sha, "branch": "pr-1", "trigger": "pull_request", "pr_number": 1}
    older = await create_run(ctx, repo_id=repo_id, head_sha=head, **kwargs)  # type: ignore[arg-type]
    newer = await create_run(ctx, repo_id=repo_id, head_sha=head, **kwargs)  # type: ignore[arg-type]
    outcomes = await RunDispatcher(ctx, max_concurrent=1).drain()
    assert [o.run_id for o in outcomes] == [newer]
    old = await load_run(ctx, older)
    assert (old.status, old.reason) == ("cancelled", f"superseded by run {newer}")
    assert [e.kind for e in await events(ctx, older)][-2:] == ["verdict", "done"]


# -- streaming robustness -------------------------------------------------------------------------


async def test_a_deeply_nested_event_line_is_only_log_text(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    script.inject = ["[" * 100_000 + "]" * 100_000]
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.verdict == "success"
    assert any(e.kind == "log" and "[[[[" in e.payload["text"] for e in await events(ctx, run_id))


async def test_bundle_only_failure_after_release_touches_no_worker(
    tmp_path: Path, ctx: OrchestratorContext, executor: FakeExecutor, script: Script
) -> None:
    repo = make_repo(tmp_path / "ff-repo", fail_fast="cancel_remaining")
    async with ctx.db.session() as session:
        repo_id = (await register_local_repo(session, repo.path)).id
        await session.commit()
    script.zero_passes = True
    script.bundle_outcomes = {"tests/test_calc.py::test_add": "failed"}
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.verdict == "failure"
    assert all(w.released and not Path(w.root).exists() for w in executor.workers)


# -- bulk writes and threads ----------------------------------------------------------------------


async def test_event_sink_writes_results_in_one_statement(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    async with ctx.db.session() as session:
        ids = list(await session.scalars(select(TestResult.test_id).where(TestResult.run_id == run_id)))
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement.split()[0].upper() + " " + statement.split()[1])

    sink = EventSink(ctx.db, run_id, lock=asyncio.Lock(), now=ctx.settings.now, start_seq=10_000)
    for test_id in ids:
        sink.result(test_id, duration_ms=1.0)
        sink.attempt(test_id, 3, outcome="passed", duration_ms=1.0, shard_id=0)
    event.listen(ctx.db.engine.sync_engine, "before_cursor_execute", record)
    try:
        await sink.flush()
    finally:
        event.remove(ctx.db.engine.sync_engine, "before_cursor_execute", record)
    assert statements.count("UPDATE test_results") == 1
    assert statements.count("INSERT INTO") == 1


async def test_scoring_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    seen: list[str] = []
    real = planner_module.score_tests

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(planner_module, "score_tests", spy)
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    assert seen and threading.main_thread().name not in seen


# -- the invariant lens reads facts ------------------------------------------------------------------


async def test_covers_changed_lines_comes_from_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    region = SimpleNamespace(
        func_key="k", path="src/p.py", def_line=10, body_start=11, end_line=20, qualname="f"
    )
    plan = SimpleNamespace(
        changes=[SimpleNamespace(region=SimpleNamespace(func_key="k"))],
        changed_lines={"src/p.py": {12, 13, 30}},
    )
    run = SimpleNamespace(repo_id=1, id="r")
    asked: list[tuple[int, int]] = []

    def covering(store: Any, repo_id: int, run_id: str, path: str, start: int, end: int) -> list[str]:
        asked.append((start, end))
        return ["t::other"]

    monkeypatch.setattr(verdict_module, "tests_covering", covering)
    assert await verdict_module._covers_changed_lines(object(), run, plan, region, ["t::linked"]) is False  # type: ignore[arg-type]
    assert asked == [(12, 13)]  # only the target's changed lines
    monkeypatch.setattr(verdict_module, "tests_covering", lambda *a: ["t::linked"])
    assert await verdict_module._covers_changed_lines(object(), run, plan, region, ["t::linked"]) is True  # type: ignore[arg-type]
    assert await verdict_module._covers_changed_lines(object(), run, plan, region, []) is False  # type: ignore[arg-type]
    unchanged = SimpleNamespace(changes=[], changed_lines={})
    assert await verdict_module._covers_changed_lines(object(), run, unchanged, region, ["t"]) is None  # type: ignore[arg-type]


async def test_weakened_invariant_base_definition_is_evaluated(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    from orch_fakes import INVARIANTS
    from orch_helpers import run_pr

    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    head = repo.commit(
        {"invariants.yaml": INVARIANTS.replace("severity: warning", "severity: info")},
        "weaken",
        branch="pr-1",
    )
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    summary = (await load_run(ctx, run_id)).summary_json
    assert summary is not None
    ids = [i["id"] for i in summary["invariants"]]
    assert ids == ["add_commutes", "add_commutes (base)"]
    changed = {i["id"]: i["status"] for i in summary["invariants_changed"]}
    assert changed == {"add_commutes": "weakened"}


async def test_cli_run_is_claimed_and_finalized_when_it_crashes(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, repo: TestRepo
) -> None:
    from coverset.commands.common import StreamPrinter
    from coverset.commands.run import _run
    from coverset.db.session import Database

    seen: dict[str, Any] = {}

    async def crash(ctx: OrchestratorContext, run_id: str, **kwargs: Any) -> Any:
        async with ctx.db.session() as session:
            row = await session.get(Run, run_id)
            assert row is not None
            seen.update(status=row.status, claimed_by=row.claimed_by, claimant=ctx.claimant, run_id=run_id)
        raise RuntimeError("executor fell over")

    monkeypatch.setattr(runner_module, "execute_run", crash)
    with pytest.raises(RuntimeError, match="fell over"):
        await _run(settings, str(repo.path), "main", None, None, None, "baseline", True, StreamPrinter())
    assert seen["status"] == "running" and seen["claimed_by"] == seen["claimant"]
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            row = await session.get(Run, seen["run_id"])
            kinds = [
                e.kind
                for e in await session.scalars(
                    select(runner_module.RunEvent)
                    .where(runner_module.RunEvent.run_id == seen["run_id"])
                    .order_by(runner_module.RunEvent.seq)
                )
            ]
    finally:
        await db.engine.dispose()
    assert row is not None and row.status == "error" and "fell over" in (row.reason or "")
    assert kinds[-2:] == ["verdict", "done"]
