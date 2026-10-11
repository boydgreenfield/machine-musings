"""Anticipatory wiring: re-snapshot after a trusted baseline, the warm-pool maintainer and the reaper loop."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from orch_fakes import FakeExecutor, Script, TestRepo, pr_change
from orch_helpers import load_run, run_baseline, run_pr
from sqlalchemy import select, update

from coverset.app import create_app, schedule_executor_upkeep
from coverset.config import Settings
from coverset.db.models import Env, Run, Snapshot
from coverset.db.session import Database
from coverset.executors.base import (
    Capabilities,
    EnvRef,
    PoolingExecutor,
    ReapingExecutor,
    ReapReport,
    SnapshotRef,
)
from coverset.orchestrator.pool import PoolMaintainer, newest_snapshot
from coverset.orchestrator.reaper import reap_forever
from coverset.orchestrator.runner import OrchestratorContext, create_run, execute_run


class PoolingFake(FakeExecutor):
    """A Modal-like fake: trusted snapshots recorded in ``snapshots``, a warm pool and a reaper."""

    name = "modal"

    def __init__(self, script: Script, db: Database, settings: Settings) -> None:
        super().__init__(script)
        self.db = db
        self.settings = settings
        self.snapshots: list[tuple[str, str]] = []
        self.pool: dict[str, list[str]] = {}
        self.fills: list[tuple[str, str, int]] = []
        self.reaps = 0
        self.closed = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="modal", egress=False, warm_pool=True, fs_snapshot=True, max_workers=self.capacity
        )

    async def ensure_snapshot(self, env: EnvRef, repo: str, sha: str) -> SnapshotRef | None:
        self.snapshots.append((repo, sha))
        now = self.settings.now()
        image_id = f"im-snap-{sha[:7]}"
        async with self.db.session() as session:
            if await session.get(Env, env.env_hash) is None:
                session.add(Env(env_hash=env.env_hash, python=env.python, image_id="im-env", created_at=now))
                await session.flush()
            row = Snapshot(
                repo_id=int(repo),
                env_hash=env.env_hash,
                sha=sha,
                image_id=image_id,
                status="ready",
                created_at=now,
                expires_at=now + timedelta(days=7),
            )
            session.add(row)
            await session.commit()
            return SnapshotRef(env=env, sha=sha, image_id=image_id, snapshot_id=row.id)

    async def fill_pool(self, base: EnvRef | SnapshotRef, repo: str, size: int, ttl_s: int) -> int:
        image = base.image_id if isinstance(base, SnapshotRef) else base.location
        entries = self.pool.setdefault(repo, [])
        entries[:] = [e for e in entries if e == image]
        made = max(0, size - len(entries))
        entries += [image] * made
        self.fills.append((repo, image, made))
        return made

    async def drain_pool(self, repo: str | None = None) -> int:
        repos = [repo] if repo is not None else list(self.pool)
        return sum(len(self.pool.pop(name, [])) for name in repos)

    async def reap(self, *, active_run_ids: set[str], all: bool = False) -> ReapReport:
        self.reaps += 1
        return ReapReport()

    async def close(self) -> None:
        self.closed = True
        await super().close()


@pytest.fixture
def pooling(script: Script, ctx: OrchestratorContext) -> PoolingFake:
    fake = PoolingFake(script, ctx.db, ctx.settings)
    ctx.executor_factory = lambda kind: fake
    ctx.executors.clear()
    return fake


def test_the_fake_is_a_pooling_reaping_executor(pooling: PoolingFake) -> None:
    assert isinstance(pooling, PoolingExecutor) and isinstance(pooling, ReapingExecutor)


async def _modal_baseline(ctx: OrchestratorContext, repo_id: int, sha: str, **kwargs: Any) -> str:
    run_id = await run_baseline(ctx, repo_id, sha, executor="modal", **kwargs)
    await ctx.finish_background()
    return run_id


# -- re-snapshot after a baseline ----------------------------------------------------------------------


async def test_trusted_baseline_resnapshots_and_refills_the_pool(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, pooling: PoolingFake
) -> None:
    script.zero_passes = True
    ctx.pool_maintainer = PoolMaintainer(ctx.db, ctx.settings, pooling)
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)
    assert (await load_run(ctx, run_id)).status == "completed"
    assert pooling.snapshots == [(str(repo_id), repo.main_sha)]
    image = f"im-snap-{repo.main_sha[:7]}"
    assert pooling.pool == {str(repo_id): [image] * ctx.settings.pool_size}
    async with ctx.db.session() as session:
        rows = list(await session.scalars(select(Snapshot).where(Snapshot.repo_id == repo_id)))
    assert [(r.sha, r.status) for r in rows] == [(repo.main_sha, "ready")]


async def test_a_failing_baseline_still_resnapshots_but_cli_runs_get_no_pool(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, pooling: PoolingFake
) -> None:
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)  # test_zero_fails fails
    assert (await load_run(ctx, run_id)).verdict == "failure"
    assert pooling.snapshots == [(str(repo_id), repo.main_sha)]
    assert pooling.fills == [], "without a maintainer (CLI runs) nothing fills a pool"


async def test_pr_untrusted_and_other_branch_runs_never_snapshot(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, pooling: PoolingFake
) -> None:
    script.zero_passes = True
    ctx.pool_maintainer = PoolMaintainer(ctx.db, ctx.settings, pooling)
    untrusted = await _modal_baseline(ctx, repo_id, repo.main_sha, trusted=False)
    assert (await load_run(ctx, untrusted)).status == "completed"
    other = await create_run(
        ctx, repo_id=repo_id, head_sha=repo.main_sha, base_sha=None, branch="feature",
        trigger="baseline", executor="modal",
    )  # fmt: skip
    await execute_run(ctx, other)
    pr = await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha, executor="modal")
    await ctx.finish_background()
    assert (await load_run(ctx, pr)).status == "completed"
    assert pooling.snapshots == [] and pooling.fills == [] and not ctx.background


async def test_a_baseline_of_a_superseded_head_does_not_snapshot(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, pooling: PoolingFake
) -> None:
    script.zero_passes = True
    old = repo.main_sha
    new = repo.commit({"NOTES.md": "later\n"}, "a later commit on main", hour=2)
    await _modal_baseline(ctx, repo_id, old)
    assert pooling.snapshots == [], "main moved on: the history replay never snapshots old commits"
    await _modal_baseline(ctx, repo_id, new)
    assert pooling.snapshots == [(str(repo_id), new)]


async def test_executors_without_snapshots_schedule_nothing(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, executor: FakeExecutor
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    assert not ctx.background


async def test_a_failed_snapshot_build_is_logged_not_raised(
    ctx: OrchestratorContext,
    repo: TestRepo,
    repo_id: int,
    script: Script,
    pooling: PoolingFake,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from coverset.executors.base import CapacityError

    async def refuse(env: EnvRef, repo: str, sha: str) -> SnapshotRef | None:
        raise CapacityError("budget exhausted ($5.00 of $5.00 today)")

    pooling.ensure_snapshot = refuse  # type: ignore[method-assign]
    script.zero_passes = True
    ctx.pool_maintainer = PoolMaintainer(ctx.db, ctx.settings, pooling)
    await _modal_baseline(ctx, repo_id, repo.main_sha)
    assert pooling.fills == []
    assert "re-snapshot of repo" in caplog.text and "budget exhausted" in caplog.text


# -- the pool maintainer ---------------------------------------------------------------------------------


async def test_maintainer_fills_active_repos_to_size_and_drains_quiet_ones(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, pooling: PoolingFake
) -> None:
    script.zero_passes = True
    maintainer = PoolMaintainer(ctx.db, ctx.settings, pooling)
    assert await maintainer.maintain_once() == {}, "no runs yet: no active repo"
    await _modal_baseline(ctx, repo_id, repo.main_sha)
    size = ctx.settings.pool_size
    assert await maintainer.maintain_once() == {str(repo_id): size}
    assert await maintainer.maintain_once() == {str(repo_id): 0}, "already full"
    assert len(pooling.pool[str(repo_id)]) == size
    async with ctx.db.session() as session:
        await session.execute(
            update(Run).values(created_at=ctx.settings.now() - timedelta(seconds=ctx.settings.pool_ttl_s + 1))
        )
        await session.commit()
    assert await maintainer.maintain_once() == {}
    assert pooling.pool == {} and maintainer.filled == set()


async def test_maintainer_skips_repos_without_a_ready_snapshot(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, pooling: PoolingFake
) -> None:
    script.zero_passes = True
    await _modal_baseline(ctx, repo_id, repo.main_sha)
    async with ctx.db.session() as session:
        await session.execute(update(Snapshot).values(expires_at=ctx.settings.now() - timedelta(seconds=1)))
        await session.commit()
        assert await newest_snapshot(session, ctx.settings, repo_id) is None
    maintainer = PoolMaintainer(ctx.db, ctx.settings, pooling)
    assert await maintainer.maintain_once() == {}
    assert pooling.fills == []


@pytest.mark.security
async def test_budget_exhaustion_skips_pool_fills_and_drains(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, pooling: PoolingFake
) -> None:
    script.zero_passes = True
    maintainer = PoolMaintainer(ctx.db, ctx.settings, pooling)
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)
    assert await maintainer.maintain_once() == {str(repo_id): ctx.settings.pool_size}
    async with ctx.db.session() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(cost_estimate=ctx.settings.modal_daily_budget_usd + 0.01)
        )
        await session.commit()
    fills = len(pooling.fills)
    assert await maintainer.maintain_once() == {}
    assert await maintainer.refill(repo_id) == 0
    assert len(pooling.fills) == fills and pooling.pool == {}


async def test_pool_size_zero_disables_the_maintainer(ctx: OrchestratorContext, pooling: PoolingFake) -> None:
    settings = ctx.settings.model_copy(update={"pool_size": 0})
    maintainer = PoolMaintainer(ctx.db, settings, pooling)
    assert not maintainer.enabled
    assert await maintainer.maintain_once() == {} and await maintainer.refill(1) == 0


# -- the reaper loop -------------------------------------------------------------------------------------


@pytest.mark.security
async def test_reap_forever_runs_on_its_cadence_until_stopped(
    ctx: OrchestratorContext, pooling: PoolingFake
) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(reap_forever(ctx.db, pooling, interval_s=0.05, stop=stop))
    await asyncio.sleep(0.32)
    stop.set()
    passes = await asyncio.wait_for(task, timeout=2)
    assert 4 <= passes <= 8 and pooling.reaps == passes


async def test_reap_forever_ignores_executors_that_do_not_reap(
    ctx: OrchestratorContext, executor: FakeExecutor
) -> None:
    assert await reap_forever(ctx.db, executor, interval_s=0.01) == 0


# -- serve: started with the app, drained on shutdown ------------------------------------------------------


@pytest.mark.security
async def test_serve_reaps_fills_and_drains_on_shutdown(
    ctx: OrchestratorContext,
    settings: Settings,
    repo: TestRepo,
    repo_id: int,
    script: Script,
    pooling: PoolingFake,
) -> None:
    script.zero_passes = True
    await _modal_baseline(ctx, repo_id, repo.main_sha)
    app = create_app(settings)
    assert settings.env == "test"  # tests never start the upkeep implicitly...
    app.extensions["orchestrator"] = ctx
    schedule_executor_upkeep(app, reap_interval_s=0.05, pool_interval_s=0.05)  # ...only when asked
    async with app.test_app():
        for _ in range(200):
            if len(pooling.pool.get(str(repo_id), [])) == settings.pool_size and pooling.reaps >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(pooling.pool[str(repo_id)]) == settings.pool_size
        assert pooling.reaps >= 3
        assert isinstance(ctx.pool_maintainer, PoolMaintainer)
    reaps = pooling.reaps
    assert pooling.pool == {}, "the pool is drained when serving stops"
    assert pooling.closed and ctx.pool_maintainer is None
    await asyncio.sleep(0.15)
    assert pooling.reaps == reaps, "the reaper stops with the app"


@pytest.mark.security
def test_create_app_schedules_the_executor_upkeep_outside_tests(settings: Settings) -> None:
    def hooks(**update: Any) -> list[str]:
        app = create_app(settings.model_copy(update=update))
        return [f.__name__ for f in app.before_serving_funcs]

    assert "_start_executor_upkeep" not in hooks()  # env=test
    assert "_start_executor_upkeep" in hooks(env="dev", executor="local")  # lazy: see below
    assert "_start_executor_upkeep" in hooks(env="dev", executor="modal")


@pytest.mark.security
async def test_a_local_serve_starts_the_modal_upkeep_when_it_builds_a_modal_executor(
    ctx: OrchestratorContext, settings: Settings, pooling: PoolingFake
) -> None:
    """``coverset run --no-wait --executor modal`` queues a Modal run for a ``serve`` whose default
    executor is local: the reaper and the pool start when that serve builds the Modal executor."""
    assert settings.executor == "local"
    app = create_app(settings)
    app.extensions["orchestrator"] = ctx
    schedule_executor_upkeep(app, reap_interval_s=0.05, pool_interval_s=0.05)
    async with app.test_app():
        await asyncio.sleep(0.15)
        assert pooling.reaps == 0 and ctx.pool_maintainer is None, "no Modal executor yet: no upkeep"
        assert ctx.executor("modal") is pooling
        for _ in range(200):
            if pooling.reaps >= 2:
                break
            await asyncio.sleep(0.02)
        assert pooling.reaps >= 2
        assert isinstance(ctx.pool_maintainer, PoolMaintainer)
    assert pooling.closed and ctx.pool_maintainer is None and ctx.on_executor_built is None
