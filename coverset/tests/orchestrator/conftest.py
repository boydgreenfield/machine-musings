"""Fixtures for the orchestrator tests: a migrated database, the test repo and a scripted executor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from orch_fakes import FakeExecutor, Script, TestRepo, fake_env_spec, make_repo

from coverset.config import Settings
from coverset.db.session import init_db
from coverset.forge.fake import FakeForge
from coverset.orchestrator.bundles import IngestPool
from coverset.orchestrator.repos import register_local_repo
from coverset.orchestrator.runner import OrchestratorContext


@pytest.fixture
def script() -> Script:
    return Script()


@pytest.fixture
def executor(script: Script) -> FakeExecutor:
    return FakeExecutor(script)


@pytest.fixture
def repo(tmp_path: Path) -> TestRepo:
    return make_repo(tmp_path / "src-repo")


@pytest.fixture
async def ctx(settings: Settings, executor: FakeExecutor) -> AsyncIterator[OrchestratorContext]:
    await init_db(settings)
    context = OrchestratorContext.from_settings(
        settings,
        executor_factory=lambda kind: executor,
        env_spec_builder=fake_env_spec,
        ingest=IngestPool("thread", max_workers=2),
        cancel_poll_s=0.05,
        stop_wait_s=2.0,
    )
    context.forge = FakeForge(session_factory=context.db.sessionmaker)
    yield context
    context.executors.clear()
    await context.close()
    assert context.ingest is not None
    context.ingest.close()


@pytest.fixture
async def repo_id(ctx: OrchestratorContext, repo: TestRepo) -> int:
    async with ctx.db.session() as session:
        row = await register_local_repo(session, repo.path)
        await session.commit()
        return row.id
