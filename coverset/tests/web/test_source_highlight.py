"""The source view's highlight modes (``view.highlight``): gap blocks, how the mode is chosen, and
that the chosen mode paints on first load (no flash of the other mode)."""

from __future__ import annotations

import html as html_lib
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from quart.typing import TestClientProtocol as Client
from web_helpers import Demo, assert_csp_clean

from coverset.web import files
from coverset.web.render.source import LineFacts, gap_lines, render_source

STATIC = Path(__file__).resolve().parents[2] / "src" / "coverset" / "web" / "static"
DATES = "src/ledger/util/dates.py"


@pytest.mark.parametrize(
    ("states", "gaps"),
    [
        (["mis", "", "mis"], {2}),
        (["mis", "", "", "mis", "", "run"], {2, 3}),  # trailing blank line stays out
        (["", "mis", "", "mis"], {3}),  # so does a leading one
        (["mis", "", "par", "", "mis"], set()),  # a partial line ran: two blocks
        (["mis", "", "exc", "mis"], set()),
        (["run", "", "run"], set()),
        (["mis", "mis", "", ""], set()),
        ([], set()),
    ],
)
def test_gap_lines(states: list[str], gaps: set[int]) -> None:
    assert gap_lines(states) == gaps  # type: ignore[arg-type]


CODE = """\
def f(x):
    if x:
        a = 1

        # a comment inside the block
        b = 2
    else:
        c = 3
    return a
"""


def _classes(html: str) -> dict[int, str]:
    return {int(n): cls for cls, n in re.findall(r'<div class="(ln[^"]*)" id="L(\d+)"', html)}


def test_blank_and_comment_lines_inside_a_never_run_block_are_painted_with_it() -> None:
    facts = LineFacts(
        statements=frozenset({1, 2, 3, 6, 8, 9}),
        executed=frozenset({1, 2, 8, 9}),
        partial={2: (3,)},
    )
    classes = _classes(str(render_source(CODE, facts, path="m.py")))
    assert classes[3].split()[:2] == ["ln", "mis"] and classes[6].split()[:2] == ["ln", "mis"]
    assert "gap" in classes[4].split() and "gap" in classes[5].split()  # blank, then comment
    assert "gap" not in classes[7].split()  # ``else:`` comes after the block: 8 ran
    assert "gap" not in classes[1].split() and "gap" not in classes[9].split()
    # a range render (the function page) finds the same block from inside it
    hunk = _classes(str(render_source(CODE, facts, path="m.py", line_range=(4, 6))))
    assert "gap" in hunk[4].split() and "gap" in hunk[5].split()


def test_unknown_facts_paint_no_gap() -> None:
    html = str(render_source(CODE, LineFacts(known=False), path="m.py"))
    assert " gap" not in html


def test_the_stylesheet_paints_both_modes_from_one_markup() -> None:
    css = (STATIC / "app.css").read_text()
    assert '[data-highlight="missing"] .ln.run' in css
    assert ".ln.gap" in css and '[data-chips="off"] .inlay' in css
    # both toggle labels share one grid cell: the button never changes width
    assert ".hl-toggle > .hl-label { grid-area: 1 / 1;" in css


async def test_default_mode_leaves_the_choice_to_the_reader(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/files/{DATES}")).get_data(as_text=True)
    assert_csp_clean(page)
    assert '<meta name="coverset-view" content="" data-highlight="all" data-highlight-from="default"' in page
    assert '<div class="src-frame">' in page  # no server choice on the frame: localStorage may decide
    assert "data-highlight-toggle" in page and 'aria-pressed="false"' in page


async def test_query_parameter_paints_the_mode_on_first_load_and_rides_along(
    client: Client, demo: Demo
) -> None:
    url = f"/repos/{demo.repo_id}/files/{DATES}?highlight=missing"
    page = await (await client.get(url)).get_data(as_text=True)
    head, body = page.split("</head>", 1)
    meta = head.index('data-highlight="missing" data-highlight-from="query"')
    script = re.search(r'/static/app\.js(\?[^"]*)?"></script>', head).start()
    assert meta < script, "the view meta comes before the synchronous script that applies it"
    assert not re.search(r'app\.js"[^>]*\b(defer|async)\b', head), "app.js must run before the body paints"
    assert '<div class="src-frame" data-highlight="missing">' in body
    assert 'data-highlight-toggle aria-pressed="true"' in body
    for rel in ("prev", "next"):
        found = re.search(rf'<a href="([^"]+)" rel="{rel}"', body)
        if found:
            assert "highlight=missing" in html_lib.unescape(found.group(1))
    listed = re.findall(r'<a href="([^"]+)" data-keep-view', body)
    assert listed and all("highlight=missing" in html_lib.unescape(h) for h in listed)
    bogus = await (await client.get(f"/repos/{demo.repo_id}/files/{DATES}?highlight=<x>")).get_data(
        as_text=True
    )
    assert 'data-highlight-from="default"' in bogus and "&lt;x&gt;" not in bogus.split("</head>")[0]


async def test_settings_choice_beats_the_reader_storage_but_not_the_query(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chosen() -> dict[str, str]:
        return {"view.highlight": "missing"}

    monkeypatch.setattr(files, "request_view_prefs", chosen)
    page = await (await client.get(f"/repos/{demo.repo_id}/files/{DATES}")).get_data(as_text=True)
    assert 'data-highlight="missing" data-highlight-from="prefs"' in page
    assert '<div class="src-frame" data-highlight="missing">' in page
    # links do not pin a mode that came from the settings
    assert not re.search(r'href="[^"]*highlight=', page)
    page = await (await client.get(f"/repos/{demo.repo_id}/files/{DATES}?highlight=all")).get_data(
        as_text=True
    )
    assert 'data-highlight="all" data-highlight-from="query"' in page


def test_the_script_applies_the_mode_before_paint_and_only_adjusts_links() -> None:
    script = (STATIC / "app.js").read_text()
    # read at load, outside any DOMContentLoaded handler: <html data-highlight> is set before the body
    top = script.split('document.addEventListener("DOMContentLoaded"', 1)[0]
    assert 'root.setAttribute("data-highlight", startHighlight());' in top
    assert "coverset-highlight" in script and "a[data-keep-view]" in script


# -- the width of the source block (content-visibility must not move the scroll width) -------------

LONG = "x = " + "'a' + " * 60 + "'z'"


def test_the_widest_line_is_copied_first_so_the_block_has_its_width_from_the_first_paint() -> None:
    text = "def f():\n    return 1\n" + "\n" * 50 + LONG + "\n"
    html = str(render_source(text, LineFacts(statements=frozenset({1, 2})), path="m.py"))
    head = html.split('<div class="src-lines">', 1)[1]
    assert head.startswith('<div class="ln src-w" aria-hidden="true">')
    spacer = head.split("</div>", 1)[0]
    assert html_lib.unescape(re.sub(r"<[^>]+>", "", spacer)) == LONG
    assert "data-line" not in spacer  # j/k and the lens never see it
    # a partial line's branch note widens its row: the copy carries it when that row is the widest
    facts = LineFacts(statements=frozenset({1, 2}), executed=frozenset({1, 2}), partial={1: (2,)})
    noted = str(render_source("if x:\n    y = 1\n", facts, path="m.py"))
    spacer = noted.split('<div class="src-lines">', 1)[1].split("</div>", 1)[0]
    assert 'class="brn">never jumped to line 2</span>' in spacer
    css = (STATIC / "app.css").read_text()
    assert ".src-lines .ln:not(.fn-head, .src-w) { content-visibility: auto;" in css
    assert ".ln.src-w { height: 0;" in css


# -- in a browser ---------------------------------------------------------------------------------


def _chromium(playwright: Any) -> Any:
    """Playwright's Chromium, else any cached build (as scripts/screenshots.py finds it)."""
    try:
        return playwright.chromium.launch()
    except Exception:  # noqa: BLE001 - the pinned build is not installed
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        if not cache.is_dir():
            cache = Path.home() / ".cache" / "ms-playwright"
        candidates = sorted(cache.glob("chromium_headless_shell-*/*/chrome-headless-shell"), reverse=True)
        candidates += sorted(cache.glob("chromium-*/chrome-*/chrome"), reverse=True)
        if not candidates:
            pytest.skip("no Chromium for Playwright")
        return playwright.chromium.launch(executable_path=str(candidates[0]))


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        chromium = _chromium(playwright)
        try:
            yield chromium
        finally:
            chromium.close()


def _page(folder: Path, body: str, meta: str = "") -> Path:
    """A page with the live pages' stylesheet and script (copied next to it), the view meta the
    server would write, and ``body``."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("app.css", "app.js"):
        shutil.copy(STATIC / name, folder / name)
    page = folder / "page.html"
    page.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="app.css">'
        f'<meta name="coverset-view" content="" data-highlight="all" data-highlight-from="default"{meta}>'
        f'<script src="app.js"></script></head><body>{body}</body></html>',
        encoding="utf-8",
    )
    return page


def test_in_a_browser_the_theme_button_beats_a_theme_from_the_settings(browser: Any, tmp_path: Path) -> None:
    button = '<button type="button" id="theme-toggle" class="theme-toggle">Theme</button>'
    dark = _page(tmp_path / "dark", button, ' data-theme="dark" data-theme-from="prefs"')
    context = browser.new_context()
    page = context.new_page()
    page.goto(dark.as_uri())

    def theme() -> str:
        return str(page.evaluate("document.documentElement.getAttribute('data-theme') || 'auto'"))

    assert theme() == "dark"
    seen = []
    for _ in range(3):
        page.click("#theme-toggle")
        seen.append(theme())
    assert seen == ["auto", "light", "dark"]  # cycles from the theme on screen
    page.click("#theme-toggle")
    page.click("#theme-toggle")  # the reader settles on light
    page.reload()
    assert theme() == "light", "the reader's pick holds over the unchanged setting"
    assert page.text_content("#theme-toggle") == "Theme: light"
    # somebody changes the setting: it wins again in this browser until the reader clicks
    (tmp_path / "dark" / "page.html").write_text(
        dark.read_text().replace('data-theme="dark"', 'data-theme="auto"'), encoding="utf-8"
    )
    page.reload()
    assert theme() == "auto"
    context.close()


def test_in_a_browser_the_scroll_width_holds_while_scrolling(browser: Any, tmp_path: Path) -> None:
    text = "".join(f"line_{n} = {n}\n" for n in range(1, 400)) + LONG + "\n"
    facts = LineFacts(statements=frozenset(range(1, 401)), executed=frozenset(range(1, 400)))
    body = f'<div class="src-frame">{render_source(text, facts, path="m.py")}</div>'  # its own .src-code
    page_file = _page(tmp_path / "wide", body)
    context = browser.new_context(viewport={"width": 900, "height": 600})
    page = context.new_page()
    page.goto(page_file.as_uri())
    width = "document.querySelector('.src-code').scrollWidth"
    before = page.evaluate(width)
    assert before > 900  # the long last line, below the fold, already counts
    page.evaluate("document.getElementById('L400').scrollIntoView()")
    page.wait_for_timeout(400)  # the long line is drawn now: without the copy the width grew here
    assert page.evaluate("document.getElementById('L400').getBoundingClientRect().width") > 900
    assert page.evaluate(width) == before
    context.close()
