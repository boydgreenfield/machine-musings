"""ModalExecutor against a fake ``modal`` module: guards, images, snapshots, pool, workers, reaper, cost."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import subprocess
import tarfile
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import Env, Repo, Snapshot
from coverset.db.session import Database, init_db
from coverset.executors import get_executor
from coverset.executors.base import (
    MAX_LINE_BYTES,
    CapacityError,
    EnvRef,
    Executor,
    ExecutorError,
    PoolingExecutor,
    ReapingExecutor,
    Resources,
    SnapshotRef,
)
from coverset.executors.modal_ import (
    APP_NAME,
    CPU_USD_PER_CORE_S,
    MEM_USD_PER_GIB_S,
    SNAPSHOT_TTL_S,
    ModalExecutor,
    ModalWorker,
    SandboxSlots,
    rate_usd_per_s,
    sandbox_options,
)
from coverset.executors.modal_images import IMAGE_BUILDER_VERSION
from coverset.lang.base import EnvSpec

RESOURCES = Resources(cpu=2.0, memory_mib=2048, network="blocked", timeout_s=1800)
LABELS = {"coverset_run": "run_A", "repo": "1", "role": "shard"}


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"modal_environment": "coverset-test", "max_sandboxes": 48, "pool_ttl_s": 600}


@pytest.fixture
async def db_settings(settings: Settings) -> Settings:
    await init_db(settings)
    return settings


@pytest.fixture
def wheel(tmp_path: Path) -> Path:
    path = tmp_path / "wheels" / "pytest_coverset-0.1.0-py3-none-any.whl"
    path.parent.mkdir()
    path.write_bytes(b"PK fake wheel")
    return path


@pytest.fixture
def spec(wheel: Path) -> EnvSpec:
    return EnvSpec(
        python="3.13",
        lock_hash="l" * 64,
        requirements_txt="pytest==9.1.1\nhypothesis==6.168.5\n",
        plugin_wheel=wheel,
        source_root="src",
        env_hash="e" * 64,
    )


_OPEN: list[ModalExecutor] = []


def make_executor(settings: Settings, fake_modal: Any, **kwargs: Any) -> ModalExecutor:
    kwargs.setdefault("slots", SandboxSlots(settings.max_sandboxes))
    kwargs.setdefault("budget_probe", _zero)
    executor = ModalExecutor(settings, client_factory=lambda: fake_modal, **kwargs)
    _OPEN.append(executor)
    return executor


@pytest.fixture(autouse=True)
async def _close_executors() -> AsyncIterator[None]:
    """Close every executor a test made (database engines included), even when the test fails."""
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _zero() -> float:
    return 0.0


def env_ref(spec: EnvSpec, image_id: str = "im-env") -> EnvRef:
    return EnvRef(env_hash=spec.env_hash, python=spec.python, location=image_id, spec=spec)


def tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# -- guards ------------------------------------------------------------------------------------------


@pytest.mark.security
def test_sandbox_options_carry_every_guard(settings: Settings) -> None:
    tags = {"coverset_instance": "i", "repo": "1", "run": "r", "role": "shard"}
    options = sandbox_options(settings, RESOURCES, tags, idle_timeout_s=600)
    assert options["cpu"] == (0.125, settings.shard_cpu_limit)
    assert options["memory"] == (512, settings.shard_memory_mib)
    assert options["timeout"] <= settings.run_timeout_s
    assert options["idle_timeout"] == 600
    assert options["secrets"] == []
    assert options["include_oidc_identity_token"] is False
    assert options["block_network"] is True
    assert options["tags"] == tags
    assert "name" not in options
    assert "outbound_domain_allowlist" not in options


def test_sandbox_options_clamp_to_the_configured_ceilings(settings: Settings) -> None:
    greedy = Resources(cpu=64.0, memory_mib=1 << 20, network="blocked", timeout_s=10**6)
    options = sandbox_options(settings, greedy, {}, idle_timeout_s=0)
    assert options["cpu"][1] == settings.shard_cpu_limit
    assert options["memory"][1] == settings.shard_memory_mib
    assert options["timeout"] == settings.run_timeout_s
    assert options["idle_timeout"] is None


@pytest.mark.security
@pytest.mark.parametrize(
    ("network", "allowlist", "expected"),
    [
        ("blocked", ["pypi.org"], {"block_network": True}),
        ("allowlist", ["pypi.org"], {"block_network": False, "outbound_domain_allowlist": ["pypi.org"]}),
        ("allowlist", [], {"block_network": True}),
        ("open", [], {"block_network": False}),
    ],
)
def test_network_policy(
    settings: Settings, network: Any, allowlist: list[str], expected: dict[str, Any]
) -> None:
    configured = settings.model_copy(update={"sandbox_domain_allowlist": allowlist})
    res = Resources(cpu=1, memory_mib=1024, network=network, timeout_s=60)
    options = sandbox_options(configured, res, {}, idle_timeout_s=60)
    for key in ("block_network", "outbound_domain_allowlist"):
        assert options.get(key) == expected.get(key)


def test_protocols_and_capabilities(settings: Settings, fake_modal: Any) -> None:
    executor = make_executor(settings, fake_modal)
    assert isinstance(executor, Executor)
    assert isinstance(executor, PoolingExecutor)
    assert isinstance(executor, ReapingExecutor)
    caps = executor.capabilities()
    assert (caps.name, caps.warm_pool, caps.fs_snapshot, caps.egress) == ("modal", True, True, False)
    assert caps.max_workers == settings.max_sandboxes
    opened = make_executor(settings.model_copy(update={"sandbox_network": "open"}), fake_modal)
    assert opened.capabilities().egress is True


def test_get_executor_builds_the_modal_executor(settings: Settings, fake_modal: Any) -> None:
    assert isinstance(get_executor(settings, "modal"), ModalExecutor)


def test_cost_formula(settings: Settings, fake_modal: Any) -> None:
    executor = make_executor(settings, fake_modal)
    hour = executor.cost_usd(RESOURCES, 3600)
    assert hour == pytest.approx(2.0 * 0.142 + 2.0 * 0.024)
    assert executor.idle_cost_usd(3600) == pytest.approx(0.125 * 0.142 + 0.5 * 0.024)
    assert rate_usd_per_s(1.0, 1024) == pytest.approx(CPU_USD_PER_CORE_S + MEM_USD_PER_GIB_S)
    assert executor.cost_usd(Resources(cpu=99, memory_mib=99_999), 1) == pytest.approx(
        rate_usd_per_s(settings.shard_cpu_limit, settings.shard_memory_mib)
    )
    assert executor.cost_usd(RESOURCES, -5) == 0.0


# -- images --------------------------------------------------------------------------------------------


async def test_ensure_env_builds_the_recipe_and_records_the_image(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec, wheel: Path
) -> None:
    executor = make_executor(db_settings, fake_modal)
    env = await executor.ensure_env(spec)
    assert os.environ["MODAL_IMAGE_BUILDER_VERSION"] == IMAGE_BUILDER_VERSION
    assert world.lookups == [
        {"name": APP_NAME, "create_if_missing": True, "environment_name": "coverset-test"}
    ]
    assert len(world.builds) == 1
    steps = world.builds[0]
    assert steps[0] == ("debian_slim", "3.13")
    assert steps[1] == ("apt_install", ("git",))
    assert steps[2] == ("env", {"UV_COMPILE_BYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    assert steps[3][0] == "uv_pip_install" and steps[3][3] == (spec.requirements_txt,)
    assert steps[4] == ("add_local_file", str(wheel), f"/opt/wheels/{wheel.name}", True)
    # without its dependencies, like the LocalExecutor: the requirements carry the coverage pin
    assert steps[5] == ("run_commands", (f"pip install --no-deps /opt/wheels/{wheel.name}",))
    assert env.location.startswith("im-") and env.spec is spec
    records = json.loads((db_settings.data_dir / "modal" / "images.json").read_text())
    assert records[spec.env_hash]["image_id"] == env.location
    database = Database.from_url(db_settings.database_url)
    async with database.session() as session:
        row = await session.get(Env, spec.env_hash)
    await database.engine.dispose()
    assert row is not None and row.image_id == env.location
    again = make_executor(db_settings, fake_modal)
    assert (await again.ensure_env(spec)).location == env.location
    assert len(world.builds) == 1, "a cached image id must not rebuild"
    await executor.close()
    await again.close()


async def test_ensure_env_reads_the_image_id_from_the_database(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    database = Database.from_url(db_settings.database_url)
    async with database.session() as session:
        session.add(Env(env_hash=spec.env_hash, python="3.13", image_id="im-from-db"))
        await session.commit()
    await database.engine.dispose()
    executor = make_executor(db_settings, fake_modal)
    assert (await executor.ensure_env(spec)).location == "im-from-db"
    assert world.builds == []


async def test_build_failure_is_an_executor_error(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    world.build_error = RuntimeError("apt exploded")
    executor = make_executor(settings, fake_modal)
    with pytest.raises(ExecutorError, match="apt exploded"):
        await executor.ensure_env(spec)


async def test_empty_requirements_skip_the_install_step(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    bare = dataclasses.replace(spec, requirements_txt="# nothing\n", env_hash="f" * 64)
    await make_executor(settings, fake_modal).ensure_env(bare)
    assert [step[0] for step in world.builds[0]] == [
        "debian_slim", "apt_install", "env", "add_local_file", "run_commands",
    ]  # fmt: skip


# -- acquire ---------------------------------------------------------------------------------------------


@pytest.mark.security
async def test_acquire_creates_guarded_tagged_sandboxes(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    workers = await executor.acquire(env_ref(spec), 3, RESOURCES, 600, LABELS)
    assert len(workers) == 3 and len(world.live()) == 3
    for call in world.create_calls:
        assert call["image"].object_id == "im-env"
        assert call["cpu"] == (0.125, 2.0) and call["memory"] == (512, 2048)
        assert call["timeout"] <= settings.run_timeout_s and call["idle_timeout"] == 600
        assert call["secrets"] == [] and call["include_oidc_identity_token"] is False
        assert call["block_network"] is True and "name" not in call
        tags = dict(call["tags"])
        assert abs(int(tags.pop("created")) - time.time()) < 600  # generous under a loaded machine
        # The owner is read once per executor; macOS may rename the host mid-suite (DHCP, Wi-Fi), so
        # compare with the executor's value and check its shape rather than calling gethostname again.
        owner = tags.pop("owner")
        assert owner == executor.owner and owner.endswith(f":{os.getpid()}")
        assert tags == {
            "coverset_instance": await executor.instance(),
            "repo": "1",
            "run": "run_A",
            "role": "shard",
        }
    assert all(isinstance(w, ModalWorker) and w.root == "/work" for w in workers)
    await executor.close()
    assert world.live() == []


async def test_acquire_keeps_successes_under_partial_capacity(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    world.create_failures = [world.exc.ResourceExhaustedError("capacity"), world.exc.InternalFailure("boom")]
    workers = await executor.acquire(env_ref(spec), 4, RESOURCES, 600, LABELS)
    assert len(workers) == 2
    assert len(executor.stats.capacity_errors) == 2
    assert executor._slots.used == 2
    await executor.close()
    assert executor._slots.used == 0


async def test_acquire_with_nothing_available_raises_capacity_error(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    world.create_failures = [world.exc.ResourceExhaustedError("no capacity")] * 2
    with pytest.raises(CapacityError, match="ResourceExhaustedError"):
        await executor.acquire(env_ref(spec), 2, RESOURCES, 600, LABELS)
    assert executor._slots.used == 0


@pytest.mark.security
async def test_acquire_respects_the_global_sandbox_cap(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    slots = SandboxSlots(3)
    first = make_executor(settings, fake_modal, slots=slots)
    second = make_executor(settings, fake_modal, slots=slots)
    assert len(await first.acquire(env_ref(spec), 2, RESOURCES, 600, LABELS)) == 2
    assert len(await second.acquire(env_ref(spec), 5, RESOURCES, 600, LABELS)) == 1
    with pytest.raises(CapacityError, match="max_sandboxes=3"):
        await second.acquire(env_ref(spec), 1, RESOURCES, 600, LABELS)
    await first.close()
    assert len(await second.acquire(env_ref(spec), 5, RESOURCES, 600, LABELS)) == 2
    await second.close()


@pytest.mark.security
async def test_budget_exhausted_refuses_before_creating(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    async def spent() -> float:
        return settings.modal_daily_budget_usd + 0.01

    executor = make_executor(settings, fake_modal, budget_probe=spent)
    with pytest.raises(CapacityError, match="budget exhausted"):
        await executor.acquire(env_ref(spec), 2, RESOURCES, 600, LABELS)
    assert await executor.fill_pool(env_ref(spec), "1", 2, 600) == 0
    with pytest.raises(CapacityError, match="budget exhausted"):
        await executor.snapshot_from_archive(env_ref(spec), 1, "a" * 40, tarball({"a.py": b""}))
    assert world.create_calls == []


async def test_budget_counts_spend_recorded_in_the_database(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    from coverset.db.models import Run

    database = Database.from_url(db_settings.database_url)
    async with database.session() as session:
        repo = Repo(forge="local", external_id="x", full_name="local/x", default_branch="main")
        session.add(repo)
        await session.flush()
        session.add(
            Run(repo_id=repo.id, head_sha="a" * 40, trigger="baseline", executor="modal", cost_estimate=99.0)
        )
        await session.commit()
    await database.engine.dispose()
    executor = ModalExecutor(db_settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    _OPEN.append(executor)
    with pytest.raises(CapacityError, match="budget exhausted"):
        await executor.acquire(env_ref(spec), 1, RESOURCES, 600, LABELS)
    await executor.close()


async def test_stale_env_image_is_rebuilt_once(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(db_settings, fake_modal)
    world.missing_images.add("im-gone")
    workers = await executor.acquire(env_ref(spec, "im-gone"), 2, RESOURCES, 600, LABELS)
    assert len(workers) == 2
    assert executor.stats.rebuilds == 1 and len(world.builds) == 1
    new_id = world.live()[0].image.object_id
    assert new_id != "im-gone"
    more = await executor.acquire(env_ref(spec, "im-gone"), 1, RESOURCES, 600, LABELS)
    assert len(more) == 1 and len(world.builds) == 1, "the remap is remembered"
    assert (await executor.ensure_env(spec)).location == new_id
    await executor.close()


async def test_stale_snapshot_falls_back_to_the_env_image(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    world.missing_images.add("im-snap-old")
    snap = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap-old", snapshot_id=7)
    workers = await executor.acquire(snap, 2, RESOURCES, 600, LABELS)
    assert len(workers) == 2
    assert {sb.image.object_id for sb in world.live()} == {"im-env"}
    assert world.builds == []
    await executor.close()


# -- the pool --------------------------------------------------------------------------------------------


async def test_pool_is_claimed_first_never_execd_and_serves_one_run(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    snap = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap", snapshot_id=1)
    assert await executor.fill_pool(snap, "1", 3, 600) == 3
    assert await executor.fill_pool(snap, "1", 3, 600) == 0, "already full"
    pooled = world.live()
    assert all(sb.tags["role"] == "pool" and sb.execs == [] for sb in pooled)
    assert all(sb.kwargs["idle_timeout"] == 600 and sb.kwargs["block_network"] for sb in pooled)
    created_before = len(world.create_calls)
    workers = await executor.acquire(snap, 2, RESOURCES, 600, LABELS)
    assert len(workers) == 2 and len(world.create_calls) == created_before
    claimed = [sb for sb in pooled if sb.tags["role"] == "shard"]
    assert len(claimed) == 2 and all(sb.tags["run"] == "run_A" for sb in claimed)
    assert executor.pool_size("1") == 1 and executor.stats.pool_claims == 2
    for worker in workers:
        await worker.release()
    assert all(sb.terminated for sb in claimed), "pooled sandboxes never go back to the pool"
    assert await executor.drain_pool() == 1
    assert world.live() == []


async def test_pool_only_matches_the_same_image_network_and_repo(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    snap = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap", snapshot_id=1)
    await executor.fill_pool(snap, "1", 1, 600)
    open_net = Resources(cpu=2.0, memory_mib=2048, network="open", timeout_s=1800)
    await executor.acquire(snap, 1, open_net, 600, LABELS)
    await executor.acquire(env_ref(spec), 1, RESOURCES, 600, LABELS)
    await executor.acquire(snap, 1, RESOURCES, 600, {**LABELS, "repo": "2"})
    assert executor.pool_size("1") == 1 and executor.stats.pool_claims == 0
    await executor.close()


async def test_dead_pooled_sandboxes_are_skipped(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    await executor.fill_pool(env_ref(spec), "1", 2, 600)
    world.live()[0].terminated = True  # idle_timeout fired on Modal's side
    workers = await executor.acquire(env_ref(spec), 2, RESOURCES, 600, LABELS)
    assert len(workers) == 2 and executor.stats.pool_claims == 1
    await executor.close()


async def test_a_new_snapshot_replaces_the_stale_pool(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    old = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-old", snapshot_id=1)
    new = SnapshotRef(env=env_ref(spec), sha="b" * 40, image_id="im-new", snapshot_id=2)
    await executor.fill_pool(old, "1", 2, 600)
    assert await executor.fill_pool(new, "1", 2, 600) == 2
    assert {sb.image.object_id for sb in world.live()} == {"im-new"}
    await executor.close()


async def _seed_pool_repo(settings: Settings, *, cost: float = 0.0) -> int:
    """A repo with a recent Modal run, its env and a ready trusted snapshot ``im-snap``."""
    from datetime import timedelta

    from coverset.db.models import Run

    database = Database.from_url(settings.database_url)
    now = settings.now()
    async with database.session() as session:
        repo = Repo(forge="local", external_id="p", full_name="local/p", default_branch="main")
        session.add(repo)
        session.add(Env(env_hash="e" * 64, python="3.13", image_id="im-env", created_at=now))
        await session.flush()
        session.add(
            Run(
                repo_id=repo.id,
                head_sha="a" * 40,
                trigger="baseline",
                executor="modal",
                cost_estimate=cost,
                created_at=now,
            )
        )
        session.add(
            Snapshot(
                repo_id=repo.id,
                env_hash="e" * 64,
                sha="a" * 40,
                image_id="im-snap",
                status="ready",
                created_at=now,
                expires_at=now + timedelta(days=6),
            )
        )
        await session.commit()
        repo_id = repo.id
    await database.engine.dispose()
    return repo_id


@pytest.mark.security
async def test_serve_fills_the_pool_reaps_and_drains_on_shutdown(
    db_settings: Settings, fake_modal: Any, world: Any
) -> None:
    from coverset.app import create_app, schedule_executor_upkeep
    from coverset.orchestrator.runner import OrchestratorContext

    repo_id = await _seed_pool_repo(db_settings)
    settings = db_settings.model_copy(update={"pool_size": 2, "executor": "modal"})
    executor = make_executor(settings, fake_modal)
    app = create_app(settings)
    ctx = OrchestratorContext.from_app(app)
    ctx.executor_factory = lambda kind: executor
    schedule_executor_upkeep(app, reap_interval_s=0.05, pool_interval_s=0.05)
    orphan = world.add_foreign(
        {"coverset_instance": await executor.instance(), "run": "gone", "role": "shard"}
    )

    def pooled() -> list[Any]:
        return [sb for sb in world.live() if sb.tags.get("role") == "pool"]

    async with app.test_app():
        for _ in range(250):
            if len(pooled()) == 2 and orphan.terminated:
                break
            await asyncio.sleep(0.02)
        assert len(pooled()) == 2 and executor.pool_size(str(repo_id)) == 2
        assert {sb.image.object_id for sb in pooled()} == {"im-snap"}
        assert all(sb.tags["repo"] == str(repo_id) and sb.execs == [] for sb in pooled())
        assert orphan.terminated, "the reaper runs while serving"
    assert world.live() == [], "shutdown drains the pool: nothing is left running"


async def test_pool_maintainer_respects_the_daily_budget(
    db_settings: Settings, fake_modal: Any, world: Any
) -> None:
    from coverset.orchestrator.pool import PoolMaintainer

    await _seed_pool_repo(db_settings, cost=db_settings.modal_daily_budget_usd)
    executor = make_executor(db_settings, fake_modal)
    database = Database.from_url(db_settings.database_url)
    try:
        assert await PoolMaintainer(database, db_settings, executor).maintain_once() == {}
    finally:
        await database.engine.dispose()
    assert world.create_calls == []


async def test_pool_fills_share_the_sandbox_semaphore(
    db_settings: Settings, fake_modal: Any, world: Any
) -> None:
    from coverset.orchestrator.pool import PoolMaintainer

    repo_id = await _seed_pool_repo(db_settings)
    executor = make_executor(db_settings, fake_modal, slots=SandboxSlots(3))
    database = Database.from_url(db_settings.database_url)
    try:
        maintainer = PoolMaintainer(database, db_settings, executor)
        assert await maintainer.maintain_once() == {str(repo_id): 3}, "pool_size 4 capped by 3 slots"
        assert await maintainer.drain() == 3
    finally:
        await database.engine.dispose()
    assert world.live() == []


# -- workers ---------------------------------------------------------------------------------------------


async def one_worker(executor: ModalExecutor, spec: EnvSpec) -> ModalWorker:
    workers = await executor.acquire(env_ref(spec), 1, RESOURCES, 600, LABELS)
    worker = workers[0]
    assert isinstance(worker, ModalWorker)
    return worker


async def test_run_streams_lines_with_a_scrubbed_environment(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    long_line = b"x" * (MAX_LINE_BYTES + 10)
    world.handler = lambda cmd, env, wd: world.Plan(
        stdout=[b"one\ntw", b"o\n", long_line + b"\nafter\n", b"tail"],
        stderr=[b"warn 1\nwarn 2\n"],
        exit_code=1,
    )
    executor = make_executor(settings, fake_modal)
    worker = await one_worker(executor, spec)
    await worker.put_tree(tarball({"setup.cfg": b""}), "repo")
    lines: list[tuple[str, str]] = []
    result = await worker.run(
        ["python", "-m", "pytest", "-q"],
        cwd="repo",
        env={
            "PYTHONPATH": "/work/repo/src",
            "COVERSET_EVENT_NONCE": "n",
            "MODAL_TOKEN_ID": "ak-leak",
            "PATH": "/evil",
        },
        timeout_s=30,
        on_line=lambda text, stream: lines.append((stream, text)),
        stdout_cap=10 * MAX_LINE_BYTES,
    )
    assert result.exit_code == 1 and not result.truncated and not result.timed_out
    stdout = [t for s, t in lines if s == "stdout"]
    assert stdout[:2] == ["one", "two"]
    assert len(stdout[2]) == MAX_LINE_BYTES and stdout[3:] == ["after", "tail"]
    assert [t for s, t in lines if s == "stderr"] == ["warn 1", "warn 2"]
    record = world.live()[0].execs[-1]
    assert record["argv"][:2] == ["env", "-i"] and record["text"] is False
    assert record["cmd"] == ["python", "-m", "pytest", "-q"] and record["workdir"] == "/work/repo"
    env = record["env"]
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin" and env["HOME"] == "/work/home"
    assert env["PYTHONPATH"] == "/work/repo/src" and env["COVERSET_EVENT_NONCE"] == "n"
    assert "MODAL_TOKEN_ID" not in env and len(env["COVERSET_EXEC_ID"]) == 32
    await executor.close()


async def test_stdout_cap_kills_and_truncates(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    world.handler = lambda cmd, env, wd: world.Plan(endless_stdout=b"spam\n" * 100)
    executor = make_executor(settings, fake_modal, kill_grace_s=0.5)
    worker = await one_worker(executor, spec)
    result = await asyncio.wait_for(
        worker.run(["yes"], cwd="", env={}, timeout_s=30, stdout_cap=10_000), timeout=10
    )
    assert result.truncated and result.stdout_bytes > 10_000
    assert any(r.get("signal") == 15 for r in world.live()[0].execs)
    await executor.close()


async def test_timeout_terminates_the_exec(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    world.handler = lambda cmd, env, wd: world.Plan(stdout=[b"started\n"], hang=True, ignore_term=True)
    executor = make_executor(settings, fake_modal, kill_grace_s=0.2)
    worker = await one_worker(executor, spec)
    result = await worker.run(["sleep", "999"], cwd="", env={}, timeout_s=0.3, stdout_cap=1000)
    assert result.timed_out and result.exit_code == 128 + 9
    signals = [r["signal"] for r in world.live()[0].execs if "signal" in r]
    assert signals == [15, 9], "SIGTERM first, SIGKILL after the grace period"
    await executor.close()


async def test_cancel_sends_sigterm_so_the_plugin_can_write_its_bundle(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    world.handler = lambda cmd, env, wd: world.Plan(hang=True)
    executor = make_executor(settings, fake_modal)
    worker = await one_worker(executor, spec)
    shard = asyncio.create_task(worker.run(["pytest"], cwd="", env={}, timeout_s=60, stdout_cap=1000))
    retry = asyncio.create_task(
        worker.run(["pytest", "retry"], cwd="", env={}, timeout_s=60, stdout_cap=1000)
    )
    await asyncio.sleep(0.05)
    await worker.cancel()
    results = await asyncio.gather(shard, retry)
    assert all(r.cancelled and r.exit_code == 128 + 15 for r in results)
    signal_execs = [r for r in world.live()[0].execs if "signal" in r]
    assert len(signal_execs) == 1 and signal_execs[0]["signal"] == 15
    assert len(signal_execs[0]["argv"]) == 4 + 2, "one signal exec covering both running execs"
    await executor.close()


async def test_put_tree_get_file_write_file(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    worker = await one_worker(executor, spec)
    sandbox = world.live()[0]
    sandbox.filesystem.files["/work/repo/stale.py"] = b"from the snapshot"
    await worker.put_tree(
        tarball({"pkg/mod.py": b"x = 1\n", "tests/test_x.py": b"def test(): pass\n"}), "repo"
    )
    assert await worker.get_file("repo/pkg/mod.py") == b"x = 1\n"
    assert "/work/repo/stale.py" not in sandbox.filesystem.files, "put_tree replaces the tree"
    assert not any(p.startswith("/work/.src-") for p in sandbox.filesystem.files), "the archive is removed"
    await worker.write_file("run/shard.args", b"tests/test_x.py::test\n")
    assert sandbox.filesystem.files["/work/run/shard.args"] == b"tests/test_x.py::test\n"
    with pytest.raises(ExecutorError, match="no such file"):
        await worker.get_file("run/out/bundle.tar.gz")
    for bad in ("/etc/passwd", "../x", "run/../../x"):
        with pytest.raises(ExecutorError, match="inside the worker"):
            worker.path(bad)
    assert worker.path("") == "/work" and worker.path("repo/a") == "/work/repo/a"
    await executor.close()


async def test_put_tree_reports_extraction_failures(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    worker = await one_worker(executor, spec)
    world.live()[0].filesystem.dirs.discard("/work")
    original = world.live()[0].filesystem._write_bytes

    async def drop(data: bytes, path: str) -> None:  # the upload "succeeds" but nothing arrives
        return None

    world.live()[0].filesystem.write_bytes.aio = drop
    with pytest.raises(ExecutorError, match="cannot extract"):
        await worker.put_tree(tarball({"a.py": b""}), "repo")
    world.live()[0].filesystem.write_bytes.aio = original
    await executor.close()


async def test_release_terminates_once_and_frees_the_slot(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    worker = await one_worker(executor, spec)
    sandbox = world.live()[0]
    assert executor._slots.used == 1 and len(executor._live) == 1
    await worker.release()
    await worker.release()
    assert sandbox.terminated and executor._slots.used == 0 and executor.stats.terminated == 1
    with pytest.raises(ExecutorError, match="released"):
        await worker.run(["true"], cwd="", env={}, timeout_s=1, stdout_cap=10)


# -- snapshots ---------------------------------------------------------------------------------------------


async def test_snapshot_from_archive_warms_snapshots_and_records(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    database = Database.from_url(db_settings.database_url)
    async with database.session() as session:
        repo = Repo(forge="local", external_id="r", full_name="local/r", default_branch="main")
        session.add(repo)
        await session.commit()
        repo_id = repo.id
    executor = make_executor(db_settings, fake_modal)
    env = await executor.ensure_env(spec)
    ref = await executor.snapshot_from_archive(env, repo_id, "c" * 40, tarball({"src/pkg/a.py": b"A = 1\n"}))
    sandbox = world.sandboxes[-1]
    assert sandbox.terminated and sandbox.tags["role"] == "snapshot"
    assert sandbox.kwargs["block_network"] is True
    assert sandbox.snapshot_kwargs == {"timeout": 300, "ttl": SNAPSHOT_TTL_S}
    commands = [r["cmd"] for r in sandbox.execs if "cmd" in r and r["cmd"][0] == "python"]
    assert commands[0] == [
        "python", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "--rootdir=/work/repo",
    ]  # fmt: skip
    assert commands[1][:3] == ["python", "-m", "compileall"] and "checked-hash" in commands[1]
    collect_env = next(r["env"] for r in sandbox.execs if r.get("cmd") == commands[0])
    assert collect_env["PYTHONPATH"] == "/work/repo/src:/work/repo"
    assert collect_env["PYTHONPYCACHEPREFIX"] == "/work/.pycache"
    assert ref.image_id.startswith("im-snap-") and ref.snapshot_id is not None
    async with database.session() as session:
        row = await session.scalar(select(Snapshot).where(Snapshot.id == ref.snapshot_id))
    await database.engine.dispose()
    assert row is not None and row.status == "ready" and row.image_id == ref.image_id
    assert row.expires_at is not None
    await executor.close()


def _git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.security
async def test_ensure_snapshot_only_snapshots_default_branch_commits(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec, tmp_path: Path
) -> None:
    work = tmp_path / "src-repo"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "a.py").write_text("A = 1\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "main", cwd=work)
    main_sha = _git("rev-parse", "HEAD", cwd=work)
    _git("checkout", "-q", "-b", "pr", cwd=work)
    (work / "a.py").write_text("A = 2\n")
    _git("commit", "-q", "-am", "pr", cwd=work)
    pr_sha = _git("rev-parse", "HEAD", cwd=work)
    database = Database.from_url(db_settings.database_url)
    async with database.session() as session:
        repo = Repo(forge="local", external_id="g", full_name="local/g", default_branch="main")
        session.add(repo)
        await session.commit()
        repo_id = repo.id
    await database.engine.dispose()
    mirror = db_settings.mirrors_dir / f"{repo_id}.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--mirror", str(work), str(mirror), cwd=tmp_path)
    executor = make_executor(db_settings, fake_modal)
    env = await executor.ensure_env(spec)
    with pytest.raises(ExecutorError, match="not on the default branch"):
        await executor.ensure_snapshot(env, "local/g", pr_sha)
    ref = await executor.ensure_snapshot(env, "local/g", main_sha)
    assert ref is not None and ref.sha == main_sha
    created = len(world.create_calls)
    again = await executor.ensure_snapshot(env, str(repo_id), main_sha)
    assert again is not None and again.image_id == ref.image_id and len(world.create_calls) == created
    with pytest.raises(ExecutorError, match="unknown repository"):
        await executor.ensure_snapshot(env, "local/nope", main_sha)
    await executor.close()


# -- reaping -----------------------------------------------------------------------------------------------


async def test_reap_terminates_orphans_by_instance_tag(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    mine = await executor.instance()
    active = world.add_foreign({"coverset_instance": mine, "run": "run_live", "role": "shard", "repo": "1"})
    orphan = world.add_foreign({"coverset_instance": mine, "run": "run_dead", "role": "shard", "repo": "1"})
    old_pool = world.add_foreign({"coverset_instance": mine, "run": "pool", "role": "pool", "repo": "1"})
    foreign = world.add_foreign({"coverset_instance": "someone-else", "run": "run_dead", "role": "shard"})
    held = await one_worker(executor, spec)  # this process's own worker is never reaped
    report = await executor.reap(active_run_ids={"run_live"})
    assert (report.found, report.terminated, report.kept) == (4, 2, 2)
    assert orphan.terminated and old_pool.terminated
    assert not active.terminated and not foreign.terminated and not world.live()[-1].terminated
    report = await executor.reap(active_run_ids={"run_live"}, all=True)
    assert report.terminated == 2 and active.terminated and not foreign.terminated
    assert len(executor._live) == 0 and executor._slots.used == 0
    await held.release()
    assert await executor.list_running() == []


async def test_reap_through_the_generic_orchestrator_helper(
    settings: Settings, fake_modal: Any, world: Any
) -> None:
    from coverset.orchestrator.reaper import reap

    executor = make_executor(settings, fake_modal)
    world.add_foreign({"coverset_instance": await executor.instance(), "run": "r1", "role": "shard"})
    report = await reap(executor, all=True)
    assert report.terminated == 1 and world.live() == []


async def test_budget_ignores_the_acquiring_runs_own_reservation(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    from coverset.db.models import Run

    database = Database.from_url(db_settings.database_url)
    async with database.session() as session:
        repo = Repo(forge="local", external_id="y", full_name="local/y", default_branch="main")
        session.add(repo)
        await session.flush()
        run = Run(
            repo_id=repo.id, head_sha="b" * 40, trigger="baseline", executor="modal", cost_estimate=99.0
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    await database.engine.dispose()
    executor = ModalExecutor(db_settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    _OPEN.append(executor)
    workers = await executor.acquire(env_ref(spec), 1, RESOURCES, 600, {**LABELS, "coverset_run": run_id})
    assert len(workers) == 1, "a run's own reservation must not lock it out (as budget.reserve)"
    with pytest.raises(CapacityError, match="budget exhausted"):
        await executor.acquire(env_ref(spec), 1, RESOURCES, 600, {**LABELS, "coverset_run": "run_other"})


# -- cost-safety fixes: cancellation, close, slots, budget, spend, image builds, reaping --------------------


async def _gate_creates_after(fake_modal: Any, k: int) -> asyncio.Event:
    """Let the first ``k`` creates through; the rest wait for the returned event."""
    original = fake_modal.Sandbox.create.aio
    gate = asyncio.Event()
    calls = {"n": 0}

    async def create(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > k:
            await gate.wait()
        return await original(*args, **kwargs)

    fake_modal.Sandbox.create.aio = create
    return gate


@pytest.mark.security
async def test_cancelled_acquire_leaves_no_sandbox_and_no_slot(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    await executor.instance()  # read once from the database, so the creates below take a few ticks
    await _gate_creates_after(fake_modal, 2)
    task = asyncio.create_task(executor.acquire(env_ref(spec), 4, RESOURCES, 600, LABELS))
    for _ in range(100):
        await asyncio.sleep(0)
        if len(world.sandboxes) >= 2:
            break
    assert len(world.sandboxes) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.live() == [] and len(executor._live) == 0
    assert executor._slots.used == 0, "every slot came back: the two created and the two cancelled"


@pytest.mark.security
async def test_cancelled_pool_claim_terminates_the_claimed_sandboxes(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    base = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap", snapshot_id=1)
    assert await executor.fill_pool(base, "1", 2, 600) == 2
    gate = asyncio.Event()
    for sandbox in world.live():

        async def slow_poll(sb: Any = sandbox) -> int | None:
            await gate.wait()
            return 0 if sb.terminated else None

        sandbox.poll.aio = slow_poll
    task = asyncio.create_task(executor.acquire(base, 2, RESOURCES, 600, LABELS))
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.live() == [] and executor._slots.used == 0 and executor.pool_size() == 0


@pytest.mark.security
async def test_a_closed_executor_creates_nothing(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    await executor.close()
    with pytest.raises(CapacityError, match="closed"):
        await executor.acquire(env_ref(spec), 2, RESOURCES, 600, LABELS)
    with pytest.raises(CapacityError, match="closed"):
        await executor.create_sandbox(fake_modal.Image.debian_slim("3.13"), role="smoke")
    assert await executor.fill_pool(env_ref(spec), "1", 2, 600) == 0
    assert world.create_calls == []


@pytest.mark.security
async def test_a_failed_rebuild_gives_its_slot_back(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    world.missing_images.add("im-env")
    world.build_error = RuntimeError("build boom")
    with pytest.raises(ExecutorError):
        await executor.snapshot_from_archive(env_ref(spec), 1, "a" * 40, tarball({"a.py": b""}))
    assert executor._slots.used == 0 and world.live() == []


@pytest.mark.security
async def test_acquire_admits_only_what_fits_in_the_budget(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    async def almost() -> float:
        return settings.modal_daily_budget_usd - 0.01

    executor = make_executor(settings, fake_modal, budget_probe=almost)
    with pytest.raises(CapacityError, match="budget exhausted"):
        await executor.acquire(env_ref(spec), 32, RESOURCES, 600, LABELS)
    assert world.create_calls == []
    roomy = make_executor(settings, fake_modal)
    one_worker_worst = roomy.cost_usd(RESOURCES, settings.run_timeout_s)
    assert 32 * one_worker_worst > settings.modal_daily_budget_usd  # $5.31 uncapped
    assert roomy._run_exposure(32, RESOURCES) == settings.max_run_cost_usd  # capped per run
    assert len(await roomy.acquire(env_ref(spec), 32, RESOURCES, 600, LABELS)) == 32


@pytest.mark.security
async def test_pooled_sandboxes_outlive_the_run_timeout(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    base = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap", snapshot_id=1)
    await executor.fill_pool(base, "1", 1, 600)
    call = world.create_calls[-1]
    assert call["timeout"] == settings.run_timeout_s + 600 and call["idle_timeout"] == 600
    no_ttl = make_executor(settings.model_copy(update={"pool_ttl_s": 0}), fake_modal)
    await no_ttl.fill_pool(base, "2", 1, 0)
    call = world.create_calls[-1]
    assert call["timeout"] == settings.run_timeout_s and call["idle_timeout"] is None


@pytest.mark.security
async def test_non_run_spend_is_persisted_and_counted_today_only(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    from coverset.db.models import ExecutorSpend
    from coverset.orchestrator import budget

    executor = ModalExecutor(db_settings, client_factory=lambda: fake_modal, slots=SandboxSlots(8))
    _OPEN.append(executor)
    executor.stats.untracked_usd = 10 * db_settings.modal_daily_budget_usd  # a stat, not the budget
    base = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap", snapshot_id=1)
    assert await executor.fill_pool(base, "1", 2, 600) == 2
    await executor.acquire(base, 1, RESOURCES, 600, LABELS)  # claims one: its idle time is recorded
    await executor.drain_pool()  # terminates the other: recorded too
    sandbox, terminate = await executor.create_sandbox(fake_modal.Image.debian_slim("3.13"), role="smoke")
    await terminate()
    await asyncio.gather(*executor._db_tasks)
    database = Database.from_url(db_settings.database_url)
    try:
        async with database.session() as session:
            rows = list(await session.scalars(select(ExecutorSpend).order_by(ExecutorSpend.id)))
            assert sorted(r.kind for r in rows) == ["pool", "pool", "smoke"]
            assert {r.day for r in rows} == {db_settings.now().date()} and all(r.usd >= 0 for r in rows)
            assert {r.executor for r in rows} == {"modal"} and all(r.sandbox_id for r in rows)
            total = sum(r.usd for r in rows)
            assert await budget.spent_today(session, db_settings) == pytest.approx(total)
            tomorrow = db_settings.model_copy(update={"fake_now": db_settings.now() + timedelta(days=1)})
            assert await budget.spent_today(session, tomorrow) == 0.0
    finally:
        await database.engine.dispose()
    restarted = ModalExecutor(db_settings, client_factory=lambda: fake_modal, slots=SandboxSlots(8))
    _OPEN.append(restarted)
    assert await restarted.spent_today_usd() == pytest.approx(total), "a restart remembers it"


@pytest.mark.security
async def test_image_builds_pass_the_budget_and_the_daily_cap(
    db_settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    from coverset.db.models import ExecutorSpend

    async def broke() -> float:
        return 1e9

    exhausted = make_executor(db_settings, fake_modal, budget_probe=broke)
    with pytest.raises(CapacityError, match="budget exhausted"):
        await exhausted.ensure_env(spec)
    assert world.builds == []
    capped = db_settings.model_copy(update={"max_image_builds_per_day": 1})
    executor = ModalExecutor(capped, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    _OPEN.append(executor)
    await executor.ensure_env(spec)
    other = dataclasses.replace(spec, env_hash="9" * 64, requirements_txt="pytest==9.1.1\nrich==14.0\n")
    with pytest.raises(CapacityError, match="image build cap reached"):
        await executor.ensure_env(other)
    assert len(world.builds) == 1 and executor.stats.build_usd >= 0
    await asyncio.gather(*executor._db_tasks)
    database = Database.from_url(db_settings.database_url)
    try:
        async with database.session() as session:
            kinds = list(await session.scalars(select(ExecutorSpend.kind)))
    finally:
        await database.engine.dispose()
    assert kinds == ["build"]


@pytest.mark.security
async def test_cached_env_never_builds(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    executor = make_executor(settings, fake_modal)
    assert await executor.cached_env(spec) is None and world.builds == []
    built = await executor.ensure_env(spec)
    assert (await executor.cached_env(spec)) == built and len(world.builds) == 1


@pytest.mark.security
async def test_a_plain_reap_keeps_other_processes_young_pool_and_snapshot_sandboxes(
    settings: Settings, fake_modal: Any, world: Any, spec: EnvSpec
) -> None:
    serve = make_executor(settings, fake_modal)
    base = SnapshotRef(env=env_ref(spec), sha="a" * 40, image_id="im-snap", snapshot_id=1)
    assert await serve.fill_pool(base, "1", 4, 600) == 4
    await serve.create_sandbox(
        fake_modal.Image.debian_slim("3.13"), role="snapshot", repo="1", run="snapshot"
    )
    old = int(time.time()) - settings.run_timeout_s - 1
    stale_pool = world.add_foreign(
        {"coverset_instance": await serve.instance(), "run": "pool", "role": "pool", "created": str(old)}
    )
    cli = make_executor(settings, fake_modal)  # `coverset modal reap` in another process
    report = await cli.reap(active_run_ids=set())
    assert (report.found, report.terminated, report.kept) == (6, 1, 5) and stale_pool.terminated
    assert serve.pool_size("1") == 4 and len(world.live()) == 5
    report = await cli.reap(active_run_ids=set(), all=True)
    assert report.terminated == 5 and world.live() == []


@pytest.mark.security
async def test_reap_any_instance_reaches_other_data_dirs_only_for_coverset_sandboxes(
    settings: Settings, fake_modal: Any, world: Any
) -> None:
    from coverset.orchestrator.reaper import reap

    executor = make_executor(settings, fake_modal)
    bench = world.add_foreign({"coverset_instance": "bench-dir", "run": "run_x", "role": "shard"})
    unrelated = world.add_foreign({"team": "someone-else"})
    report = await executor.reap(active_run_ids=set())
    assert report.found == 0 and not bench.terminated
    report = await reap(executor, any_instance=True)
    assert (report.found, report.terminated) == (1, 1) and bench.terminated and not unrelated.terminated
