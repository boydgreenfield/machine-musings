"""Every renderer that produces Markup escapes hostile text: ``<script>``, attribute breakouts and
``javascript:`` URLs never come out live."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from markupsafe import Markup

from coverset.web.render.html import attrs, breakable, esc, safe_href
from coverset.web.render.markdown import render_markdown, render_sticky
from coverset.web.render.ribbon import Flow, render_ribbon
from coverset.web.render.shapegrid import (
    GridCell,
    GridModel,
    GridRow,
    render_grid,
    render_inlay,
    render_strip,
)
from coverset.web.render.source import LineFacts, RegionMark, render_source
from coverset.web.render.timeline import Milestone, OrderRow, render_gantt, render_timeline, render_why_order
from coverset.web.render.treemap import TreeItem, render_treemap
from coverset.web.runstate import Block

pytestmark = pytest.mark.security

SCRIPT = "<script>alert(1)</script>"
BREAKOUT = '"onmouseover="alert(1)'
JS_URL = "javascript:alert(1)"
EVIL = f"{SCRIPT}{BREAKOUT}{JS_URL}"

TAG_WITH_HANDLER = re.compile(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", re.IGNORECASE)
LIVE_JS_URL = re.compile(r"""(?:href|src|hx-get|hx-post)\s*=\s*["']?\s*javascript:""", re.IGNORECASE)


def assert_safe(html: Markup | str) -> None:
    text = str(html)
    assert "<script" not in text.lower(), text[:400]
    assert not TAG_WITH_HANDLER.search(text), TAG_WITH_HANDLER.search(text)
    assert not LIVE_JS_URL.search(text), LIVE_JS_URL.search(text)


def test_html_helpers() -> None:
    assert_safe(esc(EVIL))
    assert_safe(Markup(f"<a{attrs(title=EVIL, href=safe_href(JS_URL))}>x</a>"))
    assert safe_href(JS_URL) == "#"
    assert safe_href("//evil.example/x") == "#"
    assert safe_href("/repos/1") == "/repos/1"
    assert safe_href("https://github.com/x") == "https://github.com/x"
    assert safe_href(" javascript:alert(1)") == "#"
    assert_safe(breakable(EVIL))
    assert str(breakable("t/a.py::test_x[<b>]")) == "t/<wbr>a.py::<wbr>test_<wbr>x[<wbr>&lt;b&gt;]"


def test_source_renderer() -> None:
    code = f'x = "{SCRIPT}"\n# {BREAKOUT}\ndef f():\n    return "{JS_URL}"\n'
    html = render_source(
        code,
        LineFacts(statements=frozenset({1, 3, 4}), executed=frozenset({1}), partial={}, tests={1: 2}),
        path="evil.py",
        regions=[RegionMark(3, 4, EVIL, "function", func_key="k", url=JS_URL, chips=None)],
        changed={1},
        removed={2: [SCRIPT, BREAKOUT]},
        tests_url=lambda n: JS_URL,
    )
    assert_safe(html)
    assert "&lt;script&gt;" in html


def test_treemap_renderer() -> None:
    leaf = TreeItem(
        name=EVIL, path=f"a/{EVIL}.py::{EVIL}", kind="function", statements=5, covered=3, file=EVIL
    )
    file = TreeItem(name=EVIL, path=f"a/{EVIL}.py", kind="file", statements=5, covered=3, children=[leaf])
    root = TreeItem(name="", path="", kind="dir", statements=5, covered=3, children=[file])
    html = render_treemap(root, leaf_url=lambda i: JS_URL, dir_url=lambda i: JS_URL, label=EVIL)
    assert_safe(html)
    # the hover line's text rides in data-info, escaped like the aria-label it repeats
    infos = re.findall(r'data-info="([^"]*)"', str(html))
    assert len(infos) >= 2 and all("<" not in info and '"' not in info for info in infos)
    assert any("&lt;script&gt;alert(1)&lt;/script&gt;&#34;onmouseover=&#34;alert(1)" in i for i in infos)


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("evil.py", 'x = "</span>"onmouseover=alert(1) <script>"\ny = (\n    "</div></code>",\n)\n'),
        ("evil.py", 'def f():\n    """</span><img src=x onerror=alert(1)>\n\n    "onmouseover="\n    """\n'),
        ("evil.ts", 'const a = "</span>\\"onmouseover=alert(1)";\nconst b = `</code>\n${"<script>"}\n`;\n'),
        ("evil.tsx", 'const el = <div title={"\\"onmouseover=x"}>{"</span>"}</div>;\n'),
        ("evil.txt", '</span>"onmouseover=alert(1)\n'),
    ],
)
def test_token_spans_never_break_escaping(path: str, code: str) -> None:
    facts = LineFacts(statements=frozenset({1, 2}), executed=frozenset({1}))
    html = str(render_source(code, facts, path=path, tests_url=lambda n: JS_URL))
    assert_safe(html)
    assert "<img" not in html and "<div title" not in html and "</code></code>" not in html
    # every literal "</span>" in the source came out escaped: the spans balance
    assert html.count("<span") == html.count("</span>")
    assert "&lt;/span&gt;" in html


def test_shape_grid_renderers() -> None:
    cells = (
        GridCell(f"{EVIL}:{EVIL}", EVIL, True, 3, hint=EVIL, description=EVIL),
        GridCell(f"{EVIL}:x", EVIL, False, 0, hint=EVIL),
    )
    model = GridModel(
        EVIL,
        EVIL,
        (GridRow(EVIL, EVIL, cells), GridRow("return", None, (), EVIL)),
        1,
        2,
        missing_reason=EVIL,
        note=EVIL,
    )
    assert_safe(render_grid(model, cell_url=lambda c: JS_URL, target=EVIL))
    assert_safe(render_strip(cells, label=EVIL))
    assert_safe(render_inlay([(EVIL, EVIL, cells, cells)], note=EVIL, shapes_url=JS_URL, missing=EVIL))
    returned = GridCell(f"return:{EVIL}", EVIL, True, 2, description=EVIL, role="return")
    assert_safe(
        render_grid(
            GridModel(EVIL, EVIL, (GridRow("return", EVIL, (returned,)),), 0, 0),
            cell_url=lambda c: JS_URL,
            target=EVIL,
        )
    )
    assert_safe(render_inlay([], note=EVIL, shapes_url=JS_URL))


def test_ribbon_renderer() -> None:
    flows = [Flow(EVIL, EVIL, EVIL, 3, frozenset({EVIL})), Flow("t.py", EVIL, "x", 1)]
    assert_safe(render_ribbon(flows))


def test_timeline_renderers() -> None:
    marks = [Milestone("queued", EVIL, 0.0), Milestone(EVIL, EVIL, 1.0)]
    assert_safe(render_timeline(marks, ghost=3.0, ghost_label=EVIL))
    lanes = {0: [Block(EVIL, 0.0, 1.0, EVIL), Block(EVIL, 1.0, 2.0, "failed", flaky=True)]}
    assert_safe(
        render_gantt(
            lanes,
            span=2.0,
            queued={0: 3},
            lane_status={0: EVIL},
            lane_bounds={0: (0.0, None)},
            failure_at=1.0,
            failure_label=EVIL,
        )
    )
    rows = [OrderRow(1, EVIL, 8.0, {"new_or_changed": 8.0}, EVIL, "failed", 66, True, True, tie=EVIL)]
    assert_safe(render_why_order(rows, test_url=lambda n: JS_URL, rest=3, rest_note=EVIL))


@pytest.mark.parametrize(
    "text",
    [
        SCRIPT,
        '<img src=x onerror="alert(1)">',
        f"[click]({JS_URL})",
        f"[click](<{JS_URL}>)",
        "[x](data:text/html;base64,PHNjcmlwdD4=)",
        f'<a href="{JS_URL}">x</a>',
        f"<details><summary>{SCRIPT}</summary>",
        f"| a | b |\n|---|---|\n| {SCRIPT} | {BREAKOUT} |",
        "![img](https://evil.example/x.png)",
    ],
)
def test_markdown_renderers(text: str) -> None:
    for render in (render_markdown, render_sticky):
        html = render(text)
        assert_safe(html)
        assert "<img" not in str(html)
        assert "data:text" not in str(html) or "href" not in str(html)


def test_markdown_keeps_http_links() -> None:
    html = str(render_markdown("[report](https://coverset.example/runs/1) and [bad](ftp://x)"))
    assert '<a href="https://coverset.example/runs/1">report</a>' in html
    assert 'href="ftp' not in html and "[bad](ftp://x)" in html


def test_markdown_sticky_skeleton() -> None:
    body = (
        "### coverset · FAIL\n\n<details><summary>Input classes never exercised (2)</summary>\n\n"
        "- `email:idn`\n\n</details>\n\n<!-- coverset:sticky -->\n"
    )
    html = str(render_sticky(body))
    assert "<details><summary>Input classes never exercised (2)</summary>" in html
    assert html.count("</details>") == 1
    assert "coverset:sticky" not in html
    assert "<h3>coverset · FAIL</h3>" in html


def test_markdown_unclosed_details_are_closed() -> None:
    html = str(render_sticky("<details><summary>x</summary>\n\ntext"))
    assert html.count("<details>") == html.count("</details>") == 1


def test_run_state_tolerates_garbage_payloads() -> None:
    from coverset.web.runstate import RunState

    state = RunState("r")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for kind, payload in [
        ("test", {"nodeid": 3}),
        ("test", {"nodeid": EVIL, "shard": "x", "duration_s": "slow"}),
        ("shard", {"shard": True}),
        ("status", {"status": "planned", "tests": "many"}),
        ("verdict", None),
    ]:
        state.apply(1, kind, payload, now)  # type: ignore[arg-type]
    assert EVIL in state.tests and state.planned_tests is None
