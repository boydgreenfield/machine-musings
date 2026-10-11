"""``coverset report --run`` on the local demo: the facts store and the mirror, like the pages."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from report_helpers import DemoData, all_pages, parse_page

from coverset.facts.store import FactsStore
from coverset.report import build_report
from coverset.report.load import load_run
from coverset.report.output import hashed_name

EMAILCHECK = "src/ledger/accounts/emailcheck.py"
PARSE = "src/ledger/durations/parse.py"


@pytest.fixture(scope="module")
def pr2(demo: DemoData, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("report-pr2") / "out"
    inp = asyncio.run(load_run(demo.settings, demo.pr2_run))
    build_report(inp, out)
    return out


def test_pages_and_totals_match_the_store(pr2: Path, demo: DemoData) -> None:
    settings = demo.settings
    store = FactsStore(settings.facts_dir)  # type: ignore[attr-defined]
    file_cov = store.read_table(demo.repo_id, demo.pr2_run, "file_cov").to_pylist()
    tests = {r["nodeid"] for r in store.read_table(demo.repo_id, demo.pr2_run, "tests_dim").to_pylist()}
    tests.discard("<import>")
    pages = all_pages(pr2)
    assert len([p for p in pages if p.startswith("files/")]) == len(file_cov)
    assert len([p for p in pages if p.startswith("tests/")]) == len(tests)
    functions = store.read_table(demo.repo_id, demo.pr2_run, "functions").to_pylist()
    callables = [f for f in functions if f["kind"] in ("function", "method")]
    assert len([p for p in pages if p.startswith("functions/")]) == len(callables)
    index = pages["index.html"]
    statements = sum(r["statements"] for r in file_cov)
    covered = sum(r["covered"] for r in file_cov)
    assert re.search(rf'data-stat="statements"><div class="value">{statements}</div>', index)
    assert f"{covered} of {statements}" in index
    branches = sum(r["branches"] for r in file_cov)
    taken = sum(r["branches_covered"] for r in file_cov)
    assert f"{taken} of {branches} taken" in index
    assert f">{len(tests)} passed<" in index  # PR #2 passes
    for name, html in pages.items():
        parser = parse_page(html)
        assert not any(k.startswith("on") for _, a in parser.tags for k in a), name


def test_validate_email_multi_label_domain_is_hollow(pr2: Path) -> None:
    pages = all_pages(pr2)
    page = next(
        h for n, h in pages.items() if n.startswith("functions/") and "<code>validate_email</code></h1>" in h
    )
    row = re.search(
        r'<tr data-cell-index="\d+" class="is-hollow">\s*<td><code>email:multi-label-domain</code>', page
    )
    assert row, "email:multi-label-domain is a hollow class of validate_email"
    assert 'aria-label="email:multi-label-domain: never exercised"' in page  # the grid square
    source = pages[f"files/{EMAILCHECK}.html"]
    assert 'class="chip chip-hollow" title="email:multi-label-domain: never exercised"' in source
    assert "validate_email" in pages["index.html"].split('id="gaps"', 1)[1]


def test_line_tests_and_lens_data(pr2: Path, demo: DemoData) -> None:
    from coverset.web.facts_view import run_facts

    facts = run_facts(FactsStore(demo.settings.facts_dir), demo.repo_id, demo.pr2_run)  # type: ignore[attr-defined]
    expected = facts.tests_in_range(PARSE, 18, 18)
    html = (pr2 / f"files/{PARSE}.html").read_text(encoding="utf-8")
    block = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert block
    data = json.loads(block.group(1))
    assert sorted(t[0] for t in data["tests"] if 18 in t[3]) == expected
    assert re.search(rf'id="L18" data-line="18">.*?class="gt"[^>]*>{len(expected)}</button>', html)
    assert '<select id="lens">' in html
    # the test page links back to the file with its lens preselected
    test = expected[0]
    page = (pr2 / "tests" / f"{hashed_name(test)}.html").read_text(encoding="utf-8")
    assert f"files/{PARSE}.html#lens={hashed_name(test)}" in page
    assert '<tr data-func="parse_duration">' in page


def test_cli_run(demo: DemoData, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coverset.cli import cli

    settings = demo.settings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(settings.data_dir))  # type: ignore[attr-defined]
    monkeypatch.setenv("COVERSET_DATABASE_URL", str(settings.database_url))  # type: ignore[attr-defined]
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
    result = CliRunner().invoke(cli, ["report", "--run", demo.pr1_run, "--out", str(tmp_path / "pr1")])
    assert result.exit_code == 0, result.output
    index = tmp_path / "pr1" / "index.html"
    assert result.stdout.strip().splitlines()[-1] == str(index.resolve())
    assert not opened
    html = index.read_text(encoding="utf-8")
    assert "PR #1" in html and "failed" in html
    # PR #1 fails: the failing test's message is on the index and on its page
    assert '<details class="msg">' in html
    missing = CliRunner().invoke(cli, ["report", "--run", "run_nope", "--out", str(tmp_path / "x")])
    assert missing.exit_code == 1 and "no run" in missing.output
    named = CliRunner().invoke(
        cli,
        ["report", "--run", demo.pr2_run, "--repo", "coverset-demo/ledger", "--out", str(tmp_path / "pr2")],
    )
    assert named.exit_code == 0, named.output
    wrong = CliRunner().invoke(
        cli, ["report", "--run", demo.pr2_run, "--repo", "nope/nope", "--out", str(tmp_path / "y")]
    )
    assert wrong.exit_code == 1 and "unknown repository" in wrong.output
    assert CliRunner().invoke(cli, ["report", "--repo", "x", str(tmp_path)]).exit_code == 2
