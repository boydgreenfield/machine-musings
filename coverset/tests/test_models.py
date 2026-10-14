"""Migrations build the full schema; rows round-trip; constraints and pragmas are enforced.

Runs on the suite's backend: SQLite files by default, Postgres under ``make test-pg``
(``COVERSET_TEST_DATABASE_URL``); ``sqlite_only`` tests check SQLite mechanics (pragmas, batch
table rebuilds, ``PRAGMA foreign_key_check``) that Postgres does not have.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from coverset.config import Settings
from coverset.db.models import (
    Base,
    Commit,
    Repo,
    Run,
    RunEvent,
    TestCase,
    TestResult,
    WebhookDelivery,
)
from coverset.db.session import Database, MigrationError, init_db, migrate, run_schema_change
from coverset.db.types import new_id


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database.from_url(settings.database_url)
    await init_db(settings, database.engine)
    yield database
    await database.engine.dispose()


def nodeid_hash(nodeid: str) -> bytes:
    return hashlib.blake2b(nodeid.encode(), digest_size=16).digest()


async def make_repo(db: Database, external_id: str = "1001") -> Repo:
    async with db.session() as session:
        repo = Repo(forge="fake", external_id=external_id, full_name=f"acme/app-{external_id}")
        session.add(repo)
        await session.commit()
        return repo


async def test_migration_creates_every_model_table(db: Database) -> None:
    async with db.engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


async def test_migrations_match_models(db: Database) -> None:
    """The Alembic revisions and the models must not drift (regenerate the revision if this fails)."""

    def diff(sync: Connection) -> list[object]:
        context = MigrationContext.configure(sync, opts={"compare_type": True, "render_as_batch": True})
        return compare_metadata(context, Base.metadata)

    async with db.engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_migrate_is_idempotent(db: Database) -> None:
    await migrate(db.engine)
    async with db.engine.connect() as conn:
        versions = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    assert len(versions) == 1


@pytest.mark.sqlite_only
async def test_sqlite_pragmas(db: Database) -> None:
    async with db.engine.connect() as conn:
        pragmas = {
            name: (await conn.execute(text(f"PRAGMA {name}"))).scalar()
            for name in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys")
        }
    assert pragmas == {"journal_mode": "wal", "synchronous": 1, "busy_timeout": 5000, "foreign_keys": 1}


async def test_repo_run_test_result_event_round_trip(db: Database) -> None:
    repo = await make_repo(db)
    started = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    async with db.session() as session:
        run = Run(repo_id=repo.id, head_sha="a" * 40, base_sha="b" * 40, branch="main", trigger="push")
        test = TestCase(
            repo_id=repo.id,
            nodeid="tests/test_money.py::test_allocate[3]",
            nodeid_hash=nodeid_hash("tests/test_money.py::test_allocate[3]"),
            family="tests/test_money.py::test_allocate",
        )
        session.add_all([run, test])
        await session.flush()
        session.add_all(
            [
                TestResult(
                    run_id=run.id,
                    test_id=test.id,
                    outcome="failed",
                    failed_phase="call",
                    fail_sig=b"\x01" * 8,
                    duration_ms=12.5,
                    score=0.93,
                    score_explanation={"recent_fail": 0.6, "touches_change": 0.33},
                    rank=1,
                ),
                RunEvent(run_id=run.id, seq=1, kind="test", payload={"outcome": "failed"}, at=started),
            ]
        )
        await session.commit()
        run_id = run.id

    assert re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", run_id)
    async with db.session() as session:
        loaded = await session.get(Run, run_id)
        assert loaded is not None
        assert (loaded.status, loaded.executor, loaded.timeline) == ("queued", "local", {})
        assert loaded.created_at.tzinfo is UTC
        result = (await session.execute(select(TestResult).where(TestResult.run_id == run_id))).scalar_one()
        assert result.outcome == "failed"
        assert result.score_explanation == {"recent_fail": 0.6, "touches_change": 0.33}
        assert result.fail_sig == b"\x01" * 8
        event = (await session.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalar_one()
        assert event.payload == {"outcome": "failed"}
        assert event.at == started
        assert event.at.tzinfo is UTC


async def test_datetimes_normalised_to_utc_and_naive_rejected(db: Database) -> None:
    repo = await make_repo(db)
    plus_two = datetime(2026, 10, 10, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    async with db.session() as session:
        session.add(RunEvent(run_id=(await _run(db, repo)).id, seq=1, kind="log", at=plus_two))
        await session.commit()
        stored = (await session.execute(select(RunEvent))).scalar_one()
    async with db.session() as session:
        reloaded = await session.get(RunEvent, stored.id)
        assert reloaded is not None
        assert reloaded.at == datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
        assert reloaded.at.tzinfo is UTC
    async with db.session() as session:
        session.add(Repo(forge="fake", external_id="naive", full_name="a/b", created_at=datetime(2026, 1, 1)))
        with pytest.raises(StatementError, match="naive datetime"):
            await session.commit()


async def _run(db: Database, repo: Repo) -> Run:
    async with db.session() as session:
        run = Run(repo_id=repo.id, head_sha="c" * 40, trigger="manual")
        session.add(run)
        await session.commit()
        return run


async def test_enum_values_validated(db: Database) -> None:
    repo = await make_repo(db)
    async with db.session() as session:
        session.add(Run(repo_id=repo.id, head_sha="d" * 40, trigger="manual", status="exploded"))
        with pytest.raises(StatementError):
            await session.commit()


async def _expect_integrity_error(db: Database, *rows: Base) -> None:
    async with db.session() as session:
        session.add_all(rows)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_repo_external_id_unique_per_forge(db: Database) -> None:
    await make_repo(db, "42")
    async with db.session() as session:
        session.add(Repo(forge="github", external_id="42", full_name="acme/app"))
        await session.commit()
    await _expect_integrity_error(db, Repo(forge="fake", external_id="42", full_name="other/name"))


async def test_test_nodeid_hash_unique_per_repo(db: Database) -> None:
    repo = await make_repo(db)
    digest = nodeid_hash("tests/test_a.py::test_x")
    async with db.session() as session:
        session.add(
            TestCase(repo_id=repo.id, nodeid="tests/test_a.py::test_x", nodeid_hash=digest, family="x")
        )
        await session.commit()
    await _expect_integrity_error(
        db, TestCase(repo_id=repo.id, nodeid="tests/test_a.py::test_x", nodeid_hash=digest, family="x")
    )


async def test_run_event_seq_unique_per_run(db: Database) -> None:
    run = await _run(db, await make_repo(db))
    async with db.session() as session:
        session.add(RunEvent(run_id=run.id, seq=1, kind="log"))
        await session.commit()
    await _expect_integrity_error(db, RunEvent(run_id=run.id, seq=1, kind="log"))


async def test_test_result_primary_key(db: Database) -> None:
    repo = await make_repo(db)
    run = await _run(db, repo)
    async with db.session() as session:
        test = TestCase(repo_id=repo.id, nodeid="t::a", nodeid_hash=nodeid_hash("t::a"), family="t::a")
        session.add(test)
        await session.flush()
        session.add(TestResult(run_id=run.id, test_id=test.id, outcome="passed"))
        await session.commit()
        test_id = test.id
    await _expect_integrity_error(db, TestResult(run_id=run.id, test_id=test_id, outcome="failed"))


async def test_webhook_delivery_id_unique(db: Database) -> None:
    async with db.session() as session:
        session.add(WebhookDelivery(delivery_id="d-1", forge="github", event="pull_request"))
        await session.commit()
    await _expect_integrity_error(db, WebhookDelivery(delivery_id="d-1", forge="github", event="push"))


async def test_foreign_keys_enforced(db: Database) -> None:
    await _expect_integrity_error(db, Run(repo_id=999, head_sha="e" * 40, trigger="manual"))


async def test_datetimes_keep_microseconds_and_compare_in_sql(db: Database) -> None:
    """``UTCDateTime`` on both backends: SQLite text with microseconds, Postgres ``timestamptz``
    through asyncpg (aware in, aware out); comparisons in SQL see the same instants."""
    repo = await make_repo(db)
    instants = [
        datetime(2026, 10, 10, 12, 0, 0, 123456, tzinfo=UTC) + timedelta(microseconds=n) for n in range(3)
    ]
    async with db.session() as session:
        run = Run(repo_id=repo.id, head_sha="9" * 40, trigger="manual")
        session.add(run)
        await session.flush()
        session.add_all([RunEvent(run_id=run.id, seq=n, kind="log", at=at) for n, at in enumerate(instants)])
        await session.commit()
    async with db.session() as session:
        loaded = list(await session.scalars(select(RunEvent.at).order_by(RunEvent.seq)))
        later = list(await session.scalars(select(RunEvent.seq).where(RunEvent.at > instants[0])))
    assert loaded == instants and all(at.tzinfo is UTC for at in loaded)
    assert later == [1, 2]
    async with db.engine.connect() as conn:
        stored = (await conn.execute(text("SELECT at FROM run_events WHERE seq = 0"))).scalar()
    if db.engine.dialect.name == "postgresql":
        assert isinstance(stored, datetime) and stored.utcoffset() == timedelta(0)
    else:
        assert stored == "2026-10-10 12:00:00.123456"


async def test_integer_ids_accept_any_int64(db: Database) -> None:
    """Ids are ``BIGINT`` on Postgres (``BigIntId``): the largest id a URL may carry is a miss,
    not an int4 overflow; SQLite's ``INTEGER`` is 64-bit anyway."""
    async with db.session() as session:
        assert await session.get(Repo, 2**63 - 1) is None
        assert await session.get(TestCase, 2**31) is None
    repo = await make_repo(db)
    async with db.session() as session:
        session.add(Repo(id=2**40, forge="fake", external_id="big", full_name="acme/big"))
        session.add(Commit(repo_id=repo.id, sha="1" * 40))
        await session.commit()
        assert (await session.get(Repo, 2**40)) is not None


def test_new_id_sorts_by_creation_time() -> None:
    ids = [new_id("run") for _ in range(50)]
    assert all(re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", value) for value in ids)
    assert len(set(ids)) == 50
    assert [value[4:14] for value in ids] == sorted(value[4:14] for value in ids)


async def test_json_columns_track_in_place_changes(db: Database) -> None:
    run = await _run(db, (repo := await make_repo(db)))
    async with db.session() as session:
        commit = Commit(repo_id=repo.id, sha="f" * 40)
        session.add(commit)
        await session.commit()
    async with db.session() as session:
        loaded = await session.get(Run, run.id)
        stored_commit = await session.get(Commit, commit.id)
        assert loaded is not None and stored_commit is not None
        loaded.timeline["queued"] = "2026-10-10T12:00:00Z"
        stored_commit.parent_shas.append("0" * 40)
        assert session.is_modified(loaded) and session.is_modified(stored_commit)
        await session.commit()
    async with db.session() as session:
        reloaded = await session.get(Run, run.id)
        reloaded_commit = await session.get(Commit, commit.id)
        assert reloaded is not None and reloaded_commit is not None
        assert reloaded.timeline == {"queued": "2026-10-10T12:00:00Z"}
        assert reloaded_commit.parent_shas == ["0" * 40]


@pytest.mark.sqlite_only
async def test_batch_migration_on_parent_keeps_child_rows(db: Database) -> None:
    """A batch migration recreates the parent table; with foreign keys on, that would cascade."""
    run = await _run(db, await make_repo(db))

    def recreate_repos(sync: Connection) -> None:
        operations = Operations(MigrationContext.configure(sync))
        with operations.batch_alter_table("repos", recreate="always") as batch:
            batch.add_column(sa.Column("probe", sa.Integer))

    await run_schema_change(db.engine, recreate_repos)
    async with db.session() as session:
        assert await session.get(Run, run.id) is not None


async def test_failed_schema_change_rolls_back_ddl(db: Database) -> None:
    def half_done(sync: Connection) -> None:
        sync.exec_driver_sql("CREATE TABLE probe_t (id INTEGER PRIMARY KEY)")
        raise RuntimeError("revision failed halfway")

    with pytest.raises(RuntimeError, match="halfway"):
        await run_schema_change(db.engine, half_done)
    async with db.engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert "probe_t" not in tables


@pytest.mark.sqlite_only
async def test_schema_change_leaving_orphans_is_rolled_back(db: Database) -> None:
    def orphan(sync: Connection) -> None:
        sync.exec_driver_sql(
            'INSERT INTO runs (id, repo_id, head_sha, "trigger", status, trusted, executor, shard_count,'
            " coverage_complete, cancelled_tests, cost_estimate, timeline, created_at)"
            " VALUES ('run_orphan', 999, 'a', 'manual', 'queued', 1, 'local', 0, 0, 0, 0, '{}',"
            " '2026-10-10 12:00:00.000000')"
        )

    with pytest.raises(MigrationError, match="runs"):
        await run_schema_change(db.engine, orphan)
    async with db.session() as session:
        assert await session.get(Run, "run_orphan") is None


async def test_migrate_restores_connection_state() -> None:
    """In-memory SQLite shares one connection, so the migration must hand it back as it found it."""
    database = Database.from_url("sqlite+aiosqlite:///:memory:")
    try:
        await migrate(database.engine)
        async with database.engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            raw = await conn.get_raw_connection()
            assert raw.dbapi_connection is not None
            assert raw.dbapi_connection.isolation_level == ""
        await _expect_integrity_error(database, Run(repo_id=999, head_sha="e" * 40, trigger="manual"))
    finally:
        await database.engine.dispose()


def test_every_foreign_key_column_leads_an_index() -> None:
    """SQLite looks child rows up by FK column on every parent delete (run retention, repo purge)."""
    missing = []
    for table in Base.metadata.sorted_tables:
        leading = {index.columns[0].name for index in table.indexes}
        leading |= {
            next(iter(c.columns)).name for c in table.constraints if isinstance(c, sa.UniqueConstraint)
        }
        leading.add(table.primary_key.columns[0].name)
        missing += [
            f"{table.name}.{fk.parent.name}" for fk in table.foreign_keys if fk.parent.name not in leading
        ]
    assert missing == []
