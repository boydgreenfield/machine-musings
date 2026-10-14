"""The history queries on the real local demo: six ``main`` baselines and two PRs.

``baseline_trend``, ``function_history``, ``class_changes`` and ``top_gaps`` (facts/queries.py)
read the facts the demo's runs ingested. The demo is seeded once per module with the same helper
the web tests use (``coverset.demo.flow.run_demo`` with the LocalExecutor and the fake forge).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from coverset.config import Settings
from coverset.db.testing import PostgresTestDatabases, run_sql
from coverset.facts.queries import (
    baseline_trend,
    class_changes,
    function_history,
    function_input_cov,
    run_summary,
    top_gaps,
)
from coverset.facts.schema import CALLABLE_KINDS
from coverset.facts.store import FactsStore, facts_store
from coverset.storage.testing import local_facts_store

VALIDATE_EMAIL = "validate_email"


@dataclass(frozen=True)
class DemoFacts:
    """The demo's facts store and the run ids the tests need (baselines oldest first)."""

    store: FactsStore
    repo_id: int
    baselines: tuple[str, ...]
    pr1_run: str
    pr2_run: str


@pytest.fixture(scope="module")
def demo_facts(
    tmp_path_factory: pytest.TempPathFactory, postgres_databases: PostgresTestDatabases
) -> Iterator[DemoFacts]:
    """Seed the local demo once (about 20 s) and describe its facts."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext

    root = tmp_path_factory.mktemp("history-demo")
    data_dir = root / "data"
    settings = Settings.for_tests(
        data_dir=data_dir,
        database_url=postgres_databases.create(),
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
    url = settings.database_url
    baselines = tuple(
        row[0]
        for row in run_sql(
            url, "SELECT id FROM runs WHERE pr_id IS NULL AND status = 'completed' ORDER BY created_at"
        )
    )

    def pr_run(number: int) -> str:
        return run_sql(
            url,
            "SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = :number "
            "ORDER BY r.created_at DESC LIMIT 1",
            {"number": number},
        )[0][0]

    repo_id = int(run_sql(url, "SELECT id FROM repos ORDER BY id LIMIT 1")[0][0])
    yield DemoFacts(facts_store(settings), repo_id, baselines, pr_run(1), pr_run(2))


def _func_key(facts: DemoFacts, run_id: str, qualname: str) -> str:
    rows = facts.store.read_table(facts.repo_id, run_id, "functions").to_pylist()
    return next(r["func_key"] for r in rows if r["qualname"] == qualname)


def test_baseline_trend_is_the_six_main_baselines_oldest_first(demo_facts: DemoFacts) -> None:
    trend = baseline_trend(demo_facts.store, demo_facts.repo_id, "main")
    assert [p.run_id for p in trend] == list(demo_facts.baselines)
    assert len(trend) == 6
    assert [p.created_at for p in trend] == sorted(p.created_at for p in trend)
    assert [p.run_id for p in baseline_trend(demo_facts.store, demo_facts.repo_id, "main", 2)] == list(
        demo_facts.baselines[-2:]
    )
    assert baseline_trend(demo_facts.store, demo_facts.repo_id, "no-such-branch") == []
    # The demo's suite grows baseline by baseline.
    assert [p.tests_total for p in trend] == sorted(p.tests_total for p in trend)


def test_baseline_totals_equal_the_run_facts(demo_facts: DemoFacts) -> None:
    store, rid = demo_facts.store, demo_facts.repo_id
    for point in baseline_trend(store, rid, "main"):
        summary = run_summary(store, rid, point.run_id)
        assert summary is not None
        assert (point.statements, point.lines_run) == (summary.statements, summary.covered)
        assert (point.branches_possible, point.branches_taken) == (summary.branches, summary.branches_covered)
        assert point.tests_total == summary.tests
        assert point.head_sha == summary.head_sha
        functions = store.read_table(rid, point.run_id, "functions").to_pylist()
        keys = {f["func_key"] for f in functions if f["kind"] in CALLABLE_KINDS}
        assert point.functions_total == len(keys)
        covs = [function_input_cov(store, rid, point.run_id, key) for key in keys]
        assert point.functions_called == sum(1 for c in covs if c and c.tests_called > 0)
        assert point.classes_expected == sum(c.blocks_expected or 0 for c in covs if c)
        assert point.classes_observed == sum(c.blocks_observed or 0 for c in covs if c)
        assert point.line_pct == pytest.approx(100.0 * summary.covered / summary.statements)


def test_function_history_of_validate_email_has_a_point_per_baseline(demo_facts: DemoFacts) -> None:
    key = _func_key(demo_facts, demo_facts.baselines[-1], VALIDATE_EMAIL)
    history = function_history(demo_facts.store, demo_facts.repo_id, key, "main")
    assert [p.run_id for p in history] == list(demo_facts.baselines)
    # validate_email arrives with the fourth baseline (the signup screening feature).
    assert [p.present for p in history] == [False, False, False, True, True, True]
    for point in history[:3]:
        assert (point.expected, point.observed, point.line_pct, point.tests_called) == (None, None, None, 0)
    for point in history[3:]:
        cov = function_input_cov(demo_facts.store, demo_facts.repo_id, point.run_id, key)
        assert cov is not None
        assert (point.expected, point.observed) == (cov.blocks_expected, cov.blocks_observed) == (10, 8)
        assert point.tests_called == cov.tests_called
        assert point.missing == ("email:idn", "email:multi-label-domain")
        assert point.line_pct == 100.0
        assert point.input_pct == pytest.approx(80.0)
    assert (
        function_history(demo_facts.store, demo_facts.repo_id, key, "main", last_n=2)[0].run_id
        == (demo_facts.baselines[-2])
    )


def test_class_changes_between_the_last_two_baselines(demo_facts: DemoFacts) -> None:
    store, rid = demo_facts.store, demo_facts.repo_id
    main5, main6 = demo_facts.baselines[-2:]
    changes = class_changes(store, rid, main6, main5)
    by_name = {c.qualname: c for c in changes}
    change = by_name["merge_intervals"]
    assert (change.lost, change.gained) == ((), ("intervals:single",))
    assert (change.lost_count, change.gained_count) == (0, 1)
    assert (change.prior_observed, change.observed) == (1, 2)
    assert not change.expected_changed and not change.body_changed and not change.sampled
    assert change.newly_uncovered == ()
    # Anything else that moved did so under the tracer's sample cap (the demo's sampling is not
    # deterministic: a class a capped function was passed may go unsampled in one run).
    assert all(
        c.sampled and not c.newly_uncovered for name, c in by_name.items() if name != "merge_intervals"
    )
    # The other way round, the class is lost.
    back = {c.qualname: c for c in class_changes(store, rid, main5, main6)}
    assert (back["merge_intervals"].lost, back["merge_intervals"].gained) == (("intervals:single",), ())
    # A run against itself.
    assert class_changes(store, rid, main6, main6) == []


def test_class_changes_of_a_pr_against_its_base(demo_facts: DemoFacts) -> None:
    store, rid, base = demo_facts.store, demo_facts.repo_id, demo_facts.baselines[-1]
    changes = {c.qualname: c for c in class_changes(store, rid, demo_facts.pr1_run, base)}
    # PR #1 adds a test passing a negative total to allocate_cents; the new `refund` is not listed.
    assert changes["allocate_cents"].gained == ("total_cents:negative",)
    assert "refund" not in changes
    assert all(c.sampled for name, c in changes.items() if name != "allocate_cents")
    assert all(c.sampled for c in class_changes(store, rid, demo_facts.pr2_run, base))


def _private_copy(source: FactsStore, facts_dir: Path) -> FactsStore:
    """A copy of ``source``'s cached facts that the test may rewrite in place: the files only, not
    the records of what was pulled, so the copy is an unpublished store of its own (its empty blob
    store has nothing to pull over the edits)."""
    shutil.copytree(
        source.root,
        facts_dir,
        ignore=shutil.ignore_patterns(".sync.json", ".dictionary.sync", "MANIFEST.json", ".lock"),
    )
    return local_facts_store(facts_dir)


def test_class_changes_report_body_lines_newly_uncovered(demo_facts: DemoFacts, tmp_path: Path) -> None:
    """Drop every hit on validate_email's last body line from the newest baseline's test_lines
    (a copy of the store): the line shows up as newly uncovered and its history's line % drops."""
    store = _private_copy(demo_facts.store, tmp_path / "facts")
    rid, (main5, main6) = demo_facts.repo_id, demo_facts.baselines[-2:]
    fn = next(
        r for r in store.read_table(rid, main6, "functions").to_pylist() if r["qualname"] == VALIDATE_EMAIL
    )
    statements = next(
        r["statements"]
        for r in store.read_table(rid, main6, "file_static").to_pylist()
        if r["file_id"] == fn["file_id"]
    )
    target = max(n for n in statements if fn["body_start"] <= n <= fn["end_line"])
    for path in store.table_files(rid, main6, "test_lines"):
        table = pq.read_table(path)
        hit = pc.and_(pc.equal(table["file_id"], fn["file_id"]), pc.equal(table["line"], target))
        pq.write_table(table.filter(pc.invert(hit)), path)
    changes = class_changes(store, rid, main6, main5)
    email = next(c for c in changes if c.qualname == VALIDATE_EMAIL)
    assert email.newly_uncovered == (target,)
    assert email.lost == email.gained == ()
    assert email.lines[0] == email.prior_lines[0] - 1
    history = function_history(store, rid, fn["func_key"], "main")
    assert history[-1].line_pct is not None and history[-1].line_pct < 100.0
    assert history[-2].line_pct == 100.0


def test_top_gaps_rank_called_functions_by_classes_never_passed_in(demo_facts: DemoFacts) -> None:
    store, rid, run = demo_facts.store, demo_facts.repo_id, demo_facts.baselines[-1]
    gaps = top_gaps(store, rid, run)
    assert len(gaps) == 10
    assert [g.missing for g in gaps] == sorted((g.missing for g in gaps), reverse=True)
    assert gaps[0].qualname in {"intersection", "overlaps"} and gaps[0].missing == 16
    for gap in gaps:
        cov = function_input_cov(store, rid, run, gap.func_key)
        assert cov is not None and cov.tests_called > 0
        assert gap.expected == cov.blocks_expected and gap.observed == cov.blocks_observed
        assert len(gap.classes) == gap.missing
        assert sorted(gap.classes) == sorted(n for names in cov.missing.values() for n in names)
        assert gap.line_pct is not None and 0 <= gap.line_pct <= 100
    assert [g.func_key for g in top_gaps(store, rid, run, limit=3)] == [g.func_key for g in gaps[:3]]
    assert top_gaps(store, rid, run, limit=0) == []


# -- review fixes: the window anchored at a run, candidate runs, comparable gaps -------------------


def _edit(store: FactsStore, rid: int, run: str, table: str, fn) -> None:  # type: ignore[no-untyped-def]
    import pyarrow as pa

    for path in store.table_files(rid, run, table):
        t = pq.read_table(path)
        pq.write_table(pa.Table.from_pylist(fn(t.to_pylist()), schema=t.schema), path)


def test_function_history_is_anchored_at_a_run(demo_facts: DemoFacts) -> None:
    store, rid = demo_facts.store, demo_facts.repo_id
    trend = baseline_trend(store, rid, "main")
    key = next(
        r["func_key"]
        for r in store.read_table(rid, demo_facts.baselines[0], "functions").to_pylist()
        if r["kind"] in CALLABLE_KINDS
    )
    third = trend[2]
    points = function_history(store, rid, key, "main", until=third.created_at)
    assert [p.run_id for p in points] == [p.run_id for p in trend[:3]]
    # a window of 2 ending at the third baseline: the two before it and it, never a later one
    assert [p.run_id for p in function_history(store, rid, key, "main", 2, until=third.created_at)] == [
        trend[1].run_id,
        trend[2].run_id,
    ]
    assert all(p.sig_hash for p in points if p.present)


def test_baseline_runs_from_candidates(demo_facts: DemoFacts) -> None:
    from coverset.facts.queries import _baseline_runs, baseline_candidate_limit

    store, rid = demo_facts.store, demo_facts.repo_id
    full = _baseline_runs(store, rid, "main", 12)
    newest_first = list(reversed(demo_facts.baselines))
    assert _baseline_runs(store, rid, "main", 12, candidates=newest_first) == full
    # the database says the branch has no run outside a PR: nothing is scanned
    assert _baseline_runs(store, rid, "main", 12, candidates=[]) == []
    # a short list is trusted when it yields a baseline (PR runs among the candidates are skipped)
    assert _baseline_runs(store, rid, "main", 12, candidates=[newest_first[0], demo_facts.pr1_run]) == [
        newest_first[0]
    ]
    # candidates the store does not hold: every run is scanned
    assert _baseline_runs(store, rid, "main", 12, candidates=["run_unknown"]) == full
    # a full list that yields too few: every run is scanned
    filler = [f"run_x{i}" for i in range(baseline_candidate_limit(12))]
    assert _baseline_runs(store, rid, "main", 12, candidates=filler) == full
    assert baseline_trend(store, rid, "main", candidates=newest_first) == baseline_trend(store, rid, "main")


def test_class_changes_name_classes_only_for_the_same_signature(
    demo_facts: DemoFacts, tmp_path: Path
) -> None:
    """A changed ``sig_hash`` (or expected count) means the stored gaps cannot be compared by name:
    the change is listed as uncompared, with no class called lost or gained. The sample-cap flag
    is this run's, not the prior run's."""
    store = _private_copy(demo_facts.store, tmp_path / "facts")
    rid, (main5, main6) = demo_facts.repo_id, demo_facts.baselines[-2:]

    def resign(rows: list[dict]) -> list[dict]:  # type: ignore[type-arg]
        for row in rows:
            if row["qualname"] == "merge_intervals":
                row["sig_hash"] = "0" * 16
        return rows

    def cap_prior(rows: list[dict]) -> list[dict]:  # type: ignore[type-arg]
        for row in rows:
            row["tests_truncated"] = 3
        return rows

    _edit(store, rid, main6, "functions", resign)
    _edit(store, rid, main5, "func_input_cov", cap_prior)
    change = next(c for c in class_changes(store, rid, main6, main5) if c.qualname == "merge_intervals")
    assert change.sig_changed and change.uncompared
    assert (change.lost, change.gained, change.lost_count, change.gained_count) == ((), (), 0, 0)
    assert not change.sampled  # only the prior run hit the cap
