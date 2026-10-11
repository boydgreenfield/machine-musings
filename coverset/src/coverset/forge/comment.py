"""The sticky PR comment and the check-run summary, rendered from a :class:`VerdictSummary`.

Layout (after Codecov's): a heading with the verdict, the reason, one line of headline numbers,
the "first failing test ran Nth" line, a changed-function table, failures, invariants, the
never-exercised input classes in a ``<details>`` block with one Hypothesis snippet each, and the
hidden marker. Every interpolated string is escaped (:mod:`coverset.forge.markdown`); the only
raw HTML is the fixed ``<details>``/``<summary>`` skeleton and the marker.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from coverset.forge.markdown import code_block, code_span, md_text, safe_url
from coverset.forge.verdict import (
    ESTIMATE_LABEL,
    STICKY_MARKER,
    FunctionRow,
    InvariantRow,
    MissingClass,
    VerdictSummary,
)
from coverset.units import fmt_secs

COMMENT_LIMIT = 60_000
"""GitHub rejects comment bodies over 65,536 characters; stay well below it."""
CUT_NOTE_RESERVE = 300
"""Room kept for the note naming sections that did not fit at all."""

MAX_FUNCTION_ROWS = 40
MAX_FAILURES = 10
MAX_MISSING_CLASSES = 30
MAX_INVARIANTS = 30
MAX_CLASSES_PER_CELL = 3
MAX_SNIPPET_CHARS = 1_200


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}%" if value == int(value) else f"{value:.1f}%"


def _heading(summary: VerdictSummary) -> str:
    parts = ["### coverset", summary.label]
    if summary.first_failure_s is not None:
        parts.append(f"first failure in {fmt_secs(summary.first_failure_s)}")
    return " · ".join(parts)


def missing_counts(function_rows: Iterable[FunctionRow], missing: Iterable[MissingClass]) -> tuple[int, int]:
    """``(in changed functions, more in invariant targets)``: the never-exercised input classes.

    The first number counts distinct ``param:class`` names per changed function (the PR cards show
    the same classes); the second counts the summary's never-exercised classes of other functions
    (the targets of invariants the PR did not touch), which the full ``<details>`` list includes.
    """
    rows = list(function_rows)
    changed = {(row.qualname, name) for row in rows for name in row.input_classes_missing}
    names = {row.qualname for row in rows}
    elsewhere = {(m.function, m.class_name) for m in missing if m.function not in names}
    return len(changed), len(elsewhere)


def _headline(summary: VerdictSummary) -> str:
    parts = []
    if summary.patch_total:
        parts.append(
            f"**Patch coverage {_pct(summary.patch_pct)}** "
            f"({summary.patch_covered} of {_plural(summary.patch_total, 'changed line', 'changed lines')})"
        )
    if summary.zero_test_functions:
        verb = "has" if summary.zero_test_functions == 1 else "have"
        noun = _plural(summary.zero_test_functions, "changed function", "changed functions")
        parts.append(f"**{noun} {verb} no covering test**")
    changed, elsewhere = missing_counts(summary.function_rows, summary.missing_input_classes)
    if changed:
        noun = _plural(changed, "input class", "input classes")
        parts.append(f"**{noun} never exercised in changed functions**")
    if elsewhere and changed:
        parts.append(f"{elsewhere} more in invariant targets")
    elif elsewhere:
        parts.append(
            f"{_plural(elsewhere, 'input class', 'input classes')} never exercised in invariant targets"
        )
    return " · ".join(parts)


_LENS = re.compile(
    r"^(?:(?P<n>\d+) tests?|no linked tests)"
    r"(?:, never reached (?P<unreached>[^;]+)|, reached all (?P<all>\d+) input classes)?"
    r"(?P<changed>; changed lines not covered by linked tests)?$"
)


@dataclass(frozen=True, slots=True)
class LensParts:
    """An invariant's coverage lens, parsed from its one-line summary."""

    tests: int | None
    """Linked tests (None when the summary did not parse)."""
    unreached: tuple[str, ...] = ()
    reached_all: int | None = None
    changed_uncovered: bool = False
    text: str = ""
    """The summary without its ``<key>: `` prefix."""


def lens_parts(note: str | None, key: str) -> LensParts:
    """Split a lens summary (``<key>: 6 tests, never reached a:b, c:d; changed lines …``)."""
    text = (note or "").strip()
    if text.startswith(f"{key}: "):
        text = text[len(key) + 2 :]
    found = _LENS.match(text)
    if found is None:
        return LensParts(None, text=text)
    unreached = tuple(c.strip() for c in (found["unreached"] or "").split(",") if c.strip())
    return LensParts(
        tests=int(found["n"]) if found["n"] else 0,
        unreached=unreached,
        reached_all=int(found["all"]) if found["all"] else None,
        changed_uncovered=bool(found["changed"]),
        text=text,
    )


def lens_note(note: str | None, key: str) -> str:
    """The lens as one plain line: ``linked tests: 6 · never reached: a:b, c:d``."""
    parts = lens_parts(note, key)
    if parts.tests is None:
        return parts.text
    line = f"linked tests: {parts.tests}"
    if parts.unreached:
        line += " · never reached: " + ", ".join(parts.unreached)
    elif parts.reached_all is not None:
        line += f" · reached all {parts.reached_all} input classes"
    if parts.changed_uncovered:
        line += " · changed lines not covered by linked tests"
    return line


def _rank_line(summary: VerdictSummary) -> str | None:
    if summary.rank_coverset is None:
        return None
    line = f"The first failing test ran {_ordinal(summary.rank_coverset)}"
    if summary.tests_total:
        line += f" of {summary.tests_total}"
    if summary.rank_file_order is not None:
        estimate = ""
        if summary.t_first_failure_file_order_est is not None:
            estimate = f", ~{fmt_secs(summary.t_first_failure_file_order_est)} {ESTIMATE_LABEL}"
        line += f" (file order: {_ordinal(summary.rank_file_order)}{estimate})"
    return line + "."


def _invariant_cell(rows: tuple[InvariantRow, ...]) -> str:
    if not rows:
        return "–"
    return ", ".join(
        f"{code_span(row.id, in_table=True)} {md_text(row.status, limit=20)}" for row in rows[:4]
    )


def _classes_cell(row: FunctionRow) -> str:
    if not row.input_classes_missing:
        return "all observed"
    shown = ", ".join(
        code_span(name, in_table=True) for name in row.input_classes_missing[:MAX_CLASSES_PER_CELL]
    )
    more = len(row.input_classes_missing) - MAX_CLASSES_PER_CELL
    suffix = f" +{more}" if more > 0 else ""
    return f"{len(row.input_classes_missing)} missing: {shown}{suffix}"


@dataclass(frozen=True, slots=True)
class Section:
    """A budgeted block: header lines, whole items (each a list of lines), footer lines, and the
    "… and N more" line for items beyond the row cap (``hidden``) or the character budget."""

    header: list[str]
    items: list[list[str]]
    more: Callable[[int], str]
    hidden: int = 0
    footer: list[str] = field(default_factory=list)


def _more(template: str) -> Callable[[int], str]:
    return template.format


def _size(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _fit(section: Section, budget: int) -> list[str] | None:
    """The section with as many whole items as fit in ``budget`` characters (None if even its
    header and footer do not). Items are never split, so fences and ``<details>`` always close."""
    header, items, footer, more = section.header, section.items, section.footer, section.more
    used = _size(header) + _size(footer)
    reserve = len(more(len(items) + section.hidden)) + 1 if items or section.hidden else 0
    if used + reserve > budget:
        return None
    chosen: list[str] = []
    taken = 0
    for index, item in enumerate(items):
        cost = _size(item)
        rest_reserve = reserve if index < len(items) - 1 or section.hidden else 0
        if used + cost + rest_reserve > budget:
            break
        chosen.extend(item)
        used += cost
        taken += 1
    missing = len(items) - taken + section.hidden
    return [*header, *chosen, *([more(missing)] if missing else []), *footer]


def _function_table(rows: tuple[FunctionRow, ...]) -> Section:
    header = [
        "| Changed function | Tests | Lines | Input classes | Invariants |",
        "|---|---:|---:|---|---|",
    ]
    items = []
    for row in rows[:MAX_FUNCTION_ROWS]:
        badge = md_text(row.change_kind, limit=12) + (" · inherited" if row.inherited else "")
        tests = "**0**" if row.tests == 0 else str(row.tests)
        items.append(
            [
                f"| {code_span(row.qualname, in_table=True)} ({badge}) | {tests} | {_pct(row.line_pct)} "
                f"| {_classes_cell(row)} | {_invariant_cell(row.invariants)} |"
            ]
        )
    return Section(
        header,
        items,
        _more("| … and {} more changed functions in the full report | | | | |"),
        len(rows) - len(items),
    )


def _failures(summary: VerdictSummary) -> Section:
    header = [f"**{_plural(len(summary.failures), 'failure', 'failures')}**", ""]
    items = []
    for row in summary.failures[:MAX_FAILURES]:
        where = f" ({code_span(f'{row.path}:{row.line}')})" if row.path and row.line else ""
        message = f": {md_text(row.message, limit=200)}" if row.message else ""
        items.append([f"- {code_span(row.nodeid)}{message}{where}"])
    return Section(
        header,
        items,
        _more("- … and {} more failures in the full report"),
        len(summary.failures) - len(items),
    )


def _invariants(title: str, rows: tuple[InvariantRow, ...]) -> Section:
    items = []
    for row in rows[:MAX_INVARIANTS]:
        text = lens_note(row.note, row.id) if row.note else ""
        note = f": {md_text(text, limit=200)}" if text else ""
        items.append([f"- {code_span(row.id)} {md_text(row.status, limit=20)}{note}"])
    return Section(
        [f"**{title}**", ""],
        items,
        _more("- … and {} more invariants in the full report"),
        len(rows) - len(items),
    )


def _missing_classes(rows: tuple[MissingClass, ...]) -> Section:
    header = [f"<details><summary>Input classes never exercised ({len(rows)})</summary>", ""]
    items = []
    for row in rows[:MAX_MISSING_CLASSES]:
        item = [f"- {code_span(row.function)} · {code_span(row.param)} · {md_text(row.class_name, limit=80)}"]
        if row.hypothesis_snippet:
            snippet = code_block(row.hypothesis_snippet[:MAX_SNIPPET_CHARS], "python")
            item.extend(["", *("  " + line for line in snippet.splitlines()), ""])
        items.append(item)
    more = _more("- … and {} more input classes in the full report")
    return Section(header, items, more, len(rows) - len(items), ["", "</details>"])


def _footer(summary: VerdictSummary) -> str | None:
    parts = []
    url = safe_url(summary.details_url)
    if url:
        parts.append(f"[Full report]({url})")
    if summary.ai_source:
        parts.append(f"AI review: {md_text(summary.ai_source, limit=40)}")
    return " · ".join(parts) or None


def _render(summary: VerdictSummary, *, heading: bool) -> str:
    """Lay the comment out against :data:`COMMENT_LIMIT`: the heading, intro and footer always
    appear; sections get the remaining budget in priority order (failures, functions, invariants,
    input classes), drop whole rows that do not fit and say how many more the full report has."""
    head: list[list[str]] = []
    if heading:
        head.append([_heading(summary)])
    intro = [line for line in (md_text(summary.reason) if summary.reason else "", _headline(summary)) if line]
    if (rank := _rank_line(summary)) is not None:
        intro.append(rank)
    if intro:
        head.append(["  \n".join(intro)])
    tail: list[list[str]] = []
    if (footer := _footer(summary)) is not None:
        tail.append([footer])

    sections: dict[str, Section] = {}
    if summary.failures:
        sections["failures"] = _failures(summary)
    if summary.function_rows:
        sections["changed functions"] = _function_table(summary.function_rows)
    if summary.invariants:
        sections["invariants"] = _invariants("Invariants", summary.invariants)
    if summary.invariants_changed:
        sections["invariants changed by this PR"] = _invariants(
            "Invariants changed by this PR", summary.invariants_changed
        )
    if summary.missing_input_classes:
        sections["input classes"] = _missing_classes(summary.missing_input_classes)

    budget = COMMENT_LIMIT - CUT_NOTE_RESERVE - sum(_size(block) + 1 for block in (*head, *tail))
    fitted: dict[str, list[str]] = {}
    omitted: list[str] = []
    for name, section in sections.items():
        lines = _fit(section, budget)
        if lines is None:
            omitted.append(name)
            continue
        fitted[name] = lines
        budget -= _size(lines) + 1
    order = ("changed functions", "failures", "invariants", "invariants changed by this PR", "input classes")
    blocks = [*head, *(fitted[name] for name in order if name in fitted)]
    if omitted:
        blocks.append(
            [f"_Not shown to fit GitHub's size limit: {', '.join(omitted)} (see the full report)._"]
        )
    blocks.extend(tail)
    return "\n\n".join("\n".join(block) for block in blocks)


def render_sticky_comment(summary: VerdictSummary) -> str:
    """The sticky PR comment markdown, ending with :data:`~coverset.forge.verdict.STICKY_MARKER`."""
    return f"{_render(summary, heading=True)}\n\n{STICKY_MARKER}\n"


def render_check_summary(summary: VerdictSummary) -> str:
    """The check run's ``output.summary`` (same content; the check title carries the heading)."""
    return _render(summary, heading=False)
