"""Fixtures for the static report tests: a real pytest-coverset bundle and the real local demo.

``fixture_out`` runs the plugin's own fixture project (``packages/pytest-coverset/tests/
fixture_project``) under ``pytest -p pytest_coverset --coverset-out`` once per session, the way
``packages/pytest-coverset/tests`` do. ``demo`` seeds the demo repository and runs its history and
both PRs with the LocalExecutor (about 20 s), like ``tests/web/conftest.py``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from report_helpers import DemoData, FixtureRun

from coverset.db.testing import PostgresTestDatabases, run_sql

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROJECT = ROOT / "packages" / "pytest-coverset" / "tests" / "fixture_project"


@pytest.fixture(autouse=True, scope="session")
def _pinned_build_date() -> Iterator[None]:
    """Pin the footer's build date for the whole session, so a report built by a session fixture and
    one built inside a test compare byte for byte even when the run straddles midnight UTC."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SOURCE_DATE_EPOCH", "1767225600")
        yield


@pytest.fixture(scope="session")
def fixture_out(tmp_path_factory: pytest.TempPathFactory) -> FixtureRun:
    """Run the fixture project with pytest-coverset into ``<copy>/out`` (with the neutral copy
    ``out/neutral`` that ``--coverset`` always writes, which ``--compare`` reads a base's functions from)."""
    project = tmp_path_factory.mktemp("report-fixture") / "fixture_project"
    shutil.copytree(FIXTURE_PROJECT, project, ignore=shutil.ignore_patterns("__pycache__"))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COVERAGE_", "COV_CORE", "COVERSET_"))
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_coverset", "-q", "-p", "no:cacheprovider"]
        + ["--coverset-out", "out", "--coverset-source", "fixturepkg", "--coverset-neutral"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert (project / "out" / "coverage.sqlite").is_file()
    return FixtureRun(project, project / "out")


@pytest.fixture(scope="session")
def demo(
    tmp_path_factory: pytest.TempPathFactory, postgres_databases: PostgresTestDatabases
) -> Iterator[DemoData]:
    """Run the local demo once (history, both PRs) into a temp data dir and a database of its own,
    on the suite's backends (local blobs, or the object-store prefix ``COVERSET_TEST_BLOB_URL``
    selects; see ``tests/web/conftest.py``)."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.config import Settings
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext
    from coverset.storage.testing import fresh_blob_url, test_blob_url

    root = tmp_path_factory.mktemp("report-demo")
    data_dir = root / "data"
    db_url = postgres_databases.create()  # dropped with the worker's other databases
    blob_url = fresh_blob_url("report-demo") if test_blob_url() is not None else ""
    settings = Settings.for_tests(
        data_dir=data_dir,
        database_url=db_url,
        blob_url=blob_url,
        github_webhook_secret=TEST_WEBHOOK_SECRET,
        local_allow_webhook_runs=True,
    )

    async def scenario() -> None:
        await init_db(settings)
        ctx = OrchestratorContext.from_settings(settings, ingest=IngestPool("thread", max_workers=4))
        ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker)
        try:
            await run_demo(ctx, out=data_dir / "demo.git")
        finally:
            await ctx.close()
            assert ctx.ingest is not None
            ctx.ingest.close()

    asyncio.run(scenario())
    clear_registered_secrets()

    def pr_run(number: int) -> str:
        return str(
            run_sql(
                db_url,
                "SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = :number "
                "ORDER BY r.created_at DESC LIMIT 1",
                {"number": number},
            )[0][0]
        )

    repo_id = int(run_sql(db_url, "SELECT id FROM repos ORDER BY id LIMIT 1")[0][0])
    pr1 = pr_run(1)
    main = run_sql(
        db_url,
        "SELECT b.id FROM runs r JOIN runs b ON b.repo_id = r.repo_id AND b.head_sha = r.base_sha "
        "WHERE r.id = :run AND b.pr_id IS NULL ORDER BY b.created_at DESC LIMIT 1",
        {"run": pr1},
    )
    try:
        yield DemoData(settings, pr1, pr_run(2), repo_id, str(main[0][0]) if main else "")
    finally:
        if blob_url:
            from coverset.storage import S3BlobStore

            blobs = S3BlobStore.from_url(blob_url)
            blobs.delete([info.key for info in blobs.list()])  # this demo's prefix only
