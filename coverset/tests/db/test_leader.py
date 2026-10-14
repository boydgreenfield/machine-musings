"""The leader lease: one holder per database, handed over when the holder releases it or its
session dies; :func:`run_while_leader` follows it; transaction locks serialise across "replicas"
(engines)."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text

from coverset.config import Settings
from coverset.db.leader import (
    BACKGROUND_LEASE,
    LeaderLease,
    leader_status,
    run_while_leader,
    transaction_lock,
)
from coverset.db.session import Database, make_engine
from coverset.db.testing import PostgresTestDatabases


class _Switch:
    """A stand-in lease that a test flips by hand (``run_while_leader`` needs only these members)."""

    name = "switch"

    def __init__(self) -> None:
        self._held, self._lost = asyncio.Event(), asyncio.Event()
        self._lost.set()

    @property
    def held(self) -> bool:
        return self._held.is_set()

    def flip(self, held: bool) -> None:
        (self._held if held else self._lost).set()
        (self._lost if held else self._held).clear()

    async def wait_held(self) -> None:
        await self._held.wait()

    async def wait_lost(self) -> None:
        await self._lost.wait()


async def test_run_while_leader_starts_and_cancels_the_loop() -> None:
    switch, stop = _Switch(), asyncio.Event()
    starts: list[int] = []
    cancels: list[int] = []

    async def loop() -> None:
        starts.append(len(starts))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancels.append(len(cancels))
            raise

    runner = asyncio.create_task(run_while_leader(switch, loop, stop, label="probe"))  # type: ignore[arg-type]
    await asyncio.sleep(0.02)
    assert starts == []  # a follower runs nothing
    switch.flip(True)
    await asyncio.sleep(0.02)
    assert starts == [0] and cancels == []
    switch.flip(False)
    await asyncio.sleep(0.02)
    assert cancels == [0]
    switch.flip(True)
    await asyncio.sleep(0.02)
    assert starts == [0, 1]
    stop.set()
    await asyncio.wait_for(runner, 1)
    assert cancels == [0, 1]


async def test_run_while_leader_returns_when_the_loop_returns() -> None:
    switch, stop = _Switch(), asyncio.Event()
    switch.flip(True)

    async def loop() -> None:
        return None

    await asyncio.wait_for(run_while_leader(switch, loop, stop), 1)  # type: ignore[arg-type]


async def test_run_while_leader_restarts_a_loop_that_crashed(caplog: pytest.LogCaptureFixture) -> None:
    """A takeover whose boot handling fails must not leave a holder that keeps the lease and
    dispatches nothing: the loop is started again after ``retry_s`` while the lease is held."""
    switch, stop = _Switch(), asyncio.Event()
    switch.flip(True)
    starts: list[int] = []

    async def loop() -> None:
        starts.append(len(starts))
        if len(starts) == 1:
            raise RuntimeError("boot handling failed")
        await stop.wait()

    with caplog.at_level("ERROR", logger="coverset.db.leader"):
        runner = asyncio.create_task(
            run_while_leader(switch, loop, stop, label="probe", retry_s=0.05)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        assert starts == [0] and not runner.done()  # crashed, waiting to retry
        await asyncio.sleep(0.1)
        assert starts == [0, 1] and not runner.done()  # started again, now running
        stop.set()
        await asyncio.wait_for(runner, 1)
    assert starts == [0, 1]
    assert any("probe failed; it starts again in 0 s" in record.message for record in caplog.records)


async def test_a_lost_lease_cancels_the_retry() -> None:
    switch, stop = _Switch(), asyncio.Event()
    switch.flip(True)
    starts: list[int] = []

    async def loop() -> None:
        starts.append(len(starts))
        raise RuntimeError("boom")

    runner = asyncio.create_task(
        run_while_leader(switch, loop, stop, retry_s=0.1)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.02)
    switch.flip(False)  # lost during the backoff: no retry as a follower
    await asyncio.sleep(0.15)
    assert starts == [0]
    switch.flip(True)  # regained: the loop starts again at once
    await asyncio.sleep(0.02)
    assert starts == [0, 1]
    stop.set()
    await asyncio.wait_for(runner, 1)


async def test_one_holder_and_a_handover_on_release(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = make_engine(settings.database_url), make_engine(settings.database_url)
    a, b = LeaderLease(first, "test:lease"), LeaderLease(second, "test:lease")
    try:
        assert a.held is False
        assert await a.acquire() is True and a.role == "leader"
        assert await b.acquire() is False and b.role == "follower"
        assert await a.check() and not await b.check()
        status = await leader_status(second, "test:lease")
        assert status.holder_pid is not None and status.application_name == "coverset"
        assert "held by backend pid" in status.detail
        with caplog.at_level("INFO", logger="coverset.db.leader"):
            await a.release()
        assert not a.held
        released = [r for r in caplog.records if "test:lease" in r.message]
        assert [(r.levelname, "released" in r.message) for r in released] == [("INFO", True)]  # not "lost"
        assert await b.check() and b.held
        assert (await leader_status(first, "other")).holder_pid is None
    finally:
        await a.release()
        await b.release()
        await first.dispose()
        await second.dispose()


async def test_a_dead_session_loses_the_lease(settings: Settings) -> None:
    holder_engine, other_engine = make_engine(settings.database_url), make_engine(settings.database_url)
    holder, other = LeaderLease(holder_engine, "test:dead"), LeaderLease(other_engine, "test:dead")
    try:
        assert await holder.acquire()
        pid = (await leader_status(other_engine, "test:dead")).holder_pid
        async with other_engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            ).scalar()
        assert await other.check()  # the lock went with the session
        assert not await holder.check() and holder.role == "follower"
    finally:
        await holder.release()
        await other.release()
        await holder_engine.dispose()
        await other_engine.dispose()


async def test_keep_takes_a_freed_lease_within_one_check(settings: Settings) -> None:
    first, second = make_engine(settings.database_url), make_engine(settings.database_url)
    a, b = LeaderLease(first, "test:keep"), LeaderLease(second, "test:keep", check_s=0.05)
    stop = asyncio.Event()
    keeper = asyncio.create_task(b.keep(stop))
    try:
        assert await a.acquire()
        await asyncio.sleep(0.15)
        assert not b.held
        await a.release()
        await asyncio.wait_for(b.wait_held(), 2)
    finally:
        stop.set()
        await keeper
        await a.release()
        await b.release()
        await first.dispose()
        await second.dispose()


async def test_transaction_locks_serialise_two_engines(settings: Settings) -> None:
    """Two replicas (engines) registering an upload of the same commit take turns."""
    replicas = [Database.from_url(settings.database_url), Database.from_url(settings.database_url)]
    spans: list[tuple[float, float]] = []

    async def critical(database: Database) -> None:
        async with database.session() as session:
            await transaction_lock(session, "upload:1:abc")
            started = time.monotonic()
            await asyncio.sleep(0.1)
            spans.append((started, time.monotonic()))
            await session.commit()

    try:
        await asyncio.gather(*(critical(database) for database in replicas))
    finally:
        for database in replicas:
            await database.engine.dispose()
    (first_start, first_end), (second_start, _) = sorted(spans)
    assert second_start >= first_end


async def test_a_lease_on_another_database_of_the_same_server_is_not_this_ones(
    settings: Settings, postgres_databases: PostgresTestDatabases
) -> None:
    """Advisory locks are per database, but ``pg_locks`` lists every database's: another
    installation's (or a developer's ``coverset dev``) lease must not show as held here."""
    other_url = await asyncio.to_thread(postgres_databases.create)  # the harness runs its own loop
    other_engine, this_engine = make_engine(other_url), make_engine(settings.database_url)
    other = LeaderLease(other_engine, BACKGROUND_LEASE)
    try:
        assert await other.acquire()
        assert (await leader_status(other_engine)).holder_pid is not None
        here = await leader_status(this_engine)
        assert here.holder_pid is None and "free" in here.detail
    finally:
        await other.release()
        await other_engine.dispose()
        await this_engine.dispose()
        await asyncio.to_thread(postgres_databases.retire, other_url)
