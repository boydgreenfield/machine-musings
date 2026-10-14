"""The static report's Economy page: built from the bundle and its sources, linked from the nav and
an index tile, with the deselect argument file; deterministic, escaped, and never fatal."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from report_helpers import DemoData, FixtureRun, all_pages, parse_page

from coverset.lang.base import NeutralBundle, StaticFile, TestRow
from coverset.report import build_report
from coverset.report.economy import (
    DESELECT_FILE,
    economy_css,
    economy_for_input,
    summary_text,
    view_from_store,
)
from coverset.report.load import input_from_bundle, load_bundle, load_run


@pytest.fixture(scope="module")
def built(fixture_out: FixtureRun, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("economy-report") / "out"
    result = build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
    assert result.pages["economy"] == 1
    return out


def test_the_page_tile_and_nav_exist(built: Path, fixture_out: FixtureRun) -> None:
    view = economy_for_input(load_bundle(fixture_out.out, repo_root=fixture_out.project))
    assert view is not None and view.totals is not None
    pages = all_pages(built)
    page = pages["economy.html"]
    parse_page(page)
    assert 'aria-current="page">Economy</a>' in page
    assert '<link rel="stylesheet" href="economy.css">' in page
    assert '<svg class="eco-hist"' in page and 'id="eco-tests"' in page and 'id="eco-minimal"' in page
    t = view.totals
    assert f'<div class="value">{t.minimal_set_size} <span class="of">/ {t.tests}</span>' in page
    for name, html in pages.items():
        depth = "../" * name.count("/")
        assert f'href="{depth}economy.html"' in html, name  # every page links it
    tile = re.search(r"data-economy-summary>([^<]+)<", pages["index.html"])
    assert tile and tile.group(1) == summary_text(view)
    assert "Test economy:" in pages["index.html"]
    assert (built / "economy.css").read_text(encoding="utf-8") == economy_css()
    assert ".eco-hist" in economy_css()


def test_the_leverage_table_is_sortable_and_links_the_test_pages(built: Path) -> None:
    page = (built / "economy.html").read_text(encoding="utf-8")
    table = page[page.index('id="eco-tests"') :]
    assert '<table class="sortable eco-tests-table">' in table
    assert table.count('data-sort="num"') == 6
    assert re.search(r'<a class="nodeid" href="tests/[0-9a-f]{16}\.html">', table)
    for match in re.finditer(r'href="(tests/[^"]+\.html)"', page):
        assert (built / match.group(1)).is_file()


def test_the_deselect_file_lists_the_removable_candidates(built: Path, fixture_out: FixtureRun) -> None:
    view = economy_for_input(load_bundle(fixture_out.out, repo_root=fixture_out.project))
    assert view is not None
    lines = (built / DESELECT_FILE).read_text(encoding="utf-8").splitlines()
    assert lines == sorted(f"--deselect={r.nodeid}" for r in view.tests if r.removable)
    assert lines  # the fixture has an identical-flow pair
    assert 'href="economy-deselect.txt" download' in (built / "economy.html").read_text(encoding="utf-8")


def test_two_builds_write_the_same_bytes(fixture_out: FixtureRun, tmp_path: Path) -> None:
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
        outs.append(out)
    for name in ("economy.html", "economy.css", DESELECT_FILE, "index.html"):
        assert (outs[0] / name).read_bytes() == (outs[1] / name).read_bytes(), name


def test_a_compare_base_shows_the_tax_without_inventing_statements(
    fixture_out: FixtureRun, tmp_path: Path
) -> None:
    inp = load_bundle(fixture_out.out, repo_root=fixture_out.project)
    base = load_bundle(fixture_out.out, repo_root=fixture_out.project)
    build_report(inp, tmp_path / "out", base=base)
    page = (tmp_path / "out" / "economy.html").read_text(encoding="utf-8")
    assert 'id="test-tax"' in page
    assert "statements not measured" in page  # the base's own test files were not read
    assert "over threshold" not in page


def test_hostile_test_ids_are_escaped_and_never_split_the_deselect_file(tmp_path: Path) -> None:
    hostile = "tests/test_a.py::test_x[<script>alert(1)</script>]"
    newline = "tests/test_a.py::test_y[a\nb]"
    plain = "tests/test_a.py::test_z"
    tests = tuple(
        TestRow(n, "tests/test_a.py", "passed", 0.01, None, None, None, None, None)
        for n in (hostile, newline, plain)
    )
    bundle = NeutralBundle(
        meta={},
        collected=(),
        tests=tests,
        coverage={hostile: {"src/m.py": (1, 2)}, newline: {"src/m.py": (1, 2)}, plain: {"src/m.py": (1, 2)}},
        static={"src/m.py": StaticFile("src/m.py", 2, (1, 2), (), ())},
        run_arcs={},
        shapes=(),
        sampling=(),
        durations={},
    )
    out = tmp_path / "out"
    build_report(
        input_from_bundle(bundle, source=tmp_path / "bundle", kind="python", repo_root=tmp_path), out
    )
    page = (out / "economy.html").read_text(encoding="utf-8")
    parse_page(page)
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    lines = (out / DESELECT_FILE).read_text(encoding="utf-8").splitlines()
    assert all(line.startswith("--deselect=tests/test_a.py::test_") for line in lines)
    assert not any(line.endswith("[a") or line == "b]" for line in lines)


def test_a_failing_economy_leaves_the_report_without_its_page(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("analysis exploded")

    monkeypatch.setattr("coverset.report.economy.economy_for_input", boom)
    result = build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), tmp_path / "out")
    assert "economy" not in result.pages
    assert not (tmp_path / "out" / "economy.html").exists()
    assert "economy.html" not in (tmp_path / "out" / "index.html").read_text(encoding="utf-8")


def test_a_store_run_reads_the_stored_economy(demo: DemoData, tmp_path: Path) -> None:
    from coverset.facts.store import FactsStore

    inp = asyncio.run(load_run(demo.settings, demo.pr1_run))
    build_report(inp, tmp_path / "out")
    page = (tmp_path / "out" / "economy.html").read_text(encoding="utf-8")
    view = view_from_store(FactsStore(demo.settings.facts_dir), demo.repo_id, demo.pr1_run)
    assert view is not None and view.totals is not None and view.tax is not None
    assert f"against run {view.tax.base_run_id}" in page
    assert f'<div class="value">{view.totals.removable_candidates}</div>' in page
