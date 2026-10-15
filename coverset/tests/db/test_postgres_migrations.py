"""The migration chain on a fresh database, the models compared with what it built ("alembic
check"), down to base and up again, and two replicas migrating at once."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text

from coverset.db.models import Base
from coverset.db.session import alembic_config, head_revisions, make_engine, migrate, run_schema_change
from coverset.db.testing import PostgresTestDatabases


@pytest.fixture
def empty_postgres(postgres_databases: PostgresTestDatabases) -> Iterator[str]:
    """A database with no schema at all."""
    url = postgres_databases.create(empty=True)
    yield url
    postgres_databases.drop(url)


def _drift(sync: Connection) -> list[object]:
    context = MigrationContext.configure(sync, opts={"compare_type": True})
    return compare_metadata(context, Base.metadata)


def _tables(sync: Connection) -> set[str]:
    return set(inspect(sync).get_table_names())


async def test_the_chain_builds_the_models_on_postgres_and_round_trips(empty_postgres: str) -> None:
    engine = make_engine(empty_postgres)
    try:
        applied = await migrate(engine)
        assert [step.split()[0] for step in applied] == [f"{n:04d}" for n in range(1, len(applied) + 1)]
        assert applied[-1].split()[0] in head_revisions()
        async with engine.connect() as connection:
            assert await connection.run_sync(_drift) == []
            assert set(Base.metadata.tables) <= await connection.run_sync(_tables)

        await run_schema_change(engine, lambda sync: command.downgrade(alembic_config(sync), "base"))
        async with engine.connect() as connection:
            assert await connection.run_sync(_tables) == {"alembic_version"}
            leftovers = (
                await connection.execute(text("SELECT relname FROM pg_class WHERE relkind = 'S'"))
            ).scalars()
            assert list(leftovers) == []  # every SERIAL sequence went with its table

        assert len(await migrate(engine)) == len(applied)
        async with engine.connect() as connection:
            assert await connection.run_sync(_drift) == []
    finally:
        await engine.dispose()


async def test_the_ai_review_column_moves_into_a_repository_row(empty_postgres: str) -> None:
    """0012: a repository that opted in with the old column gets ``policy.ai_review = true`` at repo
    scope, unless it already had its own row (which always won)."""
    engine = make_engine(empty_postgres)
    try:
        await run_schema_change(engine, lambda sync: command.upgrade(alembic_config(sync), "0011"))
        async with engine.begin() as connection:
            for rid, name, opted in ((1, "a/opted", True), (2, "a/own-row", True), (3, "a/plain", False)):
                await connection.execute(
                    text(
                        "INSERT INTO repos (id, forge, external_id, full_name, default_branch, private, language,"
                        " ai_review_enabled, settings_json, created_at)"
                        " VALUES (:id, 'github', :name, :name, 'main', true, 'python', :opted, '{}', now())"
                    ),
                    {"id": rid, "name": name, "opted": opted},
                )
            await connection.execute(
                text(
                    "INSERT INTO scoped_settings (scope_kind, scope_id, key, value, updated_at)"
                    " VALUES ('repo', '2', 'policy.ai_review', '{\"v\": false}', now())"
                )
            )
        await migrate(engine)
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT scope_id, value FROM scoped_settings WHERE key = 'policy.ai_review' ORDER BY scope_id"
                    )
                )
            ).all()
            columns = {
                c["name"] for c in await connection.run_sync(lambda s: inspect(s).get_columns("repos"))
            }
        assert [(sid, value) for sid, value in rows] == [("1", {"v": True}), ("2", {"v": False})]
        assert "ai_review_enabled" not in columns
    finally:
        await engine.dispose()


async def test_postgres_column_types(empty_postgres: str) -> None:
    engine = make_engine(empty_postgres)
    try:
        await migrate(engine)
        async with engine.connect() as connection:
            types = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
                            "WHERE table_schema = 'public'"
                        )
                    )
                ).all()
            )
            sequences = set(
                (await connection.execute(text("SELECT seqtypid::regtype::text FROM pg_sequence"))).scalars()
            )
            lengths = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name, character_maximum_length FROM information_schema.columns "
                            "WHERE table_name = 'runs' AND column_name IN ('status', 'trigger')"
                        )
                    )
                ).all()
            )
    finally:
        await engine.dispose()
    assert types["repos.id"] == types["runs.repo_id"] == types["run_events.id"] == "bigint"
    assert types["test_results.test_id"] == types["audit_log.id"] == "bigint"
    assert types["shards.shard_id"] == "integer"  # a position within a run, not an id
    assert types["runs.created_at"] == "timestamp with time zone"
    assert types["runs.timeline"] == "json"  # sa.JSON, not JSONB (docs/postgres.md)
    assert types["tests.nodeid_hash"] == "bytea"
    assert types["repos.private"] == "boolean"
    assert sequences == {"bigint"}
    assert lengths == {"status": 32, "trigger": 32}


async def test_two_replicas_migrating_at_once_take_turns(empty_postgres: str) -> None:
    """The advisory lock serialises them: one applies the chain, the other finds it at head."""
    first, second = make_engine(empty_postgres), make_engine(empty_postgres)
    try:
        results = await asyncio.gather(migrate(first), migrate(second))
    finally:
        await first.dispose()
        await second.dispose()
    revisions = len(list(ScriptDirectory.from_config(alembic_config()).walk_revisions()))
    assert sorted(len(applied) for applied in results) == [0, revisions]
