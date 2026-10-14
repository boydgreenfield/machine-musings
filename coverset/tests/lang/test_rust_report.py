"""The Rust fixture through the public CLI: ``coverset rust convert``, ``bundle validate``, ``report``.

The sources are copied out of the checkout first: ``coverset report --repo-root`` reads only files
git tracks when the root is a git work tree, and this test must not depend on whether the fixture
has been committed yet.
"""

from __future__ import annotations

import json
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from click.testing import CliRunner

from coverset.cli import cli

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "bundles" / "neutral-rust"


class _Scripts(HTMLParser):
    """Collects ``<script>`` tags (the report's CSP: no inline code, only JSON data blocks)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts.append(dict(attrs))
        self.handlers += [name for name, _ in attrs if name.startswith("on")]


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    tmp = tmp_path_factory.mktemp("rust-report")
    repo = tmp / "repo"
    shutil.copytree(FIXTURE / "repo", repo)
    runner = CliRunner()
    converted = runner.invoke(
        cli, ["rust", "convert", str(FIXTURE / "capture"), "--out", str(tmp / "neutral"), "--root", str(repo)]
    )
    assert converted.exit_code == 0, converted.output
    assert "16 tests, 12 functions" in converted.output and "coverset report" in converted.output
    valid = runner.invoke(cli, ["bundle", "validate", str(tmp / "neutral")])
    assert valid.exit_code == 0 and "valid" in valid.output, valid.output
    built = runner.invoke(
        cli, ["report", str(FIXTURE / "bundle.tar.gz"), "--repo-root", str(repo), "--out", str(tmp / "out")]
    )
    assert built.exit_code == 0, built.output
    out = tmp / "out"
    return {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in out.rglob("*.html")}


def test_every_page_is_written_and_keeps_the_csp_rules(report: dict[str, str]) -> None:
    assert sum(n.startswith("functions/") for n in report) == 12
    assert sum(n.startswith("files/") for n in report) == 5
    assert sum(n.startswith("tests/") for n in report) == 16
    for name, html in report.items():
        parser = _Scripts()
        parser.feed(html)
        inline = [s for s in parser.scripts if not s.get("src")]
        assert all(s.get("type") == "application/json" for s in inline), name
        assert parser.handlers == [], name


def test_rust_sources_regions_and_tests_show(report: dict[str, str]) -> None:
    money = report["files/src/money.rs.html"]
    assert "Source not available." not in money
    assert "allocate_cents" in money and "Ledger::post" in money
    post = next(
        h for n, h in report.items() if n.startswith("functions/") and "<code>Ledger::post</code>" in h
    )
    assert "ledger::ledger_flow a_failed_transfer_reports_the_shortfall" in post
    assert "coverset-rust" in report["about.html"]
    index = report["index.html"]
    assert "16" in index and "skipped" in index
    fmt = [h for n, h in report.items() if n.startswith("functions/") and "fmt::Display" in h]
    assert fmt and "&lt;LedgerError as fmt::Display&gt;::fmt" in fmt[0]  # escaped, not markup


def test_the_convert_command_refuses_a_bad_capture(tmp_path: Path) -> None:
    (tmp_path / "capture.json").write_text(json.dumps({"format": "nope"}))
    result = CliRunner().invoke(cli, ["rust", "convert", str(tmp_path), "--out", str(tmp_path / "o")])
    assert result.exit_code == 1 and "not a coverset Rust capture" in result.output


def test_run_without_a_cargo_toml_is_a_clean_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["rust", "run", "--crate", str(tmp_path)])
    assert result.exit_code == 1 and "has no Cargo.toml" in result.output
