"""Upload mode end to end: ``pytest --coverset`` in a checkout, ``coverset-ci upload`` to a live server.

The seeded demo repository stands in for a private GitHub repository whose CI runs the tests: the
test clones it, checks out PR #2's head, runs ``pytest --coverset`` there in a subprocess, and runs
the real ``coverset-ci upload --with-sources --wait`` (a subprocess, plain urllib, a chunked body)
against coverset served by Hypercorn on a loopback port, authenticated with a repository token.
The server finds the repository's mirror (the demo repo's recorded local source), diffs PR #2
against ``main``, and the PR page then shows patch coverage; the fake forge's outbox holds the
sticky comment. No network, no Modal, no runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from sqlalchemy import select

from coverset.app import create_app
from coverset.config import Settings
from coverset.db.models import ForgeCall, PullRequest, Run
from coverset.db.session import Database, init_db
from coverset.demo.flow import register_demo_repo
from coverset.demo.seed import seed_demo_repo
from coverset.forge.registry import forge_for_app
from coverset.orchestrator.bundles import IngestPool
from coverset.orchestrator.runner import OrchestratorContext
from coverset.web import tokens

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

ADMIN = "upload-e2e-admin-token-0123456789abcdef"


@dataclass
class Server:
    url: str
    settings: Settings
    token: str
    repo_id: int
    shas: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    """coverset on Hypercorn with the demo repository registered and one repository token."""
    demo_git = tmp_path / "demo.git"
    shas = seed_demo_repo(demo_git)
    port = _free_port()
    settings = Settings.for_tests(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'coverset.db'}",
        admin_token=ADMIN,
        public_url=f"http://127.0.0.1:{port}",
        forge="fake",
        ai="off",
    )
    app = create_app(settings)
    state: dict[str, Any] = {}
    started = threading.Event()

    def run() -> None:
        async def main() -> None:
            state["loop"] = asyncio.get_running_loop()
            state["stop"] = asyncio.Event()
            await init_db(settings)
            database: Database = app.extensions["db"]
            async with database.session() as session:
                repo = await register_demo_repo(session, demo_git)
                raw, _ = await tokens.create(
                    session, name="ci", kind="repo", scopes=["upload"], now=datetime.now(UTC), repo_id=repo.id
                )
                await session.commit()
                state["token"], state["repo_id"] = raw, repo.id
            ctx = OrchestratorContext.from_settings(
                settings, db=database, ingest=IngestPool("thread", max_workers=2)
            )
            ctx.forge = forge_for_app(app)
            app.extensions["orchestrator"] = ctx
            config = HypercornConfig()
            config.bind = [f"127.0.0.1:{port}"]
            config.accesslog = None
            config.graceful_timeout = 1.0
            started.set()
            await serve(app, config, shutdown_trigger=state["stop"].wait)  # type: ignore[arg-type]

        asyncio.run(main())

    thread = threading.Thread(target=run, name="coverset-upload-e2e", daemon=True)
    thread.start()
    assert started.wait(30), "server did not start"
    deadline = time.monotonic() + 15
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    try:
        yield Server(f"http://127.0.0.1:{port}", settings, state["token"], state["repo_id"], shas, state)
    finally:
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(15)


def _checkout(demo_git: Path, sha: str, dest: Path) -> Path:
    subprocess.run(["git", "clone", "--quiet", str(demo_git), str(dest)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "checkout", "--quiet", "--detach", sha], check=True, capture_output=True
    )
    return dest


def _get(server: Server, path: str) -> tuple[int, str]:
    request = urllib.request.Request(f"{server.url}{path}", headers={"Authorization": f"Bearer {ADMIN}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode("utf-8")


def test_pytest_coverset_then_upload_shows_patch_coverage_on_the_pr_page(
    server: Server, tmp_path: Path
) -> None:
    pr = next(p for p in server.shas["prs"] if p["number"] == 2)
    work = _checkout(tmp_path / "demo.git", pr["head_sha"], tmp_path / "work")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "ACTIONS_", "COVERSET"))}
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "--coverset", "-q", "-p", "no:cacheprovider"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert tests.returncode == 0, tests.stdout[-2000:] + tests.stderr[-2000:]

    def upload(*extra: str) -> str:
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest_coverset.ci",
                "upload",
                "--server",
                server.url,
                "--repo",
                "coverset-demo/ledger",
                "--sha",
                pr["head_sha"],
                "--base",
                pr["base_sha"],
                "--pr",
                "2",
                "--branch",
                pr["branch"],
                "--with-sources",
                "--wait",
                "--timeout",
                "180",
                *extra,
            ],
            cwd=work,
            env={**env, "COVERSET_TOKEN": server.token},
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        output = done.stdout + done.stderr
        assert done.returncode == 0, output
        assert server.token not in output
        return output

    output = upload()
    assert "coverset verdict: success" in output
    assert "patch coverage" in output
    # A re-run of the upload step sends the same bytes: the server recognises the replay.
    time.sleep(1.1)
    assert "(already uploaded)" in upload()
    # Another shard (a matrix job) joins the same run, which is reported again, still with patch
    # coverage (the second pass must not trip over the first pass's invariant results).
    second = upload("--label", "py312")
    assert "(already uploaded)" not in second and "patch coverage" in second

    status, page = _get(server, f"/repos/{server.repo_id}/prs/2")
    assert status == 200
    assert "Patch coverage by file" in page
    assert "check_if_new_user_signup_allowed" in page

    async def inspect() -> tuple[Run, list[ForgeCall], PullRequest]:
        """The run, its PR and the forge calls of the repo; the sticky comment is posted just after
        the verdict is stored (what ``--wait`` sees), so poll briefly for it."""
        database = Database.from_url(server.settings.database_url)
        try:
            for _ in range(100):
                async with database.session() as session:
                    run = await session.scalar(select(Run).where(Run.trigger == "upload"))
                    assert run is not None
                    calls = list(
                        await session.scalars(select(ForgeCall).where(ForgeCall.repo_id == server.repo_id))
                    )
                    pull = await session.get(PullRequest, run.pr_id)
                    assert pull is not None
                if sum(c.method == "upsert_pr_comment" for c in calls) >= 2:
                    break
                await asyncio.sleep(0.1)
            return run, calls, pull
        finally:
            await database.engine.dispose()

    run, calls, pull = asyncio.run(inspect())
    assert (run.status, run.verdict, pull.number) == ("completed", "success", 2)
    summary = run.summary_json or {}
    assert summary.get("patch_pct") is not None
    assert summary.get("function_rows"), json.dumps(summary)[:2000]
    assert "no diff available" not in (run.reason or "")
    comments = [c for c in calls if c.method == "upsert_pr_comment"]
    # One sticky comment, created by the first pass and edited in place by the second shard's pass.
    assert [c.response["created"] for c in comments if c.response] == [True, False]
    assert "patch coverage" in comments[-1].request["body"].lower()
    assert run.shard_count == 2
    # The uploaded sources went in the store; the mirror answered first, so the page needed none.
    stored = server.settings.data_dir / "sources" / str(server.repo_id) / pr["head_sha"]
    assert (stored / "src" / "ledger").is_dir()
    assert not (stored / ".coverset").exists()
