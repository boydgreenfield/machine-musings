"""Engines for both backends: pool and connection settings, hosted-URL handling, the fast
"already at head" migration path and the database report."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import StaticPool

from coverset.config import Settings
from coverset.db import session as db_session
from coverset.db.session import (
    POSTGRES_SERVER_SETTINGS,
    WEB_STATEMENT_TIMEOUT_S,
    Database,
    check_database,
    head_revisions,
    init_db,
    lift_statement_timeout,
    make_engine,
    migrate,
    pooler_warning,
    postgres_connect_args,
    probe_database,
    sqlite_file_path,
)

PG = "postgresql+asyncpg://user:secret@db.example.com:5432/coverset"


def test_hosted_libpq_urls_are_translated_for_asyncpg() -> None:
    """Neon and Supabase hand out ``?sslmode=require&channel_binding=require``; asyncpg refuses
    both as keywords, so they become ``ssl`` and nothing."""
    url, args = postgres_connect_args(
        make_url(PG + "?sslmode=require&channel_binding=require&connect_timeout=7&application_name=web-1")
    )
    assert dict(url.query) == {}
    assert args["ssl"] == "require"
    assert args["timeout"] == 7.0
    assert args["server_settings"] == {**POSTGRES_SERVER_SETTINGS, "application_name": "web-1"}


def test_postgres_server_settings_and_statement_timeout() -> None:
    url, args = postgres_connect_args(make_url(PG), statement_timeout_s=WEB_STATEMENT_TIMEOUT_S)
    assert url == make_url(PG)
    assert args == {"server_settings": {**POSTGRES_SERVER_SETTINGS, "statement_timeout": "30000"}}
    assert POSTGRES_SERVER_SETTINGS["application_name"] == "coverset"
    assert POSTGRES_SERVER_SETTINGS["timezone"] == "UTC"
    # A leader host that vanishes is noticed in idle + count x interval = 90 s, not the OS's 2 h.
    assert (
        int(POSTGRES_SERVER_SETTINGS["tcp_keepalives_idle"])
        + int(POSTGRES_SERVER_SETTINGS["tcp_keepalives_count"])
        * int(POSTGRES_SERVER_SETTINGS["tcp_keepalives_interval"])
    ) == 90
    assert "statement_timeout" not in postgres_connect_args(make_url(PG))[1]["server_settings"]


async def test_postgres_engine_pool_without_connecting() -> None:
    engine = make_engine(PG + "?sslmode=require")
    try:
        pool = engine.pool
        assert pool.size() == 10
        assert pool._max_overflow == 10  # type: ignore[attr-defined]
        assert pool._pre_ping is True
        assert pool._recycle == 1800
        assert engine.url.query == {}
    finally:
        await engine.dispose()


async def test_lifting_the_statement_timeout_is_a_no_op_on_sqlite(tmp_path: Path) -> None:
    database = Database.from_url(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    try:
        async with database.session() as session:
            await lift_statement_timeout(session)  # no SET LOCAL sent: SQLite would reject it
            assert (await session.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await database.engine.dispose()


async def test_sqlite_engines_are_unchanged(tmp_path: Path) -> None:
    memory = make_engine("sqlite+aiosqlite:///:memory:", statement_timeout_s=5)
    on_disk = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    try:
        assert isinstance(memory.pool, StaticPool)
        assert on_disk.pool.size() == 5  # SQLAlchemy's default, no Postgres pool settings
        async with on_disk.connect() as connection:
            assert (await connection.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
    finally:
        await memory.dispose()
        await on_disk.dispose()


def test_sqlite_file_path_is_none_for_postgres(tmp_path: Path) -> None:
    assert sqlite_file_path(PG) is None
    assert sqlite_file_path("sqlite+aiosqlite:///:memory:") is None
    assert sqlite_file_path(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}") == tmp_path / "a.db"


@pytest.mark.parametrize(
    ("url", "warns"),
    [
        ("postgresql+asyncpg://u:p@ep-cool-1234-pooler.us-east-2.aws.neon.tech/neondb", True),
        ("postgresql+asyncpg://u:p@ep-cool-1234.us-east-2.aws.neon.tech/neondb", False),
        ("postgresql+asyncpg://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres", True),
        ("postgresql+asyncpg://u:p@aws-0-us-east-1.pooler.supabase.com:5432/postgres", False),
        ("sqlite+aiosqlite:///x.db", False),
    ],
)
def test_transaction_mode_poolers_are_flagged(url: str, warns: bool) -> None:
    assert (pooler_warning(url) is not None) is warns


async def test_migrate_reads_one_row_when_already_at_head(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database.from_url(settings.database_url)
    try:
        await init_db(settings, database.engine)

        async def forbidden(*_args: object) -> None:
            raise AssertionError("a database at head opened a schema change")

        monkeypatch.setattr(db_session, "run_schema_change", forbidden)
        assert await migrate(database.engine) == []
    finally:
        await database.engine.dispose()


def test_check_database_on_sqlite(tmp_path: Path) -> None:
    """Sync on purpose: ``check_database`` runs its own loop, as the CLI does. Unpacks as a pair."""
    settings = Settings.for_tests(data_dir=tmp_path)
    status, detail = check_database(settings.database_url)
    assert status == "warn" and "does not exist yet" in detail
    assert not (tmp_path / "coverset.db").exists()
    import asyncio

    asyncio.run(init_db(settings))
    report = check_database(settings.database_url)
    assert report.status == "pass", report.detail
    assert report.backend == "SQLite" and report.server_version
    assert report.current == report.heads == tuple(sorted(head_revisions()))
    assert "one writer" in report.pool


async def test_an_unreachable_postgres_is_a_failed_report() -> None:
    report = await probe_database("postgresql+asyncpg://coverset:x@127.0.0.1:1/coverset?connect_timeout=2")
    assert report.status == "fail" and report.error
    assert report.backend == "Postgres" and "cannot connect" in report.detail


async def test_report_on_the_suites_backend(settings: Settings) -> None:
    await init_db(settings)
    report = await probe_database(settings.database_url)
    assert report.status == "pass", report.detail
    assert set(report.current) == head_revisions()


# -- Postgres only ---------------------------------------------------------------------------------


async def test_postgres_connections_carry_the_server_settings(postgres_url: str) -> None:
    keepalives = ("tcp_keepalives_idle", "tcp_keepalives_interval", "tcp_keepalives_count")
    web = Database.from_url(postgres_url, statement_timeout_s=WEB_STATEMENT_TIMEOUT_S)
    cli = Database.from_url(postgres_url)
    try:
        async with web.engine.connect() as connection:
            shown = {
                name: (await connection.execute(text(f"SHOW {name}"))).scalar()
                for name in ("statement_timeout", "timezone", "application_name", *keepalives)
            }
        assert shown == {
            "statement_timeout": "30s",
            "timezone": "UTC",
            "application_name": "coverset",
            **{name: POSTGRES_SERVER_SETTINGS[name] for name in keepalives},
        }
        async with cli.engine.connect() as connection:
            assert (await connection.execute(text("SHOW statement_timeout"))).scalar() == "0"
    finally:
        await web.engine.dispose()
        await cli.engine.dispose()


async def test_the_statement_timeout_cancels_a_long_statement(postgres_url: str) -> None:
    database = Database.from_url(postgres_url, statement_timeout_s=0.2)
    try:
        async with database.engine.connect() as connection:
            with pytest.raises(DBAPIError, match="statement timeout"):
                await connection.execute(text("SELECT pg_sleep(2)"))
    finally:
        await database.engine.dispose()


async def test_lifting_the_statement_timeout_lasts_one_transaction(postgres_url: str) -> None:
    """The gc's bulk deletes run without the web timeout; the connection's next transaction has
    it back (``SET LOCAL``), so a pooled connection never leaks the lift into a request."""
    database = Database.from_url(postgres_url, statement_timeout_s=0.2)
    try:
        async with database.session() as session:
            await session.execute(text("SELECT 1"))  # the transaction the gc's reads begin
            await lift_statement_timeout(session)
            assert (await session.execute(text("SHOW statement_timeout"))).scalar() == "0"
            await session.execute(text("SELECT pg_sleep(0.4)"))  # longer than the timeout: completes
            await session.commit()
            assert (await session.execute(text("SHOW statement_timeout"))).scalar() == "200ms"
            with pytest.raises(DBAPIError, match="statement timeout"):
                await session.execute(text("SELECT pg_sleep(0.4)"))
    finally:
        await database.engine.dispose()


async def test_the_postgres_report(postgres_url: str) -> None:
    report = await probe_database(postgres_url)
    assert report.status == "pass", report.detail
    assert report.backend == "Postgres" and report.server_version and report.server_version[0].isdigit()
    assert "10 + 10 overflow" in report.pool
