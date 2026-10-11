"""``coverset report`` from a real pytest-coverset bundle (the plugin's fixture project)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from report_helpers import FixtureRun, all_pages, parse_page

from coverset.facts.convert import read_python_bundle
from coverset.report import MARKER, OutDirError, build_report
from coverset.report.load import load_bundle
from coverset.report.output import hashed_name

CORE = "src/fixturepkg/core.py"
VALIDATE = "tests/test_core.py::test_validate_email[alice@example.com]"


def data_block(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert match, "no data block"
    return json.loads(match.group(1))


@pytest.fixture(scope="module")
def report(fixture_out: FixtureRun, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("report") / "out"
    build_report(load_bundle(fixture_out.out), out)
    return out


def test_pages_cover_every_file_function_and_test(report: Path, fixture_out: FixtureRun) -> None:
    bundle = read_python_bundle(fixture_out.out)
    pages = all_pages(report)
    files = [p for p in pages if p.startswith("files/")]
    functions = [p for p in pages if p.startswith("functions/")]
    tests = [p for p in pages if p.startswith("tests/")]
    assert sorted(files) == sorted(f"files/{path}.html" for path in bundle.static)
    assert len(tests) == len({t.nodeid for t in bundle.tests} | {c.nodeid for c in bundle.collected})
    assert len(functions) == 18  # every function and method of the four modules
    assert {"index.html", "about.html"} <= set(pages)
    for name in ("report.css", "report.js", "search-index.js", MARKER):
        assert (report / name).is_file()


def test_every_page_parses_and_has_no_inline_script(report: Path) -> None:
    for name, html in all_pages(report).items():
        parser = parse_page(html)
        assert any(tag == "body" and attrs.get("data-ready") == "0" for tag, attrs in parser.tags), name
        for _, attrs in parser.tags:
            assert not any(key.startswith("on") for key in attrs), (name, attrs)
            assert "style" not in attrs, (name, attrs)
        for block in parser.data_blocks:
            json.loads(block)


def test_index_totals_match_the_bundle(report: Path, fixture_out: FixtureRun) -> None:
    bundle = read_python_bundle(fixture_out.out)
    statements = sum(len(s.statements) for s in bundle.static.values())
    executed: dict[str, set[int]] = {}
    for by_path in bundle.coverage.values():
        for path, lines in by_path.items():
            executed.setdefault(path, set()).update(lines)
    run = sum(len(set(s.statements) & executed.get(p, set())) for p, s in bundle.static.items())
    branches = sum(len(s.arcs_possible) for s in bundle.static.values())
    taken = sum(len(set(s.arcs_possible) & set(bundle.run_arcs.get(p, ()))) for p, s in bundle.static.items())
    html = (report / "index.html").read_text(encoding="utf-8")

    def stat(name: str) -> str:
        match = re.search(
            rf'data-stat="{name}"><div class="value">([^<]*)</div><div class="label">(.*?)</div>', html
        )
        assert match, name
        return match.group(1) + " | " + match.group(2)

    assert stat("statements").startswith(f"{statements} |")
    assert f"{run} of {statements}" in stat("run")
    assert f"{taken} of {branches} taken" in stat("branches")
    assert stat("tests").startswith(f"{len(bundle.tests)} |")
    assert f">{len(bundle.tests)} passed<" in stat("tests")
    assert stat("functions").startswith("18 |")
    # one row per file in the sortable file table
    assert html.count('<tr data-path="') == len(bundle.static)


def test_known_uncovered_and_partial_lines(report: Path) -> None:
    html = (report / f"files/{CORE}.html").read_text(encoding="utf-8")
    assert re.search(r'<div class="ln mis" id="L17" data-line="17">', html)
    assert re.search(r'<div class="ln par[^"]*" id="L16" data-line="16">', html)
    assert "never jumped to line 17" in html


def test_per_line_tests_list(report: Path, fixture_out: FixtureRun) -> None:
    bundle = read_python_bundle(fixture_out.out)
    html = (report / f"files/{CORE}.html").read_text(encoding="utf-8")
    data = data_block(html)
    assert data["path"] == CORE
    # the line test count in the gutter equals the tests whose lines include it, from the bundle
    expected = sorted(
        nodeid
        for nodeid, by_path in bundle.coverage.items()
        if nodeid != "<import>" and 16 in by_path.get(CORE, ())
    )
    listed = sorted(test[0] for test in data["tests"] if 16 in test[3])
    assert listed == expected and expected
    match = re.search(
        r'id="L16" data-line="16">.*?<button type="button" class="gt"[^>]*>(\d+)</button>', html
    )
    assert match and int(match.group(1)) == len(expected)
    # each entry links to its test page by hash
    for nodeid, _, digest, _ in data["tests"]:
        assert digest == hashed_name(nodeid)
        assert (report / "tests" / f"{digest}.html").is_file()
    assert data["import"], "module-level lines ran at import time"


def test_test_page_lists_touched_functions(report: Path) -> None:
    html = (report / "tests" / f"{hashed_name(VALIDATE)}.html").read_text(encoding="utf-8")
    assert '<tr data-func="validate_email">' in html
    assert f'href="../files/{CORE}.html#lens={hashed_name(VALIDATE)}"' in html
    assert 'data-outcome="passed"' in html
    # the input classes it passed in for the email parameter (plain is alice@example.com's class)
    row = html.split('<tr data-func="validate_email">', 1)[1].split("</tr>", 1)[0]
    assert '<div class="reach-row" data-param="email">' in row
    assert '<span class="chip">plain</span>' in row


def test_function_page_shows_hollow_classes_and_suggestion(report: Path) -> None:
    pages = all_pages(report)
    name, html = next(
        (n, h)
        for n, h in pages.items()
        if n.startswith("functions/") and "<code>validate_email</code></h1>" in h
    )
    assert "email:uppercase" in html
    assert "never exercised" in html
    assert "Suggested Hypothesis test" in html and "@given(" in html
    assert 'class="copy" data-copy-target="#suggest-' in html
    # the capped function says so instead of "never exercised"
    hot = next(h for n, h in pages.items() if n.startswith("functions/") and "<code>hot</code></h1>" in h)
    assert "not observed in 66 sampled calls (1 test hit the sample cap)" in hot
    assert name.endswith(".html")


def test_regeneration_is_idempotent(fixture_out: FixtureRun, tmp_path: Path) -> None:
    out = tmp_path / "out"
    inp = load_bundle(fixture_out.out)
    build_report(inp, out)
    first = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    build_report(load_bundle(fixture_out.out), out)
    second = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert first == second


def test_out_dir_is_refused_unless_it_is_ours(fixture_out: FixtureRun, tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(OutDirError):
        build_report(load_bundle(fixture_out.out), precious)
    assert (precious / "notes.txt").read_text(encoding="utf-8") == "keep me"
    empty = tmp_path / "empty"
    empty.mkdir()
    build_report(load_bundle(fixture_out.out), empty)
    (empty / "stale.html").write_text("old", encoding="utf-8")
    build_report(load_bundle(fixture_out.out), empty)  # ours: emptied and rewritten
    assert not (empty / "stale.html").exists()


def test_cli_from_the_out_dir_and_from_the_archive(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.cli import cli

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
    runner = CliRunner()
    copy = tmp_path / "bundle"
    shutil.copytree(fixture_out.out, copy, ignore=shutil.ignore_patterns("report"))
    result = runner.invoke(cli, ["report", str(copy), "--repo-root", str(fixture_out.project)])
    assert result.exit_code == 0, result.output
    index = copy / "report" / "index.html"
    assert result.stdout.strip().splitlines()[-1] == str(index.resolve())
    assert index.is_file() and not opened
    # an archive: default out is ./coverset-report; --open opens the index once
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        [
            "report",
            str(copy / "bundle.tar.gz"),
            "--repo-root",
            str(fixture_out.project),
            "--open",
            "--title",
            "T<b>",
        ],
    )
    assert result.exit_code == 0, result.output
    built = tmp_path / "coverset-report" / "index.html"
    assert built.is_file()
    assert opened == [built.resolve().as_uri()]
    html = built.read_text(encoding="utf-8")
    assert "T&lt;b&gt;" in html and "T<b>" not in html
    assert f'href="files/{CORE}.html"' in html


def test_cli_usage_errors(tmp_path: Path) -> None:
    from coverset.cli import cli

    runner = CliRunner()
    assert runner.invoke(cli, ["report"]).exit_code == 2
    missing = runner.invoke(cli, ["report", str(tmp_path)])
    assert missing.exit_code == 1 and "missing coverage.sqlite" in missing.output


def test_missing_source_file_shows_its_summary_only(fixture_out: FixtureRun, tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(fixture_out.project, project, ignore=shutil.ignore_patterns("out", "__pycache__"))
    (project / CORE).unlink()
    out = tmp_path / "out"
    build_report(load_bundle(fixture_out.out, repo_root=project), out)
    html = (out / f"files/{CORE}.html").read_text(encoding="utf-8")
    assert "Source not available." in html
    assert '<div class="src-lines">' not in html
    assert "<dt>Never ran</dt><dd>17</dd>" in html
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "source not available" in index
    # no function pages for a file without source (its regions come from the source)
    titles = [p.read_text(encoding="utf-8") for p in (out / "functions").glob("*.html")]
    assert titles and not any("<code>validate_email</code></h1>" in html for html in titles)


def test_sources_are_never_read_outside_the_repo_root(fixture_out: FixtureRun, tmp_path: Path) -> None:
    from coverset.report.facts import read_sources

    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    (root / "src" / "link.py").symlink_to(tmp_path / "secret.py")
    found = read_sources(
        root, ["src/a.py", "../secret.py", str(tmp_path / "secret.py"), "src/link.py", "src/nope.py"]
    )
    assert found["src/a.py"] == "x = 1\n"
    assert found["../secret.py"] is None
    assert found[str(tmp_path / "secret.py")] is None
    assert found["src/link.py"] is None
    assert found["src/nope.py"] is None
