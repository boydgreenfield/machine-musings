"""Changed-function cards for the PR page and the function detail they expand into.

Card severity orders "Needs attention": 4 = an invariant on the function is violated or one of its
covering tests failed, 3 = no test calls it, 2 = some body lines never ran, 1 = some expected input
class was never passed in, 0 = nothing to report. The detail (shared with the function page) is the
diff hunk through the coverage formatter, the shape grid, the flow ribbon, the top covering tests
and a suggested Hypothesis test.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from quart import url_for

from coverset.ai.review import SOURCE_LABELS, VERDICT_LABELS, ReviewFindingRow
from coverset.db.models import Repo, ReviewFinding, Run
from coverset.lang.base import FunctionChange, FunctionRegion
from coverset.web.facts_view import FunctionView, RunFacts, test_file
from coverset.web.loaders import read_source
from coverset.web.pr_view import DiffAnalysis
from coverset.web.render.ribbon import render_ribbon
from coverset.web.render.shapegrid import render_grid, render_strip
from coverset.web.render.source import render_source

__all__ = ["Card", "build_cards", "card_detail", "function_detail", "hypothesis_stub"]

TOP_TESTS = 5
GRID_W = 580.0
BADGES = {"added": "NEW", "modified": "CHANGED", "trivial": "TRIVIAL", "renamed": "RENAMED", "moved": "MOVED"}


@dataclass(slots=True)
class Card:
    """One changed function on the PR page."""

    change: FunctionChange
    region: FunctionRegion
    tests: list[str]
    line_pct: float | None
    inherited: bool
    view: FunctionView
    invariants: list[dict[str, Any]] = field(default_factory=list)
    finding: ReviewFinding | None = None
    failing: list[str] = field(default_factory=list)
    severity: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def badge(self) -> str:
        """NEW, CHANGED, TRIVIAL, RENAMED or MOVED."""
        return BADGES.get(self.change.kind, self.change.kind.upper())

    @property
    def strip(self) -> Any:
        """The mini shape strip."""
        return render_strip(self.view.cells())

    @property
    def ai_label(self) -> str | None:
        """``AI: <verdict> (fixture)``."""
        if self.finding is None:
            return None
        source = SOURCE_LABELS.get(self.finding.source, self.finding.source)
        verdict = VERDICT_LABELS.get(self.finding.verdict, self.finding.verdict)
        return f"AI ({source}): {verdict}"


def build_cards(
    facts: RunFacts,
    analysis: DiffAnalysis,
    results: list[dict[str, Any]],
    invariants: list[dict[str, Any]],
    findings: dict[str, ReviewFinding],
) -> list[Card]:
    """Cards for every changed function, most severe first."""
    if analysis.patch is None:
        return []
    ranks = {r["nodeid"]: r["rank"] or 10**9 for r in results}
    outcomes = {r["nodeid"]: r["outcome"] for r in results}
    cards: list[Card] = []
    for row in analysis.patch.function_rows:
        region = row.change.region
        if region is None:
            continue
        view = facts.function_view(region, analysis.head_sources.get(region.path))
        tests = sorted(row.tests, key=lambda n: (ranks.get(n, 10**9), n))
        failing = [t for t in tests if outcomes.get(t) in ("failed", "error")]
        targeted = [inv for inv in invariants if inv["target_func_key"] == region.func_key]
        card = Card(
            change=row.change,
            region=region,
            tests=tests,
            line_pct=row.line_pct,
            inherited=row.inherited,
            view=view,
            invariants=targeted,
            finding=findings.get(region.func_key),
            failing=failing,
        )
        _grade(card)
        cards.append(card)
    cards.sort(
        key=lambda c: (
            -c.severity,
            -sum(1 for inv in c.invariants if inv["status"] != "satisfied"),
            c.inherited,
            -c.view.hollow,
            c.region.path,
            c.region.def_line,
        )
    )
    return cards


def _grade(card: Card) -> None:
    reasons = []
    violated = [inv["key"] for inv in card.invariants if inv["status"] == "violated"]
    if violated:
        reasons.append("violates " + ", ".join(violated))
    if card.failing:
        reasons.append(f"{len(card.failing)} covering test{'s' if len(card.failing) != 1 else ''} failed")
    severity = 4 if reasons else 0
    if not card.tests and not card.inherited:
        reasons.append("no test calls it")
        severity = severity or 3
    if card.line_pct is not None and card.line_pct < 100:
        reasons.append(f"lines {card.line_pct:.0f}%")
        severity = severity or 2
    hollow = card.view.hollow if card.view.observations else 0
    if hollow:
        reasons.append(f"{hollow} input class{'es' if hollow != 1 else ''} never exercised")
        severity = severity or 1
    card.severity = severity
    card.reasons = reasons


def _module(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def hypothesis_stub(view: FunctionView) -> str | None:
    """A runnable-looking Hypothesis test for the first missing class of each parameter."""
    region = view.region
    missing: dict[str, Any] = {}
    for cls in view.cov.missing:
        missing.setdefault(cls.param, cls)
    if not missing:
        return None
    name = region.qualname.split(".")[-1]
    importable = "<locals>" not in region.qualname and "." not in region.qualname
    target = name if importable else region.qualname
    lines = ["from hypothesis import given, strategies as st", ""]
    if importable:
        lines.insert(0, f"from {_module(region.path)} import {name}")
    args = ", ".join(f"{p}={c.hypothesis_hint}" for p, c in missing.items())
    slug = re.sub(
        r"[^a-z0-9]+", "_", "_".join(c.name.split(":", 1)[1] for c in missing.values()).lower()
    ).strip("_")
    params = ", ".join(missing)
    lines += [
        "",
        f"@given({args})",
        f"def test_{name}_{slug}({params}):",
        f"    result = {target}({', '.join(f'{p}={p}' for p in missing)})",
        "    # assert what must hold for these inputs, e.g. an invariant of the result",
    ]
    return "\n".join(lines) + "\n"


def _finding_view(finding: ReviewFinding | None) -> dict[str, Any] | None:
    if finding is None:
        return None
    row = ReviewFindingRow.from_db(finding)
    verdict = row.function_verdict
    return {
        "source": SOURCE_LABELS.get(row.source, row.source),
        "verdict": VERDICT_LABELS.get(row.verdict, row.verdict),
        "confidence": row.confidence,
        "missing": [
            (m.name, m.description, m.example_value)
            for m in (verdict.missing_input_classes if verdict else [])
        ],
        "suggested": [(t.name, t.code) for t in (verdict.suggested_tests if verdict else [])],
        "invariants": [
            (f.invariant_id, f.status, f.evidence) for f in (verdict.invariant_findings if verdict else [])
        ],
    }


async def function_detail(
    repo: Repo,
    run: Run,
    facts: RunFacts,
    region: FunctionRegion,
    *,
    view: FunctionView | None = None,
    analysis: DiffAnalysis | None = None,
    results: list[dict[str, Any]] | None = None,
    finding: ReviewFinding | None = None,
    tests: list[str] | None = None,
) -> dict[str, Any]:
    """Template context of the function detail block (grid, ribbon, hunk, tests, suggestion)."""
    source = (analysis.head_sources.get(region.path) if analysis else None) or await read_source(
        run, region.path, facts
    )
    if view is None:
        view = await asyncio.to_thread(facts.function_view, region, source)
    outcomes = {r["nodeid"]: r for r in results or []}
    if tests is None:
        ranks = {r["nodeid"]: r["rank"] or 10**9 for r in results or []}
        covering = await asyncio.to_thread(
            facts.tests_in_range, region.path, region.body_start, region.end_line
        )
        tests = sorted(
            covering,
            key=lambda n: (ranks.get(n, 10**9), n),
        )
    changed = analysis.changed_lines.get(region.path, set()) if analysis else set()
    removed = analysis.removed.get(region.path, {}) if analysis else {}
    hunk = None
    line_facts = await asyncio.to_thread(facts.line_facts, region.path)
    if source is not None:

        def tests_url(line: int) -> str:
            return url_for("files.line_tests", rid=repo.id, path=region.path, n=line, run=run.id)

        hunk = render_source(
            source,
            line_facts,
            path=region.path,
            line_range=(region.def_line, region.end_line),
            changed=changed,
            removed={k: v for k, v in removed.items() if region.def_line <= k <= region.end_line + 1},
            tests_url=tests_url,
            anchor_prefix=f"F{region.func_key[:8]}-",
        )
    target = f"sg-{region.func_key}"
    grid = render_grid(
        view.grid(),
        cell_url=lambda cell: url_for(
            "files.shapes", rid=repo.id, func_key=region.func_key, run=run.id, cls=cell.name
        ),
        target=target,
        width=GRID_W,
    )
    finding_view = _finding_view(finding)
    suggestion = None
    if finding_view and finding_view["suggested"]:
        suggestion = finding_view["suggested"][0][1]
    suggestion = suggestion or hypothesis_stub(view)
    return {
        "region": region,
        "view": view,
        "hunk": hunk,
        "grid": grid,
        "ribbon": render_ribbon(view.flows(test_file), width=GRID_W),
        "top_tests": [(t, outcomes.get(t, {}).get("outcome")) for t in tests[:TOP_TESTS]],
        "test_total": len(tests),
        "suggestion": suggestion,
        "suggestion_source": "AI review (advisory)"
        if finding_view and finding_view["suggested"]
        else "generated from the missing input classes",
        "finding": finding_view,
        "source_url": url_for("files.source", rid=repo.id, path=region.path, run=run.id)
        + f"#L{region.def_line}",
        "function_url": url_for("files.function", rid=repo.id, func_key=region.func_key, run=run.id),
    }


async def card_detail(
    repo: Repo,
    run: Run,
    facts: RunFacts,
    card: Card,
    analysis: DiffAnalysis | None,
    results: list[dict[str, Any]],
    *,
    pr: Any = None,
) -> dict[str, Any]:
    """The detail block of a PR card."""
    return await function_detail(
        repo,
        run,
        facts,
        card.region,
        view=card.view,
        analysis=analysis,
        results=results,
        finding=card.finding,
        tests=card.tests,
    )
