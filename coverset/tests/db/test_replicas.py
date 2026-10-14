"""Several web replicas on one database: the leader lease decides which one dispatches runs and
runs the background loops; a follower takes over when the leader stops. Boot handling and the
stale-run sweep of the lease holder."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from quart import Quart
from sqlalchemy import select

from coverset.app import add_leader_task, create_app, install_leader_lease
from coverset.config import Settings
from coverset.db.leader import LeaderLease
from coverset.db.models import Repo, Run
from coverset.db.session import init_db
from coverset.orchestrator import queue as queue_module
from coverset.orchestrator.queue import RunDispatcher, install_run_queue
from coverset.orchestrator.runner import OrchestratorContext, RunOutcome


def _replica(
    settings: Settings, name: str, *, check_s: float = 0.05
) -> tuple[Quart, RunDispatcher, LeaderLease]:
    """An app as ``create_app`` builds it outside tests: lease, run queue, lease released last."""
    replica_settings = settings.model_copy(update={"data_dir": settings.data_dir / name})
    app = create_app(replica_settings)
    lease = install_leader_lease(app)
    lease.check_s = check_s
    dispatcher = install_run_queue(app)
    dispatcher.poll_s = 0.05  # a run queued through another replica is found by polling (2 s in production)
    app.after_serving(lease.release)
    return app, dispatcher, lease


async def _repo(settings: Settings) -> int:
    from coverset.db.session import Database

    database = Database.from_url(settings.database_url)
    try:
        async with database.session() as session:
            repo = Repo(forge="fake", external_id="1", full_name="acme/app")
            session.add(repo)
            await session.commit()
            return repo.id
    finally:
        await database.engine.dispose()


async def _queue(ctx: OrchestratorContext, repo_id: int) -> str:
    async with ctx.db.session() as session:
        run = Run(repo_id=repo_id, head_sha="a" * 40, trigger="manual", created_at=datetime.now(UTC))
        session.add(run)
        await session.commit()
        return run.id


def _recording_executor(record: dict[str, str]) -> Callable[[OrchestratorContext, str], Any]:
    async def fake_execute(context: OrchestratorContext, run_id: str) -> RunOutcome:
        record[run_id] = context.claimant
        async with context.db.session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            run.status, run.verdict = "completed", "success"
            await session.commit()
        return RunOutcome(run_id, "completed", "success", "ok")

    return fake_execute


async def _until(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_one_replica_dispatches_and_a_follower_takes_over(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db(settings)
    repo_id = await _repo(settings)
    dispatched: dict[str, str] = {}
    monkeypatch.setattr(queue_module, "execute_run", _recording_executor(dispatched))
    leader_app, leader_queue, leader_lease = _replica(settings, "a")
    follower_app, follower_queue, follower_lease = _replica(settings, "b")
    leader_ctx = OrchestratorContext.from_app(leader_app)
    follower_ctx = OrchestratorContext.from_app(follower_app)

    async def completed(run_id: str) -> bool:
        async with leader_ctx.db.session() as session:
            run = await session.get(Run, run_id)
            return run is not None and run.status == "completed"

    leader_serving = leader_app.test_app()
    await leader_serving.__aenter__()  # serves first, so it takes the lease
    try:
        await _until(lambda: _true(leader_lease.held and leader_queue.started))
        async with follower_app.test_app():
            assert not follower_lease.held and not follower_queue.started
            first = await _queue(follower_ctx, repo_id)  # queued through the follower
            await _until(lambda: completed(first))
            assert dispatched[first] == leader_ctx.claimant
            await leader_serving.__aexit__(None, None, None)  # the leader stops and releases the lease
            leader_serving = None
            await _until(lambda: _true(follower_lease.held and follower_queue.started))
            second = await _queue(follower_ctx, repo_id)
            await _until(lambda: completed(second))
            assert dispatched[second] == follower_ctx.claimant
    finally:
        if leader_serving is not None:
            await leader_serving.__aexit__(None, None, None)
    assert sorted(dispatched) == sorted([first, second])  # each run dispatched once


async def _true(value: bool) -> bool:
    return value


async def test_background_tasks_run_only_on_the_leader(settings: Settings) -> None:
    await init_db(settings)
    ran: list[str] = []

    async def probe(name: str, stop: asyncio.Event) -> None:
        ran.append(name)
        await stop.wait()

    apps = []
    for name in ("a", "b"):
        app = create_app(settings.model_copy(update={"data_dir": settings.data_dir / name}))
        lease = install_leader_lease(app)
        lease.check_s = 0.05

        @app.before_serving
        async def _start(app: Quart = app, name: str = name) -> None:
            add_leader_task(app, probe, name, app.shutdown_event)

        app.after_serving(lease.release)
        apps.append(app)
    async with apps[0].test_app(), apps[1].test_app():
        await _until(lambda: _true(len(ran) == 1))
        await asyncio.sleep(0.2)
        assert len(ran) == 1  # never both
        holder = ran[0]
    assert holder in ("a", "b")


# -- boot handling and the sweep ------------------------------------------------------------------


async def _run(ctx: OrchestratorContext, repo_id: int, **fields: Any) -> str:
    async with ctx.db.session() as session:
        run = Run(repo_id=repo_id, head_sha="b" * 40, **fields)
        session.add(run)
        await session.commit()
        return run.id


async def test_the_lease_holders_boot_keeps_runs_queued_after_it_started(settings: Settings) -> None:
    await init_db(settings)
    repo_id = await _repo(settings)
    app = create_app(settings)
    ctx = OrchestratorContext.from_app(app)
    booted = datetime.now(UTC)
    old = await _run(ctx, repo_id, trigger="manual", created_at=booted - timedelta(minutes=5))
    new = await _run(ctx, repo_id, trigger="manual", created_at=booted + timedelta(seconds=1))
    touched = await RunDispatcher(ctx).on_boot(created_before=booted)
    async with ctx.db.session() as session:
        statuses = dict((await session.execute(select(Run.id, Run.status))).all())
    assert touched == [old]
    assert statuses == {old: "abandoned", new: "queued"}
    await ctx.db.engine.dispose()


async def test_the_lease_holders_boot_keeps_runs_of_a_live_serve_on_this_host(settings: Settings) -> None:
    """Two replicas on one host: the new leader must not abandon runs the other replica (or the
    previous leader, finishing its runs after a transient loss of the lease) is still executing.
    The same-host rule is the pid, not the role."""
    await init_db(settings)
    repo_id = await _repo(settings)
    app = create_app(settings)
    ctx = OrchestratorContext.from_app(app)
    assert ctx.claimant.startswith("serve@")
    host = socket.gethostname()[:64] or "localhost"
    exited = subprocess.Popen(["true"])  # a serve of this host that exited
    exited.wait()
    dead_pid = exited.pid
    now = datetime.now(UTC)
    fields = dict(trigger="manual", status="running", heartbeat_at=now, created_at=now - timedelta(hours=1))
    other_replica = await _run(ctx, repo_id, claimed_by=f"serve@{host}:{os.getpid()}:abcd1234", **fields)
    earlier_serve = await _run(ctx, repo_id, claimed_by=f"serve@{host}:{dead_pid}:abcd1234", **fields)
    dispatcher = RunDispatcher(ctx)
    assert await dispatcher.on_boot(created_before=now) == [earlier_serve]
    assert await dispatcher.on_boot() == [other_replica]  # the single-process rule, unchanged
    await ctx.db.engine.dispose()


async def test_the_sweep_abandons_runs_whose_executor_died(settings: Settings) -> None:
    await init_db(settings)
    repo_id = await _repo(settings)
    app = create_app(settings)
    ctx = OrchestratorContext.from_app(app)
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=10)
    elsewhere = "serve@other-host:1:dead"
    dead = await _run(
        ctx, repo_id, trigger="manual", status="running", claimed_by=elsewhere, heartbeat_at=stale
    )
    alive = await _run(
        ctx, repo_id, trigger="manual", status="running", claimed_by=elsewhere, heartbeat_at=now
    )
    upload = await _run(
        ctx, repo_id, trigger="upload", status="running", claimed_by=elsewhere, heartbeat_at=stale
    )
    mine = await _run(
        ctx, repo_id, trigger="manual", status="running", claimed_by=ctx.claimant, heartbeat_at=stale
    )
    dispatcher = RunDispatcher(ctx)
    dispatcher.active[mine] = asyncio.get_running_loop().create_future()  # executing here
    assert await dispatcher.sweep_stale() == [dead]
    async with ctx.db.session() as session:
        statuses = dict((await session.execute(select(Run.id, Run.status))).all())
    assert statuses == {dead: "abandoned", alive: "running", upload: "running", mine: "running"}
    dispatcher.active.clear()
    await ctx.db.engine.dispose()
