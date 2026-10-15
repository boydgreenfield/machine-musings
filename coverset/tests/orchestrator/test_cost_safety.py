"""Modal cost safety in the orchestrator: admission with what a run may spend, the per-run cap
(reserved and enforced live), non-run spend in the budget, untrusted environments, ``--shards``."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest
from orch_fakes import FakeExecutor, Script, TestRepo, fake_env_spec
from orch_helpers import events, load_run
from pydantic import ValidationError
from sqlalchemy import update

from coverset.config import Settings
from coverset.db.models import Run
from coverset.executors.base import EnvRef
from coverset.lang.base import EnvSpec
from coverset.orchestrator import budget
from coverset.orchestrator.runner import OrchestratorContext, create_run, execute_run


async def _modal_baseline(ctx: OrchestratorContext, repo_id: int, sha: str, **kwargs: Any) -> str:
    return await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=sha,
        base_sha=None,
        branch="main",
        trigger="baseline",
        executor="modal",
        **kwargs,
    )


async def _add_spend(ctx: OrchestratorContext, usd: float, kind: str = "pool") -> None:
    async with ctx.db.session() as session:
        budget.record_spend(session, ctx.settings, "modal", kind, usd)
        await session.commit()


# -- settings ------------------------------------------------------------------------------------------


@pytest.mark.security
def test_cost_settings_have_upper_bounds_and_the_run_cap_follows_the_budget() -> None:
    for key, value in (
        ("run_timeout_s", 7201),
        ("shard_cpu_limit", 16.5),
        ("shard_memory_mib", 65537),
        ("max_sandboxes", 257),
        ("max_shards", 129),
        ("pool_size", 33),
        ("pool_ttl_s", 86401),
        ("modal_daily_budget_usd", 1000.5),
    ):
        with pytest.raises(ValidationError):
            Settings.for_tests(**{key: value})
    defaults = Settings.for_tests()
    assert (defaults.max_run_cost_usd, defaults.max_image_builds_per_day) == (1.0, 20)
    with pytest.raises(ValidationError, match="COVERSET_MAX_RUN_COST_USD"):
        Settings.for_tests(modal_daily_budget_usd=0.5, max_run_cost_usd=0.75)
    assert Settings.for_tests(modal_daily_budget_usd=0.5).max_run_cost_usd == 0.5  # the default follows


# -- budget.reserve: spent + reservations + amount ---------------------------------------------------


@pytest.mark.security
async def test_reserve_admits_only_if_the_amount_fits(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)
    await _add_spend(ctx, 4.5)  # e.g. pool idle time and snapshot builds earlier today
    async with ctx.db.session() as session:
        assert await budget.spent_today(session, ctx.settings) == pytest.approx(4.5)
        assert not await budget.reserve(session, ctx.settings, run_id, "modal", 0.6)
        assert await budget.reserve(session, ctx.settings, run_id, "modal", 0.4)
        assert (await session.get(Run, run_id)).cost_estimate == pytest.approx(0.4)  # type: ignore[union-attr]
        assert await budget.budget_exhausted(session, ctx.settings, "modal") is False
        await session.commit()
    await _add_spend(ctx, 0.5)
    async with ctx.db.session() as session:
        assert await budget.budget_exhausted(session, ctx.settings, "modal")


@pytest.mark.security
async def test_non_run_spend_makes_a_modal_run_neutral(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    await _add_spend(ctx, ctx.settings.modal_daily_budget_usd, kind="snapshot")
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion, outcome.reason) == (
        "completed",
        "neutral",
        "budget exhausted",
    )


@pytest.mark.security
async def test_a_planned_run_is_readmitted_at_its_worst_case(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.zero_passes = True
    executor.rate_per_s = 0.0008  # before planning: 600 s = $0.48; planned: 1800 s = $1.44, capped at $1
    await _add_spend(ctx, 4.5)
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion, outcome.reason) == (
        "completed",
        "neutral",
        "budget exhausted",
    )
    kinds = [e.kind for e in await events(ctx, run_id)]
    assert "test" not in kinds and executor.acquire_calls, "planned (workers held), then refused"
    assert all(w.released for w in executor.workers)


@pytest.mark.security
async def test_the_run_cost_cap_stops_a_run_live(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    executor.rate_per_s = 1.0  # $1 per worker-second
    ctx.settings = ctx.settings.model_copy(update={"max_run_cost_usd": 0.5})
    ctx.heartbeat_s = 0.05
    script.zero_passes = True
    script.delay_s = 1.0
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha)
    outcome = await asyncio.wait_for(execute_run(ctx, run_id), timeout=30)
    assert (outcome.status, outcome.conclusion, outcome.reason) == (
        "error",
        "neutral",
        "run cost cap $0.50 reached",
    )
    run = await load_run(ctx, run_id)
    assert 0.5 < run.cost_estimate < 5.0  # the real cost is recorded, not the reservation
    rows = await events(ctx, run_id)
    assert any(e.kind == "log" and e.payload.get("warning") == "cost_cap" for e in rows)
    assert rows[-1].kind == "done" and rows[-1].payload["exit_code"] == 2
    assert all(w.released for w in executor.workers)


# -- untrusted runs never build a new environment ------------------------------------------------------


class BuildingExecutor(FakeExecutor):
    """A Modal-like fake that knows which environments have an image (``cached_env``)."""

    name = "modal"

    def __init__(self, script: Script) -> None:
        super().__init__(script)
        self.images: set[str] = set()
        self.builds: list[str] = []

    async def cached_env(self, spec: EnvSpec) -> EnvRef | None:
        if spec.env_hash not in self.images:
            return None
        return EnvRef(env_hash=spec.env_hash, python=spec.python, location="im-cached", spec=spec)

    async def ensure_env(self, spec: EnvSpec) -> EnvRef:
        if spec.env_hash not in self.images:
            self.builds.append(spec.env_hash)
            self.images.add(spec.env_hash)
        return EnvRef(env_hash=spec.env_hash, python=spec.python, location="im-built", spec=spec)


@pytest.fixture
def building(script: Script, ctx: OrchestratorContext) -> BuildingExecutor:
    fake = BuildingExecutor(script)
    ctx.executor_factory = lambda kind: fake
    ctx.executors.clear()
    return fake


async def _untrusted(ctx: OrchestratorContext, repo: TestRepo, repo_id: int, *, approved: bool) -> str:
    run_id = await _modal_baseline(ctx, repo_id, repo.main_sha, trusted=False)
    if approved:
        async with ctx.db.session() as session:
            await session.execute(update(Run).where(Run.id == run_id).values(approved_by="maintainer"))
            await session.commit()
    return run_id


@pytest.mark.security
async def test_untrusted_runs_never_build_an_environment_without_approval(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, building: BuildingExecutor
) -> None:
    script.zero_passes = True
    run_id = await _untrusted(ctx, repo, repo_id, approved=False)
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion, outcome.reason) == (
        "skipped",
        "neutral",
        "untrusted PR changes the environment: approve to build",
    )
    assert building.builds == [] and building.acquire_calls == []
    approved = await _untrusted(ctx, repo, repo_id, approved=True)
    assert (await execute_run(ctx, approved)).conclusion == "success"
    assert building.builds == ["f" * 64]
    again = await _untrusted(ctx, repo, repo_id, approved=False)  # same lockfile: the image exists
    assert (await execute_run(ctx, again)).conclusion == "success"
    assert building.builds == ["f" * 64]


@pytest.mark.security
@pytest.mark.parametrize(
    "line",
    [
        "evil @ https://example.invalid/evil-1.0.tar.gz",
        "evil @ git+https://example.invalid/evil.git",
        "git+https://example.invalid/evil.git#egg=evil",
        "--extra-index-url https://example.invalid/simple",
        "-e file:///tmp/evil",
    ],
)
async def test_approved_untrusted_runs_never_build_direct_references(
    ctx: OrchestratorContext,
    repo: TestRepo,
    repo_id: int,
    script: Script,
    building: BuildingExecutor,
    line: str,
) -> None:
    def spec_with_reference(checkout: Path, config: Any) -> EnvSpec:
        spec = fake_env_spec(checkout, config)
        return dataclasses.replace(spec, requirements_txt=f"pytest==9.1.1\n{line}\n", env_hash="d" * 64)

    ctx.env_spec_builder = spec_with_reference
    script.zero_passes = True
    run_id = await _untrusted(ctx, repo, repo_id, approved=True)
    outcome = await execute_run(ctx, run_id)
    assert (outcome.status, outcome.conclusion) == ("skipped", "neutral")
    assert "direct URL, VCS or index reference" in (outcome.reason or "")
    assert "example.invalid" not in (outcome.reason or ""), "attacker text is never echoed"
    assert building.builds == []
    trusted = await _modal_baseline(ctx, repo_id, repo.main_sha)  # trusted runs keep building
    assert (await execute_run(ctx, trusted)).conclusion == "success"
    assert building.builds == ["d" * 64]


# -- coverset run --shards N is capped ------------------------------------------------------------------


@pytest.mark.security
async def test_forced_shards_are_capped_by_max_shards(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.zero_passes = True
    executor.capacity = 64
    ctx.settings = ctx.settings.model_copy(update={"max_shards": 2})
    run_id = await create_run(
        ctx, repo_id=repo_id, head_sha=repo.main_sha, base_sha=None, branch="main", trigger="baseline"
    )
    await execute_run(ctx, run_id, shards=256)
    assert executor.acquire_calls and max(executor.acquire_calls) <= 2
