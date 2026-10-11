"""review_changes (reuse, untrusted skip, rows), advisory invariant mapping, and PR markdown."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ai_helpers import BundleFactory, make_bundle, make_function, verdict

from coverset.ai.client import ReviewError, ReviewOutcome
from coverset.ai.fake import FakeReviewClient
from coverset.ai.review import (
    ReviewFindingRow,
    findings_to_invariant_statuses,
    format_findings_markdown,
    review_changes,
    review_evidence,
)
from coverset.ai.schema import ReviewBundle, ReviewResult
from coverset.analysis.invariants import InvariantContext, evaluate, load_invariants
from coverset.config import Settings


class CountingClient:
    """Wraps a client and records every bundle it is asked to review."""

    name = "counting"

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.seen: list[ReviewBundle] = []

    def review(self, bundle: ReviewBundle) -> ReviewOutcome:
        self.seen.append(bundle)
        return self.inner.review(bundle)


class FailingClient:
    name = "failing"

    def review(self, bundle: ReviewBundle) -> ReviewOutcome:
        raise ReviewError("Anthropic API failed after 4 attempts (InternalServerError)")


def two_function_bundle() -> ReviewBundle:
    other = make_function(qualname="other", func_key="fedcba9876543210", body_hash="0" * 16)
    return make_bundle(make_function(), other)


# --- review_changes ----------------------------------------------------------------------------


def test_review_changes_reviews_one_function_per_call(ai_settings: Settings, tmp_path: Path) -> None:
    client = CountingClient(FakeReviewClient(tmp_path))
    bundle = two_function_bundle()
    rows = review_changes(client, bundle, untrusted=False, settings=ai_settings)
    assert [len(b.functions) for b in client.seen] == [1, 1]
    assert [r.qualname for r in rows] == ["scale", "other"]
    first = rows[0]
    assert first.func_key == "0123456789abcdef"
    assert first.body_hash == "feedfacecafebeef"
    assert first.bundle_hash == bundle.for_function(first.func_key).bundle_hash != bundle.bundle_hash
    assert (first.source, first.verdict, first.confidence) == ("heuristic", "partially_covered", "medium")
    assert first.payload["verdict"]["qualname"] == "scale"
    assert first.payload["advisory"] is True
    assert first.reused is False


def test_review_changes_reuses_a_stored_finding(ai_settings: Settings, tmp_path: Path) -> None:
    bundle = two_function_bundle()
    first_rows = review_changes(FakeReviewClient(tmp_path), bundle, untrusted=False, settings=ai_settings)
    stored = {(r.func_key, r.bundle_hash): r for r in first_rows[:1]}
    client = CountingClient(FakeReviewClient(tmp_path))
    rows = review_changes(
        client, bundle, untrusted=False, settings=ai_settings, existing=lambda k, h: stored.get((k, h))
    )
    assert [b.functions[0].qualname for b in client.seen] == ["other"]  # only the new function was reviewed
    assert rows[0].reused is True and rows[1].reused is False
    assert rows[0].payload == first_rows[0].payload


def test_review_changes_skips_untrusted_bundles_unless_allowed(ai_settings: Settings, tmp_path: Path) -> None:
    client = CountingClient(FakeReviewClient(tmp_path))
    assert review_changes(client, two_function_bundle(), untrusted=True, settings=ai_settings) == []
    assert client.seen == []
    allowed = Settings.for_tests(data_dir=tmp_path / "d", ai_review_untrusted=True)
    assert len(review_changes(client, two_function_bundle(), untrusted=True, settings=allowed)) == 2


def test_review_changes_with_ai_off_or_a_failing_client(ai_settings: Settings) -> None:
    assert review_changes(None, two_function_bundle(), untrusted=False, settings=ai_settings) == []
    assert review_changes(FailingClient(), two_function_bundle(), untrusted=False, settings=ai_settings) == []


def test_review_changes_redacts_model_output(tmp_path: Path) -> None:
    secret = "my-live-github-token-value"
    settings = Settings.for_tests(data_dir=tmp_path / "d", github_token=secret)

    class Echo:
        name = "echo"

        def review(self, bundle: ReviewBundle) -> ReviewOutcome:
            v = verdict(
                evidence=[{"test_id": "tests/test_maths.py::test_scale", "what_it_exercises": secret}]
            )
            return ReviewOutcome(
                result=ReviewResult(functions=[v]), source="anthropic", model="claude-opus-5-5"
            )

    [row] = review_changes(Echo(), make_bundle(make_function()), untrusted=False, settings=settings)
    assert secret not in str(row.payload)


def test_demo_fixture_rows_for_pr_2(email_bundle: BundleFactory, ai_settings: Settings) -> None:
    rows = review_changes(FakeReviewClient(), email_bundle(), untrusted=False, settings=ai_settings)
    [row] = rows
    assert (row.qualname, row.source, row.verdict) == ("validate_email", "fixture", "partially_covered")
    assert row.tokens == 0 and row.cost == 0.0


def test_rows_round_trip_through_the_db_model(ai_settings: Settings, tmp_path: Path) -> None:
    from coverset.db.models import ReviewFinding

    [row, _] = review_changes(
        FakeReviewClient(tmp_path), two_function_bundle(), untrusted=False, settings=ai_settings
    )
    kwargs = row.to_db_kwargs()
    assert set(kwargs) == {
        "func_key",
        "body_hash",
        "bundle_hash",
        "source",
        "verdict",
        "confidence",
        "payload",
    }
    stored = ReviewFinding(run_id="run-1", **kwargs)
    back = ReviewFindingRow.from_db(stored)
    assert back.reused is True
    assert (back.func_key, back.qualname, back.bundle_hash, back.verdict) == (
        row.func_key,
        row.qualname,
        row.bundle_hash,
        row.verdict,
    )
    assert back.function_verdict == row.function_verdict


# --- invariants (advisory) ---------------------------------------------------------------------


def row_with(
    *findings: tuple[str, str], verdict_name: str = "partially_covered", body: str = "b" * 16
) -> ReviewFindingRow:
    v = verdict(
        verdict=verdict_name,
        invariant_findings=[{"invariant_id": i, "status": s, "evidence": f"{i} is {s}"} for i, s in findings],
        abstain_reason="unsure" if verdict_name == "abstain" else "",
    )
    return ReviewFindingRow(
        func_key="k" * 16,
        qualname="scale",
        body_hash=body,
        bundle_hash="h" * 64,
        source="anthropic",
        verdict=v.verdict,
        confidence=v.confidence,
        payload={"qualname": "scale", "verdict": v.model_dump(mode="json")},
    )


def test_invariant_statuses_only_replace_needs_review() -> None:
    rows = [
        row_with(("review_only", "satisfied"), ("tested_ok", "violated"), ("tested_bad", "satisfied")),
        row_with(("flagged", "violated")),
    ]
    current = {
        "review_only": "needs_review",
        "tested_ok": "satisfied",
        "tested_bad": "violated",
        "flagged": "needs_review",
    }
    result = findings_to_invariant_statuses(rows, current)
    assert result == {"review_only": "satisfied", "flagged": "needs_review"}
    merged = {**current, **result}
    assert merged["tested_ok"] == "satisfied" and merged["tested_bad"] == "violated"
    assert "violated" not in result.values()


def test_invariant_statuses_ignore_abstentions_and_not_applicable() -> None:
    rows = [row_with(("a", "satisfied"), verdict_name="abstain"), row_with(("b", "not_applicable"))]
    assert findings_to_invariant_statuses(rows) == {}


def test_conflicting_reviews_keep_needs_review() -> None:
    rows = [row_with(("inv", "satisfied")), row_with(("inv", "untested"))]
    assert findings_to_invariant_statuses(rows, {"inv": "needs_review"}) == {"inv": "needs_review"}


def test_review_evidence_feeds_evaluate_without_overriding_tests() -> None:
    [review_only, tested] = load_invariants(
        """
invariants:
  - id: review_only
    scope: {kind: function, target: pkg.maths.scale}
    statement: scale is linear
    verify: [{kind: review}]
  - id: tested
    scope: {kind: function, target: pkg.maths.scale}
    statement: scale never overflows
    verify: [{kind: test}, {kind: review}]
"""
    )
    body = "b" * 16
    evidence = review_evidence([row_with(("review_only", "satisfied"), ("tested", "satisfied"), body=body)])
    ok = evaluate(
        review_only,
        InvariantContext(results={}, links={}, target_body_hash=body, review_finding=evidence["review_only"]),
    )
    assert ok.status == "satisfied"
    assert ok.evidence["entries"][0]["advisory"] is True
    failing = evaluate(
        tested,
        InvariantContext(
            results={"tests/test_maths.py::test_overflow": "failed"},
            links={"tests/test_maths.py::test_overflow": ["tested"]},
            target_body_hash=body,
            review_finding=evidence["tested"],
        ),
    )
    assert failing.status == "violated"  # the test outcome wins over the reviewer
    stale_body = evaluate(
        review_only,
        InvariantContext(
            results={}, links={}, target_body_hash="c" * 16, review_finding=evidence["review_only"]
        ),
    )
    assert stale_body.status == "needs_review"


# --- markdown ----------------------------------------------------------------------------------


def test_markdown_lists_findings_with_source_labels(
    email_bundle: BundleFactory, ai_settings: Settings, tmp_path: Path
) -> None:
    rows = review_changes(FakeReviewClient(), email_bundle(), untrusted=False, settings=ai_settings)
    rows += review_changes(
        FakeReviewClient(tmp_path), make_bundle(make_function()), untrusted=False, settings=ai_settings
    )
    text = format_findings_markdown(rows)
    assert text.startswith("### AI review (fixture, heuristic)")
    assert "Advisory" in text
    assert "| `validate_email` | partially covered | high | `email:multi-label-domain`" in text
    assert "| fixture |" in text and "| heuristic |" in text
    assert "<summary>Suggested test for validate_email" in text
    assert "```python\n" in text
    assert format_findings_markdown([]) == ""


def outside_code(markdown: str) -> str:
    """``markdown`` without fenced code blocks and inline code spans."""
    without_blocks = re.sub(r"^(`{3,})[^\n]*\n.*?^\1$", "", markdown, flags=re.DOTALL | re.MULTILINE)
    return re.sub(r"(`+)(?:(?!\1).)+?\1", "", without_blocks)


def test_markdown_escapes_everything_from_code_and_models() -> None:
    hostile = verdict(
        qualname="a|b<script>alert(1)</script>",
        missing_input_classes=[
            {
                "name": "x:`tick`|pipe",
                "description": "d",
                "why_likely": "w",
                "example_value": "e",
                "severity": "high",
                "hypothesis_strategy": "st.none()",
            }
        ],
        suggested_tests=[
            {"name": "test_<img src=x onerror=alert(1)>", "style": "example", "code": "s = '```'\nassert s\n"}
        ],
        invariant_findings=[
            {"invariant_id": "inv", "status": "violated", "evidence": "<img src=x onerror=alert(1)> **bold**"}
        ],
    )
    row = ReviewFindingRow(
        func_key="k" * 16,
        qualname=hostile.qualname,
        body_hash="b" * 16,
        bundle_hash="h" * 64,
        source="anthropic",
        verdict=hostile.verdict,
        confidence=hostile.confidence,
        payload={"verdict": hostile.model_dump(mode="json")},
    )
    text = format_findings_markdown([row])
    prose = outside_code(text)
    assert "<script" not in prose and "<img" not in prose
    assert set(re.findall(r"</?([a-z]+)", prose)) == {"details", "summary"}  # only the tags we emit
    assert "`a\\|b<script>alert(1)</script>`" in text  # code from the repo stays inside a code span
    assert "&lt;img src=x onerror=alert(1)&gt;" in text
    assert "\\*\\*bold\\*\\*" in text
    table_rows = [line for line in text.splitlines() if line.startswith("| ") and "Function" not in line]
    assert len(table_rows) == 1
    assert table_rows[0].count(" | ") == 4  # pipes inside values are escaped
    assert "````python\ns = '```'" in text  # the fence is longer than any backtick run in the code
    assert "Claude (anthropic)" in text


@pytest.mark.parametrize("reused", [True, False])
def test_markdown_marks_reused_findings(reused: bool) -> None:
    row = dataclasses.replace(row_with(), reused=reused)
    text = format_findings_markdown([row])
    assert ("(reused)" in text) is reused


def test_function_verdict_is_none_for_a_corrupt_payload() -> None:
    row = SimpleNamespace(
        func_key="k",
        body_hash="b",
        bundle_hash="h",
        source="fixture",
        verdict="covered",
        confidence="low",
        payload={},
    )
    assert ReviewFindingRow.from_db(row).function_verdict is None
