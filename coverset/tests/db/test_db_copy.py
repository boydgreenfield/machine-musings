"""``coverset db copy``: every table, in dependency order, values converted by the column types,
verified by row counts; refusals; the CLI. SQLite to SQLite always runs; SQLite to Postgres (the
real move) runs under ``make test-pg``."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from coverset.cli import cli
from coverset.config import Settings
from coverset.db.copy import CopyError, copy_database
from coverset.db.models import (
    AuditEvent,
    Base,
    Commit,
    ExecutorSpend,
    PullRequest,
    Repo,
    Run,
    RunEvent,
    TestCase,
    TestResult,
    User,
)
from coverset.db.session import Database, init_db
from coverset.db.testing import PostgresTestDatabases

T0 = datetime(2026, 10, 14, 9, 30, 15, 123456, tzinfo=UTC)


def _sqlite(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _seed(url: str) -> dict[str, Any]:
    """Rows in most tables, including a run whose scoring basis is a *later* run (a self-reference
    that points forward in primary-key order)."""
    await init_db(Settings.for_tests(database_url=url))
    database = Database.from_url(url)
    try:
        async with database.session() as session:
            repo = Repo(forge="github", external_id="9", full_name="acme/app", settings_json={"k": [1, 2]})
            session.add(repo)
            await session.flush()
            pr = PullRequest(repo_id=repo.id, number=7, head_sha="a" * 40, base_sha="b" * 40, title="Fix")
            later = Run(id="run_02", repo_id=repo.id, head_sha="c" * 40, trigger="push", created_at=T0)
            earlier = Run(
                id="run_01",
                repo_id=repo.id,
                head_sha="d" * 40,
                trigger="pull_request",
                timeline={"queued": T0.isoformat()},
                created_at=T0 - timedelta(minutes=1),
            )
            session.add_all([pr, later, earlier])
            await session.flush()
            earlier.scoring_basis_run_id = later.id
            earlier.pr_id = pr.id
            test = TestCase(
                repo_id=repo.id,
                nodeid="tests/test_a.py::test_x[1]",
                nodeid_hash=hashlib.blake2b(b"x", digest_size=16).digest(),
                family="tests/test_a.py::test_x",
            )
            session.add_all(
                [
                    test,
                    Commit(repo_id=repo.id, sha="e" * 40, parent_shas=["f" * 40], committed_at=T0),
                    User(forge="github", external_id="42", login="dana", role="admin"),
                    ExecutorSpend(day=date(2026, 10, 14), executor="modal", kind="pool", usd=0.25),
                    AuditEvent(actor_kind="system", action="probe", details={"nested": {"a": None}}),
                ]
            )
            await session.flush()
            session.add(
                TestResult(run_id=earlier.id, test_id=test.id, outcome="failed", fail_sig=b"\x00" * 8)
            )
            session.add_all(
                [
                    RunEvent(run_id=earlier.id, seq=n, kind="log", payload={"n": n}, at=T0)
                    for n in range(1, 2501)
                ]
            )
            await session.commit()
            return {"repo_id": repo.id, "test_id": test.id}
    finally:
        await database.engine.dispose()


async def _snapshot(url: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, as the ORM-free Core types read it (comparable across backends)."""
    database = Database.from_url(url)
    try:
        async with database.engine.connect() as connection:
            out = {}
            for table in Base.metadata.sorted_tables:
                rows = (await connection.execute(select(table).order_by(*table.primary_key.columns))).all()
                out[table.name] = [tuple(row) for row in rows]
            return out
    finally:
        await database.engine.dispose()


async def test_sqlite_to_sqlite_copies_every_value(tmp_path: Path) -> None:
    source, target = _sqlite(tmp_path / "a" / "coverset.db"), _sqlite(tmp_path / "b" / "coverset.db")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    await _seed(source)
    seen: dict[str, int] = {}
    report = await copy_database(
        source, target, batch_size=1000, progress=lambda table, rows: seen.__setitem__(table, rows)
    )
    assert report.mismatched == [] and report.source == report.target
    assert report.target["run_events"] == 2500 and seen["run_events"] == 2500
    assert report.applied  # the empty target was migrated first
    assert await _snapshot(target) == await _snapshot(source)


async def test_refuses_a_target_with_rows_and_a_source_behind_head(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    source, target = _sqlite(tmp_path / "a" / "x.db"), _sqlite(tmp_path / "b" / "x.db")
    await _seed(source)
    await _seed(target)
    with pytest.raises(CopyError, match="already holds rows: .*repos \\(1\\)"):
        await copy_database(source, target)
    with pytest.raises(CopyError, match="not the latest revision"):
        await copy_database(_sqlite(tmp_path / "c" / "empty.db"), target)
    with pytest.raises(CopyError, match="same database"):
        await copy_database(source, source)


def test_the_cli_copies_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    asyncio.run(_seed(_sqlite(data / "coverset.db")))
    (tmp_path / "copy").mkdir()
    for name in [name for name in os.environ if name.startswith("COVERSET_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(data))
    runner = CliRunner()
    result = runner.invoke(cli, ["db", "copy", "--to", _sqlite(tmp_path / "copy" / "coverset.db")])
    assert result.exit_code == 0, result.output
    assert "run_events" in result.output and "2,500 rows" in result.output
    assert "row counts match the source" in result.output
    again = runner.invoke(cli, ["db", "copy", "--to", _sqlite(tmp_path / "copy" / "coverset.db")])
    assert again.exit_code == 1 and "already holds rows" in again.output
    status = runner.invoke(cli, ["db", "status"])
    assert status.exit_code == 0, status.output
    assert "SQLite" in status.output and "implicit" in status.output and "pass" in status.output


# -- SQLite to Postgres ----------------------------------------------------------------------------


@pytest.fixture
def postgres_target(postgres_databases: PostgresTestDatabases | None) -> Iterator[str]:
    """An empty (no rows), migrated Postgres database."""
    if postgres_databases is None:
        pytest.skip("needs Postgres: set COVERSET_TEST_DATABASE_URL (make test-pg)")
    url = postgres_databases.create()
    yield url
    postgres_databases.drop(url)


async def test_sqlite_to_postgres(tmp_path: Path, postgres_target: str) -> None:
    source = _sqlite(tmp_path / "coverset.db")
    ids = await _seed(source)
    report = await copy_database(source, postgres_target, batch_size=700)
    assert report.mismatched == [] and report.total == sum(report.source.values())
    assert await _snapshot(postgres_target) == await _snapshot(source)
    database = Database.from_url(postgres_target)
    try:
        async with database.session() as session:
            run = await session.get(Run, "run_01")
            assert run is not None and run.scoring_basis_run_id == "run_02"
            assert run.created_at == T0 - timedelta(minutes=1) and run.created_at.tzinfo is UTC
            event = await session.scalar(select(RunEvent).where(RunEvent.seq == 2500))
            assert event is not None and event.at == T0  # microseconds survive
            # The sequences moved past the copied ids: new rows get fresh ones.
            repo = Repo(forge="fake", external_id="new", full_name="acme/new")
            session.add(repo)
            await session.commit()
            assert repo.id > ids["repo_id"]
    finally:
        await database.engine.dispose()
