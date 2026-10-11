"""Sticky comment rendering (escaping, fences, mentions, marker) and ``post_verdict``."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
from markdown_it import MarkdownIt

from coverset.forge.base import ForgeCapabilities, RepoRef
from coverset.forge.comment import (
    lens_note,
    lens_parts,
    missing_counts,
    render_check_summary,
    render_sticky_comment,
)
from coverset.forge.fake import FakeForge
from coverset.forge.markdown import ZERO_WIDTH_SPACE, code_block, code_span, md_text, safe_url
from coverset.forge.verdict import (
    CHECK_NAME,
    STICKY_MARKER,
    FailureRow,
    FunctionRow,
    InvariantRow,
    MissingClass,
    VerdictSummary,
    commit_state,
    post_verdict,
)

REPO = RepoRef(
    external_id=424242, full_name="coverset-demo/ledger", owner_id=777, installation_id=55, id=None
)
SHA = "c" * 40

FAIL = VerdictSummary(
    conclusion="failure",
    title="1 failed, 211 passed",
    reason="test_refund_partial failed: cents not conserved",
    first_failure_s=1.84,
    rank_coverset=1,
    rank_file_order=187,
    t_first_failure_file_order_est=41.2,
    patch_pct=70.6,
    patch_covered=24,
    patch_total=34,
    function_rows=(
        FunctionRow("billing.refund", "added", 1, 100.0, (), (InvariantRow("cents_conserved", "violated"),)),
        FunctionRow(
            "billing.allocate_cents", "modified", 0, 40.0, ("total_cents:negative", "weights:zero-element")
        ),
    ),
    zero_test_functions=1,
    missing_input_classes=(
        MissingClass(
            "billing.allocate_cents",
            "weights",
            "weights:zero-element",
            "@given(st.lists(st.just(0), min_size=1))",
        ),
    ),
    invariants=(InvariantRow("cents_conserved", "violated", "sum is -98, expected -100"),),
    failures=(
        FailureRow(
            "tests/test_refund.py::test_refund_partial", "assert -98 == -100", "tests/test_refund.py", 12
        ),
    ),
    details_url="http://127.0.0.1:8000/runs/run_1",
    ai_source="fixture",
    tests_total=212,
)


def html_of(markdown: str) -> str:
    return MarkdownIt("commonmark", {"html": True}).enable("table").render(markdown)


def test_layout_of_a_failing_verdict() -> None:
    text = render_sticky_comment(FAIL)
    lines = text.splitlines()
    assert lines[0] == "### coverset · FAIL · first failure in 1.8 s"
    assert "**Patch coverage 70.6%** (24 of 34 changed lines)" in text
    assert "**1 changed function has no covering test**" in text
    assert "**2 input classes never exercised in changed functions**" in text
    assert "more in invariant targets" not in text
    assert "The first failing test ran 1st of 212 (file order: 187th, ~41 s est.)." in text
    assert "| Changed function | Tests | Lines | Input classes | Invariants |" in text
    assert "<details><summary>Input classes never exercised (1)</summary>" in text and "</details>" in text
    assert "```python\n  @given(st.lists(st.just(0), min_size=1))\n  ```" in text
    assert "[Full report](http://127.0.0.1:8000/runs/run_1)" in text
    assert text.rstrip().endswith(STICKY_MARKER)
    assert text.count(STICKY_MARKER) == 1
    html = html_of(text)
    assert "<table>" in html and "<details>" in html


@pytest.mark.parametrize(
    ("summary", "label"),
    [
        (replace(FAIL, conclusion="success", failures=()), "PASS with gaps"),
        (
            VerdictSummary(
                conclusion="success", title="all passed", patch_pct=100.0, patch_covered=3, patch_total=3
            ),
            "PASS",
        ),
        (VerdictSummary(conclusion="neutral", title="budget exhausted"), "NEUTRAL"),
        (VerdictSummary(conclusion="cancelled", title="superseded"), "CANCELLED"),
        (VerdictSummary(conclusion="timed_out", title="slow"), "TIMED OUT"),
    ],
)
def test_labels(summary: VerdictSummary, label: str) -> None:
    assert summary.label == label
    assert render_sticky_comment(summary).startswith(f"### coverset · {label}")


def test_headline_splits_changed_functions_from_invariant_targets() -> None:
    """The headline counts what the PR's cards show; other functions' classes are a muted extra."""
    rows = (
        FunctionRow("validate_email", "modified", 4, 100.0, ("email:idn", "email:multi-label-domain")),
        FunctionRow("signup", "modified", 3, 100.0, ("email:idn", "email:idn")),
    )
    missing = (
        MissingClass("validate_email", "email", "email:idn"),
        MissingClass("validate_email", "email", "email:multi-label-domain"),
        MissingClass("signup", "email", "email:idn"),
        MissingClass("allocate_cents", "weights", "weights:empty"),
        MissingClass("parse_duration", "text", "text:signed"),
    )
    assert missing_counts(rows, missing) == (3, 2)
    summary = VerdictSummary(
        conclusion="success", title="ok", function_rows=rows, missing_input_classes=missing
    )
    text = render_sticky_comment(summary)
    assert "**3 input classes never exercised in changed functions** · 2 more in invariant targets" in text
    assert "<details><summary>Input classes never exercised (5)</summary>" in text
    only_targets = replace(summary, function_rows=())
    assert "5 input classes never exercised in invariant targets" in render_sticky_comment(only_targets)


def test_invariant_lens_note_drops_its_key_prefix() -> None:
    note = "cents_conserved: 6 tests, never reached weights:empty, total_cents:negative"
    assert lens_note(note, "cents_conserved") == (
        "linked tests: 6 · never reached: weights:empty, total_cents:negative"
    )
    parts = lens_parts(
        "sig: no linked tests, never reached email:idn; changed lines not covered by linked tests", "sig"
    )
    assert parts.tests == 0 and parts.unreached == ("email:idn",) and parts.changed_uncovered
    assert lens_note("m: 1 test, reached all 4 input classes", "m") == (
        "linked tests: 1 · reached all 4 input classes"
    )
    assert lens_note("anything else", "k") == "anything else"
    summary = VerdictSummary(
        conclusion="success", title="ok", invariants=(InvariantRow("cents_conserved", "satisfied", note),)
    )
    text = render_sticky_comment(summary)
    assert "cents\\_conserved: 6 tests" not in text and "cents_conserved: 6 tests" not in text
    assert "linked tests: 6 · never reached: weights:empty" in text.replace("\\", "")


def test_untested_invariant_is_a_gap() -> None:
    summary = VerdictSummary(
        conclusion="success",
        title="ok",
        patch_pct=100.0,
        patch_total=1,
        patch_covered=1,
        invariants=(InvariantRow("signup_rejects_existing_domain", "untested"),),
    )
    assert summary.label == "PASS with gaps"


HOSTILE = "<script>alert(1)</script> **bold** [link](javascript:alert(1)) ![img](http://x/i.png) @octocat | pipe & <!-- coverset:sticky -->"


def test_hostile_text_renders_literally() -> None:
    summary = VerdictSummary(
        conclusion="failure",
        title=HOSTILE,
        reason=HOSTILE,
        failures=(FailureRow(f"tests/test_x.py::test[{HOSTILE}]", HOSTILE, "tests/test_x.py", 3),),
        function_rows=(
            FunctionRow(HOSTILE, "modified", 2, 50.0, (HOSTILE,), (InvariantRow(HOSTILE, HOSTILE),)),
        ),
        invariants=(InvariantRow(HOSTILE, "violated", HOSTILE),),
        invariants_changed=(InvariantRow(HOSTILE, "removed", HOSTILE),),
        missing_input_classes=(MissingClass(HOSTILE, HOSTILE, HOSTILE, f"x = 1\n```\n{HOSTILE}\n````"),),
    )
    text = render_sticky_comment(summary)
    assert text.count(STICKY_MARKER) == 1 and text.rstrip().endswith(STICKY_MARKER)
    without_blocks = re.sub(r"(?ms)^\s*(`{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    outside_code = re.sub(r"(`+).*?\1", "", without_blocks)
    assert "@octocat" not in outside_code and f"@{ZERO_WIDTH_SPACE}octocat" in outside_code
    html = html_of(text)
    assert "<script" not in html
    assert "<img" not in html
    assert 'href="javascript' not in html
    assert html.count("<!-- coverset:sticky -->") == 1
    assert "<strong>bold</strong>" not in html
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
    header_cells = rows[0].count(" | ")
    assert all(_cells(row) == _cells(rows[0]) for row in rows), "a pipe escaped a table cell"
    assert header_cells == 4


def _cells(row: str) -> int:
    return len(re.findall(r"(?<!\\)\|", row))


def test_check_summary_has_no_marker_or_heading() -> None:
    summary = render_check_summary(FAIL)
    assert STICKY_MARKER not in summary and not summary.startswith("###")
    assert "Patch coverage" in summary


@pytest.mark.security
def test_md_text_escapes_and_neutralises() -> None:
    assert md_text("a*b_c`d") == "a\\*b\\_c\\`d"
    assert md_text("<b>&amp;") == "\\<b\\>&amp;amp;"
    assert md_text("@team/devs") == f"@{ZERO_WIDTH_SPACE}team/devs"
    assert md_text("line\nbreak\x00") == "line break"
    assert md_text("x" * 500, limit=10) == "x" * 9 + "…"


@pytest.mark.security
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "`plain`"),
        ("a`b", "``a`b``"),
        ("``x``", "``` ``x`` ```"),
        ("`", "`` ` ``"),
        ("", "` `"),
    ],
)
def test_code_span_fences(text: str, expected: str) -> None:
    assert code_span(text) == expected
    rendered = MarkdownIt().render(code_span(text)) if text else ""
    if text:
        assert f"<code>{text}</code>" in rendered.replace("&lt;", "<")


def test_code_span_in_table_escapes_pipes_and_breaks_markers() -> None:
    assert code_span("a|b", in_table=True) == "`a\\|b`"
    assert "<!--" not in code_span("x <!-- coverset:sticky -->")


def test_code_block_fence_outgrows_the_content() -> None:
    block = code_block("print(1)\n````\nmore", "python")
    fence = block.splitlines()[0]
    assert fence == "`````python" and block.endswith("\n`````")
    assert code_block("x", "py thon").startswith("```\n")


def test_safe_url() -> None:
    assert safe_url("https://coverset.example/runs/1") == "https://coverset.example/runs/1"
    for bad in ("javascript:alert(1)", "http://x/a b", "http://x/)", "", None, "ftp://x"):
        assert safe_url(bad) is None


def test_commit_state_mapping() -> None:
    """Only success is green: a neutral (errored, no tests, budget) run must not satisfy a required
    status in token mode."""
    assert [commit_state(c) for c in ("success", "neutral", "failure", "cancelled", "timed_out")] == [
        "success",
        "error",
        "failure",
        "error",
        "error",
    ]


async def test_post_verdict_creates_check_and_comment_then_updates_in_place() -> None:
    forge = FakeForge()
    posted = await post_verdict(
        forge, REPO, 1, SHA, FAIL, check_id=None, comment_id=None, external_id="run_1"
    )
    assert posted.check_id is not None and posted.comment_id is not None and not posted.status_posted
    assert [call.method for call in forge.calls] == ["create_check", "update_check", "upsert_pr_comment"]
    check = forge.checks[posted.check_id]
    assert (check.name, check.status, check.conclusion, check.external_id) == (
        CHECK_NAME,
        "completed",
        "failure",
        "run_1",
    )
    assert [a["identifier"] for a in check.actions] == ["rerun-failed", "rerun-all"]
    assert check.annotations[0]["path"] == "tests/test_refund.py" and check.annotations[0]["start_line"] == 12
    assert check.output is not None and check.output["title"] == "1 failed, 211 passed"

    again = await post_verdict(
        forge,
        REPO,
        1,
        SHA,
        replace(FAIL, conclusion="success", failures=()),
        check_id=posted.check_id,
        comment_id=posted.comment_id,
    )
    assert (again.check_id, again.comment_id) == (posted.check_id, posted.comment_id)
    assert len(forge.comments) == 1
    assert forge.checks[posted.check_id].conclusion == "success"


async def test_post_verdict_ignores_comments_it_did_not_write() -> None:
    forge = FakeForge()
    foreign = forge.add_foreign_comment(REPO, 1, f"looks like coverset {STICKY_MARKER}")
    posted = await post_verdict(forge, REPO, 1, SHA, FAIL, check_id=None, comment_id=foreign)
    assert posted.comment_id != foreign
    assert forge.comments[foreign].body == f"looks like coverset {STICKY_MARKER}"


class StatusOnlyForge(FakeForge):
    """Token-mode capabilities: statuses and comments, no check runs."""

    def capabilities(self) -> ForgeCapabilities:
        return replace(super().capabilities(), name="token", check_runs=False, check_actions=False)


async def test_post_verdict_in_token_mode_posts_a_status() -> None:
    forge = StatusOnlyForge()
    posted = await post_verdict(forge, REPO, None, SHA, FAIL, check_id=None, comment_id=None)
    assert posted.status_posted and posted.check_id is None and posted.comment_id is None
    (status,) = forge.statuses
    assert (status["state"], status["context"], status["target_url"]) == (
        "failure",
        "coverset",
        FAIL.details_url,
    )
    assert status["description"].startswith("FAIL: ")


async def test_post_verdict_keeps_going_when_one_step_fails() -> None:
    forge = FakeForge(fail_on={"update_check"})
    posted = await post_verdict(forge, REPO, 1, SHA, FAIL, check_id=None, comment_id=None)
    assert posted.comment_id is not None
    assert posted.errors and posted.errors[0].startswith("check:")


@pytest.mark.security
def test_issue_and_gh_references_are_neutralised() -> None:
    """GitHub links references after rendering, so escaping ``#`` is not enough."""
    text = md_text("fixes #12, see torvalds/linux#1 and GH-5 and gh-6")
    assert "#12" not in text and "#1 " not in text.replace("\\", "")
    assert f"#{ZERO_WIDTH_SPACE}12" in text and f"linux\\#{ZERO_WIDTH_SPACE}1" in text
    assert f"GH{ZERO_WIDTH_SPACE}\\-5" in text and f"gh{ZERO_WIDTH_SPACE}\\-6" in text
    html = MarkdownIt().render(text)
    assert re.search(r"#\d", html) is None and re.search(r"(?i)gh-\d", html) is None


@pytest.mark.security
@pytest.mark.parametrize("char", ["‮", "‪", "⁦", "⁩", "‎", "‏", "؜", "﻿"])
def test_bidi_and_format_characters_are_stripped_everywhere(char: str) -> None:
    hostile = f"safe{char}evil"
    assert char not in md_text(hostile)
    assert char not in code_span(hostile)
    assert char not in code_block(hostile)
    summary = VerdictSummary(
        conclusion="failure",
        title=hostile,
        reason=hostile,
        failures=(FailureRow(f"t.py::{hostile}", hostile),),
        missing_input_classes=(MissingClass(hostile, hostile, hostile, hostile),),
    )
    assert char not in render_sticky_comment(summary)


def _huge_summary(rows: int, snippets: int, failures: int = 0) -> VerdictSummary:
    long = "q" * 290
    return VerdictSummary(
        conclusion="failure",
        title="big",
        function_rows=tuple(
            FunctionRow(f"{long}{i}", "modified", 1, 50.0, tuple(f"{long}{j}" for j in range(3)))
            for i in range(rows)
        ),
        failures=tuple(FailureRow(f"t.py::{long}{i}", long) for i in range(failures)),
        missing_input_classes=tuple(
            MissingClass(f"f{i}", "p", f"class{i}", "@given(st.text())\n" + "x = 1\n" * 300)
            for i in range(snippets)
        ),
        details_url="http://127.0.0.1:8000/runs/run_1",
    )


@pytest.mark.security
@pytest.mark.parametrize(
    ("rows", "snippets", "failures"), [(16, 30, 0), (40, 30, 10), (10, 30, 10), (40, 0, 10)]
)
def test_long_comments_stay_well_formed_within_the_limit(rows: int, snippets: int, failures: int) -> None:
    text = render_sticky_comment(_huge_summary(rows, snippets, failures))
    assert len(text) <= 60_000 + len(STICKY_MARKER) + 4
    assert text.count("<details>") == text.count("</details>")
    fences = [line for line in text.splitlines() if line.strip().startswith("```")]
    assert len(fences) % 2 == 0, "every code fence is closed"
    assert text.rstrip().endswith(STICKY_MARKER) and "[Full report]" in text
    html = MarkdownIt("commonmark", {"html": True}).enable("table").render(text)
    assert html.count("<!-- coverset:sticky -->") == 1
    if snippets:
        assert "Input classes never exercised (30)" in text
        assert re.search(r"… and \d+ more input classes in the full report", text), "says what was cut"
    if failures:
        assert "**10 failures**" in text and text.count("- `t.py::") == 10, "failures are kept first"


def test_a_section_that_cannot_fit_is_named() -> None:
    summary = _huge_summary(40, 30, 10)
    from coverset.forge import comment

    original = comment.COMMENT_LIMIT
    try:
        comment.COMMENT_LIMIT = 3_800  # small enough that the input-class section cannot fit
        text = render_sticky_comment(summary)
    finally:
        comment.COMMENT_LIMIT = original
    assert len(text) <= 3_800 + len(STICKY_MARKER) + 4
    assert "Not shown to fit GitHub's size limit" in text and "input classes" in text
    assert text.count("<details>") == text.count("</details>")
