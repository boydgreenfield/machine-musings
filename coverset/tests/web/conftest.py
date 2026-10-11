"""Fixtures for the web UI tests: the real local demo, run once per session.

The ``demo`` fixture seeds the demo repository and runs its history and both PRs with the
LocalExecutor (about 20 s once the cached virtualenv exists), recording forge calls with the fake
forge. Module tests then point the shared ``settings`` fixture at that data directory through
``settings_overrides``; tests that change state (quarantine) restore it.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from web_helpers import Demo

from coverset.config import Settings


@pytest.fixture(scope="session")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    """Run the local demo once and describe the result."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext

    root = tmp_path_factory.mktemp("web-demo")
    data_dir = root / "data"
    db_url = f"sqlite+aiosqlite:///{root / 'coverset.db'}"
    settings = Settings.for_tests(
        data_dir=data_dir,
        database_url=db_url,
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
    con = sqlite3.connect(root / "coverset.db")
    try:

        def pr_run(number: int) -> str:
            row = con.execute(
                "SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = ? "
                "ORDER BY r.created_at DESC LIMIT 1",
                (number,),
            ).fetchone()
            return row[0]

        baseline = con.execute(
            "SELECT id FROM runs WHERE pr_id IS NULL AND status = 'completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        repo_id = con.execute("SELECT id FROM repos ORDER BY id LIMIT 1").fetchone()[0]
        yield Demo(data_dir, db_url, pr_run(1), pr_run(2), baseline, repo_id)
    finally:
        con.close()


@pytest.fixture
def settings_overrides(demo: Demo) -> dict[str, Any]:
    """Point the app at the demo data (override and extend per module)."""
    return {"data_dir": demo.data_dir, "database_url": demo.db_url}
