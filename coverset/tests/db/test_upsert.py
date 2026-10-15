"""``coverset.db.upsert.upsert``: insert, update chosen columns, ignore pure-key conflicts, and
concurrent writers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from coverset.db.models import Env, Repo, TestCase, TestStats
from coverset.db.session import Database
from coverset.db.upsert import upsert

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


async def _envs(db: Database) -> dict[str, tuple[str, str | None]]:
    async with db.session() as session:
        return {row.env_hash: (row.python, row.image_id) for row in await session.scalars(select(Env))}


async def test_insert_then_update_only_the_named_columns(db: Database) -> None:
    async with db.session() as session:
        await upsert(
            session,
            Env,
            [{"env_hash": "e1", "python": "3.13", "image_id": "im-1", "created_at": NOW}],
            keys=["env_hash"],
            update_columns=["image_id"],
        )
        await session.commit()
    async with db.session() as session:
        await upsert(
            session,
            Env,
            [
                {"env_hash": "e1", "python": "3.12", "image_id": "im-2", "created_at": NOW},
                {"env_hash": "e2", "python": "3.12", "image_id": None, "created_at": NOW},
            ],
            keys=["env_hash"],
            update_columns=["image_id"],
        )
        await session.commit()
    assert await _envs(db) == {"e1": ("3.13", "im-2"), "e2": ("3.12", None)}


async def test_default_updates_every_non_key_column_and_rows_may_differ_in_columns(db: Database) -> None:
    async with db.session() as session:
        repo = Repo(forge="fake", external_id="1", full_name="acme/app")
        session.add(repo)
        await session.flush()
        test = TestCase(repo_id=repo.id, nodeid="t::a", nodeid_hash=b"\x01" * 16, family="t::a")
        session.add(test)
        await session.flush()
        keys = {"repo_id": repo.id, "test_id": test.id}
        await upsert(session, TestStats, [{**keys, "runs_n": 1, "fails_n": 0}], keys=["repo_id", "test_id"])
        await upsert(
            session,
            TestStats,
            [{**keys, "runs_n": 2, "fails_n": 1, "p50_ms": 12.5}],
            keys=["repo_id", "test_id"],
        )
        await session.commit()
    async with db.session() as session:
        stats = (await session.scalars(select(TestStats))).one()
        assert (stats.runs_n, stats.fails_n, stats.p50_ms) == (2, 1, 12.5)


async def test_only_keys_means_insert_or_ignore(db: Database) -> None:
    async with db.session() as session:
        await upsert(
            session,
            Env.__table__,
            [{"env_hash": "e1", "python": "3.13", "created_at": NOW}],
            keys=["env_hash"],
        )
        await upsert(
            session,
            Env.__table__,
            [{"env_hash": "e1", "python": "3.11", "created_at": NOW}],
            keys=["env_hash"],
            update_columns=[],
        )
        await session.commit()
    assert await _envs(db) == {"e1": ("3.13", None)}


async def test_concurrent_writers_of_one_key_both_succeed(db: Database) -> None:
    """Two transactions racing on one primary key: the second waits for the first and then updates
    its row (no unique violation)."""

    async def write(image: str) -> None:
        async with db.session() as session:
            row = {"env_hash": "shared", "python": "3.13", "image_id": image, "created_at": NOW}
            await upsert(session, Env, [row], keys=["env_hash"], update_columns=["image_id"])
            await asyncio.sleep(0.05)  # hold the row lock
            await session.commit()

    await asyncio.gather(*(write(f"im-{n}") for n in range(4)))
    envs = await _envs(db)
    assert list(envs) == ["shared"] and envs["shared"][1] in {f"im-{n}" for n in range(4)}


async def test_overlapping_batches_in_opposite_orders_do_not_deadlock(db: Database) -> None:
    """Two runs recording ``test_stats`` for the same tests (one ``upsert`` each, rows in different
    orders): rows are locked in key order, so on Postgres the second waits for the first instead
    of failing with a deadlock."""
    async with db.session() as session:
        for name in [f"e{n:05d}" for n in range(4000)]:
            session.add(Env(env_hash=name, python="3.13", created_at=NOW))
        await session.commit()
    names = [f"e{n:05d}" for n in range(4000)]

    async def write(order: list[str], image: str) -> None:
        async with db.session() as session:
            rows = [
                {"env_hash": name, "python": "3.13", "image_id": image, "created_at": NOW} for name in order
            ]
            await upsert(session, Env, rows, keys=["env_hash"], update_columns=["image_id"])
            await asyncio.sleep(0.05)  # keep the row locks a while
            await session.commit()

    await asyncio.gather(write(names, "forward"), write(names[::-1], "backward"))
    assert {image for _python, image in (await _envs(db)).values()} <= {"forward", "backward"}
