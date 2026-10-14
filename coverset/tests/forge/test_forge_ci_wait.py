"""``coverset-ci wait`` (the stdlib client in pytest-coverset) against the real app served by
Hypercorn on a loopback port: pass, fail, run appearing late, SSE then polling, JUnit, timeout,
plus ``coverset replay`` and ``coverset forge calls``."""

from __future__ import annotations

import asyncio
import io
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from forge_helpers import DEMO_REPO_ID, SHA_A, LiveServer, add_events, load_fixture, seed_repo, seed_run

from coverset import cli as coverset_cli
from coverset.config import get_settings
from coverset.db.models import ForgeCall
from coverset.db.session import Database
from coverset.web import api
from pytest_coverset import ci

PASSED = {
    "nodeid": "tests/test_a.py::test_ok",
    "outcome": "passed",
    "duration_s": 0.01,
    "failed_phase": None,
    "longrepr": None,
    "shard": 0,
    "flaky": False,
}
FAILED = {
    "nodeid": "tests/test_refund.py::test_refund_partial",
    "outcome": "failed",
    "duration_s": 0.02,
    "failed_phase": "call",
    "longrepr": "assert -98 == -100",
    "shard": 0,
    "flaky": False,
}


async def wait(server: LiveServer, *extra: str, timeout: float = 20.0) -> tuple[int, str]:
    """Run ``coverset-ci wait`` in a thread; returns (exit code, output)."""
    out = io.StringIO()
    argv = [
        "wait",
        "--server",
        server.url,
        "--repo-id",
        str(DEMO_REPO_ID),
        "--sha",
        SHA_A,
        "--timeout",
        str(timeout),
        "--poll-interval",
        "0.1",
        *extra,
    ]
    code = await asyncio.to_thread(ci.main, argv, out=out)
    return code, out.getvalue()


@pytest.fixture(autouse=True)
def fast_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "POLL_INTERVAL_S", 0.05)


async def test_pass(live_server: LiveServer, live_db: Database, tmp_path: Path) -> None:
    repo = await seed_repo(live_db)
    await seed_run(
        live_db,
        repo,
        status="completed",
        verdict="success",
        events=[
            ("queued", {}),
            ("test", PASSED),
            ("progress", {"done": 1, "total": 1, "passed": 1, "failed": 0, "shards": 1, "elapsed_s": 0.5}),
            ("verdict", {"conclusion": "success", "reason": "all passed"}),
            ("done", {"conclusion": "success", "exit_code": 0, "counts": {"passed": 1}}),
        ],
    )
    junit = tmp_path / "junit.xml"
    code, output = await wait(live_server, "--junit", str(junit))
    assert code == 0, output
    assert "PASSED tests/test_a.py::test_ok" in output and "1 passed" in output
    assert any("text/event-stream" in line for line in live_server.requests)
    cases = ET.parse(junit).getroot().iter("testcase")
    assert [case.get("name") for case in cases] == ["test_ok"]


async def test_fail(live_server: LiveServer, live_db: Database, tmp_path: Path) -> None:
    repo = await seed_repo(live_db)
    await seed_run(
        live_db,
        repo,
        status="completed",
        verdict="failure",
        events=[
            ("queued", {}),
            ("test", FAILED),
            ("verdict", {"conclusion": "failure", "reason": "1 failed"}),
        ],
    )
    junit = tmp_path / "junit.xml"
    code, output = await wait(live_server, "--junit", str(junit))
    assert code == 1, output
    assert "FAILED tests/test_refund.py::test_refund_partial" in output and "assert -98 == -100" in output
    root = ET.parse(junit).getroot()
    assert root.find(".//failure") is not None


async def test_run_that_appears_later(live_server: LiveServer, live_db: Database) -> None:
    task = asyncio.create_task(wait(live_server))
    await asyncio.sleep(0.6)
    assert not task.done()
    repo = await seed_repo(live_db)
    run = await seed_run(live_db, repo, status="running", events=[("queued", {})])
    await asyncio.sleep(0.3)
    await add_events(
        live_db,
        run.id,
        [
            ("test", PASSED),
            ("verdict", {"conclusion": "success", "reason": "ok"}),
            ("done", {"conclusion": "success", "exit_code": 0, "counts": {"passed": 1}}),
        ],
    )
    code, output = await task
    assert code == 0, output
    assert "waiting for a coverset run" in output and "PASSED tests/test_a.py::test_ok" in output
    verdict_calls = [line for line in live_server.requests if line.startswith("GET /v1/verdict")]
    assert len(verdict_calls) >= 2


async def test_sse_then_polling_fallback(
    live_server: LiveServer, live_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "SSE_MAX_STREAM_S", 0.2)
    repo = await seed_repo(live_db)
    run = await seed_run(live_db, repo, status="running", events=[("queued", {}), ("test", PASSED)])
    task = asyncio.create_task(wait(live_server))
    deadline = time.monotonic() + 15
    while not any("since=" in line for line in live_server.requests):
        assert time.monotonic() < deadline, live_server.requests
        await asyncio.sleep(0.05)
    await add_events(
        live_db,
        run.id,
        [
            ("test", FAILED),
            ("done", {"conclusion": "failure", "exit_code": 1, "counts": {"passed": 1, "failed": 1}}),
        ],
    )
    code, output = await task
    assert code == 1, output
    streams = [line for line in live_server.requests if line.endswith("text/event-stream")]
    polls = [line for line in live_server.requests if "/events?since=" in line]
    assert len(streams) >= 3 and polls
    assert output.index("PASSED tests/test_a.py::test_ok") < output.index("FAILED tests/test_refund.py")


async def test_timeout(live_server: LiveServer) -> None:
    started = time.monotonic()
    code, output = await wait(live_server, timeout=1.0)
    assert code == 2 and "timed out" in output
    assert time.monotonic() - started < 10


async def test_cancelled_run_ends_the_wait(live_server: LiveServer, live_db: Database) -> None:
    repo = await seed_repo(live_db)
    await seed_run(live_db, repo, status="cancelled", events=[("queued", {})])
    code, output = await wait(live_server)
    assert code == 2, output


def _invoke(args: list[str], env: dict[str, str]) -> Any:
    get_settings.cache_clear()
    try:
        return CliRunner().invoke(coverset_cli.cli, args, env=env, catch_exceptions=False)
    finally:
        get_settings.cache_clear()


async def test_replay(live_server: LiveServer, live_db: Database, tmp_path: Path) -> None:
    settings = live_server.settings
    env = {
        "COVERSET_ENV": "test",
        "COVERSET_DATA_DIR": str(settings.data_dir),
        "COVERSET_DATABASE_URL": settings.database_url,
        "COVERSET_GITHUB_WEBHOOK_SECRET": "coverset-test-secret",
    }
    fixture = str(Path(__file__).resolve().parents[2] / "fixtures" / "webhooks" / "pr_opened_fork.json")
    result = await asyncio.to_thread(_invoke, ["replay", fixture, "--server", live_server.url], env)
    assert result.exit_code == 0, result.output
    assert "HTTP 202" in result.output and "as recorded" in result.output
    assert json.loads(result.output.splitlines()[-1])["status"] == "awaiting_approval"

    again = await asyncio.to_thread(_invoke, ["replay", fixture, "--server", live_server.url], env)
    assert "HTTP 200" in again.output and "duplicate" in again.output

    deadline = time.monotonic() + 10
    while True:
        async with live_db.session() as session:
            from sqlalchemy import select

            call = await session.scalar(select(ForgeCall).order_by(ForgeCall.id))
            if call is not None:
                break
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    assert call.method == "create_check" and call.target == "coverset-demo/ledger"

    resigned = await asyncio.to_thread(
        _invoke,
        ["replay", fixture, "--server", live_server.url, "--resign", "--new-delivery"],
        {**env, "COVERSET_GITHUB_WEBHOOK_SECRET": "a-different-secret-0123456789"},
    )
    assert "HTTP 403" in resigned.output and resigned.exit_code == 1

    headers, _body = load_fixture("pr_opened_fork")
    assert headers["X-GitHub-Event"] == "pull_request"
    missing = await asyncio.to_thread(_invoke, ["replay", str(tmp_path / "nope.json")], env)
    assert missing.exit_code != 0 and "no such fixture" in missing.output
