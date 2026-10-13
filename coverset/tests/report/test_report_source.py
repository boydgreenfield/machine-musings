"""The report's file pages: syntax tokens and multi-line statements in a TypeScript source, the
summary line (synthetic neutral bundle and the plugin's fixture project), and the map's status line."""

from __future__ import annotations

import hashlib
import json
import re
from html import unescape
from pathlib import Path

import pytest
from report_helpers import FixtureRun, all_pages, parse_page

from coverset.report import build_report
from coverset.report.load import load_bundle, neutral_reader

PATH = "src/email.ts"
T_OK = "src/email.test.ts > validateEmail > accepts plain"
T_BAD = "src/email.test.ts > validateEmail > rejects garbage"
SOURCE = """export function validateEmail(email: string, role: Role): string {
  if (email.includes("@")) {
    return "ok";
  }
  const message = format(
    "bad </span>\\"onmouseover=x",
    email,
  );
  return message;
}
const re = /a+"b/g;
export function unused(a: number): number {
  return compute(
    a,
    2,
  );
}
"""


def _key(path: str, qualname: str, ordinal: int) -> str:
    return hashlib.blake2b(f"{path}\0{qualname}\0{ordinal}".encode(), digest_size=8).hexdigest()


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _summary(html: str) -> str:
    found = re.search(r'<span class="src-summary" data-file-stats>(.*?)</span></span>', html, re.S)
    assert found, "the summary line"
    return unescape(re.sub(r"<[^>]+>", "", found.group(1)))


def _test(nodeid: str, outcome: str) -> dict:
    return {"id": nodeid, "file": "src/email.test.ts", "line": 3, "outcome": outcome, "duration_s": 0.002,
            "failed_phase": None, "fail_sig": None, "message": None, "markers": [], "invariants": [],
            "property": False, "start": None, "stop": None}  # fmt: skip


@pytest.fixture
def ts_report(tmp_path: Path) -> Path:
    if neutral_reader() is None:
        pytest.skip("this build has no neutral bundle reader (coverset.lang.read_any_bundle)")
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {"format": "coverset-neutral", "version": 1, "language": "typescript",
             "producer": {"name": "coverset-vitest", "version": "0.1.1"}, "created_at": "2026-10-11T19:04:05Z",
             "shard": None, "meta": {"tracer": "v8"}}
        ),
        encoding="utf-8",
    )  # fmt: skip
    _jsonl(root / "tests.jsonl", [_test(T_OK, "passed"), _test(T_BAD, "passed")])
    _jsonl(
        root / "files.jsonl",
        [{"path": PATH, "n_lines": 17, "statements": [1, 2, 3, 5, 9, 11, 12, 13], "excluded": [],
          "branches": [[2, 3], [2, 5]]}],
    )  # fmt: skip
    _jsonl(
        root / "coverage.jsonl",
        [
            {"test": "<import>", "file": PATH, "lines": [1, 11, 12]},
            {"test": T_OK, "file": PATH, "lines": [2, 5, 9]},
            {"test": T_BAD, "file": PATH, "lines": [2, 5]},
        ],
    )
    _jsonl(root / "arcs.jsonl", [{"file": PATH, "arcs": [[2, 5]]}])
    _jsonl(
        root / "functions.jsonl",
        [{"key": _key(PATH, "validateEmail", 0), "path": PATH, "qualname": "validateEmail", "ordinal": 0,
          "def_line": 1, "body_start": 2, "end_line": 10, "body_hash": "a1", "sig_hash": "b2",
          "params": ["email", "role"], "kind": "function",
          "types": {"email": {"kind": "str"}, "role": {"kind": "enum", "name": "Role", "members": ["admin", "member"]},
                    "return": {"kind": "str"}}},
         {"key": _key(PATH, "unused", 0), "path": PATH, "qualname": "unused", "ordinal": 0,
          "def_line": 12, "body_start": 13, "end_line": 17, "body_hash": "a3", "sig_hash": "b4",
          "params": ["a"], "kind": "function", "types": {"a": {"kind": "int"}}}],
    )  # fmt: skip
    func = [PATH, "validateEmail", 1]
    _jsonl(
        root / "shapes.jsonl",
        [{"kind": "obs", "func": func, "param": "email", "shape": "str|ascii|kind:email/plain|len:11-100",
          "caller": {"test": T_OK}, "samples": 2, "tests": [T_OK, T_BAD], "truncated": False,
          "example": "'bob@acme.com'", "example_test": T_OK},
         {"kind": "sampling", "func": func, "calls": 2, "tests": [T_OK, T_BAD], "truncated": False, "cap": None}],
    )  # fmt: skip
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / PATH).write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"
    build_report(load_bundle(root, repo_root=checkout), out)
    return out


def test_a_typescript_page_has_tokens_blocks_and_a_summary(ts_report: Path) -> None:
    pages = all_pages(ts_report)
    for html in pages.values():
        parse_page(html)
    page = pages[f"files/{PATH}.html"]
    assert 'class="tk-kd">function</span>' in page and 'class="tk-kt">string</span>' in page
    assert (
        'class="tk-sr">' in page and '<span class="sr">/a+' not in page
    )  # a regex is not screen-reader text
    assert "&lt;/span&gt;" in page
    blocks = dict(
        (int(n), cls) for cls, n in re.findall(r'<div class="(ln[^"]*)" id="L(\d+)" data-line="\d+"', page)
    )
    assert blocks[5] == "ln run" and [blocks[n] for n in (6, 7, 8)] == ["ln run cont"] * 3
    assert blocks[13] == "ln mis" and [blocks[n] for n in (14, 15, 16)] == ["ln mis cont"] * 3
    assert blocks[3] == "ln mis" and blocks[4] == "ln"  # a block's closing brace is no continuation
    assert 'id="L14" data-line="14" data-first="13">' in page
    text = _summary(page)
    # 8 statements, 6 ran (1, 2, 5, 9, 11, 12); 1 of 2 branch arcs taken; email's classes
    found = re.fullmatch(
        r"lines 75\.0% \(6 of 8\) · branches 50\.0% \(1 of 2\) · input classes (\d+) of (\d+) \(([\d.]+)%\)"
        r" · gap (\d+) pts · 2 tests",
        text,
    )
    assert found, text
    observed, expected = int(found.group(1)), int(found.group(2))
    assert 0 < observed < expected
    assert int(found.group(4)) == round(max(0.0, 75.0 - 100.0 * observed / expected))


def test_the_index_map_has_a_status_line(ts_report: Path) -> None:
    index = (ts_report / "index.html").read_text(encoding="utf-8")
    box = index.split("data-tm-box>", 1)[1]
    assert box.lstrip().startswith('<p class="tm-status" data-tm-status aria-hidden="true">')
    assert '<span class="tm-status-idle">hover a tile</span>' in box
    infos = [
        unescape(i) for i in re.findall(r'class="tm-leaf-link" aria-label="[^"]*" data-info="([^"]*)"', box)
    ]
    assert any(i.startswith(f"{PATH}::validateEmail · ") and "input classes" in i for i in infos)
    assert any(i.startswith(f"{PATH}::unused · ") and "never called by a test" in i for i in infos)


def test_the_fixture_file_summary(fixture_out: FixtureRun, tmp_path: Path) -> None:
    from coverset.facts.convert import read_python_bundle

    out = tmp_path / "out"
    build_report(load_bundle(fixture_out.out, repo_root=fixture_out.project), out)
    bundle = read_python_bundle(fixture_out.out)
    for path, static in bundle.static.items():
        html = (out / "files" / f"{path}.html").read_text(encoding="utf-8")
        text = _summary(html)
        executed = set()
        tests = set()
        for nodeid, by_path in bundle.coverage.items():
            lines = set(by_path.get(path, ()))
            executed |= lines
            if lines and nodeid != "<import>":
                tests.add(nodeid)
        statements = set(static.statements) - set(static.excluded)
        ran = len(statements & executed)
        if not statements:
            assert text.startswith("no statements · "), text
            continue
        assert text.startswith(
            f"lines {100.0 * ran / len(statements):.1f}% ({ran} of {len(statements)}) · "
        ), text
        arcs = set(static.arcs_possible)
        if arcs:
            taken = len(arcs & set(bundle.run_arcs.get(path, ())))
            assert f" · branches {100.0 * taken / len(arcs):.1f}% ({taken} of {len(arcs)}) · " in text, text
        else:
            assert "branches" not in text
        assert text.endswith(f" · {len(tests)} test{'' if len(tests) == 1 else 's'}"), text
        assert ("gap" in text) == ("no input classes" not in text), text
