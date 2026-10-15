"""What a report reads and where it writes: the bundle never picks the source root, page names never
collide, a failed build keeps the previous report, odd files under the root never hang the reader."""

from __future__ import annotations

import codecs
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner
from report_helpers import FixtureRun, all_pages

from coverset.lang.base import NeutralBundle, StaticFile, TestRow
from coverset.report import MARKER, build_report
from coverset.report.build import MAX_LISTED
from coverset.report.facts import read_sources
from coverset.report.load import input_from_bundle, load_bundle, source_access
from coverset.report.output import PageNames, hashed_name

SECRET = "FAKE-SECRET-1234"
CORE = "src/fixturepkg/core.py"


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _git_repo(root: Path, *tracked: str) -> None:
    _git(root, "init", "-q")
    if tracked:
        _git(root, "add", "--", *tracked)


def _neutral(dest: Path, paths: list[str], meta: dict[str, object] | None = None) -> Path:
    dest.mkdir(parents=True)
    manifest = {
        "format": "coverset-neutral",
        "version": 1,
        "language": "typescript",
        "producer": {"name": "x", "version": "1"},
        "created_at": "2026-10-11T12:00:00Z",
        "shard": 0,
        "meta": meta or {},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    test = {
        "id": "t::a",
        "file": "t.ts",
        "line": 1,
        "outcome": "passed",
        "duration_s": 0.1,
        "failed_phase": None,
        "fail_sig": None,
        "message": None,
        "markers": [],
        "invariants": [],
        "property": False,
        "start": 1.0,
        "stop": 2.0,
    }
    (dest / "tests.jsonl").write_text(json.dumps(test) + "\n", encoding="utf-8")
    files = "".join(
        json.dumps({"path": p, "n_lines": 3, "statements": [1, 2, 3], "excluded": [], "branches": []}) + "\n"
        for p in paths
    )
    (dest / "files.jsonl").write_text(files, encoding="utf-8")
    cov = "".join(json.dumps({"test": "t::a", "file": p, "lines": [1, 2]}) + "\n" for p in paths)
    (dest / "coverage.jsonl").write_text(cov, encoding="utf-8")
    return dest


def _bundle(paths: list[str], tests: tuple[TestRow, ...] = (), coverage: dict | None = None) -> NeutralBundle:
    return NeutralBundle(
        meta={},
        collected=(),
        tests=tests,
        coverage=coverage if coverage is not None else {"t::a": {p: (1,) for p in paths}},
        static={p: StaticFile(p, 3, (1, 2, 3), (), ()) for p in paths},
        run_arcs={},
        shapes=(),
        sampling=(),
        durations={},
    )


def _secret_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text(f"[default]\nkey = {SECRET}\nx\n", encoding="utf-8")
    return home


def _no_secret(out: Path) -> None:
    for path in out.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace"), path


# -- B1: the bundle never chooses which local files are read --------------------------------------


def test_a_hostile_neutral_bundle_cannot_pick_the_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _secret_home(tmp_path)
    bundle = _neutral(tmp_path / "bundle", [".aws/credentials"], meta={"git_root": str(home)})
    monkeypatch.chdir(home)  # even run from the very directory the bundle names
    out = tmp_path / "out"
    result = CliRunner().invoke(_cli(), ["report", str(bundle), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "sources not read" in result.stderr and "--repo-root" in result.stderr
    _no_secret(out)
    pages = all_pages(out)
    for name, html in pages.items():
        assert "data-sources-notice" in html, name
    page = next(h for n, h in pages.items() if n.startswith("files/"))
    assert "Source not available." in page
    assert "nothing (no source file was read)" in pages["about.html"]


def test_a_hostile_plugin_dir_cannot_pick_the_source_root(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _secret_home(tmp_path)
    copy = tmp_path / "pyh"
    shutil.copytree(fixture_out.out, copy, ignore=shutil.ignore_patterns("report", "bundle.tar.gz"))
    meta = json.loads((copy / "meta.json").read_text(encoding="utf-8"))
    meta.update(git_root=str(home), rootdir=str(home), repo_root=str(home))
    (copy / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    # the run measured a file named like the secret, relative to the root the bundle claims
    access = source_access(copy, "python", [".aws/credentials"], cwd=tmp_path)
    assert access.root is None and access.notice and "not a git work tree" in access.notice
    monkeypatch.chdir(tmp_path)  # not a git work tree
    inp = load_bundle(copy)
    assert inp.notice and all(text is None for text in inp.sources.values())
    out = tmp_path / "out"
    build_report(inp, out)
    _no_secret(out)
    assert all("data-sources-notice" in html for html in all_pages(out).values())


def test_a_plugin_dir_reads_the_tracked_files_of_the_current_git_repo(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    shutil.copytree(fixture_out.project, project, ignore=shutil.ignore_patterns("out", "__pycache__"))
    others = sorted(
        p.relative_to(project).as_posix() for p in (project / "src").rglob("*.py") if p.as_posix() != CORE
    )
    untracked = others[0]
    _git_repo(project, *[p for p in others if p != untracked], CORE)
    monkeypatch.chdir(project)
    inp = load_bundle(fixture_out.out)
    assert inp.notice is None
    assert inp.sources[CORE] is not None
    assert inp.sources[untracked] is None  # not tracked: never read
    assert "only files git tracks there" in inp.meta["repo_root"]
    assert "1 not tracked by git there" in inp.meta["sources"]
    out = tmp_path / "out"
    build_report(inp, out)
    assert '<div class="src-lines">' in (out / f"files/{CORE}.html").read_text(encoding="utf-8")
    # a git repo that tracks none of the bundle's files reads nothing and says so
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "README").write_text("x", encoding="utf-8")
    _git_repo(elsewhere, "README")
    monkeypatch.chdir(elsewhere)
    inp = load_bundle(fixture_out.out)
    assert inp.notice and "git tracks none" in inp.notice
    assert all(text is None for text in inp.sources.values())


def test_an_archive_without_repo_root_reads_no_source(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fixture_out.project)
    inp = load_bundle(fixture_out.out / "bundle.tar.gz")
    assert inp.notice and "--repo-root" in inp.notice
    assert all(text is None for text in inp.sources.values())
    inp = load_bundle(fixture_out.out / "bundle.tar.gz", repo_root=fixture_out.project)
    assert inp.notice is None and inp.sources[CORE] is not None


def test_repo_root_in_a_git_repo_reads_tracked_files_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("a\n", encoding="utf-8")
    (root / "src" / "new.ts").write_text("new\n", encoding="utf-8")
    (root / ".env").write_text(f"{SECRET}\n", encoding="utf-8")
    (root / "src" / "env.ts").symlink_to(root / ".env")  # tracked link to an untracked file
    _git_repo(root, "src/a.ts", "src/env.ts")
    paths = ["src/a.ts", "src/new.ts", ".env", "src/env.ts"]
    inp = input_from_bundle(_bundle(paths), source=tmp_path / "b", kind="neutral", repo_root=root)
    assert inp.sources == {"src/a.ts": "a\n", "src/new.ts": None, ".env": None, "src/env.ts": None}
    # outside git the root itself is the only fence
    plain = tmp_path / "plain"
    shutil.copytree(root, plain, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    found = read_sources(plain, ["src/a.ts", "src/new.ts"])
    assert found == {"src/a.ts": "a\n", "src/new.ts": "new\n"}


def test_repo_root_does_not_go_with_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(_cli(), ["report", "--run", "run_x", "--repo-root", str(tmp_path)])
    assert result.exit_code == 2 and "--repo-root does not go with --run" in result.output


def _cli():  # type: ignore[no-untyped-def]
    from coverset.cli import cli

    return cli


# -- minors: odd files, encodings ------------------------------------------------------------------


def test_a_fifo_under_the_root_does_not_hang(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    os.mkfifo(tmp_path / "src" / "x.ts")
    found: dict[str, str | None] = {}
    worker = threading.Thread(target=lambda: found.update(read_sources(tmp_path, ["src/x.ts"])), daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "read_sources blocked on a FIFO"
    assert found == {"src/x.ts": None}


def test_sources_drop_the_bom_and_honour_a_coding_cookie(tmp_path: Path) -> None:
    (tmp_path / "bom.py").write_bytes(codecs.BOM_UTF8 + "x = 'é'\n".encode())
    (tmp_path / "bom.ts").write_bytes(codecs.BOM_UTF8 + b"let x = 1;\n")
    (tmp_path / "latin.py").write_bytes(b"# -*- coding: latin-1 -*-\nx = '\xe9'\n")
    found = read_sources(tmp_path, ["bom.py", "bom.ts", "latin.py"])
    assert found["bom.py"] == "x = 'é'\n"
    assert found["bom.ts"] == "let x = 1;\n"
    assert found["latin.py"] == "# -*- coding: latin-1 -*-\nx = 'é'\n"


# -- M5: page names --------------------------------------------------------------------------------


def test_page_names_never_collide() -> None:
    names = PageNames()
    x = names.file_page("x")
    nested = names.file_page("x.html/y")
    assert x == "files/x.html" and nested.startswith("files/_/")
    names = PageNames()
    names.file_page("x.html/y")
    assert names.file_page("x").startswith("files/_/")  # files/x.html is a directory now
    names = PageNames()
    unsafe = names.file_page("a b.ts")
    plain = names.file_page(f"_/{hashed_name('a b.ts')}")  # a plain path spelling the hashed name
    assert unsafe == f"files/_/{hashed_name('a b.ts')}.html"
    assert plain != unsafe and plain.startswith("files/_/")
    names = PageNames()
    assert names.file_page("A/b.py") == "files/A/b.py.html"
    other = names.file_page("a/B.py")
    assert other.startswith("files/_/")
    third = names.file_page("a/c.py")  # same directory, other spelling: hashed too
    assert third.startswith("files/_/")
    assert names.file_page("A/c.py") == "files/A/c.py.html"
    assert len({x, nested, unsafe, plain, other, third}) == 6


def test_colliding_paths_build_and_link_to_their_own_pages(tmp_path: Path) -> None:
    paths = [
        "x",
        "x.html/y",
        "a b.ts",
        f"_/{hashed_name('a b.ts')}",
        "Src/c.ts",
        "src/d.ts",
        "A/b.py",
        "a/B.py",
    ]
    out = tmp_path / "out"
    inp = input_from_bundle(_bundle(paths), source=tmp_path / "b", kind="neutral", repo_root=tmp_path)
    build_report(inp, out)
    import re

    index = (out / "index.html").read_text(encoding="utf-8")
    pages = dict(re.findall(r'<tr data-path="([^"]+)"[^>]*>\s*<td[^>]*><a href="([^"#]+)"', index))
    assert sorted(pages) == sorted(paths)
    assert len({p.casefold() for p in pages.values()}) == len(paths)
    for path, page in pages.items():
        html = (out / page).read_text(encoding="utf-8")
        data = json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S).group(1))  # type: ignore[union-attr]
        assert data["path"] == path
        # case-sensitive resolution: each link's exact spelling exists on disk
        cur = out
        for part in page.split("/"):
            assert part in os.listdir(cur), page
            cur = cur / part


def test_a_failed_build_keeps_the_previous_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"
    good = input_from_bundle(_bundle(["a.ts"]), source=tmp_path / "b", kind="neutral", repo_root=tmp_path)
    build_report(good, out)
    before = sorted(p.relative_to(out).as_posix() for p in out.rglob("*"))
    from coverset.report import build as build_module

    def boom(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(build_module._Builder, "test_page", boom)
    with pytest.raises(RuntimeError):
        build_report(good, out)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]  # no staging directory left behind
    assert (out / MARKER).is_file()


# -- minors: labels and caps -----------------------------------------------------------------------


def test_coverage_without_a_result_is_labelled_no_result(tmp_path: Path) -> None:
    run = TestRow("t::ran", "t.py", "not run", None, None, None, None, None, None)
    weird = TestRow("t::weird", "t.py", "weird outcome", None, None, None, None, None, None)
    bundle = _bundle(["a.ts"], tests=(run, weird), coverage={"t::ghost": {"a.ts": (1,)}})
    out = tmp_path / "out"
    build_report(input_from_bundle(bundle, source=tmp_path / "b", kind="neutral"), out)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'data-outcome="no result">1 no result' in index
    assert 'data-outcome="not run">1 not run' in index
    assert 'data-outcome="weird outcome">1 weird outcome' in index


def test_test_and_function_lists_are_capped(tmp_path: Path) -> None:
    n = MAX_LISTED + 7
    src = "".join(f"def f{i}(x):\n    return x\n\n\n" for i in range(n))
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    lines = tuple(4 * i + 2 for i in range(n))
    statements = tuple(sorted({4 * i + 1 for i in range(n)} | set(lines)))
    tests = {f"t.py::t{j}": {"m.py": lines} for j in range(n)}
    bundle = NeutralBundle(
        meta={},
        collected=(),
        tests=tuple(TestRow(t, "t.py", "passed", 0.1, None, None, None, None, None) for t in tests),
        coverage=tests,
        static={"m.py": StaticFile("m.py", 4 * n, statements, (), ())},
        run_arcs={},
        shapes=(),
        sampling=(),
        durations={},
    )
    out = tmp_path / "out"
    build_report(input_from_bundle(bundle, source=tmp_path / "b", kind="python", repo_root=tmp_path), out)
    test_page = (out / f"tests/{hashed_name('t.py::t0')}.html").read_text(encoding="utf-8")
    assert test_page.count("<tr data-func=") == MAX_LISTED
    assert "+7 more functions not listed" in test_page
    assert f"{n} functions touched" in test_page
    fn_page = next(p for p in (out / "functions").glob("*.html"))
    html = fn_page.read_text(encoding="utf-8")
    covering = html.split("data-covering>", 1)[1].split("</ul>", 1)[0]
    assert covering.count("<li>") == MAX_LISTED
    assert "+7 more covering tests not listed" in html


def test_a_subdirectory_of_the_repository_still_reads_the_tracked_files(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle paths are repository-relative, so building from a subdirectory of the work tree (or
    naming one as --repo-root) reads from the top of the work tree."""
    project = tmp_path / "project"
    shutil.copytree(fixture_out.project, project, ignore=shutil.ignore_patterns("out", "__pycache__"))
    _git_repo(project, CORE)
    monkeypatch.chdir(project / "src")
    inp = load_bundle(fixture_out.out)
    assert inp.notice is None and inp.sources[CORE] is not None
    assert str(project.resolve()) in inp.meta["repo_root"]
    inp = load_bundle(fixture_out.out, repo_root=project / "src")
    assert inp.notice is None and inp.sources[CORE] is not None
