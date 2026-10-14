"""Page polish against the demo data: the tests page (one loop, pages of 500, the filter box), the
``r`` key's run links, the function page's highlight toggle, the shared source cache on function
pages, and the URL builder the map and the tests page use instead of one ``url_for`` per item."""

from __future__ import annotations

import re
from typing import Any

import pytest
from quart import Quart, render_template, url_for
from quart.typing import TestClientProtocol as Client
from web_helpers import Demo, assert_csp_clean

from coverset.settings_scopes import ViewPrefs, default_view_prefs
from coverset.web import repos, settings_pages
from coverset.web.pages import get_repo
from coverset.web.render import source as render_source_module
from coverset.web.repos import URL_INT_SLOT, URL_SLOT, url_builder

ROW = re.compile(r'<tr id="test-\d+".*?</tr>', re.S)


def squash(html: str) -> str:
    return re.sub(r"\s+", " ", html).strip()


async def pr2_function_key(client: Client, demo: Demo) -> str:
    pr2 = await (await client.get(f"/repos/{demo.repo_id}/prs/2")).get_data(as_text=True)
    keys = re.findall(r"/prs/2/functions/([0-9a-f]{16})", pr2)
    assert keys
    return keys[0]


# -- the tests page ------------------------------------------------------------------------------


async def test_the_tests_page_has_the_filter_box(client: Client, demo: Demo) -> None:
    html = await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(as_text=True)
    assert_csp_clean(html)
    box = re.search(r'<label class="filter-box[^"]*">(.*?)</label>', html, re.S)
    assert box is not None
    assert '<span class="sr">Filter tests</span>' in box.group(1)
    field = re.search(r"<input [^>]*>", box.group(1))
    assert field is not None
    for attribute in (
        'type="search"',
        'data-filter=".tests-table tbody tr"',
        'placeholder="Filter tests  /"',
        'autocomplete="off"',
        'spellcheck="false"',
    ):
        assert attribute in field.group(0), attribute
    # the selector matches the table: its class and rows in a tbody
    assert '<table class="tests-table">' in html and "<tbody>" in html
    # under the sort switch
    assert html.index('aria-label="Sort by"') < html.index('class="filter-box') < html.index("tests-table")


async def test_rows_render_in_one_loop_like_the_fragment(
    app: Quart, client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row of the page is the HTML ``tests_row.html`` renders for the htmx swap."""
    # Pin the clock: the rows carry "N s ago", and the page and the fragments render at different moments.
    settings = app.extensions["settings"]
    monkeypatch.setattr(settings, "fake_now", settings.now())
    html = await (await client.get(f"/repos/{demo.repo_id}/tests?sort=name")).get_data(as_text=True)
    page_rows = ROW.findall(html)
    assert page_rows and "Quarantine</button>" in page_rows[0]
    async with app.test_request_context(f"/repos/{demo.repo_id}/tests"):
        rows = {r["id"]: r for r in await repos._test_rows(demo.repo_id)}
        repo_row = await get_repo(demo.repo_id)
        for page_row in page_rows:
            test_id = int(re.match(r'<tr id="test-(\d+)"', page_row).group(1))  # type: ignore[union-attr]
            fragment = await render_template(
                "tests_row.html", repo=repo_row, row=rows[test_id], can_quarantine=True, oob=False
            )
            assert squash(page_row) == squash(fragment.strip()), test_id
            readonly = await render_template(
                "tests_row.html", repo=repo_row, row=rows[test_id], can_quarantine=False, oob=False
            )
            assert "<button" not in readonly
    assert len(page_rows) == len(rows), "fewer than 500 tests: one page, no pager"
    assert "tests-pager" not in html and "show all" not in html


async def test_the_tests_page_is_paginated_with_a_show_all_link(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repos, "TESTS_PAGE", 3)
    base = f"/repos/{demo.repo_id}/tests"
    full = await (await client.get(f"{base}?all=1&sort=name")).get_data(as_text=True)
    every = ROW.findall(full)
    total = len(every)
    assert total > 6, "the demo has enough tests for three pages"
    assert f"All {total} tests." in full and "Show 3 a page" in full
    first = await (await client.get(f"{base}?sort=name")).get_data(as_text=True)
    assert ROW.findall(first) == every[:3]
    assert f"Tests 1–3 of {total}" in first and f"show all {total}</a>" in first
    assert f'href="{base}?sort=name&amp;all=1" data-show-all' in first
    assert f'href="{base}?sort=name&amp;page=2" rel="next"' in first and 'rel="prev"' not in first
    assert f"<span>{total} tests</span>" in first, "the count is every test, not the page"
    second = await (await client.get(f"{base}?sort=name&page=2")).get_data(as_text=True)
    assert ROW.findall(second) == every[3:6] and 'rel="prev"' in second
    # out of range or junk page numbers land on a real page
    last = await (await client.get(f"{base}?sort=name&page=999")).get_data(as_text=True)
    assert ROW.findall(last) == every[-((total - 1) % 3 + 1) :]
    junk = await (await client.get(f"{base}?sort=name&page=-1")).get_data(as_text=True)
    assert ROW.findall(junk) == every[:3]


# -- the r key and the highlight toggle --------------------------------------------------------


def run_links(html: str) -> list[str]:
    return re.findall(r'<a [^>]*href="(/runs/[^"]+)"[^>]*data-go="r"', html)


async def test_pages_that_show_a_run_carry_the_r_link(client: Client, demo: Demo) -> None:
    rid = demo.repo_id
    key = await pr2_function_key(client, demo)
    pages = {
        f"/repos/{rid}/prs/2": demo.pr2_run,
        f"/repos/{rid}": demo.baseline_run,
        f"/repos/{rid}/functions/{key}?run={demo.pr2_run}": demo.pr2_run,
    }
    for url, run_id in pages.items():
        html = await (await client.get(url)).get_data(as_text=True)
        links = run_links(html)
        assert links and links[0] == f"/runs/{run_id}", (url, links)
    dashboard = await (await client.get("/")).get_data(as_text=True)
    links = run_links(dashboard)
    assert len(links) == 1, "only the newest run of the list"
    found = await client.get(
        f"/repos/{rid}/tests/by-nodeid",
        query_string={"nodeid": "tests/test_refund.py::test_refund_partial", "run": demo.pr1_run},
    )
    assert found.status_code == 302
    test_page = await client.get(found.headers["Location"])
    assert run_links(await test_page.get_data(as_text=True)) == [f"/runs/{demo.pr1_run}"]


async def test_the_function_page_has_the_highlight_toggle(client: Client, demo: Demo) -> None:
    key = await pr2_function_key(client, demo)
    url = f"/repos/{demo.repo_id}/functions/{key}?run={demo.pr2_run}"
    html = await (await client.get(url)).get_data(as_text=True)
    assert_csp_clean(html)
    button = re.search(
        r'<button type="button" class="hl-toggle" data-highlight-toggle[^>]*>.*?</button>', html
    )
    assert button is not None
    assert 'aria-pressed="false"' in button.group(0)
    assert "Highlight: all lines" in button.group(0) and "Highlight: missing only" in button.group(0)
    assert 'class="src-lines' in html or "src-frame" in html, "the h key has lines to act on"
    missing = await (await client.get(url + "&highlight=missing")).get_data(as_text=True)
    assert 'data-highlight="missing"' in missing
    toggle = re.search(r'class="hl-toggle" data-highlight-toggle aria-pressed="(\w+)"', missing)
    assert toggle is not None and toggle.group(1) == "true"


TOGGLE_STATE = re.compile(r'class="hl-toggle" data-highlight-toggle aria-pressed="(\w+)"')


async def test_the_function_toggle_follows_the_scoped_highlight_setting(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No query parameter: the toggle starts in the mode the scoped settings chose (what the layout's
    view meta paints), and a ``?highlight=`` still wins over that choice."""

    async def chosen() -> ViewPrefs:
        prefs = default_view_prefs()
        prefs["view.highlight"] = "missing"
        return prefs

    monkeypatch.setattr(settings_pages, "request_view_prefs", chosen)
    key = await pr2_function_key(client, demo)
    url = f"/repos/{demo.repo_id}/functions/{key}?run={demo.pr2_run}"
    html = await (await client.get(url)).get_data(as_text=True)
    assert 'data-highlight="missing" data-highlight-from="prefs"' in html
    assert TOGGLE_STATE.search(html).group(1) == "true"
    html = await (await client.get(url + "&highlight=all")).get_data(as_text=True)
    assert 'data-highlight="all" data-highlight-from="query"' in html
    assert TOGGLE_STATE.search(html).group(1) == "false"


# -- performance wiring --------------------------------------------------------------------------


async def test_function_pages_share_the_lexed_source(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function hunk renders through the process's ``SourceCache``: a second function page (or
    PR card) of the same file lexes nothing."""
    calls: list[str] = []
    real = render_source_module.lex

    def counting(text: str, path: str = "") -> Any:
        calls.append(path)
        return real(text, path)

    monkeypatch.setattr(render_source_module, "lex", counting)
    key = await pr2_function_key(client, demo)
    url = f"/repos/{demo.repo_id}/functions/{key}?run={demo.pr2_run}"
    assert (await client.get(url)).status_code == 200
    first = len(calls)
    assert first <= 1
    assert (await client.get(url)).status_code == 200
    card = await client.get(f"/repos/{demo.repo_id}/prs/2/functions/{key}?run={demo.pr2_run}")
    assert card.status_code == 200
    assert len(calls) == first, calls


ODD = ["src/a b/c.py", "src/ünï/ç.py", "x/#frag?.py", "p/100%.py", "a+b&c=d;e,f/(x)!$'*@:.py", "~/.-_"]


async def test_url_builder_matches_url_for(app: Quart, demo: Demo) -> None:
    async with app.test_request_context(f"/repos/{demo.repo_id}/map"):
        source = url_builder("files.source", rid=1, path=URL_SLOT, run="run_x")
        zoom = url_builder("repos.map_page", rid=1, mode="gap", path=URL_SLOT, run="run_x", modules="thin")
        tests = url_builder("testpage.test_page", rid=1, test_id=URL_INT_SLOT)
        for value in ODD:
            assert source(value) == url_for("files.source", rid=1, path=value, run="run_x"), value
            assert zoom(value) == url_for(
                "repos.map_page", rid=1, mode="gap", path=value, run="run_x", modules="thin"
            ), value
        for test_id in (1, 42, 10**12):
            assert tests(test_id) == url_for("testpage.test_page", rid=1, test_id=test_id)
        with pytest.raises(ValueError, match="exactly one slot"):
            url_builder("files.source", rid=1, path="x")


async def test_the_map_links_are_unchanged(client: Client, demo: Demo) -> None:
    """Tile links still open the source view (functions at their def line) and directories zoom."""
    html = await (await client.get(f"/repos/{demo.repo_id}/map?run={demo.baseline_run}")).get_data(
        as_text=True
    )
    hrefs = set(re.findall(r'href="([^"]+)"', html))
    files = [h for h in hrefs if h.startswith(f"/repos/{demo.repo_id}/files/")]
    assert any("#L" in h for h in files) and all(f"run={demo.baseline_run}" in h for h in files)
    zooms = [h for h in hrefs if h.startswith(f"/repos/{demo.repo_id}/map?mode=gap&amp;path=")]
    assert zooms and all(f"run={demo.baseline_run}" in h for h in zooms)
