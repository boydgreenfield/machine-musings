"""Fixtures for the web UI tests: the real local demo, run once per session.

The ``demo`` fixture seeds the demo repository and runs its history and both PRs with the
LocalExecutor (about 20 s once the cached virtualenv exists), recording forge calls with the fake
forge. It uses the suite's backends: a SQLite file and local facts by default, or a Postgres
database and an object-store prefix when ``COVERSET_TEST_DATABASE_URL`` / ``COVERSET_TEST_BLOB_URL``
are set, so every page test also runs on the hosted shape. Module tests then point the shared
``settings`` fixture at that state through ``settings_overrides`` (``Demo.overrides``); tests that
change state (quarantine) restore it.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from web_helpers import Demo

from coverset.config import Settings
from coverset.db.testing import PostgresTestDatabases, run_sql


@pytest.fixture(scope="session")
def demo(
    tmp_path_factory: pytest.TempPathFactory, postgres_databases: PostgresTestDatabases | None
) -> Iterator[Demo]:
    """Run the local demo once and describe the result (on the suite's backends: a SQLite file and
    local facts by default, a Postgres database and an object-store prefix when
    ``COVERSET_TEST_DATABASE_URL`` / ``COVERSET_TEST_BLOB_URL`` are set)."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext
    from coverset.storage.testing import fresh_blob_url, test_blob_url

    root = tmp_path_factory.mktemp("web-demo")
    data_dir = root / "data"
    db_url = f"sqlite+aiosqlite:///{root / 'coverset.db'}"
    if postgres_databases is not None:
        db_url = postgres_databases.create()  # dropped with the worker's other databases
    blob_url = fresh_blob_url("web-demo") if test_blob_url() is not None else ""
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
        return run_sql(
            db_url,
            "SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = :number "
            "ORDER BY r.created_at DESC LIMIT 1",
            {"number": number},
        )[0][0]

    baseline = run_sql(
        db_url,
        "SELECT id FROM runs WHERE pr_id IS NULL AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
    )[0][0]
    repo_id = int(run_sql(db_url, "SELECT id FROM repos ORDER BY id LIMIT 1")[0][0])
    try:
        yield Demo(data_dir, db_url, pr_run(1), pr_run(2), baseline, repo_id, blob_url)
    finally:
        if blob_url:
            from coverset.storage import S3BlobStore

            blobs = S3BlobStore.from_url(blob_url)
            blobs.delete([info.key for info in blobs.list()])  # this demo's prefix only


@pytest.fixture
def settings_overrides(demo: Demo) -> dict[str, Any]:
    """Point the app at the demo data (override and extend per module)."""
    return demo.overrides()
