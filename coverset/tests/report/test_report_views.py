"""The static report's view settings and its shared look with the live pages: the highlight toggle
and ``?highlight=``, the map's module weight variants and ``?mode=`` / ``?modules=``, the lazy lens
menu, the file filter, the footer that says which product this is, and, in a real browser when one
is available, that the keys and the query parameters do what the help says."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from report_helpers import FixtureRun, all_pages

from coverset.report import build as build_module
from coverset.report import build_report
from coverset.report.load import load_bundle

CORE = "src/fixturepkg/core.py"
STATIC = Path(build_module.__file__).parent / "static"


@pytest.fixture(scope="module")
def report(fixture_out: FixtureRun, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("report-views") / "out"
    build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
    return out


def test_source_pages_carry_the_highlight_toggle_and_keep_view_links(report: Path) -> None:
    html = (report / "files" / f"{CORE}.html").read_text()
    assert 'data-highlight-toggle aria-pressed="false"' in html
    assert html.count('class="hl-label hl-') == 2  # both labels, stacked: the width never changes
    links = re.findall(r'<a href="([^"]+)"[^>]*\bdata-keep-view\b', html)
    assert links and all(href.endswith(".html") for href in links)  # the script adds ?highlight=
    assert re.search(r'rel="(?:prev|next)" data-key-nav data-keep-view', html)
    assert '<input type="search" data-filter="#file-links > a"' in html
    function_page = next(p for n, p in all_pages(report).items() if n.startswith("functions/"))
    assert "data-highlight-toggle" in function_page


def test_the_lens_menu_is_built_on_use_from_the_data_block(report: Path) -> None:
    html = (report / "files" / f"{CORE}.html").read_text()
    menu = html[html.index('<select id="lens">') : html.index("</select>")]
    assert menu.count("<option") == 1 and "All tests (" in menu
    script = (STATIC / "report.js").read_text()
    assert "function fillLens(select)" in script and 'addEventListener("pointerdown", primeLens' in script


def test_the_overview_ships_module_weight_variants_and_says_why(report: Path) -> None:
    html = (report / "index.html").read_text()
    for weight in ("full", "thin", "hidden"):
        assert f'data-map-modules="{weight}"' in html
    for variant in ("gap:full", "gap:hidden", "lines:full", "lines:hidden"):
        assert f'<template class="map-variant" data-map-variant="{variant}">' in html
    assert 'data-map-variant="gap:thin"' not in html  # the live map is the thin one, shipped once
    live = html.split('data-map-view="gap">', 1)[1].split("</svg>", 1)[0]
    assert 'data-modules="thin" data-layout="ordered"' in live
    assert html.count('class="map-legend"') == 6
    assert "module-level code shown reduced" in html and "imports" in html.lower()
    for panel in ("files", "gaps", "tests"):
        assert f'<section class="panel lazy-panel" id="{panel}">' in html


def test_a_large_report_ships_the_thin_maps_only(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_module, "MAP_VARIANTS_MAX_LEAVES", 1)
    out = tmp_path / "out"
    build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
    html = (out / "index.html").read_text()
    assert "map-variant" not in html and "data-map-modules" not in html
    assert "so it ships the thin map only" in html


def test_the_footer_says_this_is_the_static_report_and_when_it_was_built(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")  # 2026-01-01
    out = tmp_path / "out"
    build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
    for name, html in all_pages(out).items():
        footer = html[html.index("<footer>") : html.index("</footer>")]
        assert '<span class="where">static report built 2026-01-01 from bundle' in footer, name
        assert '<span class="brand-tag">report</span>' in html
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", build_module.built_date())


def test_the_script_reads_the_view_parameters_before_the_body() -> None:
    script = (STATIC / "report.js").read_text()
    top = script.split('document.addEventListener("DOMContentLoaded"', 1)[0]
    for name in ("highlight", "mode", "modules"):
        assert f'param("{name}")' in top
    assert 'html.setAttribute("data-highlight", view.highlight);' in top


# -- in a browser -------------------------------------------------------------------------------------


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


def _open(browser: Any, url: str) -> tuple[Any, list[str]]:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    page.wait_for_selector('body[data-ready="1"]', state="attached")
    return page, errors


def test_in_a_browser_h_and_the_query_parameter_set_the_mode(report: Path, browser: Any) -> None:
    source = (report / "files" / f"{CORE}.html").as_uri()
    page, errors = _open(browser, source + "?highlight=missing")
    assert page.evaluate("document.documentElement.dataset.highlight") == "missing"
    hrefs = page.evaluate(
        "[...document.querySelectorAll('a[data-keep-view]')].map(a => a.getAttribute('href'))"
    )
    assert hrefs and all("highlight=missing" in href for href in hrefs)
    page.keyboard.press("h")
    assert page.evaluate("document.documentElement.dataset.highlight") == "all"
    assert (
        page.evaluate("document.querySelector('[data-highlight-toggle]').getAttribute('aria-pressed')")
        == "false"
    )
    page.keyboard.press(".")
    assert page.evaluate("document.documentElement.dataset.chips") == "off"
    page.keyboard.press("?")
    shown = page.evaluate(
        "[...document.querySelectorAll('#keys-help tr')].filter(r => !r.hidden).map(r => r.dataset.keys)"
    )
    assert "j k" in shown and "h" in shown and "m" not in shown  # the map keys belong to the overview
    page.keyboard.press("Escape")
    page.click("#lens")
    assert page.evaluate("document.getElementById('lens').options.length") > 1
    page.context.close()
    assert not errors


def test_in_a_browser_m_and_shift_m_switch_the_overview_map(report: Path, browser: Any) -> None:
    page, errors = _open(browser, (report / "index.html").as_uri() + "?modules=hidden")
    assert page.evaluate("document.querySelector('[data-map-view=gap]').dataset.modules") == "hidden"
    page.keyboard.press("m")
    assert page.evaluate("document.querySelector('[data-map-view=lines]').hidden") is False
    page.keyboard.press("M")  # hidden -> full
    modules = page.evaluate("document.querySelector('[data-map-view=lines]').dataset.modules")
    assert modules == "full"
    pressed = page.evaluate("document.querySelector('[data-map-modules=full]').getAttribute('aria-pressed')")
    assert pressed == "true"
    page.context.close()
    assert not errors
