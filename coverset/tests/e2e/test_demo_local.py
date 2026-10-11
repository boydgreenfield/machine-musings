"""End to end on the demo app with the LocalExecutor: six baseline runs, both PRs, a replayed webhook.

Real subprocesses, a real virtualenv (cached under ~/.cache/coverset), real pytest-coverset bundles
ingested by a real (spawned) process pool, the real facts store and the fake forge. About 20 s once
the venv exists. Pins the README's claims: the refund test runs first (rank 1) and the verdict posts
before the shard finishes, sooner than the file-order estimate.
"""

from __future__ import annotations

import ast
import shutil
import subprocess

import pytest
from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import ForgeCall, PullRequest, Run, RunEvent, TestCase, TestResult
from coverset.db.session import init_db
from coverset.demo.flow import format_table, run_demo
from coverset.demo.seed import PR_OPENED_FIXTURE
from coverset.forge.fake import FakeForge
from coverset.forge.signing import TEST_WEBHOOK_SECRET
from coverset.orchestrator.bundles import IngestPool
from coverset.orchestrator.runner import OrchestratorContext

pytestmark = pytest.mark.slow

FIVE_CLASSES = {
    ("validate_email", "email:multi-label-domain"),
    ("parse_duration", "text:multi-letter-unit"),
    ("allocate_cents", "total_cents:negative"),
    ("allocate_cents", "weights:zero-element"),
    ("merge_intervals", "intervals:unsorted"),
}
FLAKY = "tests/test_windows.py::test_hold_survives_clock_skew"
REFUND = "tests/test_refund.py::test_refund_partial"


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("needs uv and git")
    import asyncio

    root = tmp_path_factory.mktemp("demo")
    settings = Settings.for_tests(
        data_dir=root / "data",
        database_url=f"sqlite+aiosqlite:///{root / 'coverset.db'}",
        github_webhook_secret=TEST_WEBHOOK_SECRET,
        local_allow_webhook_runs=True,
    )

    async def scenario() -> dict:  # type: ignore[type-arg]
        await init_db(settings)
        ctx = OrchestratorContext.from_settings(settings, ingest=IngestPool("process", max_workers=2))
        ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker)
        try:
            result = await run_demo(ctx, out=root / "data" / "demo.git")
            again = await run_demo(ctx, out=root / "data" / "demo.git")
            replay = await _replay(settings) if PR_OPENED_FIXTURE.is_file() else None
            async with ctx.db.session() as session:
                runs = {r.id: r for r in await session.scalars(select(Run))}
                calls = list(await session.scalars(select(ForgeCall)))
                comment_ids = {pr.number: pr.comment_id for pr in await session.scalars(select(PullRequest))}
                flaky = list(
                    await session.execute(
                        select(TestResult.run_id, TestCase.nodeid)
                        .join(TestCase, TestCase.id == TestResult.test_id)
                        .where(TestResult.flaky.is_(True))
                    )
                )
                nodeids = {
                    run_id: [n for _, n in rows]
                    for run_id, rows in _group(
                        await session.execute(
                            select(TestResult.run_id, TestCase.nodeid).join(
                                TestCase, TestCase.id == TestResult.test_id
                            )
                        )
                    ).items()
                }
                run_events = {
                    run_id: list(
                        await session.scalars(
                            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                        )
                    )
                    for run_id in {s.run_id for s in result.runs}
                }
            return {
                "result": result,
                "again": again,
                "runs": runs,
                "calls": calls,
                "comment_ids": comment_ids,
                "flaky": flaky,
                "nodeids": nodeids,
                "events": run_events,
                "demo_git": root / "data" / "demo.git",
                "replay": replay,
            }
        finally:
            await ctx.close()
            assert ctx.ingest is not None
            ctx.ingest.close()

    return asyncio.run(scenario())


def _group(rows: object) -> dict[str, list[tuple[str, str]]]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for run_id, nodeid in rows:  # type: ignore[attr-defined]
        grouped.setdefault(run_id, []).append((run_id, nodeid))
    return grouped


def _file_order_position(nodeids: list[str], demo_git: object, sha: str, target: str) -> int:
    """Where plain pytest collection puts ``target``: files in name order, then definition order."""
    path, _, name = target.partition("::")
    before = sum(1 for n in nodeids if n.split("::", 1)[0] < path)
    source = subprocess.run(
        ["git", "--git-dir", str(demo_git), "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    functions = [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    ]
    in_file = [n for n in nodeids if n.split("::", 1)[0] == path]
    earlier = functions[: functions.index(name)]
    return before + sum(1 for n in in_file if n.split("::", 1)[1].split("[", 1)[0] in earlier) + 1


async def _replay(settings: Settings) -> dict:  # type: ignore[type-arg]
    from coverset.app import create_app
    from coverset.demo.flow import replay_fixture
    from coverset.orchestrator.queue import install_run_queue

    app = create_app(settings)
    install_run_queue(app)
    async with app.test_app():
        return await replay_fixture(app, settings)


def _by_name(demo: dict) -> dict[str, Run]:  # type: ignore[type-arg]
    return {seeded.name: demo["runs"][seeded.run_id] for seeded in demo["result"].runs}


def test_history_and_prs_ran(demo: dict) -> None:  # type: ignore[type-arg]
    runs = _by_name(demo)
    assert list(runs) == ["main-1", "main-2", "main-3", "main-4", "main-5", "main-6", "pr-1", "pr-2"]
    assert all(run.status == "completed" for run in runs.values())
    assert [runs[f"main-{n}"].verdict for n in range(1, 7)] == ["success"] * 6
    table = format_table(demo["result"].runs)
    assert "pr-1" in table and "failure" in table


def test_pr1_fails_fast_with_the_refund_test_first(demo: dict) -> None:  # type: ignore[type-arg]
    pr1 = _by_name(demo)["pr-1"]
    summary = pr1.summary_json
    assert pr1.verdict == "failure" and summary is not None
    assert summary["failures"][0]["nodeid"] == REFUND
    assert [f["nodeid"] for f in summary["failures"]] == [REFUND]
    assert pr1.rank_coverset == 1
    assert pr1.rank_executed is not None and pr1.rank_executed >= 1
    assert pr1.rank_file_order == _file_order_position(
        demo["nodeids"][pr1.id], demo["demo_git"], pr1.head_sha, REFUND
    )
    assert pr1.rank_file_order > pr1.rank_coverset
    assert pr1.t_first_failure_file_order_est is not None
    assert "first_failure" in pr1.timeline and summary["first_failure_s"] is not None
    assert summary["first_failure_s"] < pr1.t_first_failure_file_order_est
    assert pr1.scoring_basis_run_id == _by_name(demo)["main-6"].id


def test_pr1_verdict_posts_before_the_shards_finish(demo: dict) -> None:  # type: ignore[type-arg]
    log = demo["events"][_by_name(demo)["pr-1"].id]
    early = [e for e in log if e.kind == "verdict" and e.payload.get("early")]
    shard_ends = [e for e in log if e.kind == "shard" and e.payload.get("status") != "running"]
    assert early and shard_ends
    assert early[0].seq < shard_ends[-1].seq
    assert [e.kind for e in log][-2:] == ["verdict", "done"]
    assert log[-1].payload["exit_code"] == 1


def test_pr2_passes_with_gaps(demo: dict) -> None:  # type: ignore[type-arg]
    pr2 = _by_name(demo)["pr-2"]
    summary = pr2.summary_json
    assert pr2.verdict == "success" and summary is not None
    assert summary["label"] == "PASS with gaps"
    assert summary["patch_pct"] == 100.0 and summary["patch_total"] > 0
    missing = {(m["function"], m["class_name"]) for m in summary["missing_input_classes"]}
    assert missing >= FIVE_CLASSES
    statuses = {i["id"]: i["status"] for i in summary["invariants"]}
    assert statuses["signup_rejects_existing_domain"] == "untested"
    assert summary["ai_source"] == "fixture"  # fixtures/ai/ledger/ found for coverset-demo/ledger


def test_main4_flaky_test_is_recorded(demo: dict) -> None:  # type: ignore[type-arg]
    main4 = _by_name(demo)["main-4"]
    assert (main4.id, FLAKY) in {(run_id, nodeid) for run_id, nodeid in demo["flaky"]}
    assert all(run_id == main4.id for run_id, _ in demo["flaky"])
    assert "1 flaky" in (main4.reason or "")


def test_fake_forge_received_checks_and_sticky_comments(demo: dict) -> None:  # type: ignore[type-arg]
    runs = _by_name(demo)
    methods = {c.method for c in demo["calls"]}
    assert {"create_check", "update_check", "upsert_pr_comment"} <= methods
    comments = [c for c in demo["calls"] if c.method == "upsert_pr_comment"]
    assert any("coverset:sticky" in str(c.request) for c in comments)
    assert runs["pr-1"].check_run_id is not None and runs["pr-2"].check_run_id is not None
    assert demo["comment_ids"][1] is not None and demo["comment_ids"][2] is not None
    assert all(runs[f"main-{n}"].check_run_id is None for n in range(1, 7))  # baselines post nothing


def test_replayed_webhook_lands_on_the_demo_repo(demo: dict) -> None:  # type: ignore[type-arg]
    replay = demo["replay"]
    if replay is None:
        pytest.skip("no pr_opened fixture")
    assert replay["http_status"] in (200, 202)
    assert replay["status"] == "duplicate"
    assert replay["run_id"] == _by_name(demo)["pr-1"].id


def test_repeat_demo_reuses_every_run(demo: dict) -> None:  # type: ignore[type-arg]
    again = demo["again"]
    assert again.shas == demo["result"].shas
    assert [r.run_id for r in again.runs] == [r.run_id for r in demo["result"].runs]
    assert all(r.reused for r in again.runs)
