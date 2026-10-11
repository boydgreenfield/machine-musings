"""Regression tests for the UI review: the demo-only cold-path bar, the estimate label, and the
sticky comment's never-exercised count matching the PR page's cards."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from quart.typing import TestClientProtocol as Client
from web_helpers import Demo

from coverset.config import Settings
from coverset.facts.store import FactsStore
from coverset.forge.comment import missing_counts
from coverset.orchestrator.repos import mirror_path
from coverset.orchestrator.verdict import summary_from_json
from coverset.web.detail import build_cards
from coverset.web.facts_view import run_facts
from coverset.web.pr_view import analyse_diff
from coverset.web.render.timeline import Milestone, render_timeline
from coverset.web.repos import target_gaps
from coverset.web.run_view import DEMO_REPO, GHOST_LABEL, cold_path


def test_only_the_demo_repository_gets_a_cold_path() -> None:
    assert cold_path(SimpleNamespace(full_name="acme/payments")) == (None, None)  # type: ignore[arg-type]
    seconds, failure = cold_path(SimpleNamespace(full_name=DEMO_REPO))  # type: ignore[arg-type]
    assert seconds is not None and failure is not None and 0 < failure <= seconds < 60


def test_the_ghost_bar_carries_its_first_failure_tick() -> None:
    marks = [Milestone("queued", "queued", 0.0), Milestone("finished", "finished", 5.0)]
    html = str(render_timeline(marks, ghost=7.3, ghost_label=GHOST_LABEL, ghost_mark=6.4))
    assert "measured cold path, demo app: fresh sandbox, install, plain pytest" in html
    assert "first failure 6.4" in html and html.count("tl-tick is-failure") == 1
    plain = str(render_timeline(marks, ghost=7.3, ghost_label=GHOST_LABEL))
    assert "first failure" not in plain


async def test_demo_run_pages_show_the_demo_cold_path(client: Client, demo: Demo) -> None:
    failing = await (await client.get(f"/runs/{demo.pr1_run}")).get_data(as_text=True)
    assert GHOST_LABEL in failing and "first failure" in failing
    assert "estimated from the first test" in failing  # the head line after a confirmed failure
    passing = await (await client.get(f"/runs/{demo.baseline_run}")).get_data(as_text=True)
    assert GHOST_LABEL in passing


@pytest.mark.parametrize("number", [1, 2])
async def test_the_comment_counts_the_classes_the_cards_count(demo: Demo, number: int) -> None:
    con = sqlite3.connect(demo.data_dir.parent / "coverset.db")
    try:
        run_id = demo.pr1_run if number == 1 else demo.pr2_run
        head, base, summary_text = con.execute(
            "SELECT head_sha, base_sha, summary_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        comments = [
            json.loads(row[0])
            for row in con.execute(
                "SELECT request FROM forge_calls WHERE method = 'upsert_pr_comment' ORDER BY id"
            )
        ]
        bodies = [c["body"] for c in comments if c.get("pr_number") == number][-1:]  # the final verdict's
    finally:
        con.close()
    summary = summary_from_json(json.loads(summary_text))
    changed, _elsewhere = missing_counts(summary.function_rows, summary.missing_input_classes)

    def cards_count() -> int:
        facts = run_facts(FactsStore(demo.data_dir / "facts"), demo.repo_id, run_id)
        settings = Settings.for_tests(data_dir=demo.data_dir, database_url=demo.db_url)
        analysis = analyse_diff(mirror_path(settings, demo.repo_id), base, head, facts)
        cards = build_cards(facts, analysis, [], [], {})
        return len(
            {
                (card.region.qualname, cls.name)
                for card in cards
                if card.view.observations
                for cls in card.view.cov.missing
            }
        )

    assert changed == await asyncio.to_thread(cards_count)
    if changed:
        noun = "input class" if changed == 1 else "input classes"
        assert any(f"{changed} {noun} never exercised in changed functions" in body for body in bodies)


@pytest.mark.parametrize("number", [1, 2])
async def test_the_invariants_page_lists_the_target_gaps(client: Client, demo: Demo, number: int) -> None:
    run_id = demo.pr1_run if number == 1 else demo.pr2_run
    con = sqlite3.connect(demo.data_dir.parent / "coverset.db")
    try:
        summary = json.loads(
            con.execute("SELECT summary_json FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        )
    finally:
        con.close()
    parsed = summary_from_json(summary)
    _changed, elsewhere = missing_counts(parsed.function_rows, parsed.missing_input_classes)
    gaps = target_gaps(summary)
    assert sum(len(classes) for _f, classes in gaps) == elsewhere
    html = await (await client.get(f"/repos/{demo.repo_id}/invariants?run={run_id}")).get_data(as_text=True)
    assert ('id="target-gaps"' in html) == bool(gaps)
    for function, classes in gaps:
        assert function in html and all(c in html for c in classes)
    if elsewhere:
        pr = await (await client.get(f"/repos/{demo.repo_id}/prs/{number}")).get_data(as_text=True)
        assert "#target-gaps" in pr
