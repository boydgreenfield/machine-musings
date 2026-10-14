"""Writes that must stay correct with several writers: the dispatcher's atomic claim on the suite's
backend (Postgres READ COMMITTED under ``make test-pg``, SQLite's single writer otherwise), and an
upload's registration from two replicas (two engines, no shared in-process lock) on Postgres."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import Repo, Run, Shard
from coverset.db.session import Database
from coverset.facts.store import FactsStore
from coverset.orchestrator.runner import OrchestratorContext, _Execution
from coverset.orchestrator.upload import UnpackedUpload, UploadMeta, register_upload


async def _repo(db: Database) -> int:
    async with db.session() as session:
        repo = Repo(forge="fake", external_id="7", full_name="acme/app")
        session.add(repo)
        await session.commit()
        return repo.id


async def _queued_run(db: Database, repo_id: int) -> str:
    async with db.session() as session:
        run = Run(repo_id=repo_id, head_sha="a" * 40, trigger="manual")
        session.add(run)
        await session.commit()
        return run.id


async def test_exactly_one_claimant_wins_a_queued_run(db: Database, settings: Settings) -> None:
    """The runner's claim (:meth:`_Execution._claim`, what ``execute_run`` calls first: ``UPDATE ... WHERE status = 'queued'``) raced
    by four replicas: under READ COMMITTED a second UPDATE waits for the first, re-reads the row,
    finds it no longer queued and changes nothing, so exactly one execution gets the run back."""
    repo_id = await _repo(db)
    run_id = await _queued_run(db, repo_id)
    url = db.engine.url.render_as_string(hide_password=False)
    replicas = [
        OrchestratorContext(
            settings=settings,
            db=Database.from_url(url),
            store=FactsStore(settings.facts_dir),
            claimant=f"serve@replica-{n}:1:{n:08d}",
        )
        for n in range(4)
    ]
    runners = [_Execution(ctx, run_id, on_event=None, shards_override=None) for ctx in replicas]
    try:
        claimed = await asyncio.gather(*(runner._claim() for runner in runners))
    finally:
        for ctx in replicas:
            await ctx.db.engine.dispose()
    winners = [runner.ctx.claimant for runner, run in zip(runners, claimed, strict=True) if run is not None]
    assert len(winners) == 1, winners
    assert [run.status for run in claimed if run is not None] == ["running"]
    async with db.session() as session:
        row = await session.get(Run, run_id)
        assert row is not None
        assert (row.status, row.claimed_by) == ("running", winners[0])
        assert row.heartbeat_at is not None


async def test_a_claimed_run_without_a_heartbeat_is_claimed_again_by_its_claimant_only(
    db: Database, settings: Settings
) -> None:
    """The claim's other branch: a ``running`` run ``create_run(claim=True)`` inserted for this
    context (no heartbeat yet) is claimed by that context and by nobody else."""
    repo_id = await _repo(db)
    ours, theirs = (
        OrchestratorContext(
            settings=settings, db=db, store=FactsStore(settings.facts_dir), claimant=f"serve@{name}:1:x"
        )
        for name in ("ours", "theirs")
    )
    async with db.session() as session:
        run = Run(
            repo_id=repo_id, head_sha="d" * 40, trigger="manual", status="running", claimed_by=ours.claimant
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    assert await _Execution(theirs, run_id, on_event=None, shards_override=None)._claim() is None
    claimed = await _Execution(ours, run_id, on_event=None, shards_override=None)._claim()
    assert claimed is not None and claimed.claimed_by == ours.claimant and claimed.heartbeat_at is not None
    assert (
        await _Execution(ours, run_id, on_event=None, shards_override=None)._claim() is None
    )  # has a heartbeat now


async def test_two_replicas_registering_one_commit_share_one_run(
    db: Database, settings: Settings, tmp_path: Path, postgres_url: str
) -> None:
    """Postgres only: replicas share no in-process lock, so ``register_upload`` takes a
    transaction-scoped advisory lock per commit. (One process serves a SQLite database, and the
    upload view's per-commit ``asyncio.Lock`` serialises its registrations.)"""
    repo_id = await _repo(db)
    url = db.engine.url.render_as_string(hide_password=False)
    replicas = [Database.from_url(url), Database.from_url(url)]

    def upload(label: str) -> tuple[UploadMeta, UnpackedUpload]:
        root = tmp_path / f"unpacked-{label}"
        root.mkdir()
        (root / "bundle.txt").write_text(label)
        digest = hashlib.sha256(label.encode()).hexdigest()
        meta = UploadMeta(
            repo_id=repo_id,
            head_sha="c" * 40,
            base_sha=None,
            branch="main",
            pr_number=None,
            label=label,
            archive_sha256=digest,
            archive_bytes=10,
        )
        return meta, UnpackedUpload(root=root, has_python=True, has_neutral=False, has_sources=False)

    try:
        registrations = await asyncio.gather(
            *(
                register_upload(database, settings, *upload(label), claimant=f"serve@{label}")
                for database, label in zip(replicas, ("py312", "py313"), strict=True)
            )
        )
    finally:
        for database in replicas:
            await database.engine.dispose()
    assert len({registration.run_id for registration in registrations}) == 1
    assert sorted(registration.created for registration in registrations) == [False, True]
    async with db.session() as session:
        runs = list(await session.scalars(select(Run).where(Run.trigger == "upload")))
        shards = sorted(await session.scalars(select(Shard.shard_id)))
    assert len(runs) == 1 and shards == [0, 1]
