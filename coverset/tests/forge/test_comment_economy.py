"""The PR test tax in the sticky comment and the check-run summary (docs/economy.md).

One line, built from numbers only, rendered only for a PR run whose verdict carries a tax (a PR
run with a base run); it survives the summary's JSON round trip, so a re-posted comment keeps it.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

from coverset.config import Settings
from coverset.forge.comment import (
    EconomySummary,
    TaxTest,
    TestTaxLine,
    render_check_summary,
    render_sticky_comment,
    test_tax_line,
    with_test_tax,
)
from coverset.forge.verdict import VerdictSummary
from coverset.orchestrator.verdict import RunReport, build_summary, summary_from_json, summary_to_json

HOSTILE = "tests/test_x.py::test_a[<img src=x onerror=alert(1)> @team #12]"

BASE = VerdictSummary(conclusion="success", title="12 tests passed", reason="all 12 tests passed")

BLOAT = TestTaxLine(
    added_statements=95,
    removed_statements=0,
    newly_covered_lines=3,
    new_classes=0,
    tax=31.667,
    threshold=8.0,
    flagged=True,
    new_tests=5,
    lowest_leverage=(TaxTest(HOSTILE, 40, 0, 0.0),),
    duplicates=(
        TaxTest(HOSTILE, 40, 0, 0.0, "tests/test_env.py::test_flag", "same flow"),
        TaxTest("tests/test_env.py::test_b", 30, 0, 0.0, "tests/test_env.py::test_flag", "subset"),
    ),
    duplicate_count=2,
)
LINE = "Test tax: 95 test statements for 3 newly covered lines (threshold 8)"


def test_the_horror_story_line_is_exact_and_bold_when_flagged() -> None:
    line = test_tax_line(BLOAT)
    assert line == f"**{LINE}** · 2 new tests duplicate existing flows"
    calm = dataclasses.replace(BLOAT, flagged=False, duplicate_count=0, duplicates=())
    assert test_tax_line(calm) == LINE


def test_classes_one_duplicate_and_an_inexact_base_read_naturally() -> None:
    tax = dataclasses.replace(
        BLOAT,
        added_statements=1,
        newly_covered_lines=1,
        new_classes=1,
        flagged=False,
        duplicate_count=1,
        base_exact=False,
    )
    assert test_tax_line(tax) == (
        "Test tax: 1 test statement for 1 newly covered line and 1 input class (threshold 8)"
        " · 1 new test duplicates existing flows · compared with an earlier run, not the base commit"
    )


def test_nothing_to_say_is_no_line() -> None:
    quiet = dataclasses.replace(BLOAT, added_statements=0, duplicate_count=0, duplicates=(), flagged=False)
    assert test_tax_line(quiet) is None
    assert test_tax_line(None) is None
    unmeasured = dataclasses.replace(quiet, statements_measured=False)
    assert test_tax_line(unmeasured) == "Test tax: test statements not measured, 3 newly covered lines"


def test_the_comment_and_the_check_summary_carry_the_line() -> None:
    summary = with_test_tax(BASE, BLOAT)
    assert isinstance(summary, EconomySummary) and summary.label == "PASS"
    comment = render_sticky_comment(summary)
    check = render_check_summary(summary)
    for body in (comment, check):
        assert f"**{LINE}** · 2 new tests duplicate existing flows" in body
        # the line is numbers only: no test id from the PR reaches the comment through it
        assert "onerror" not in body and "test_flag" not in body and "@team" not in body
    assert comment.index("all 12 tests passed") < comment.index("Test tax:")


def test_runs_without_a_tax_render_exactly_as_before() -> None:
    assert with_test_tax(BASE, None) is BASE
    assert "Test tax" not in render_sticky_comment(BASE)
    assert "Test tax" not in render_check_summary(BASE)
    assert "test_tax" not in summary_to_json(BASE)


def test_the_tax_survives_the_stored_json() -> None:
    data = summary_to_json(with_test_tax(BASE, BLOAT))
    assert data["test_tax"]["duplicates"][0]["how"] == "same flow"
    again = summary_from_json(data)
    assert isinstance(again, EconomySummary)
    assert again.test_tax == BLOAT
    assert render_sticky_comment(again) == render_sticky_comment(with_test_tax(BASE, BLOAT))
    assert TestTaxLine.from_json({"added_statements": "x"}) is None
    assert TestTaxLine.from_json(["not", "a", "dict"]) is None
    assert summary_from_json({**data, "test_tax": {"bogus": 1}}).__class__ is VerdictSummary


def test_build_summary_passes_the_report_tax_through() -> None:
    run = SimpleNamespace(
        timeline={},
        rank_coverset=None,
        rank_file_order=None,
        t_first_failure_file_order_est=None,
        id="r",
        created_at=datetime.now(UTC),
    )
    settings = Settings.for_tests()
    taxed = build_summary(
        settings,
        run,
        None,
        {},
        conclusion="success",
        reason="ok",
        report=RunReport(test_tax=BLOAT),  # type: ignore[arg-type]
    )
    assert getattr(taxed, "test_tax", None) == BLOAT
    assert LINE in render_sticky_comment(taxed)
    plain = build_summary(settings, run, None, {}, conclusion="success", reason="ok", report=RunReport())  # type: ignore[arg-type]
    assert type(plain) is VerdictSummary and "Test tax" not in render_sticky_comment(plain)
    early = build_summary(settings, run, None, {}, conclusion="failure", reason="first failure")  # type: ignore[arg-type]
    assert "Test tax" not in render_sticky_comment(early)
