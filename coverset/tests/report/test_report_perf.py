"""The report build's caches change no byte: each source is lexed once, the test pages reuse the
per-test indexes and memos, and the pages equal those of a build without any of it (the whole text
lexed for every page, the test pages recomputed from scratch)."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any

import pytest
from report_helpers import FixtureRun

from coverset.report import build as report_build
from coverset.report import build_report
from coverset.report.load import load_bundle
from coverset.web.render import source as render_source_module
from coverset.web.render.html import breakable
from coverset.web.render.source import CoverageFormatter, continuation_lines, lexer_for


def _files(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


def _uncached_render_source(text: str, facts: Any, *, path: str = "", **options: Any) -> Any:
    """``render_source`` as it was before the cache: Pygments lexes and formats the whole text for
    every page, and the formatter skips the lines outside ``line_range`` itself."""
    from markupsafe import Markup
    from pygments import highlight

    options.pop("cache", None)
    formatter = CoverageFormatter(
        facts, text_lines=text.split("\n"), continuations=continuation_lines(text, path), **options
    )
    return Markup(highlight(text, lexer_for(path), formatter))


def _scanning_test_page(self: Any, entry: Any, index: Any) -> None:
    """The test page as it was before the per-test indexes: every observation of every function of
    a touched file scanned, body lines counted by a pass over the test's lines, no memo."""
    page = entry.page
    nodeid = entry.info.nodeid
    files = []
    for path, lines in sorted(entry.files.items()):
        file_entry = self.files[path]
        statements = file_entry.facts.statements - file_entry.facts.excluded
        files.append((file_entry, len(lines & statements), len(statements)))
    functions = []
    for file_entry, _, _ in files:
        lines = entry.files[file_entry.path]
        for fn in file_entry.functions:
            body = {n for n in lines if fn.region.body_start <= n <= fn.region.end_line}
            observations = [
                o
                for o in fn.view.observations
                if nodeid in o.tests or o.caller == nodeid or o.example_test == nodeid
            ]
            if not body and not observations:
                continue
            functions.append((fn, len(body), observations))
    total = len(functions)
    functions = [
        (fn, n, report_build._param_reach(fn.view, obs))
        for fn, n, obs in functions[: report_build.MAX_LISTED]
    ]
    message = entry.info.message
    if message and len(message) > report_build.MAX_MESSAGE_CHARS:
        message = message[: report_build.MAX_MESSAGE_CHARS] + "\n… (cut)"
    self.render(
        page,
        "test.html",
        kind="test",
        test=entry,
        message=message,
        files=files,
        functions=functions,
        functions_total=total,
        strip=report_build.render_strip,
        data=None,
    )


@pytest.fixture(scope="module")
def cached(fixture_out: FixtureRun, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("cached") / "out"
    build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
    return out


def test_a_build_lexes_each_source_once(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lexed: list[str] = []
    real = render_source_module.lex

    def counting(text: str, path: str = "") -> Any:
        lexed.append(path)
        return real(text, path)

    monkeypatch.setattr(render_source_module, "lex", counting)
    inp = load_bundle(fixture_out.out, repo_root=fixture_out.project)
    result = build_report(inp, tmp_path / "out")
    with_source = sorted(path for path, text in inp.sources.items() if text is not None)
    assert result.pages["functions"] > len(with_source) > 0  # several pages per file
    assert sorted(lexed) == with_source


def test_the_pages_equal_those_of_an_uncached_build(
    cached: Path, fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_build, "render_source", _uncached_render_source)
    monkeypatch.setattr(report_build._Builder, "test_page", _scanning_test_page)
    monkeypatch.setattr(report_build, "MEMO_SIZE", 0)
    monkeypatch.setattr(report_build, "_memo_text", lambda render, limit=0: render)
    plain = tmp_path / "plain"
    build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), plain)
    a, b = _files(cached), _files(plain)
    assert sorted(a) == sorted(b)
    assert [name for name in a if a[name] != b[name]] == []
    assert any(name.startswith("functions/") for name in a) and any(name.startswith("tests/") for name in a)


def test_the_memos_return_what_they_remember() -> None:
    memo = report_build._Recent[int](2)
    made: list[str] = []

    def make(key: str) -> int:
        made.append(key)
        return len(made)

    assert memo.get("a", lambda: make("a")) == 1
    assert memo.get("a", lambda: make("a")) == 1
    memo.get("b", lambda: make("b"))
    memo.get("c", lambda: make("c"))  # evicts "a", the least recently used
    assert len(memo) == 2
    assert memo.get("a", lambda: make("a")) == 4
    assert made == ["a", "b", "c", "a"]
    nodeid = report_build._memo_text(breakable)
    assert nodeid("tests/test_x.py::test_y[a-b]") is nodeid("tests/test_x.py::test_y[a-b]")
    assert nodeid("tests/a.py::t") == breakable("tests/a.py::t")


def test_body_lines_by_bisection_match_a_scan() -> None:
    lines = sorted({1, 2, 5, 8, 9, 10, 40})
    for start in range(0, 45):
        for end in range(0, 45):
            scan = sum(1 for n in lines if start <= n <= end)
            assert max(0, bisect_right(lines, end) - bisect_left(lines, start)) == scan
            assert report_build._within(lines, start, end) == [n for n in lines if start <= n <= end]
