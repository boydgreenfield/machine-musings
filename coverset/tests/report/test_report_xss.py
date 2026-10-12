"""Hostile bundles: every string in a bundle is attacker-controlled on a pull request.

A synthetic bundle carries ``<script>``, ``"onmouseover=``, ``javascript:``, ``<img onerror>`` and
bidirectional control characters in test ids, failure messages, reprs, caller names, paths, meta and
the source text, plus paths that try to leave the report (``../x.py``, an absolute path). The report
must escape all of it on every page, keep its script rules, never read a source outside the repo
root and never write a file outside ``--out``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from report_helpers import all_pages, parse_page

from coverset.lang.base import FuncSampling, ShapeObs, StaticFile, TestRow
from coverset.report import OutDirError, build_report
from coverset.report.load import input_from_bundle
from coverset.report.output import Writer, prepare_out_dir
from coverset.web.render.html import relative_hrefs, safe_href, safe_relative_href

pytestmark = pytest.mark.security

SCRIPT = "<script>alert(1)</script>"
ATTR = '"onmouseover="alert(1)'
JS = "javascript:alert(1)"
IMG = "<img src=x onerror=alert(1)>"
CLOSE = "</script><script>alert(2)</script>"
BIDI = "‮evil⁦x⁩"
DATA_BLOCK = re.compile(r'<script type="application/json" id="report-data">.*?</script>', re.S)
BIDI_CHARS = re.compile("[؜‎‏‪-‮⁦-⁩]")

SOURCE = f'''"""{SCRIPT} {ATTR} {JS}"""
# {BIDI} {IMG}


def f(text: str, n: int) -> str:
    """{CLOSE}"""
    if n > 0:
        return text + "{IMG}"
    return text
'''
F_LINE = 5

TESTS = [
    f"tests/test_x.py::test_{SCRIPT}",
    f"tests/test_x.py::test_attr[{ATTR}]",
    f"tests/test_x.py::test_js[{JS}]",
    f"tests/test_x.py::test_bidi[{BIDI}]",
]
PATHS = [
    "src/ok.py",
    f"src/{SCRIPT}.py",
    "../outside.py",
    f"src/a{ATTR}.py",
    f"src/{BIDI}.py",
    f"src/{JS}.py",
]


@dataclass(frozen=True)
class HostileBundle:
    """A NeutralBundle look-alike that also ships function regions and type descriptors."""

    meta: dict[str, Any]
    collected: tuple[Any, ...]
    tests: tuple[TestRow, ...]
    coverage: dict[str, dict[str, tuple[int, ...]]]
    static: dict[str, StaticFile]
    run_arcs: dict[str, tuple[tuple[int, int], ...]]
    shapes: tuple[ShapeObs, ...]
    sampling: tuple[FuncSampling, ...]
    durations: dict[str, float]
    functions: tuple[dict[str, Any], ...] = ()
    types: dict[str, Any] = field(default_factory=dict)


def hostile_bundle(repo: Path, outside: Path) -> HostileBundle:
    abs_path = str(outside / "abs_secret.py")
    paths = [*PATHS, abs_path]
    static = {
        p: StaticFile(p, 9, (1, 5, 7, 8, 9), (), ((7, 8), (7, 9)))
        if p == "src/ok.py"
        else StaticFile(p, 2, (1, 2), (), ())
        for p in paths
    }
    coverage: dict[str, dict[str, tuple[int, ...]]] = {"<import>": {"src/ok.py": (1, 5)}}
    for i, nodeid in enumerate(TESTS):
        coverage[nodeid] = {
            "src/ok.py": (7, 8) if i % 2 == 0 else (7, 9),
            paths[(i % (len(paths) - 1)) + 1]: (1,),
        }
        coverage[nodeid][abs_path] = (2,)
    triple = ("src/ok.py", "f", F_LINE)
    shapes = (
        ShapeObs(
            triple,
            "text",
            "str|ascii|len:11-100",
            ("src/" + IMG, SCRIPT, 3),
            2,
            (TESTS[0],),
            False,
            repr(SCRIPT + BIDI),
            TESTS[1],
        ),
        ShapeObs(triple, "n", "int|pos", TESTS[2], 1, (TESTS[2],), False, repr(CLOSE), TESTS[2]),
        ShapeObs(
            triple, "return", "str|ascii|len:11-100", TESTS[3], 1, (TESTS[3],), False, repr(IMG), TESTS[3]
        ),
    )
    return HostileBundle(
        meta={
            "tracer": SCRIPT,
            "rootdir": str(repo),
            "errors": [IMG, BIDI],
            "coverage_warnings": [ATTR],
            "convert": {"dropped": {SCRIPT: 1, JS: 2}},
            "nested": {"x": CLOSE},
        },
        collected=(),
        tests=tuple(
            TestRow(
                nodeid,
                "tests/test_x.py",
                "failed",
                0.01,
                "call",
                "f" * 16,
                CLOSE + IMG + BIDI + JS,
                None,
                None,
            )
            for nodeid in TESTS
        ),
        coverage=coverage,
        static=static,
        run_arcs={"src/ok.py": ((7, 8),)},
        shapes=shapes,
        sampling=(FuncSampling(triple, 3, tuple(TESTS[:3]), False),),
        durations={},
        functions=(
            {
                "key": "../../etc/passwd",
                "path": f"src/a{ATTR}.py",
                "qualname": SCRIPT,
                "def_line": 1,
                "body_start": 2,
                "end_line": 2,
                "params": [ATTR, "role"],
                "kind": "function",
            },
        ),
        types={
            "../../etc/passwd": {
                "role": {"kind": "enum", "name": "Role", "members": ["admin", SCRIPT]},
                "return": {"kind": "literal", "values": [SCRIPT, JS]},
            }
        },
    )


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "ok.py").write_text(SOURCE, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "abs_secret.py").write_text("ABS_SECRET = 1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("OUTSIDE_SECRET = 1\n", encoding="utf-8")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    out = tmp_path / "out"
    bundle = hostile_bundle(repo, outside)
    inp = input_from_bundle(
        bundle, source=Path(f"bundle-{SCRIPT}"), kind="neutral", repo_root=repo, title=f"T {SCRIPT}"
    )  # type: ignore[arg-type]
    build_report(inp, out)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if not p.is_relative_to(out))
    assert after == before, "the report wrote outside --out"
    return out, tmp_path


def test_every_page_escapes_hostile_strings(built: tuple[Path, Path]) -> None:
    out, _ = built
    pages = all_pages(out)
    assert len(pages) > 10
    for name, html in pages.items():
        parser = parse_page(html)
        for tag, attrs in parser.tags:
            assert tag not in ("img", "iframe", "object", "embed", "base", "meta") or (
                tag == "meta" and set(attrs) <= {"charset", "name", "content", "http-equiv"}
            ), (name, tag, attrs)
            assert not any(key.startswith("on") for key in attrs), (name, tag, attrs)
            for key in ("href", "src", "action", "formaction", "xlink:href"):
                value = (attrs.get(key) or "").strip().lower()
                assert not value.startswith(("javascript:", "data:", "//")), (name, tag, attrs)
        # outside the JSON data block (raw text: quotes are inert there, "<" and ">" are escaped)
        markup = DATA_BLOCK.sub("", html)
        for needle in ("<script>alert", "<img", "</script><script>"):
            assert needle not in markup, (name, needle)
        assert not BIDI_CHARS.search(html), name
        assert "OUTSIDE_SECRET" not in html and "ABS_SECRET" not in html, name
        for block in parser.data_blocks:
            assert "<" not in block and ">" not in block
            json.loads(block)
    index = pages["index.html"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in index
    assert "&lt;U+202E&gt;evil" in index  # bidi controls are shown, not applied
    # in an attribute the quote stays an entity (a quote in text content is harmless)
    assert 'data-path="src/a&#34;onmouseover=&#34;alert(1).py"' in index


def test_hostile_paths_get_hashed_pages(built: tuple[Path, Path]) -> None:
    out, _ = built
    files = sorted(p.relative_to(out).as_posix() for p in (out / "files").rglob("*.html"))
    assert "files/src/ok.py.html" in files
    hashed = [f for f in files if f.startswith("files/_/")]
    assert len(hashed) == len(PATHS)  # every path but src/ok.py (the absolute one included)
    assert all(re.fullmatch(r"files/_/[0-9a-f]{16}\.html", f) for f in hashed)
    for path in out.rglob("*"):
        assert path.resolve().is_relative_to(out.resolve())
    functions = sorted(p.name for p in (out / "functions").glob("*.html"))
    assert len(functions) == 2 and all(re.fullmatch(r"[0-9a-f]{16}\.html", f) for f in functions)


def test_data_survives_escaping(built: tuple[Path, Path]) -> None:
    out, _ = built
    html = (out / "files/src/ok.py.html").read_text(encoding="utf-8")
    block = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert block
    data = json.loads(block.group(1))
    assert TESTS[0] in [t[0] for t in data["tests"]]
    assert any("<U+202E>" in t[0] for t in data["tests"])
    search = (out / "search-index.js").read_text(encoding="utf-8")
    assert "<" not in search.split("*/", 1)[1] and not BIDI_CHARS.search(search)
    items = json.loads(search.split("window.COVERSET_SEARCH = ", 1)[1].rstrip().rstrip(";"))
    assert ["test", TESTS[0], "failed"] == items[[i[1] for i in items].index(TESTS[0])][:3]
    # the source text is escaped by the source renderer, bidi made visible
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;U+202E&gt;" in html


def test_descriptor_types_drive_the_shipped_function(built: tuple[Path, Path]) -> None:
    out, _ = built
    page = next(
        p.read_text(encoding="utf-8")
        for p in (out / "functions").glob("*.html")
        if "&lt;script&gt;alert(1)&lt;/script&gt;</code></h1>" in p.read_text(encoding="utf-8")
    )
    assert "role: Role" in page  # the enum descriptor became the annotation the catalogue reads


def test_relative_links_admit_only_generated_pages() -> None:
    assert safe_relative_href("../files/src/a.py.html#L12") == "../files/src/a.py.html#L12"
    assert safe_relative_href("#lens=0123456789abcdef") == "#lens=0123456789abcdef"
    for bad in (
        JS,
        "//evil.example/x.html",
        '../a".html',
        "files/../../x.html",
        "/x.html",
        "x.htm",
        "a b.html",
    ):
        assert safe_relative_href(bad) == "#", bad
    assert safe_href("../files/a.py.html") == "#"  # the live pages never take relative links
    with relative_hrefs():
        assert safe_href("../files/a.py.html") == "../files/a.py.html"
        assert safe_href(JS) == "#"
    assert safe_href("../files/a.py.html") == "#"


def test_out_dir_guards(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (target / "x").write_text("1", encoding="utf-8")
    link = tmp_path / "link"
    os.symlink(target, link)
    with pytest.raises(OutDirError):
        prepare_out_dir(link)
    (tmp_path / "file").write_text("1", encoding="utf-8")
    with pytest.raises(OutDirError):
        prepare_out_dir(tmp_path / "file")
    out = prepare_out_dir(tmp_path / "new")
    writer = Writer(out)
    for bad in ("../x.html", "/etc/x", "a/../../x", "a\\b", "", "a//b"):
        with pytest.raises(OutDirError):
            writer.text(bad, "x")
    (out / "sub").symlink_to(tmp_path)
    with pytest.raises(OutDirError):
        writer.text("sub/escape.html", "x")
    assert not (tmp_path / "escape.html").exists()


# -- --compare: the Changes page and the "since base" marks carry the same hostile strings ----------

GONE = f"tests/test_x.py::gone[{SCRIPT}{BIDI}]"
REMOVED = f"src/removed{ATTR}{SCRIPT}.py"


def hostile_base(repo: Path, outside: Path) -> HostileBundle:
    """The hostile bundle as a base: every test passed, one more test (only in the base) ran line 8
    of src/ok.py and a file that is gone now, with a function named like markup."""
    now = hostile_bundle(repo, outside)
    tests = tuple(
        TestRow(t.nodeid, t.file, "passed", 0.001, None, None, None, None, None) for t in now.tests
    ) + (TestRow(GONE, "tests/test_x.py", "failed", 0.5, "call", None, CLOSE + IMG, None, None),)
    coverage = {k: dict(v) for k, v in now.coverage.items()}
    coverage[GONE] = {"src/ok.py": (7, 8, 9), REMOVED: (1, 2)}
    static = {**now.static, REMOVED: StaticFile(REMOVED, 2, (1, 2), (), ())}
    gone_fn = {
        "key": f"{SCRIPT}-gone",
        "path": REMOVED,
        "qualname": IMG + BIDI,
        "def_line": 1,
        "body_start": 2,
        "end_line": 2,
        "params": [ATTR],
        "kind": "function",
    }
    return dataclasses.replace(
        now, tests=tests, coverage=coverage, static=static, functions=(*now.functions, gone_fn)
    )


def test_the_changes_page_escapes_hostile_strings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "ok.py").write_text(SOURCE, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "abs_secret.py").write_text("ABS_SECRET = 1\n", encoding="utf-8")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    now = input_from_bundle(
        hostile_bundle(repo, outside), source=Path(f"now-{SCRIPT}"), kind="neutral", repo_root=repo
    )  # type: ignore[arg-type]
    base = input_from_bundle(hostile_base(repo, outside), source=Path(f"base-{IMG}{BIDI}"), kind="neutral")  # type: ignore[arg-type]
    out = tmp_path / "out"
    result = build_report(now, out, base=base)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if not p.is_relative_to(out))
    assert after == before, "the report wrote outside --out"
    c = result.changes
    assert c is not None and GONE in c.base_only
    assert any(t.kind == "outcome" for t in c.tests) and any(t.kind == "removed" for t in c.tests)
    assert any(f.status == "removed" and f.path == REMOVED for f in c.files)
    pages = all_pages(out)
    changes = pages["changes.html"]
    assert "data-test-sets" in changes and 'data-test-change="removed"' in changes
    for name, html in pages.items():
        parser = parse_page(html)
        for tag, attrs in parser.tags:
            assert tag not in ("img", "iframe", "object", "embed", "base"), (name, tag)
            assert not any(key.startswith("on") for key in attrs), (name, tag, attrs)
            for key in ("href", "src", "action", "formaction", "xlink:href"):
                value = (attrs.get(key) or "").strip().lower()
                assert not value.startswith(("javascript:", "data:", "//")), (name, tag, attrs)
        markup = DATA_BLOCK.sub("", html)
        for needle in ("<script>alert", "<img", "</script><script>"):
            assert needle not in markup, (name, needle)
        assert not BIDI_CHARS.search(html), name
        assert "ABS_SECRET" not in html, name
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in changes
    assert "&lt;U+202E&gt;evil" in changes
    assert 'data-file="src/removed&#34;onmouseover=&#34;alert(1)' in changes
