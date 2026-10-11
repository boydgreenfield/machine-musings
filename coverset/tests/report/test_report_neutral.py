"""A neutral bundle (``manifest.json``) from another language: regions and types come from the bundle.

The source of ``src/email.ts`` is not on disk, so the file page shows its coverage summary only and
the function page takes its region from ``functions.jsonl`` and its declared types from the
descriptors there (``role`` is an enum, ``email`` a string).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from report_helpers import all_pages, parse_page

from coverset.report import build_report
from coverset.report.load import load_bundle, neutral_reader

PATH = "src/email.ts"
T_OK = "src/email.test.ts > validateEmail > accepts plain"
T_BAD = "src/email.test.ts > validateEmail > rejects <garbage>"


def key(path: str, qualname: str, ordinal: int) -> str:
    return hashlib.blake2b(f"{path}\0{qualname}\0{ordinal}".encode(), digest_size=8).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def neutral_dir(tmp_path: Path) -> Path:
    if neutral_reader() is None:
        pytest.skip("this build has no neutral bundle reader (coverset.lang.read_any_bundle)")
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format": "coverset-neutral",
                "version": 1,
                "language": "typescript",
                "producer": {"name": "jest-coverset", "version": "0.0.1"},
                "created_at": "2026-10-11T19:04:05Z",
                "shard": None,
                "meta": {"tracer": "v8"},
            }
        ),
        encoding="utf-8",
    )
    write_jsonl(
        root / "tests.jsonl",
        [
            {"id": T_OK, "file": "src/email.test.ts", "line": 3, "outcome": "passed", "duration_s": 0.002,
             "failed_phase": None, "fail_sig": None, "message": None, "markers": [], "invariants": [],
             "property": False, "start": None, "stop": None},
            {"id": T_BAD, "file": "src/email.test.ts", "line": 9, "outcome": "failed", "duration_s": 0.004,
             "failed_phase": "call", "fail_sig": "0123456789abcdef", "message": "expected <b>throw</b>",
             "markers": [], "invariants": [], "property": False, "start": None, "stop": None},
        ],
    )  # fmt: skip
    write_jsonl(
        root / "files.jsonl",
        [
            {
                "path": PATH,
                "n_lines": 9,
                "statements": [1, 2, 3, 5, 6, 8],
                "excluded": [],
                "branches": [[2, 3], [2, 5]],
            }
        ],
    )
    write_jsonl(
        root / "coverage.jsonl",
        [
            {"test": "<import>", "file": PATH, "lines": [1, 8]},
            {"test": T_OK, "file": PATH, "lines": [2, 5, 6]},
            {"test": T_BAD, "file": PATH, "lines": [2, 5]},
        ],
    )
    write_jsonl(root / "arcs.jsonl", [{"file": PATH, "arcs": [[2, 5]]}])
    write_jsonl(
        root / "functions.jsonl",
        [
            {"key": key(PATH, "validateEmail", 0), "path": PATH, "qualname": "validateEmail", "ordinal": 0,
             "def_line": 1, "body_start": 2, "end_line": 6, "body_hash": "a1", "sig_hash": "b2",
             "params": ["email", "role"], "kind": "function",
             "types": {"email": {"kind": "str"},
                       "role": {"kind": "enum", "name": "Role", "members": ["admin", "member"]},
                       "return": {"kind": "str"}}},
        ],
    )  # fmt: skip
    func = [PATH, "validateEmail", 1]
    write_jsonl(
        root / "shapes.jsonl",
        [
            {"kind": "obs", "func": func, "param": "email", "shape": "str|ascii|kind:email/plain|len:11-100",
             "caller": {"test": T_OK}, "samples": 2, "tests": [T_OK, T_BAD], "truncated": False,
             "example": "'bob@acme.com'", "example_test": T_OK},
            {"kind": "sampling", "func": func, "calls": 2, "tests": [T_OK, T_BAD], "truncated": False, "cap": None},
        ],
    )  # fmt: skip
    return root


def test_neutral_bundle_report(neutral_dir: Path, tmp_path: Path) -> None:
    empty_root = tmp_path / "checkout"
    empty_root.mkdir()
    out = tmp_path / "out"
    build_report(load_bundle(neutral_dir, repo_root=empty_root), out)
    pages = all_pages(out)
    for html in pages.values():
        parse_page(html)
    source = pages[f"files/{PATH}.html"]
    assert "Source not available." in source
    functions = [h for n, h in pages.items() if n.startswith("functions/")]
    assert len(functions) == 1
    page = functions[0]
    assert "<code>validateEmail</code></h1>" in page
    assert "validateEmail(email: str, role: Role) -&gt; str" in page
    assert "email:multi-label-domain" in page and "never exercised" in page
    index = pages["index.html"]
    assert "expected &lt;b&gt;throw&lt;/b&gt;" in index
    assert "jest-coverset" in pages["about.html"]


def test_shipped_descriptors_win_over_source_parsing(tmp_path: Path) -> None:
    """The TypeScript fixture with its sources on disk: the declared types come from the bundle's
    descriptors (a .ts file has no Python annotations), so a literal and an enum parameter expect
    their declared values, not the string classes the observations alone would suggest."""
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "bundles" / "neutral-ts"
    out = tmp_path / "out"
    build_report(load_bundle(fixture / "bundle", repo_root=fixture / "repo"), out)
    pages = all_pages(out)
    rate = next(h for n, h in pages.items() if n.startswith("functions/") and "<code>rateFor</code>" in h)
    assert (
        "Literal[&#39;domestic&#39;, &#39;eu&#39;, &#39;world&#39;]" in rate or "Literal['domestic'" in rate
    )
    price = next(
        h for n, h in pages.items() if n.startswith("functions/") and "<code>formatPrice</code>" in h
    )
    assert "currency: Currency" in price
    assert "Source not available." not in pages[next(n for n in pages if n.startswith("files/"))]
