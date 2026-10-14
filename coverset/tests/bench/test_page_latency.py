"""Server time of every UI page on the local demo, with a generous p95 bound (``slow``).

The real-repository numbers live in docs/benchmarks.md ("Real repository (anonymised)"), measured
with ``scripts/bench_facts.py --real``; this test keeps the warm path honest on data every checkout
can build: it runs the demo once (its history and both pull requests, local executor), then times
each page through the in-process test client, a warm-up request first. The bound is ten times what
the demo needs on a laptop, so it only trips on a regression of the kind the caches remove (a
per-request rebuild of DuckDB views, a ``test_lines`` scan per function, a git process per file),
not on a busy CI machine.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest

from coverset.app import create_app
from coverset.config import Settings

pytestmark = pytest.mark.slow

P95_BOUND_MS = 400.0
"""Generous: the demo's pages take 5 to 40 ms warm on an M-series laptop."""
SAMPLES = 10


@dataclass(frozen=True)
class DemoData:
    data_dir: Path
    db_url: str
    repo_id: int
    baseline: str
    pr_number: int


@pytest.fixture(scope="module")
def demo_data(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DemoData]:
    """The local demo, run once for this module (as ``tests/web/conftest.py`` does)."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext

    root = tmp_path_factory.mktemp("bench-demo")
    data_dir = root / "data"
    db_url = f"sqlite+aiosqlite:///{root / 'coverset.db'}"
    settings = Settings.for_tests(
        data_dir=data_dir, database_url=db_url, github_webhook_secret=TEST_WEBHOOK_SECRET
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
        repo_id = con.execute("SELECT id FROM repos ORDER BY id LIMIT 1").fetchone()[0]
        baseline = con.execute(
            "SELECT id FROM runs WHERE pr_id IS NULL AND status = 'completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        pr_number = con.execute("SELECT number FROM pull_requests ORDER BY number LIMIT 1").fetchone()[0]
    finally:
        con.close()
    yield DemoData(data_dir, db_url, repo_id, baseline, pr_number)


async def _time(client: object, url: str, headers: dict[str, str] | None = None) -> tuple[float, int, bytes]:
    start = time.perf_counter()
    response = await client.get(url, headers=headers)  # type: ignore[attr-defined]
    body = await response.get_data()
    return (time.perf_counter() - start) * 1000, response.status_code, body


async def test_every_page_is_fast_when_warm(demo_data: DemoData) -> None:
    settings = Settings.for_tests(data_dir=demo_data.data_dir, database_url=demo_data.db_url)
    app = create_app(settings)
    r = f"/repos/{demo_data.repo_id}"
    async with app.test_app():
        client = app.test_client()
        _ms, status, page = await _time(client, f"{r}/map?mode=lines&run={demo_data.baseline}")
        assert status == 200
        source = re.search(rf'href="{r}/files/([^"?#]+)\?run=', page.decode())
        assert source, "the map links to a source file"
        path = source.group(1)
        _ms, _status, page = await _time(client, f"{r}/files/{path}?run={demo_data.baseline}")
        function = re.search(rf'href="{r}/functions/([0-9a-f]{{16}})', page.decode())
        line = re.search(rf'{r}/files/[^"]+/lines/(\d+)/tests', page.decode())
        assert function and line, "the source view links to a function and a line's tests"
        test = re.search(rf'href="{r}/tests/(\d+)', (await _time(client, f"{r}/tests"))[2].decode())
        assert test, "the tests page links to a test"
        pages = {
            "dashboard": "/",
            "repo overview": r,
            "map (gap)": f"{r}/map?mode=gap",
            "map (inputs)": f"{r}/map?mode=inputs",
            "source": f"{r}/files/{path}",
            "line tests": f"{r}/files/{path}/lines/{line.group(1)}/tests",
            "function": f"{r}/functions/{function.group(1)}",
            "tests": f"{r}/tests",
            "test": f"{r}/tests/{test.group(1)}",
            "pull request": f"{r}/prs/{demo_data.pr_number}",
        }
        slow: dict[str, float] = {}
        for name, url in pages.items():
            headers = {"HX-Request": "true"} if "/lines/" in url else None
            _ms, status, _body = await _time(client, quote(url, safe="/?=&"), headers)  # warm-up
            assert status == 200, (name, url, status)
            times = sorted(
                [(await _time(client, quote(url, safe="/?=&"), headers))[0] for _ in range(SAMPLES)]
            )
            p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
            if p95 > P95_BOUND_MS:
                slow[name] = round(p95, 1)
            assert statistics.median(times) <= p95
        assert not slow, f"pages over {P95_BOUND_MS} ms at p95: {slow}"
