"""The source renderer: lexer by extension, token classes, multi-line statements painted as one
block, and the file summary line."""

from __future__ import annotations

import re

import pytest

from coverset.web.render.source import (
    FileSummary,
    LineFacts,
    bracket_continuations,
    branch_counts,
    continuation_lines,
    lexer_for,
    python_continuations,
    render_source,
)


def _blocks(html: str) -> dict[int, str]:
    """Line number → the class attribute of its block."""
    return {int(n): cls for cls, n in re.findall(r'<div class="(ln[^"]*)" id="L(\d+)" data-line="\d+"', html)}


# -- lexers and token classes -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "lexer"),
    [
        ("", "PythonLexer"),
        ("a/b.py", "PythonLexer"),
        ("a/b.pyi", "PythonLexer"),
        ("src/x.ts", "TypeScriptLexer"),
        ("src/x.mts", "TypeScriptLexer"),
        ("src/x.tsx", "TsxLexer"),
        ("src/x.js", "JavascriptLexer"),
        ("src/x.mjs", "JavascriptLexer"),
        ("src/x.cjs", "JavascriptLexer"),
        ("src/x.jsx", "JsxLexer"),
        ("README", "TextLexer"),
        ("data.toml", "TextLexer"),
        ("dir.py/notes.txt", "TextLexer"),
    ],
)
def test_the_lexer_follows_the_extension(path: str, lexer: str) -> None:
    assert type(lexer_for(path)).__name__ == lexer


def test_token_spans_carry_prefixed_classes() -> None:
    html = str(render_source('@dec\ndef f(x):\n    """Doc."""\n    return len("s") + 1  # c\n', LineFacts()))
    for cls in ("tk-nd", "tk-k", "tk-nf", "tk-sd", "tk-nb", "tk-s2", "tk-mi", "tk-c1"):
        assert f'class="{cls}"' in html, cls
    # no bare Pygments class that collides with the page's own (sr = screen-reader only, gd/gt = gutter)
    ts = str(render_source("const re = /a+/g;\n", LineFacts(), path="x.ts"))
    assert 'class="tk-sr"' in ts and '<span class="sr">' not in ts


def test_plain_text_still_gets_one_block_per_line() -> None:
    facts = LineFacts(statements=frozenset({1, 3}), executed=frozenset({1}))
    html = str(render_source("a = (\nb\nc\n", facts, path="notes.txt"))
    assert _blocks(html) == {1: "ln run", 2: "ln", 3: "ln mis"}  # no continuation map for text


def test_typescript_renders_with_coverage_blocks() -> None:
    source = "export function rate(plan: string): number {\n  return plan.length;\n}\n"
    facts = LineFacts(statements=frozenset({1, 2}), executed=frozenset({1}), tests={1: 1})
    html = str(render_source(source, facts, path="src/rate.ts"))
    assert _blocks(html) == {1: "ln run", 2: "ln mis", 3: "ln"}
    assert 'class="tk-kd">function</span>' in html and 'class="tk-kt">string</span>' in html


# -- multi-line statements: Python ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("x = 1\ny = 2\n", {}),  # one-line statements
        ("from a import (\n    b,\n    c,\n)\n", {2: 1, 3: 1, 4: 1}),  # parentheses
        ("x = f(g(\n    [1,\n     2]),\n    3)\ny = 1\n", {2: 1, 3: 1, 4: 1}),  # nested brackets
        ("total = 1 + \\\n    2 + \\\n    3\n", {2: 1, 3: 1}),  # backslash
        ('s = """\n\nline\n\n"""\nz = 0\n', {2: 1, 3: 1, 4: 1, 5: 1}),  # triple-quoted, blank lines
        ('def f():\n    """Doc.\n\n    More.\n    """\n    return 1\n', {3: 2, 4: 2, 5: 2}),  # docstring
        ("@dec(\n    a=1,\n)\n@other\ndef f():\n    pass\n", {2: 1, 3: 1}),  # decorators
        (
            'd = {\n    # a comment\n    "k": 1,\n\n    "j": 2,  # trailing\n}\n',
            {2: 1, 3: 1, 4: 1, 5: 1, 6: 1},
        ),
        ('msg = f"{a} {{literal}} {b}"\nok = f"""x\n{y}\n"""\n', {3: 2, 4: 2}),  # f-strings with braces
        ("def f(\n    a,\n    b,\n):\n    return a\n", {2: 1, 3: 1, 4: 1}),  # header only, not the body
        ("if (a and\n        b):\n    c = 1\n", {2: 1}),  # compound header over two lines
        ("# lead\n\nx = [\n    1,\n]  # end\n# after\n", {4: 3, 5: 3}),  # comments outside stay out
    ],
)
def test_python_continuations(source: str, expected: dict[int, int]) -> None:
    assert python_continuations(source) == expected


def test_python_continuations_keep_what_precedes_a_tokenize_error() -> None:
    assert python_continuations("x = (\n  1,\n)\ny = (\n") == {2: 1, 3: 1}
    assert python_continuations("") == {}


# -- multi-line statements: TypeScript / JavaScript -------------------------------------------------


def test_bracket_continuations() -> None:
    source = "\n".join(
        [
            "import {",  # 1
            "  a,",
            "} from 'x';",
            "describe('x', () => {",  # 4: a block brace inside the call: its body is not continuation
            "  it('y', () => {",
            "    expect(f(",  # 6
            "      1,",
            "    )).toBe(2);",
            "  });",
            "});",
            "const o = {",  # 11
            "  a: 1, // ( not a bracket",
            "  b: `t",
            "${x}",
            "`,",
            '  c: "don\'t (",',
            "};",
            "function g(a: number,",  # 18
            "           b: string): void {",
            "  /* ( */ return;",
            "}",
            "",
        ]
    )
    assert bracket_continuations(source) == {
        2: 1,
        3: 1,
        7: 6,
        8: 6,
        12: 11,
        13: 11,
        14: 11,
        15: 11,
        16: 11,
        17: 11,
        19: 18,
    }


def test_continuation_lines_by_extension() -> None:
    assert continuation_lines("f(\n1)\n", "a.py") == {2: 1}
    assert continuation_lines("f(\n1)\n", "a.tsx") == {2: 1}
    assert continuation_lines("f(\n1)\n", "a.md") == {}


# -- multi-line statements: rendering ----------------------------------------------------------------

IMPORT = "from pkg.module import (\n    alpha,\n    # beta is gone\n\n    gamma,\n)\nx = call(\n    1,\n    2,\n)\n"


def test_a_never_run_import_reads_as_one_block() -> None:
    facts = LineFacts(statements=frozenset({1, 7}), executed=frozenset({7}), tests={7: 2})
    html = str(render_source(IMPORT, facts, path="m.py", tests_url=lambda n: f"/t/{n}"))
    blocks = _blocks(html)
    assert blocks[1] == "ln mis"
    assert [blocks[n] for n in range(2, 7)] == ["ln mis cont"] * 5
    assert blocks[7] == "ln run" and [blocks[n] for n in (8, 9, 10)] == ["ln run cont"] * 3
    assert len(blocks) == 10
    # the first line keeps the "never ran" note and the test count; continuation lines carry neither
    assert html.count('<span class="sr">never ran</span>') == 1
    assert html.count('class="gt" popovertarget') == 1
    assert 'id="L2" data-line="2" data-first="1">' in html and 'data-first="7"' in html


def test_partial_branch_and_own_statements_stay_on_their_lines() -> None:
    source = "if (a and\n        b):\n    c = [\n        1,\n    ]\n"
    facts = LineFacts(
        statements=frozenset({1, 3}), executed=frozenset({1, 3}), partial={1: (-1,)}, tests={1: 1, 3: 1}
    )
    blocks = _blocks(str(render_source(source, facts, path="m.py")))
    assert blocks == {1: "ln par", 2: "ln run cont", 3: "ln run", 4: "ln run cont", 5: "ln run cont"}
    # a line coverage measures on its own keeps its own state (a statement inside a TS callback)
    ts = "run(() => {\n  go(\n    1);\n}, (\n  2));\n"
    facts = LineFacts(statements=frozenset({1, 2}), executed=frozenset({1}))
    blocks = _blocks(str(render_source(ts, facts, path="a.ts")))
    assert blocks == {1: "ln run", 2: "ln mis", 3: "ln mis cont", 4: "ln", 5: "ln run cont"}


def test_lens_and_diff_column_on_continuation_lines() -> None:
    facts = LineFacts(statements=frozenset({1, 7}), executed=frozenset({1, 7}))
    html = str(render_source(IMPORT, facts, path="m.py", changed={3}, focus={7}))
    blocks = _blocks(html)
    assert blocks[1] == "ln run dim" and blocks[3] == "ln run cont chg dim"  # chg stays per line
    assert blocks[8] == "ln run cont"  # lit: its statement's first line is in the lens


def test_excluded_statement_continuations() -> None:
    facts = LineFacts(statements=frozenset({1}), excluded=frozenset({1}))
    blocks = _blocks(str(render_source("x = (\n  1)\n", facts, path="m.py")))
    assert blocks == {1: "ln exc", 2: "ln exc cont"}


# -- the summary line --------------------------------------------------------------------------------


def test_summary_line() -> None:
    summary = FileSummary(
        statements=1030,
        ran=294,
        branches=49,
        branches_taken=30,
        expected=68,
        observed=12,
        tests=1,
        partial=37,
    )
    assert summary.text() == (
        "lines 28.5% (294 of 1030) · branches 61.2% (30 of 49) · input classes 12 of 68 (17.6%)"
        " · gap 11 pts · 1 test"
    )
    titles = dict(summary.items())
    assert titles["lines 28.5% (294 of 1030)"] == "736 never ran"
    assert "37 lines took one branch only" in titles["branches 61.2% (30 of 49)"]
    assert titles["input classes 12 of 68 (17.6%)"] == "56 input classes never exercised"
    bare = FileSummary(statements=10, ran=10, tests=3)
    assert bare.text() == "lines 100.0% (10 of 10) · no input classes · 3 tests"
    assert bare.gap is None and bare.branch_pct is None
    assert FileSummary(statements=4, ran=1, expected=4, observed=4).text().endswith("gap 0 pts · 0 tests")
    assert FileSummary(statements=0, ran=0).text() == "no statements · no input classes · 0 tests"


def test_branch_counts_are_distinct_arcs() -> None:
    assert branch_counts([(1, 2), (1, 3), (1, 2), (4, -1)], {(1, 2), (9, 9)}) == (1, 3)
