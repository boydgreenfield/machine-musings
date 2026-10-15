"""The planner: prior-run selection, wave 1 at the head of every shard, worker files, re-sharding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from orch_fakes import NODEIDS, FakeExecutor, Script, TestRepo, fake_env_spec, pr_change
from orch_helpers import load_run, run_baseline, run_pr
from sqlalchemy import select

from coverset.db.models import Run, Shard, TestCase, TestResult
from coverset.orchestrator.planner import (
    LAYOUT,
    PlanError,
    parse_collect_json,
    parse_collect_listing,
    plan_run,
    split_diff,
)
from coverset.orchestrator.runner import OrchestratorContext, create_run


async def _plan(ctx: OrchestratorContext, executor: FakeExecutor, run_id: str, **kwargs):  # type: ignore[no-untyped-def]
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
    return await plan_run(
        ctx.db, ctx.settings, ctx.store, run, executor=executor, env_spec_builder=fake_env_spec, **kwargs
    )


async def test_first_pr_without_prior_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor
) -> None:
    head = repo.commit({"tests/test_new.py": "def test_new():\n    pass\n"}, "add a test", branch="pr-1")
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=head,
        base_sha=repo.main_sha,
        branch="pr-1",
        trigger="pull_request",
        pr_number=1,
    )
    plan = await _plan(ctx, executor, run_id)
    try:
        assert plan.prior is None
        assert plan.changed_files == {"tests/test_new.py"}
        assert plan.wave1.tests == []  # the fake collection has no tests in the new file
        assert [i.nodeid for i in plan.collected] == list(NODEIDS)
        assert sorted(plan.ranks.values()) == list(range(1, len(NODEIDS) + 1))
        assert plan.file_ranks[NODEIDS[0]] == 1
        worker = plan.shards[0].worker
        args = (Path(worker.path(LAYOUT.args_file))).read_text().splitlines()
        scores = json.loads(Path(worker.path(LAYOUT.scores_file)).read_text())
        assert args == plan.shards[0].nodeids
        assert max(scores, key=scores.get) == args[0]
        assert (
            Path(worker.path(LAYOUT.full_trace_file)).read_text() == "src/factspkg/calc.py\n"
        )  # invariant target
        assert Path(worker.path("repo/src/factspkg/calc.py")).is_file()
    finally:
        await plan.cleanup()
    async with ctx.db.session() as session:
        rows = list(await session.scalars(select(TestResult).where(TestResult.run_id == run_id)))
        shards = list(await session.scalars(select(Shard).where(Shard.run_id == run_id)))
        run = await session.get(Run, run_id)
    assert len(rows) == len(NODEIDS) and all(r.outcome == "not_run" and r.rank for r in rows)
    assert all(r.score_explanation and r.score_explanation["basis"] == "no prior run" for r in rows)
    assert sum(s.test_count for s in shards) == len(NODEIDS)
    assert run is not None and run.scoring_basis_run_id is None and run.env_hash == "f" * 64


async def test_prior_run_selection(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    baseline = await run_baseline(ctx, repo_id, repo.main_sha)
    head = pr_change(repo)
    first = await run_pr(ctx, repo_id, head, repo.main_sha)
    assert (await load_run(ctx, first)).scoring_basis_run_id == baseline  # merge-base run
    newer = pr_change(repo, body="return a + b + 0", hour=2)
    second = await run_pr(ctx, repo_id, newer, repo.main_sha)
    run = await load_run(ctx, second)
    assert run.scoring_basis_run_id == first  # the PR's previous head
    async with ctx.db.session() as session:
        explanation = await session.scalar(
            select(TestResult.score_explanation)
            .join(TestCase, TestCase.id == TestResult.test_id)
            .where(TestResult.run_id == second, TestCase.nodeid == NODEIDS[0])
        )
    assert explanation is not None and "previous head" in explanation["basis"]


async def test_wave1_heads_every_shard(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    head = pr_change(repo)
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=head,
        base_sha=repo.main_sha,
        branch="pr-1",
        trigger="pull_request",
        pr_number=1,
    )
    plan = await _plan(ctx, executor, run_id, shards_override=2)
    try:
        assert plan.wave1.tests == ["tests/test_calc.py::test_add"]
        assert plan.order[0] == "tests/test_calc.py::test_add"
        assert plan.ranks["tests/test_calc.py::test_add"] == 1
        assert len(plan.shards) == 2
        assert plan.shards[0].nodeids[0] == "tests/test_calc.py::test_add"
        assert plan.changed_lines == {"src/factspkg/calc.py": {5}}
        assert [c.kind for c in plan.changes] == ["modified"]
        assert plan.full_trace == ["src/factspkg/calc.py"]
    finally:
        await plan.cleanup()


async def test_reshards_to_the_workers_obtained(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor
) -> None:
    executor.capacity = 1
    run_id = await create_run(
        ctx, repo_id=repo_id, head_sha=repo.main_sha, base_sha=None, branch="main", trigger="baseline"
    )
    plan = await _plan(ctx, executor, run_id, shards_override=3)
    try:
        assert len(plan.shards) == 1 and len(plan.workers) == 1
        assert sorted(plan.shards[0].nodeids) == sorted(NODEIDS)
    finally:
        await plan.cleanup()


async def test_no_capacity_is_a_plan_error(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor
) -> None:
    executor.capacity = 0
    run_id = await create_run(
        ctx, repo_id=repo_id, head_sha=repo.main_sha, base_sha=None, branch="main", trigger="baseline"
    )
    with pytest.raises(PlanError) as info:
        await _plan(ctx, executor, run_id)
    assert info.value.reason == "capacity"


@pytest.mark.security
def test_parse_collect_json_validates_items() -> None:
    raw = json.dumps(
        {
            "items": [
                {
                    "nodeid": "t.py::a",
                    "file": "t.py",
                    "line": 3,
                    "markers": ["x"],
                    "invariant_ids": ["i"],
                    "hypothesis": True,
                },
                {"nodeid": "t.py::a", "file": "t.py"},
                {"nodeid": "x" * 5000, "file": "t.py"},
                {"nodeid": 5},
                "junk",
            ],
            "errors": [{"nodeid": "bad.py", "longrepr": "boom"}],
        }
    ).encode()
    items, errors = parse_collect_json(raw)
    assert [(i.nodeid, i.line, i.invariant_ids, i.hypothesis) for i in items] == [
        ("t.py::a", 3, ("i",), True)
    ]
    assert errors == [{"nodeid": "bad.py", "longrepr": "boom"}]
    with pytest.raises(ValueError):
        parse_collect_json(b"[]")


def test_parse_collect_listing_fallback() -> None:
    lines = [
        "tests/a.py::test_x",
        "tests/a.py::test_y[1]",
        "",
        "2 tests collected in 0.01s",
        "\x1ecoverset n {}",
    ]
    assert [i.nodeid for i in parse_collect_listing(lines)] == ["tests/a.py::test_x", "tests/a.py::test_y[1]"]


def test_split_diff_keys_by_head_path() -> None:
    text = (
        "diff --git a/x.py b/y.py\nsimilarity index 90%\nrename from x.py\nrename to y.py\n"
        "--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    chunks = split_diff(text)
    assert set(chunks) == {"y.py", "gone.py"}
    assert chunks["y.py"].startswith("diff --git a/x.py b/y.py") and "+b" in chunks["y.py"]
