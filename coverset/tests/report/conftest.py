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
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from report_helpers import DemoData, FixtureRun

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROJECT = ROOT / "packages" / "pytest-coverset" / "tests" / "fixture_project"


@pytest.fixture(scope="session")
def fixture_out(tmp_path_factory: pytest.TempPathFactory) -> FixtureRun:
    """Run the fixture project with pytest-coverset into ``<copy>/out``."""
    project = tmp_path_factory.mktemp("report-fixture") / "fixture_project"
    shutil.copytree(FIXTURE_PROJECT, project, ignore=shutil.ignore_patterns("__pycache__"))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COVERAGE_", "COV_CORE", "COVERSET_"))
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_coverset", "-q", "-p", "no:cacheprovider"]
        + ["--coverset-out", "out", "--coverset-source", "fixturepkg"],
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
def demo(tmp_path_factory: pytest.TempPathFactory) -> DemoData:
    """Run the local demo once (history, both PRs) into a temp data dir."""
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

    root = tmp_path_factory.mktemp("report-demo")
    data_dir = root / "data"
    settings = Settings.for_tests(
        data_dir=data_dir,
        database_url=f"sqlite+aiosqlite:///{root / 'coverset.db'}",
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
            return str(row[0])

        repo_id = int(con.execute("SELECT id FROM repos ORDER BY id LIMIT 1").fetchone()[0])
        return DemoData(settings, pr_run(1), pr_run(2), repo_id)
    finally:
        con.close()
