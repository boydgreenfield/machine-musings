"""The coverage source view: Pygments with one ``<div class="ln run|mis|par|exc">`` per line.

:class:`CoverageFormatter` overrides :meth:`HtmlFormatter.wrap` so every source line becomes one
block carrying its line state (``run`` = a statement some test or import executed, ``mis`` = a
statement nothing executed, ``par`` = executed but some branch exit never taken, ``exc`` =
excluded from measurement; other lines carry no state), a diff-column marker for changed lines, a
line-number anchor, a covering-test count that opens the line's tests popover, and a partial-branch
note. Function and class regions become nested ``<section>`` elements whose header line is sticky,
followed by the region's inlay chips. Pygments escapes token text; every attribute value here is
escaped by hand (:func:`~coverset.web.render.html.attr`).

Token spans carry Pygments' short classes with a ``tk-`` prefix (``tk-k`` keyword, ``tk-s2``
string, ...), so a token class never collides with the page's own classes (Pygments' regex class
``sr`` is the stylesheet's screen-reader-only class, its ``gd``/``gt`` the gutter's).

A statement that spans several physical lines (coverage.py reports it at its first line) is painted
as one block: every continuation line (:func:`continuation_lines`) takes the state of the
statement's first line and the ``cont`` class, without a gutter count or a "never ran" note.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from markupsafe import Markup
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexer import Lexer
from pygments.lexers import JavascriptLexer, PythonLexer, TextLexer, TypeScriptLexer
from pygments.lexers.jsx import JsxLexer, TsxLexer

from coverset.web.render.html import attr, esc, safe_href

__all__ = [
    "CoverageFormatter",
    "FileSummary",
    "LineFacts",
    "LineState",
    "RegionMark",
    "branch_counts",
    "bracket_continuations",
    "continuation_lines",
    "header_line",
    "lexer_for",
    "python_continuations",
    "render_source",
]

LineState = Literal["run", "mis", "par", "exc", ""]
TOKEN_PREFIX = "tk-"
"""Prefix of the Pygments token classes (``tk-k``, ``tk-s2``, ...): clear of the page's classes."""
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


# -- lexers and multi-line statements -----------------------------------------------------------

PYTHON_SUFFIXES = (".py", ".pyi")
_SCRIPT_LEXERS: dict[str, type[Lexer]] = {
    ".ts": TypeScriptLexer,
    ".mts": TypeScriptLexer,
    ".cts": TypeScriptLexer,
    ".tsx": TsxLexer,
    ".js": JavascriptLexer,
    ".mjs": JavascriptLexer,
    ".cjs": JavascriptLexer,
    ".jsx": JsxLexer,
}


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def lexer_for(path: str) -> Lexer:
    """Python for ``.py``/``.pyi`` (and an empty path), the TypeScript / JavaScript lexers for
    ``.ts``/``.tsx``/``.js``/``.jsx``/``.mjs``/``.cjs`` (``.mts``/``.cts`` too), else plain text."""
    options = {"stripnl": False, "ensurenl": True}
    suffix = _suffix(path)
    if not path or suffix in PYTHON_SUFFIXES:
        return PythonLexer(**options)
    script = _SCRIPT_LEXERS.get(suffix)
    if script is not None:
        return script(**options)
    return TextLexer(**options)


def continuation_lines(text: str, path: str = "") -> dict[int, int]:
    """Physical line → first line of its statement, for every continuation line of a statement
    that spans several lines (first lines and one-line statements are not keys). Python sources use
    :func:`python_continuations`, TypeScript / JavaScript sources :func:`bracket_continuations`;
    other files have none."""
    suffix = _suffix(path)
    if not path or suffix in PYTHON_SUFFIXES:
        return python_continuations(text)
    if suffix in _SCRIPT_LEXERS:
        return bracket_continuations(text)
    return {}


def python_continuations(text: str) -> dict[int, int]:
    """Continuation lines of Python statements, by the rule coverage.py builds its ``_multiline``
    map with: a logical line runs from its first non-comment token to the ``NEWLINE`` token that
    ends it, so the lines inside open brackets, after a backslash and inside a multi-line string
    belong to it, with any blank or comment-only lines among them. A compound statement's header
    ends at its colon (its body starts a new logical line), so ``def f(\\n a,\\n):`` covers the
    header lines only. A source ``tokenize`` cannot read keeps the statements it ended before the
    error."""
    out: dict[int, int] = {}
    first: int | None = None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.NEWLINE:
                if first is not None:
                    for line in range(first + 1, tok.end[0] + 1):
                        out[line] = first
                first = None
            elif first is None and tok.type not in _PY_SKIP and tok.string.strip():
                first = tok.start[0]
    except (tokenize.TokenError, SyntaxError):
        pass
    return out


_PY_SKIP = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = frozenset(_OPENERS.values())
_EXPR_BEFORE_BRACE = frozenset("=(,:[?!&|+-*%<~^")
"""A ``{`` after one of these characters opens an object literal, not a block."""
_EXPR_WORDS = frozenset(
    {"return", "yield", "await", "import", "export", "default", "typeof", "in", "of", "case", "throw", "new"}
)
"""A ``{`` after one of these words opens an object literal / import list, not a block."""


def bracket_continuations(text: str) -> dict[int, int]:
    """Continuation lines of TypeScript / JavaScript statements by bracket depth: a line that
    starts while a ``(``, ``[`` or an object-literal ``{`` is open belongs to the statement whose
    line opened the outermost of them; so does every inner line of a multi-line template literal.
    A block ``{`` (after ``)``, ``=>``, ``else``, ``class X``, at the start of a statement) ends
    the chain: the lines inside a callback body passed as an argument are statements of their own.
    Strings, ``//`` and ``/* */`` comments are skipped by a simple scanner (a quote never spans a
    line; a regex literal or ``${`` nesting inside a template can mislead it)."""
    out: dict[int, int] = {}
    stack: list[tuple[str, bool, int]] = []  # (closer, opens an expression, statement first line)
    in_comment = False
    template: int | None = None  # the statement line of an open template literal
    prev = ""  # the last significant character
    word = ""  # the last identifier, when it is the last significant token
    for number, raw in enumerate(text.split("\n"), 1):
        if template is not None:
            out[number] = template
        elif stack and stack[-1][1]:
            out[number] = stack[-1][2]
        stmt = out.get(number, number)
        i, n = 0, len(raw)
        while i < n:
            ch = raw[i]
            if in_comment:
                end = raw.find("*/", i)
                if end < 0:
                    break
                in_comment, i = False, end + 2
                continue
            if template is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == "`":
                    template, prev, word = None, "`", ""
                i += 1
                continue
            if ch == "/" and raw.startswith("//", i):
                break
            if ch == "/" and raw.startswith("/*", i):
                in_comment, i = True, i + 2
                continue
            if ch in "'\"":
                i += 1
                while i < n and raw[i] != ch:
                    i += 2 if raw[i] == "\\" else 1
                i += 1
                prev, word = ch, ""
                continue
            if ch == "`":
                template = stmt
                i += 1
                continue
            if ch.isalnum() or ch in "_$":
                start = i
                while i < n and (raw[i].isalnum() or raw[i] in "_$"):
                    i += 1
                word, prev = raw[start:i], "w"
                continue
            if ch in _OPENERS:
                # ``=> {`` is a block: ``>`` is not an expression character
                expr = ch != "{" or prev in _EXPR_BEFORE_BRACE or (prev == "w" and word in _EXPR_WORDS)
                # inside an open expression the bracket belongs to its statement, even after a
                # callback's block closed on this line (``}, (``); else it starts on this line
                owner = stack[-1][2] if stack and stack[-1][1] else stmt
                stack.append((_OPENERS[ch], expr, owner))
            elif ch in _CLOSERS:
                while stack and stack[-1][0] != ch:
                    stack.pop()
                if stack:
                    stack.pop()
            if not ch.isspace():
                prev, word = ch, ""
            i += 1
    return out


# -- the file summary line ----------------------------------------------------------------------


def branch_counts(
    arcs_possible: Iterable[tuple[int, int]], taken: Collection[tuple[int, int]]
) -> tuple[int, int]:
    """``(taken, possible)`` branch arcs of a file, counted as the facts store's ``file_cov`` and
    the report's totals count them: the distinct possible arcs, and those the run took."""
    possible = set(arcs_possible)
    return sum(1 for arc in possible if arc in taken), len(possible)


def _pct1(value: float) -> str:
    return f"{value:.1f}%"


@dataclass(frozen=True, slots=True)
class FileSummary:
    """The numbers of one file's summary line (source view bar, report file page)."""

    statements: int
    ran: int
    branches: int = 0
    branches_taken: int = 0
    expected: int = 0
    """Expected input classes of the file's called functions."""
    observed: int = 0
    tests: int = 0
    partial: int = 0
    """Lines that took one branch only (said in the branch figure's hover text)."""

    @property
    def line_pct(self) -> float | None:
        return 100.0 * self.ran / self.statements if self.statements else None

    @property
    def branch_pct(self) -> float | None:
        return 100.0 * self.branches_taken / self.branches if self.branches else None

    @property
    def input_pct(self) -> float | None:
        return 100.0 * self.observed / self.expected if self.expected else None

    @property
    def gap(self) -> float | None:
        """Line % minus input-class % (never below 0), as the map's Gap mode colours it."""
        if self.line_pct is None or self.input_pct is None:
            return None
        return max(0.0, self.line_pct - self.input_pct)

    def items(self) -> list[tuple[str, str]]:
        """The line's pieces with their hover text: ``lines 28.5% (294 of 1030)``, ``branches
        61.2% (30 of 49)`` (left out without branches), ``input classes 12 of 68 (17.6%)`` (or
        ``no input classes``), ``gap 11 pts`` (only with input classes), ``1 test``."""
        out = []
        if self.line_pct is None:
            out.append(("no statements", "nothing measured in this file"))
        else:
            missed = self.statements - self.ran
            out.append(
                (f"lines {_pct1(self.line_pct)} ({self.ran} of {self.statements})", f"{missed} never ran")
            )
        if self.branch_pct is not None:
            noun = "line" if self.partial == 1 else "lines"
            out.append(
                (
                    f"branches {_pct1(self.branch_pct)} ({self.branches_taken} of {self.branches})",
                    f"branch exits taken; {self.partial} {noun} took one branch only",
                )
            )
        if self.input_pct is None:
            out.append(("no input classes", "no called function of this file expects an input class"))
        else:
            hollow = self.expected - self.observed
            out.append(
                (
                    f"input classes {self.observed} of {self.expected} ({_pct1(self.input_pct)})",
                    f"{hollow} input class{'' if hollow == 1 else 'es'} never exercised",
                )
            )
            if self.gap is not None:
                out.append((f"gap {self.gap:.0f} pts", "line % minus input-class %, as on the map"))
        tests = f"{self.tests} test" + ("" if self.tests == 1 else "s")
        out.append((tests, "tests that ran lines of this file (import time aside)"))
        return out

    def text(self) -> str:
        """The whole line, pieces joined by `` · ``."""
        return " · ".join(text for text, _title in self.items())


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
        continuations: Mapping[int, int] | None = None,
    ) -> None:
        super().__init__(nowrap=False, cssclass="src-code", classprefix=TOKEN_PREFIX)
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
        self.continuations = continuations or {}
        """Continuation line → its statement's first line (:func:`continuation_lines`)."""

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

    def _first_line(self, number: int) -> int | None:
        """The first line of the multi-line statement ``number`` continues (None for a first
        line, a one-line statement, or a line coverage measures on its own)."""
        first = self.continuations.get(number)
        if first is None or number in self.facts.statements or number in self.facts.excluded:
            return None
        return first

    def _line(self, number: int, html: str, head: RegionMark | None, depth: int) -> str:
        first = self._first_line(number)
        state = self.facts.state(number if first is None else first)
        if first is not None and state == "par":
            state = "run"  # the partial-branch cue stays on the statement's first line
        classes = ["ln"]
        if state:
            classes.append(state)
        if first is not None:
            classes.append("cont")
        if number in self.changed:
            classes.append("chg")
        # the lens follows the statement: a continuation line is lit when its first line is
        if self.focus is not None and (number if first is None else first) not in self.focus:
            classes.append("dim")
        if head is not None:
            classes.append(f"fn-head d{min(depth, MAX_DEPTH)}")
        anchor = f"{self.anchor_prefix}{number}"
        diff = (
            '<span class="gd" aria-label="changed">+</span>'
            if number in self.changed
            else '<span class="gd"></span>'
        )
        count = self.facts.tests.get(number, 0) if first is None else 0
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
        if first is not None:
            pass  # the first line carries the note
        elif state == "par":
            note = f'<span class="brn">{esc(_branch_note(self.facts.partial[number]))}</span>'
        elif state == "mis":
            note = '<span class="sr">never ran</span>'
        head_link = ""
        if head is not None and head.url:
            head_link = f'<a class="fn-link" href="{attr(safe_href(head.url))}">function</a>'
        body = html.rstrip("\n") or " "
        cont = f' data-first="{first}"' if first is not None else ""
        return (
            f'<div class="{" ".join(classes)}" id="{anchor}" data-line="{number}"{cont}>'
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
    """Highlight ``text`` into the line-block structure, with the lexer :func:`lexer_for` picks
    for ``path`` and its multi-line statements painted as blocks."""
    lexer = lexer_for(path)
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
        continuations=continuation_lines(text, path),
    )
    return Markup(highlight(text, lexer, formatter))
