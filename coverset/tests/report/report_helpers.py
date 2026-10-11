"""Helpers shared by the static report tests (a module of its own: ``conftest`` is ambiguous
when several test directories have one)."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


@dataclass(frozen=True)
class FixtureRun:
    """The fixture project copy and its ``--coverset-out`` directory."""

    project: Path
    out: Path


@dataclass(frozen=True)
class DemoData:
    """Where the demo lives and the ids the report tests need."""

    settings: object
    pr1_run: str
    pr2_run: str
    repo_id: int


# -- HTML checks shared by the report tests -------------------------------------------------------

ALLOWED_SCRIPTS = {"report.js", "search-index.js"}


class PageParser(HTMLParser):
    """Collects every start tag with its attributes (and fails on nothing: html.parser is lenient,
    so the checks below look at what it found)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[dict[str, str | None]] = []
        self.data_blocks: list[str] = []
        self._in_data = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.tags.append((tag, values))
        if tag == "script":
            self.scripts.append(values)
            self._in_data = values.get("type") == "application/json"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_data = False

    def handle_data(self, data: str) -> None:
        if self._in_data:
            self.data_blocks.append(data)


def parse_page(html: str) -> PageParser:
    """Parse ``html`` and check the report's script rules: only ``report.js`` and
    ``search-index.js`` by ``src``, plus at most one ``application/json`` data block."""
    parser = PageParser()
    parser.feed(html)
    parser.close()
    inline = [s for s in parser.scripts if not s.get("src")]
    assert all(s.get("type") == "application/json" and s.get("id") == "report-data" for s in inline), inline
    assert len(inline) <= 1
    for script in parser.scripts:
        if script.get("src"):
            assert Path(str(script["src"])).name in ALLOWED_SCRIPTS, script
    return parser


def all_pages(out: Path) -> dict[str, str]:
    """Every ``.html`` file of a report, by path relative to ``out``."""
    return {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in sorted(out.rglob("*.html"))}
