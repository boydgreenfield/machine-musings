"""Engines: pool and connection settings, hosted-URL handling, creating the database, the fast
"already at head" migration path and the database report."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from coverset.config import Settings
from coverset.db import session as db_session
from coverset.db.session import (
    POSTGRES_SERVER_SETTINGS,
    START_HINT,
    WEB_STATEMENT_TIMEOUT_S,
    Database,
    DatabaseUnavailable,
    check_database,
    create_database,
    head_revisions,
    init_db,
    lift_statement_timeout,
    make_engine,
    migrate,
    migrate_url,
    pooler_warning,
    postgres_connect_args,
    probe_database,
    quote_database,
)
from coverset.db.testing import PostgresTestDatabases, run_sql

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


@pytest.mark.parametrize(
    ("url", "warns"),
    [
        ("postgresql+asyncpg://u:p@ep-cool-1234-pooler.us-east-2.aws.neon.tech/neondb", True),
        ("postgresql+asyncpg://u:p@ep-cool-1234.us-east-2.aws.neon.tech/neondb", False),
        ("postgresql+asyncpg://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres", True),
        ("postgresql+asyncpg://u:p@aws-0-us-east-1.pooler.supabase.com:5432/postgres", False),
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


def _missing(databases: PostgresTestDatabases) -> str:
    """The URL of a database that does not exist yet on the test server (its name is unique)."""
    return databases.url_for(f"{databases.prefix}_new_{uuid.uuid4().hex[:8]}")


def test_init_db_creates_a_missing_database_then_migrates_it(
    postgres_databases: PostgresTestDatabases, tmp_path: Path
) -> None:
    """Sync on purpose: ``check_database`` runs its own loop, as the CLI does. Unpacks as a pair."""
    url = _missing(postgres_databases)
    status, detail = check_database(url)
    assert status == "fail" and "does not exist" in detail and "coverset init-db" in detail
    settings = Settings.for_tests(data_dir=tmp_path, database_url=url)
    try:
        applied = asyncio.run(init_db(settings))
        assert applied and applied[0].startswith("0001")
        report = check_database(url)
        assert report.status == "pass", report.detail
        assert report.current == report.heads == tuple(sorted(head_revisions()))
        assert asyncio.run(init_db(settings)) == []  # the second time: there, and at head
    finally:
        postgres_databases.drop(url)


def test_replicas_creating_the_database_together_all_get_it(
    postgres_databases: PostgresTestDatabases,
) -> None:
    """Three replicas start on a fresh server at once: each finds the database missing and creates
    it. The losers see ``unique_violation`` (23505) while the winner's ``CREATE DATABASE`` is still
    copying the template, or ``duplicate_database`` (42P04) once it has committed; either way they
    return, and each replica's migration then finds the one database (the advisory lock serialises
    the migrations: one applies the chain, the others find it at head)."""
    url = _missing(postgres_databases)

    async def race() -> list[list[str]]:
        created = await asyncio.gather(*(create_database(url) for _ in range(3)))
        assert created == [None, None, None]
        return await asyncio.gather(*(migrate_url(url) for _ in range(3)))

    try:
        for _ in range(3):  # the interleaving is the server's choice; three trials see both states
            applied = asyncio.run(race())
            assert sum(1 for revisions in applied if revisions) == 1, applied
            assert run_sql(url, "SELECT current_database()") == [(make_url(url).database,)]
            postgres_databases.drop(url)
    finally:
        postgres_databases.drop(url)


def test_a_database_another_process_created_meanwhile_is_fine(
    postgres_databases: PostgresTestDatabases,
) -> None:
    url = _missing(postgres_databases)
    try:
        asyncio.run(create_database(url))
        asyncio.run(create_database(url))  # duplicate_database: the other process's commit is done
        assert run_sql(url, "SELECT current_database()") == [(make_url(url).database,)]
    finally:
        postgres_databases.drop(url)


@pytest.mark.parametrize("name", ["has-dash", "with space", "x" * 64, "1starts_with_a_digit", 'quo"te', ""])
def test_only_plain_identifiers_are_created(name: str) -> None:
    with pytest.raises(ValueError, match="plain identifier"):
        quote_database(name)
    url = make_url("postgresql+asyncpg://localhost/x").set(database=name)
    with pytest.raises(DatabaseUnavailable, match="create it yourself"):
        asyncio.run(create_database(url))


def test_plain_identifiers_are_quoted_as_they_are() -> None:
    assert quote_database("Coverset_2") == '"Coverset_2"'


def test_an_unreachable_server_says_how_to_start_one() -> None:
    url = "postgresql+asyncpg://localhost:1/coverset?connect_timeout=2"
    with pytest.raises(DatabaseUnavailable) as raised:
        asyncio.run(migrate_url(url))
    message = str(raised.value)
    assert "\n" not in message and message.startswith("cannot reach Postgres at localhost:1")
    assert (
        START_HINT in message
        and "brew services start postgresql@14" in message
        and "make services" in message
    )


def test_a_refused_login_is_one_line(postgres_databases: PostgresTestDatabases) -> None:
    url = make_url(postgres_databases.url_for("postgres")).set(username="no_such_role_zz", password="x")
    with pytest.raises(DatabaseUnavailable, match="cannot connect to") as raised:
        asyncio.run(migrate_url(url))
    message = str(raised.value)
    assert "\n" not in message and "no_such_role_zz" in message and ":x@" not in message


async def test_an_unreachable_postgres_is_a_failed_report() -> None:
    report = await probe_database("postgresql+asyncpg://coverset:x@127.0.0.1:1/coverset?connect_timeout=2")
    assert report.status == "fail" and report.error
    assert "Postgres: cannot connect" in report.detail


async def test_report_on_the_suites_server(settings: Settings) -> None:
    report = await probe_database(settings.database_url)
    assert report.status == "pass", report.detail
    assert set(report.current) == head_revisions()
    assert report.server_version and report.server_version[0].isdigit()
    assert "10 + 10 overflow" in report.pool
    assert report.leader == "coverset:background: free (no process runs the background tasks right now)"
    assert report.leader in report.detail


async def test_postgres_connections_carry_the_server_settings(settings: Settings) -> None:
    postgres_url = settings.database_url
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


async def test_the_statement_timeout_cancels_a_long_statement(settings: Settings) -> None:
    database = Database.from_url(settings.database_url, statement_timeout_s=0.2)
    try:
        async with database.engine.connect() as connection:
            with pytest.raises(DBAPIError, match="statement timeout"):
                await connection.execute(text("SELECT pg_sleep(2)"))
    finally:
        await database.engine.dispose()


async def test_lifting_the_statement_timeout_lasts_one_transaction(settings: Settings) -> None:
    """The gc's bulk deletes run without the web timeout; the connection's next transaction has
    it back (``SET LOCAL``), so a pooled connection never leaks the lift into a request."""
    database = Database.from_url(settings.database_url, statement_timeout_s=0.2)
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
