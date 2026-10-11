"""``coverset invariants scan``: review-kind invariants at the default-branch head, with the fake client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from orch_fakes import Script, TestRepo, pr_change
from orch_helpers import run_baseline, run_pr
from sqlalchemy import select

from coverset.ai.fake import FakeReviewClient
from coverset.commands.invariants import REVIEW_TARGET, ScanReport, scan_invariants
from coverset.config import Settings
from coverset.db.models import Invariant, InvariantResult, ReviewFinding
from coverset.orchestrator.runner import OrchestratorContext

REVIEW_INVARIANTS = """invariants:
  - id: add_commutes
    scope: {kind: function, target: factspkg.calc.add}
    statement: add(a, b) equals add(b, a).
    severity: warning
    verify:
      - kind: test
        marker: add_commutes
      - kind: review
  - id: classify_names_sign
    scope: {kind: function, target: factspkg.calc.classify}
    statement: classify returns neg, zero or pos by the sign of n.
    severity: info
    verify:
      - kind: review
  - id: doubles
    scope: {kind: function, target: factspkg.calc.never_called}
    statement: never_called returns twice its argument.
    severity: info
    verify:
      - kind: review
  - id: box_totals
    scope: {kind: function, target: factspkg.calc.Box.total}
    statement: Box.total sums the items.
    severity: info
    verify:
      - kind: test
"""

CLASSIFY_FIXTURE = {
    "description": "classify is reviewed as upholding its invariant.",
    "source": "fixture",
    "model": None,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cost_usd_estimate": 0.0,
    "requests": 0,
    "notes": ["test fixture"],
    "result": {
        "functions": [
            {
                "qualname": "classify",
                "verdict": "covered",
                "confidence": "high",
                "evidence": [],
                "missing_input_classes": [],
                "suggested_tests": [],
                "invariant_findings": [
                    {
                        "invariant_id": "classify_names_sign",
                        "status": "satisfied",
                        "evidence": "test_classify covers negative and positive n; zero is a branch of its own.",
                    }
                ],
                "abstain_reason": "",
            }
        ]
    },
}


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """These tests run the scan by hand: no automatic scan after their baselines."""
    return {"scan_after_baseline": False}


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    root = tmp_path / "ai-fixtures"
    (root / "default").mkdir(parents=True)
    (root / "default" / "classify.json").write_text(json.dumps(CLASSIFY_FIXTURE))
    return root


@pytest.fixture
def client_factory(fixtures_dir: Path) -> Any:
    seen: list[dict[str, Any]] = []

    def factory(settings: Settings, repo_enabled: bool) -> FakeReviewClient:
        client = FakeReviewClient(fixtures_dir)
        original = client.review

        def review(bundle: Any) -> Any:
            seen.append(bundle.model_dump(mode="json"))
            return original(bundle)

        client.review = review  # type: ignore[method-assign]
        return client

    factory.seen = seen  # type: ignore[attr-defined]
    return factory


async def _scan(ctx: OrchestratorContext, client_factory: Any, **kwargs: Any) -> ScanReport:
    return await scan_invariants(
        ctx.db, ctx.settings, ctx.store, None, client_factory=client_factory, **kwargs
    )


async def _results(ctx: OrchestratorContext, run_id: str) -> dict[str, InvariantResult]:
    async with ctx.db.session() as session:
        rows = await session.execute(
            select(Invariant.inv_key, InvariantResult)
            .join(InvariantResult, InvariantResult.invariant_id == Invariant.id)
            .where(InvariantResult.run_id == run_id)
        )
        return {key: result for key, result in rows}


async def test_scan_reviews_targets_and_records_statuses(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, client_factory: Any
) -> None:
    script.zero_passes = True
    head = repo.commit({"invariants.yaml": REVIEW_INVARIANTS}, "review-kind invariants")
    run_id = await run_baseline(ctx, repo_id, head)
    before = await _results(ctx, run_id)
    assert before["classify_names_sign"].status == "needs_review"
    assert before["add_commutes"].status == "untested"

    report = await _scan(ctx, client_factory)
    assert report.run_id == run_id and report.head_sha == head
    rows = {r.inv_id: r for r in report.rows}
    assert set(rows) == {"add_commutes", "classify_names_sign", "doubles"}, "test-only invariants are skipped"
    assert rows["classify_names_sign"].status == "satisfied"
    assert rows["classify_names_sign"].run_status == "needs_review"
    assert rows["classify_names_sign"].review == "fixture: satisfied"
    assert rows["doubles"].status == "needs_review"  # the heuristic sees no covering test
    assert rows["add_commutes"].status == "untested", "a review never overrides what tests decided"
    assert report.findings_stored == 3 and report.findings_reused == 0
    assert report.ai_source == "fixture, heuristic"

    bundles = client_factory.seen
    assert {b["functions"][0]["qualname"] for b in bundles} == {"add", "classify", "never_called"}
    by_name = {b["functions"][0]["qualname"]: b["functions"][0] for b in bundles}
    assert all(f["change_kind"] == REVIEW_TARGET and f["diff_hunk"] == "" for f in by_name.values())
    assert by_name["classify"]["covering_tests"], "covering tests come from the run's facts"
    assert any("def test_classify" in t["body"] for t in by_name["classify"]["covering_tests"])
    assert by_name["never_called"]["covering_tests"] == []
    assert "classify_names_sign" in {i["id"] for i in by_name["classify"]["invariants"]}

    after = await _results(ctx, run_id)
    assert {k for k, r in after.items() if r.trigger == "scan"} == {
        "add_commutes",
        "classify_names_sign",
        "doubles",
    }
    assert after["box_totals"].trigger == "run"
    classify = after["classify_names_sign"]
    assert classify.status == "satisfied" and classify.target_body_hash is not None
    assert classify.evidence["scan"]["run_status"] == "needs_review"
    assert classify.evidence["entries"][0]["finding"]["source"] == "fixture"
    kinds = [e["kind"] for e in after["add_commutes"].evidence["entries"]]
    assert kinds == ["test", "review"]
    async with ctx.db.session() as session:
        findings = list(await session.scalars(select(ReviewFinding)))
    assert len(findings) == 3 and {f.run_id for f in findings} == {run_id}

    again = await _scan(ctx, client_factory)
    assert again.findings_stored == 0 and again.findings_reused == 3
    assert {r.inv_id: (r.run_status, r.status) for r in again.rows} == {
        r.inv_id: (r.run_status, r.status) for r in report.rows
    }
    lines = again.lines()
    assert lines[0].startswith(f"local/src-repo at {head[:12]}")
    assert any(line.startswith("classify_names_sign") and line.endswith("satisfied") for line in lines)


async def test_scan_uses_the_latest_default_branch_baseline_only(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, client_factory: Any
) -> None:
    script.zero_passes = True
    head = repo.commit({"invariants.yaml": REVIEW_INVARIANTS}, "review-kind invariants")
    baseline = await run_baseline(ctx, repo_id, head)
    await run_pr(ctx, repo_id, pr_change(repo), head)
    report = await _scan(ctx, client_factory)
    assert report.run_id == baseline
    empty = await _scan(ctx, client_factory, executor="modal")
    assert empty.run_id is None and "no completed baseline run of main on executor modal" in (
        empty.note or ""
    )
    assert empty.lines() == [empty.note]


async def test_scan_without_review_invariants_or_baseline(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, client_factory: Any
) -> None:
    nothing = await _scan(ctx, client_factory)
    assert nothing.run_id is None and "has no completed baseline run" in (nothing.note or "")
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)  # the default invariants.yaml has no review entries
    report = await _scan(ctx, client_factory)
    assert report.rows == [] and report.note == "no review-kind invariants in invariants.yaml"
    assert client_factory.seen == []


async def test_scan_with_ai_off_still_records_statuses(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    head = repo.commit({"invariants.yaml": REVIEW_INVARIANTS}, "review-kind invariants")
    run_id = await run_baseline(ctx, repo_id, head)
    report = await _scan(ctx, lambda settings, enabled: None)
    assert report.ai_source == "off" and report.findings_stored == 0
    assert {r.inv_id: r.status for r in report.rows} == {
        "add_commutes": "untested",
        "classify_names_sign": "needs_review",
        "doubles": "needs_review",
    }
    assert (await _results(ctx, run_id))["doubles"].trigger == "scan"


def test_scan_command_is_registered() -> None:
    from coverset.cli import cli

    group = cli.commands["invariants"]
    assert "scan" in group.commands  # type: ignore[attr-defined]
    assert "arrives in phase" not in (group.commands["scan"].help or "")  # type: ignore[attr-defined]
