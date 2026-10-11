"""The coverage source view: Pygments with one ``<div class="ln run|mis|par|exc">`` per line.

:class:`CoverageFormatter` overrides :meth:`HtmlFormatter.wrap` so every source line becomes one
block carrying its line state (``run`` = a statement some test or import executed, ``mis`` = a
statement nothing executed, ``par`` = executed but some branch exit never taken, ``exc`` =
excluded from measurement; other lines carry no state), a diff-column marker for changed lines, a
line-number anchor, a covering-test count that opens the line's tests popover, and a partial-branch
note. Function and class regions become nested ``<section>`` elements whose header line is sticky,
followed by the region's inlay chips. Pygments escapes token text; every attribute value here is
escaped by hand (:func:`~coverset.web.render.html.attr`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from markupsafe import Markup
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import PythonLexer, TextLexer

from coverset.web.render.html import attr, esc, safe_href

__all__ = [
    "CoverageFormatter",
    "LineFacts",
    "LineState",
    "RegionMark",
    "header_line",
    "render_source",
]

LineState = Literal["run", "mis", "par", "exc", ""]
_DEF = re.compile(r"^\s*(?:async\s+def|def|class)\b")
MAX_DEPTH = 3


@dataclass(frozen=True, slots=True)
class LineFacts:
    """Per-line coverage facts of one file in one run (``known`` is False without facts)."""

    statements: frozenset[int] = frozenset()
    excluded: frozenset[int] = frozenset()
    executed: frozenset[int] = frozenset()
    partial: Mapping[int, tuple[int, ...]] = field(default_factory=dict)
    """Line → branch destinations never taken (negative numbers are exits from the code object)."""
    tests: Mapping[int, int] = field(default_factory=dict)
    """Line → number of tests (not ``<import>``) that executed it."""
    known: bool = True

    def state(self, line: int) -> LineState:
        """The line's state class."""
        if not self.known:
            return ""
        if line in self.excluded:
            return "exc"
        if line not in self.statements:
            return ""
        if line not in self.executed:
            return "mis"
        return "par" if line in self.partial else "run"


@dataclass(frozen=True, slots=True)
class RegionMark:
    """A function or class region to wrap in a sticky section (lines ``start``..``end``)."""

    start: int
    end: int
    qualname: str
    kind: str
    func_key: str | None = None
    header: int | None = None
    """The ``def``/``class`` line (``start`` may be a decorator); found from the text when None."""
    hollow: int = 0
    """Expected input classes no test exercised (drives the n/N keys)."""
    url: str | None = None
    chips: Markup | None = None
    """Pre-rendered inlay row shown under the header line."""


def header_line(lines: Sequence[str], region: RegionMark) -> int:
    """The first ``def``/``class`` line of ``region`` (skipping decorators), else its start."""
    if region.header is not None:
        return region.header
    for number in range(region.start, min(region.end, len(lines)) + 1):
        if _DEF.match(lines[number - 1]):
            return number
    return region.start


def _branch_note(targets: Sequence[int]) -> str:
    jumps = sorted(t for t in targets if t > 0)
    parts = []
    if jumps:
        parts.append("never jumped to line " + ", ".join(str(t) for t in jumps))
    if any(t <= 0 for t in targets):
        parts.append("never took the exit")
    return "; ".join(parts)


class CoverageFormatter(HtmlFormatter):  # type: ignore[type-arg]
    """``HtmlFormatter`` whose :meth:`wrap` emits one coverage-annotated block per line."""

    def __init__(
        self,
        facts: LineFacts,
        *,
        text_lines: Sequence[str],
        regions: Sequence[RegionMark] = (),
        changed: Iterable[int] = (),
        removed: Mapping[int, Sequence[str]] | None = None,
        line_range: tuple[int, int] | None = None,
        tests_url: Callable[[int], str] | None = None,
        anchor_prefix: str = "L",
        focus: Collection[int] | None = None,
    ) -> None:
        super().__init__(nowrap=False, cssclass="src-code")
        self.facts = facts
        self.text_lines = text_lines
        self.regions = sorted(regions, key=lambda r: (r.start, -r.end))
        self.changed = frozenset(changed)
        self.removed = removed or {}
        self.line_range = line_range
        self.tests_url = tests_url
        self.anchor_prefix = anchor_prefix
        self.focus = None if focus is None else frozenset(focus)
        """When set, every line outside it is dimmed (the data-flow lens of one test)."""

    # Pygments calls wrap(source) with (is_line, html) pairs; we yield our own structure.
    def wrap(self, source: Iterable[tuple[int, str]]) -> Iterator[tuple[int, str]]:  # type: ignore[override]
        lo, hi = self.line_range or (1, 1 << 30)
        stack: list[RegionMark] = []
        pending = list(self.regions) if self.line_range is None else []
        headers = {id(r): header_line(self.text_lines, r) for r in pending}
        number = 0
        yield 0, '<div class="src-lines">'
        for is_line, html in source:
            if not is_line:
                yield 0, html
                continue
            number += 1
            if number < lo or number > hi:
                continue
            while stack and stack[-1].end < number:
                stack.pop()
                yield 0, "</section>"
            while pending and pending[0].start <= number:
                region = pending.pop(0)
                if region.end < number:
                    continue
                stack.append(region)
                yield 0, self._open_section(region, len(stack) - 1)
            for text in self.removed.get(number, ()):
                yield 0, self._removed_line(text)
            head_of = next((r for r in stack if headers.get(id(r)) == number), None)
            depth = stack.index(head_of) if head_of is not None else 0
            yield 1, self._line(number, html, head_of, depth)
            if head_of is not None and head_of.chips is not None:
                yield 0, str(head_of.chips)
        for text in self.removed.get(number + 1, ()):
            yield 0, self._removed_line(text)
        while stack:
            stack.pop()
            yield 0, "</section>"
        yield 0, "</div>"

    def _open_section(self, region: RegionMark, depth: int) -> str:
        key = f' data-fn="{attr(region.func_key)}"' if region.func_key else ""
        return (
            f'<section class="fn-region d{min(depth, MAX_DEPTH)}" data-kind="{attr(region.kind)}"'
            f' data-qualname="{attr(region.qualname)}" data-hollow="{int(region.hollow)}"{key}>'
        )

    def _removed_line(self, text: str) -> str:
        return (
            '<div class="ln del"><span class="gd" aria-label="removed">−</span>'
            '<span class="gn"></span><span class="gt"></span>'
            f'<code class="src">{esc(text.rstrip(chr(10)))}</code></div>'
        )

    def _line(self, number: int, html: str, head: RegionMark | None, depth: int) -> str:
        state = self.facts.state(number)
        classes = ["ln"]
        if state:
            classes.append(state)
        if number in self.changed:
            classes.append("chg")
        if self.focus is not None and number not in self.focus:
            classes.append("dim")
        if head is not None:
            classes.append(f"fn-head d{min(depth, MAX_DEPTH)}")
        anchor = f"{self.anchor_prefix}{number}"
        diff = (
            '<span class="gd" aria-label="changed">+</span>'
            if number in self.changed
            else '<span class="gd"></span>'
        )
        count = self.facts.tests.get(number, 0)
        if count and self.tests_url is not None:
            noun = "test" if count == 1 else "tests"
            gutter = (
                f'<button type="button" class="gt" popovertarget="line-tests"'
                f' hx-get="{attr(safe_href(self.tests_url(number)))}" hx-target="#line-tests-body"'
                f' hx-swap="innerHTML" aria-label="{count} {noun} ran line {number}">{count}</button>'
            )
        else:
            gutter = '<span class="gt"></span>'
        note = ""
        if state == "par":
            note = f'<span class="brn">{esc(_branch_note(self.facts.partial[number]))}</span>'
        elif state == "mis":
            note = '<span class="sr">never ran</span>'
        head_link = ""
        if head is not None and head.url:
            head_link = f'<a class="fn-link" href="{attr(safe_href(head.url))}">function</a>'
        body = html.rstrip("\n") or " "
        return (
            f'<div class="{" ".join(classes)}" id="{anchor}" data-line="{number}">'
            f'{diff}<a class="gn" href="#{anchor}">{number}</a>{gutter}'
            f'<code class="src">{body}</code>{note}{head_link}</div>'
        )


def render_source(
    text: str,
    facts: LineFacts,
    *,
    path: str = "",
    regions: Sequence[RegionMark] = (),
    changed: Iterable[int] = (),
    removed: Mapping[int, Sequence[str]] | None = None,
    line_range: tuple[int, int] | None = None,
    tests_url: Callable[[int], str] | None = None,
    anchor_prefix: str = "L",
    focus: Collection[int] | None = None,
) -> Markup:
    """Highlight ``text`` (Python when ``path`` ends in ``.py``) into the line-block structure."""
    lexer = (
        PythonLexer(stripnl=False, ensurenl=True)
        if path.endswith(".py") or not path
        else TextLexer(stripnl=False, ensurenl=True)
    )
    formatter = CoverageFormatter(
        facts,
        text_lines=text.split("\n"),
        regions=regions,
        changed=changed,
        removed=removed,
        line_range=line_range,
        tests_url=tests_url,
        anchor_prefix=anchor_prefix,
        focus=focus,
    )
    return Markup(highlight(text, lexer, formatter))
