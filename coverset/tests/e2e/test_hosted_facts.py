"""The local demo with its facts in an object store, served by a replica that never saw the ingest.

The demo (seeded repository, six baselines, both PRs) runs with ``COVERSET_BLOB_URL`` pointing at
moto (or ``COVERSET_TEST_BLOB_URL``) and a *process* ingest pool, so the shards are ingested by
worker processes that get only the cache directory and the knobs, find the bucket from them and
publish there. Then the cache's facts are wiped, as on a new replica or after a restart on an
ephemeral disk, and the dashboard, the PR pages, the source view and the economy page are rendered
through the Quart test client, the static report is loaded with ``load_run`` and the agent's
``StoreBackend`` answers: everything comes back from the bucket, and answers what it answered
before the wipe. About 30 s once the demo's virtualenv is cached.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import Repo, Run

pytestmark = pytest.mark.slow


@dataclass(frozen=True)
class Hosted:
    settings: Settings
    repo_id: int
    run_ids: list[str]
    pr1_run: str
    pr2_run: str
    summaries: dict[str, Any]
    report_totals: dict[str, Any]
    pages: dict[str, str]
    """URL -> HTML rendered from the writer's own cache, before the wipe."""


def _urls(repo_id: int, pr1: str, pr2: str) -> list[str]:
    return [
        "/",
        f"/repos/{repo_id}",
        f"/repos/{repo_id}/prs/1",
        f"/repos/{repo_id}/prs/2",
        f"/repos/{repo_id}/economy?run={pr1}",
        f"/repos/{repo_id}/map",
        f"/repos/{repo_id}/tests",
        f"/repos/{repo_id}/files/src/ledger/durations/parse.py?run={pr2}",
    ]


async def _render(settings: Settings, urls: list[str]) -> dict[str, str]:
    from coverset.app import create_app

    app = create_app(settings)
    pages: dict[str, str] = {}
    async with app.test_app():
        client = app.test_client()
        for url in urls:
            response = await client.get(url)
            assert response.status_code == 200, url
            pages[url] = await response.get_data(as_text=True)
    return pages


def _shape(html: str) -> str:
    """The page without its numbers (relative times move between two renders)."""
    return re.sub(r"\d+", "#", html)


def _report_totals(inp: Any) -> dict[str, Any]:
    return {
        "files": sorted(inp.facts.statics),
        "tests": sorted(t.nodeid for t in inp.tests),
        "covered": {
            nodeid: {p: len(lines) for p, lines in by_path.items()}
            for nodeid, by_path in inp.coverage.items()
        },
        "sources": sorted(path for path, text in inp.sources.items() if text),
    }


@pytest.fixture(scope="module")
def hosted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Hosted]:
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.facts import queries
    from coverset.facts.cache import configure
    from coverset.facts.store import facts_store
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext
    from coverset.report.load import load_run
    from coverset.storage.testing import fresh_blob_url

    root = tmp_path_factory.mktemp("hosted-demo")
    settings = Settings.for_tests(
        data_dir=root / "data",
        database_url=f"sqlite+aiosqlite:///{root / 'coverset.db'}",
        github_webhook_secret=TEST_WEBHOOK_SECRET,
        local_allow_webhook_runs=True,
        blob_url=fresh_blob_url("hosted-demo"),
    )

    async def scenario() -> tuple[int, list[str], str, str]:
        await init_db(settings)
        ctx = OrchestratorContext.from_settings(settings, ingest=IngestPool("process", max_workers=2))
        ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker)
        try:
            await run_demo(ctx, out=settings.data_dir / "demo.git")
            async with ctx.db.session() as session:
                repo_id = int(await session.scalar(select(Repo.id).order_by(Repo.id).limit(1)) or 0)
                runs = list(await session.scalars(select(Run).order_by(Run.created_at)))
        finally:
            await ctx.close()
            assert ctx.ingest is not None
            ctx.ingest.close()
        prs = [r for r in runs if r.pr_id is not None]
        by_pr: dict[int, str] = {}
        for run in prs:
            by_pr[run.pr_id] = run.id  # type: ignore[index]
        pr_runs = [by_pr[k] for k in sorted(by_pr)]
        return repo_id, [r.id for r in runs], pr_runs[0], pr_runs[-1]

    repo_id, run_ids, pr1, pr2 = asyncio.run(scenario())
    clear_registered_secrets()
    configure(settings.cache_overrides())
    store = facts_store(settings)
    summaries = {run: queries.run_summary(store, repo_id, run) for run in run_ids}
    totals = _report_totals(asyncio.run(load_run(settings, pr2)))
    pages = asyncio.run(_render(settings, _urls(repo_id, pr1, pr2)))
    # A new replica: the facts cache is gone (the mirror, a separate cache entry, stays), and so
    # is every in-process cache.
    shutil.rmtree(settings.facts_dir)
    from coverset.web.loaders import clear_caches

    clear_caches()
    yield Hosted(settings, repo_id, run_ids, pr1, pr2, summaries, totals, pages)
    configure(None)
    from coverset.storage import store_from_url

    blobs = store_from_url(settings.blob_url)
    blobs.delete([info.key for info in blobs.list()])  # this module's prefix only


@pytest.fixture
async def client(hosted: Hosted) -> AsyncIterator[Any]:
    from coverset.app import create_app

    app: Quart = create_app(hosted.settings)
    async with app.test_app():
        yield app.test_client()


def test_every_run_is_in_the_bucket_and_nothing_under_the_data_dir(hosted: Hosted) -> None:
    from coverset.storage import store_from_url

    keys = {info.key for info in store_from_url(hosted.settings.blob_url).list("facts/")}
    for run in hosted.run_ids:
        assert f"facts/repo={hosted.repo_id}/run={run}/MANIFEST.json" in keys, run
    assert f"facts/repo={hosted.repo_id}/dictionary.json" in keys
    assert not (hosted.settings.data_dir / "facts").exists()
    assert hosted.settings.facts_dir == hosted.settings.cache_dir / "facts"  # type: ignore[operator]


async def test_pages_render_from_the_bucket_on_a_fresh_cache(client: Any, hosted: Hosted) -> None:
    rid = hosted.repo_id
    pages = {
        "/": "ledger",
        f"/repos/{rid}": "ledger",
        f"/repos/{rid}/prs/1": "refund",
        f"/repos/{rid}/prs/2": "emailcheck",
        f"/repos/{rid}/economy?run={hosted.pr1_run}": "economy",
        f"/repos/{rid}/map": "treemap",
    }
    for url, marker in pages.items():
        response = await client.get(url)
        html = await response.get_data(as_text=True)
        assert response.status_code == 200, url
        assert marker in html.lower(), (url, marker)
    for url, before in hosted.pages.items():
        after = await (await client.get(url)).get_data(as_text=True)
        # The same page: an empty-facts render of any of these is a fraction of its size.
        assert abs(len(_shape(after)) - len(_shape(before))) <= 0.02 * len(_shape(before)), url
    source = await client.get(f"/repos/{rid}/files/src/ledger/durations/parse.py?run={hosted.pr2_run}")
    html = await source.get_data(as_text=True)
    assert source.status_code == 200
    assert re.search(r'<div class="ln run[^"]*" id="L15"', html)
    assert "multi-letter-unit <b>0</b>" in html and "chip-hollow" in html
    assert (hosted.settings.facts_dir / f"repo={rid}" / f"run={hosted.pr2_run}" / "MANIFEST.json").is_file()


def test_queries_answer_what_they_answered_before_the_wipe(hosted: Hosted) -> None:
    from coverset.facts import queries
    from coverset.facts.store import facts_store

    store = facts_store(hosted.settings)
    for run, before in hosted.summaries.items():
        assert queries.run_summary(store, hosted.repo_id, run) == before


async def test_load_run_and_the_agent_backend_read_the_bucket(hosted: Hosted, tmp_path: Path) -> None:
    from coverset.agent.backends import StoreBackend
    from coverset.agent.service import AgentService
    from coverset.db.session import Database
    from coverset.report.load import load_run

    shutil.rmtree(hosted.settings.facts_dir, ignore_errors=True)
    inp = await load_run(hosted.settings, hosted.pr2_run)
    assert _report_totals(inp) == hosted.report_totals
    db = Database.from_url(hosted.settings.database_url)
    try:
        service = AgentService(StoreBackend(hosted.settings, db))
        summary = await service.call("get_run_summary", {"run": hosted.pr2_run})
        before = hosted.summaries[hosted.pr2_run]
        assert before is not None and hosted.pr2_run in str(summary) and str(before.tests) in str(summary)
        gaps = await service.call("find_gaps", {"run": hosted.pr2_run})
        assert gaps and "error" not in gaps
    finally:
        await db.engine.dispose()


async def test_an_evicted_mirror_is_cloned_again_for_the_source_view(client: Any, hosted: Hosted) -> None:
    from coverset.web.loaders import clear_caches

    shutil.rmtree(hosted.settings.mirrors_dir)
    clear_caches()
    url = f"/repos/{hosted.repo_id}/files/src/ledger/durations/parse.py?run={hosted.pr2_run}"
    html = await (await client.get(url)).get_data(as_text=True)
    assert "parse_duration" in html and re.search(r'<div class="ln run[^"]*" id="L15"', html)
    assert (hosted.settings.mirrors_dir / f"{hosted.repo_id}.git" / "HEAD").is_file()


async def test_a_replica_without_its_mirror_shows_the_pr_diff_on_the_first_page(
    client: Any, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica whose whole cache is gone (facts and mirror, what `coverset dev` against the dev
    stack showed after deleting the cache dir) renders PR #1 with its changed functions on the
    first request: the page waits for the clone (``loaders.diff_mirror``), and an analysis made
    without the mirror is never cached (``pr_view``) to linger on the page afterwards."""
    from coverset.orchestrator import repos
    from coverset.web import pr_view
    from coverset.web.loaders import clear_caches

    clone = repos.ensure_mirror_async

    async def slow_clone(*args: Any, **kwargs: Any) -> Any:  # a real repository takes a while
        await asyncio.sleep(0.5)
        return await clone(*args, **kwargs)

    monkeypatch.setattr(repos, "ensure_mirror_async", slow_clone)
    assert hosted.settings.cache_dir is not None
    shutil.rmtree(hosted.settings.cache_dir)
    clear_caches()
    pr_view._cache.clear()
    url = f"/repos/{hosted.repo_id}/prs/1"
    first = await (await client.get(url)).get_data(as_text=True)
    before = _shape(hosted.pages[url])
    assert abs(len(_shape(first)) - len(before)) <= 0.02 * len(before)
    assert "No changed Python functions" not in first
    assert (hosted.settings.mirrors_dir / f"{hosted.repo_id}.git" / "HEAD").is_file()
