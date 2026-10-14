"""``coverset report --compare``: the Changes page, the index tile and the "since base" marks.

Synthetic pairs of bundles exercise every delta kind (lines lost, gained and not re-run in an
unchanged file; a changed file compared on an unchanged function body and a changed one; files and
functions added and removed; input classes lost, gained and not re-run; a function newly without a
covering test; tests added, removed, with another outcome and slower), a base whose Python regions
would be borrowed from the local tree, and the demo: PR #1's run against the ``main`` baseline at its
base commit, from the facts store.
"""

from __future__ import annotations

import asyncio
import json
import re
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from report_helpers import DemoData, FixtureRun, all_pages, parse_page

from coverset.lang.base import FunctionRegion, NeutralBundle, ShapeObs, StaticFile, TestRow, func_key
from coverset.report import build_report
from coverset.report.compare import Side, compare, side_totals
from coverset.report.facts import ReportInput
from coverset.report.load import input_from_bundle, load_run

SAME = "src/same.ts"
MOVED = "src/moved.ts"
OLD = "src/old.ts"
NEW = "src/new.ts"
INT = {"kind": "int"}


def region(path: str, qualname: str, def_line: int, end_line: int, body_hash: str) -> FunctionRegion:
    return FunctionRegion(
        func_key=func_key(path, qualname, 0),
        path=path,
        qualname=qualname,
        ordinal=0,
        def_line=def_line,
        body_start=def_line + 1,
        end_line=end_line,
        body_hash=body_hash,
        sig_hash="s",
        params=("n",),
        kind="function",
    )


def shape(r: FunctionRegion, key: str, test: str) -> ShapeObs:
    return ShapeObs(r.triple, "n", key, test, 1, (test,), False, "1", test)


def bundle(
    static: dict[str, tuple[int, ...]],
    tests: list[tuple[str, str, float]] | list[tuple[str, str, float, str]],
    coverage: dict[str, dict[str, tuple[int, ...]]],
    functions: tuple[FunctionRegion, ...],
    shapes: tuple[ShapeObs, ...] = (),
) -> NeutralBundle:
    return NeutralBundle(
        meta={},
        collected=(),
        tests=tuple(
            TestRow(t[0], t[3] if len(t) > 3 else "t.ts", t[1], t[2], None, None, None, None, None)  # type: ignore[misc]
            for t in tests
        ),
        coverage=coverage,
        static={p: StaticFile(p, max(s), s, (), ()) for p, s in static.items()},
        run_arcs={},
        shapes=shapes,
        sampling=(),
        durations={},
        functions=functions,
        types={r.triple: {"n": INT} for r in functions},
    )


def as_input(b: NeutralBundle, name: str, repo: Path | None = None) -> ReportInput:
    return input_from_bundle(b, source=Path(name), kind="neutral", repo_root=repo)


# base: f (same.ts 2-4), g (5-6), h (8-10); p (moved.ts 2-4), q (5-6); old_fn (old.ts)
F, G, H = region(SAME, "f", 2, 4, "F"), region(SAME, "g", 5, 6, "G"), region(SAME, "h", 8, 10, "H")
P0, Q0 = region(MOVED, "p", 2, 4, "P"), region(MOVED, "q", 5, 6, "Q1")
OLD_FN = region(OLD, "old_fn", 1, 2, "O")
# now: two lines inserted at the top of moved.ts; q's body changed; new.ts with new_fn
P1, Q1 = region(MOVED, "p", 4, 6, "P"), region(MOVED, "q", 7, 8, "Q2")
NEW_FN = region(NEW, "new_fn", 1, 2, "N")


def base_bundle() -> NeutralBundle:
    return bundle(
        {SAME: tuple(range(1, 11)), MOVED: (1, 2, 3, 4, 5, 6), OLD: (1, 2)},
        [
            ("t::keep", "passed", 0.1),
            ("t::fast", "passed", 0.01),
            ("t::flip", "passed", 0.02),
            ("t::gone", "passed", 0.02, "gone.ts"),  # its file has no test in this run
        ],
        {
            "<import>": {SAME: (1, 2, 5, 8), MOVED: (1, 2, 5), OLD: (1,)},
            "t::keep": {SAME: (3, 4, 9, 10), MOVED: (3, 4, 6)},
            "t::gone": {SAME: (6,), OLD: (2,)},
            "t::fast": {SAME: (3,)},
            "t::flip": {SAME: (3,)},
        },
        (F, G, H, P0, Q0, OLD_FN),
        (shape(F, "int|pos", "t::keep"), shape(F, "int|zero", "t::gone")),
    )


def now_bundle() -> NeutralBundle:
    return bundle(
        {SAME: tuple(range(1, 11)), MOVED: (1, 2, 3, 4, 5, 6, 7, 8), NEW: (1, 2)},
        [
            ("t::keep", "passed", 0.5),
            ("t::fast", "passed", 0.05),
            ("t::flip", "failed", 0.02),
            ("t::new", "passed", 0.01),
        ],
        {
            "<import>": {SAME: (1, 2, 5, 8), MOVED: (1, 2, 4, 7), NEW: (1,)},
            "t::keep": {SAME: (3,), MOVED: (5, 8)},
            "t::new": {SAME: (7,), NEW: (2,)},
            "t::fast": {SAME: (3,)},
            "t::flip": {SAME: (3,)},
        },
        (F, G, H, P1, Q1, NEW_FN),
        (shape(F, "int|neg", "t::keep"), shape(NEW_FN, "int|pos", "t::new")),
    )


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[ReportInput, ReportInput]:
    """The base has no sources at all (only its facts are needed); this run's are on disk."""
    repo = tmp_path_factory.mktemp("compare-repo")
    (repo / "src").mkdir()
    for path, n in ((SAME, 10), (MOVED, 8), (NEW, 2)):
        (repo / path).write_text("".join(f"line_{i}();\n" for i in range(1, n + 1)), encoding="utf-8")
    return as_input(base_bundle(), "base"), as_input(now_bundle(), "now", repo)


@pytest.fixture(scope="module")
def built(pair: tuple[ReportInput, ReportInput], tmp_path_factory: pytest.TempPathFactory) -> Path:
    base, now = pair
    out = tmp_path_factory.mktemp("compare") / "out"
    result = build_report(now, out, base=base)
    assert result.pages["changes"] == 1 and result.changes is not None
    return out


def test_lines_of_an_unchanged_file(pair: tuple[ReportInput, ReportInput]) -> None:
    c = compare(*pair)
    same = next(f for f in c.files if f.path == SAME)
    assert same.status == "same"
    # 4, 9, 10 ran under t::keep in the base and not now; t::keep ran now, so they are lost
    assert same.lost == (4, 9, 10)
    # 6 ran only under t::gone, which did not run now: not re-run, never lost
    assert same.not_rerun == (6,)
    assert same.gained == (7,)
    assert c.marks[SAME] == (frozenset({4, 9, 10}), frozenset({7}))


def test_a_changed_file_is_compared_on_function_bodies(pair: tuple[ReportInput, ReportInput]) -> None:
    c = compare(*pair)
    moved = next(f for f in c.files if f.path == MOVED)
    assert moved.status == "changed" and moved.why == "its statements differ"
    assert (moved.compared_fns, moved.changed_fns) == (1, 1)
    # p moved down two lines with the same body: base line 4 (now 6) no longer runs
    assert moved.lost == (6,) and moved.gained == ()
    p = next(f for f in c.functions if f.qualname == "p")
    assert p.status == "body unchanged" and p.lost_lines == (6,)
    assert (p.base_body, p.now_body) == ((2, 2), (1, 2))
    q = next(f for f in c.functions if f.qualname == "q")
    assert q.status == "body changed" and q.lost_lines == () and q.now_body == (1, 1)
    # line numbers are never compared across a changed file: base line 6 (q's body) is not "lost"
    assert 4 not in moved.lost


def test_files_and_functions_added_and_removed(pair: tuple[ReportInput, ReportInput]) -> None:
    c = compare(*pair)
    status = {f.path: f.status for f in c.files}
    assert status[NEW] == "added" and status[OLD] == "removed"
    fns = {f.qualname: f for f in c.functions}
    assert fns["new_fn"].status == "new" and fns["new_fn"].classes_gained == ("n:positive",)
    assert fns["new_fn"].newly_called
    assert fns["old_fn"].status == "removed" and fns["old_fn"].func_key is None


def test_input_classes_and_covering_tests(pair: tuple[ReportInput, ReportInput]) -> None:
    c = compare(*pair)
    fns = {f.qualname: f for f in c.functions}
    f = fns["f"]
    assert f.classes_lost == ("n:positive",)  # t::keep passed it in and ran now
    assert f.classes_not_rerun == ("n:zero",)  # only t::gone passed it in
    assert f.classes_gained == ("n:negative",)
    assert c.classes_lost == 1
    assert fns["h"].uncovered and not fns["h"].uncovered_not_rerun
    assert fns["g"].uncovered_not_rerun and not fns["g"].uncovered
    assert "no test calls it any more" in fns["h"].summary()


def test_tests_added_removed_outcome_and_slower(pair: tuple[ReportInput, ReportInput]) -> None:
    c = compare(*pair)
    kinds = {(t.kind, t.nodeid) for t in c.tests}
    assert kinds == {
        ("added", "t::new"),
        ("removed", "t::gone"),
        ("outcome", "t::flip"),
        ("slower", "t::keep"),  # 5x and 400 ms slower; t::fast is 5x but only 40 ms slower
    }
    flip = c.tests_of("outcome")[0]
    assert (flip.base_outcome, flip.now_outcome) == ("passed", "failed")
    assert c.tests_changed == 3
    assert c.base_only == ["t::gone"] and c.now_only == 1


def test_totals_are_the_two_reports_own(
    pair: tuple[ReportInput, ReportInput], built: Path, tmp_path: Path
) -> None:
    base, now = pair
    build_report(base, tmp_path / "base")
    rows = {r.key: r for r in compare(base, now).totals}

    def stat(out: Path, name: str) -> str:
        html = (out / "index.html").read_text(encoding="utf-8")
        match = re.search(
            rf'data-stat="{name}"><div class="value">([^<]*)</div><div class="label">(.*?)</div>', html
        )
        assert match, name
        return match.group(1) + " | " + match.group(2)

    for out, side in ((tmp_path / "base", "base"), (built, "now")):
        assert stat(out, "statements").startswith(f"{getattr(rows['statements'], side)} |")
        assert f"{getattr(rows['run'], side)} of {getattr(rows['statements'], side)}" in stat(out, "run")
        assert stat(out, "tests").startswith(f"{getattr(rows['tests'], side)} |")
    assert rows["statements"].text == "18 → 20 (+2)"
    assert rows["tests-failed"].text == "0 → 1 (+1)"
    assert side_totals(Side(now))["classes_observed"] == rows["classes-observed"].now


def test_the_pages(built: Path) -> None:
    pages = all_pages(built)
    for name, html in pages.items():
        parse_page(html)
        assert 'href="' + "../" * name.count("/") + 'changes.html"' in html, name  # in the nav
    index = pages["index.html"]
    tile = re.search(r'<a class="changes-tile" href="changes.html" data-changes-tile>(.*?)</a>', index, re.S)
    assert tile
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", tile.group(1)))
    assert "Changes since base: 4 lines lost, 1 class lost, 3 tests changed" in text
    changes = pages["changes.html"]
    assert "data-test-sets" in changes and "Different test sets" in changes
    assert (
        '<tr data-total="statements"><td>Statements</td><td class="num delta moved">18 → 20 (+2)</td></tr>'
        in changes
    )
    assert f'href="files/{SAME}.html#L4">4</a>' in changes
    assert '<span class="chip">file changed</span>' in changes
    assert "data-class-lost" in changes and "n:positive" in changes
    assert '<tr data-test-change="outcome" data-nodeid="t::flip">' in changes
    assert '<tr data-test-change="slower" data-nodeid="t::keep">' in changes
    assert '<tr data-test-change="slower" data-nodeid="t::fast">' not in changes
    # the file page: marks in the diff column, a legend entry for each, and the line counts
    same = pages[f"files/{SAME}.html"]
    assert re.search(
        r'<div class="ln mis since-lost" id="L4" data-line="4"><span class="gd" title="newly uncovered', same
    )
    assert re.search(
        r'<div class="ln run since-gained" id="L7" data-line="7"><span class="gd" title="newly covered', same
    )
    assert 'id="L6" data-line="6"><span class="gd"></span>' in same  # not re-run: no mark
    assert "newly uncovered since base</li>" in same and "newly covered since base</li>" in same
    moved = pages[f"files/{MOVED}.html"]
    assert re.search(r'class="ln [^"]*since-lost" id="L6"', moved)
    # the function page's one line
    f_page = pages[f"functions/{F.func_key}.html"]
    since = re.search(r'<p class="since-note" data-since>(.*?)</p>', f_page)
    assert since and "input classes lost: n:positive" in since.group(1)
    assert "input classes gained: n:negative" in since.group(1)
    assert "its base tests did not run" in pages[f"functions/{G.func_key}.html"]
    assert "no test calls it any more" in pages[f"functions/{H.func_key}.html"]


def test_no_base_means_no_changes_page(pair: tuple[ReportInput, ReportInput], tmp_path: Path) -> None:
    _, now = pair
    result = build_report(now, tmp_path / "out")
    assert result.changes is None and "changes" not in result.pages
    pages = all_pages(tmp_path / "out")
    assert "changes.html" not in pages
    assert not any("changes.html" in html or "since-lost" in html for html in pages.values())


def test_a_base_with_borrowed_python_regions_makes_no_body_claims(tmp_path: Path) -> None:
    """A Python base bundle whose regions would be indexed from the local tree (the current text)
    is not trusted for a file whose statements changed: no "body unchanged", no line numbers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("\n\ndef f(n):\n    return n\n", encoding="utf-8")
    common = {"meta": {}, "collected": (), "run_arcs": {}, "shapes": (), "sampling": (), "durations": {}}
    row = (TestRow("t.py::a", "t.py", "passed", 0.1, None, None, None, None, None),)
    base = NeutralBundle(
        tests=row,
        coverage={"<import>": {"m.py": (1,)}, "t.py::a": {"m.py": (2,)}},
        static={"m.py": StaticFile("m.py", 2, (1, 2), (), ())},
        **common,  # type: ignore[arg-type]
    )
    now = NeutralBundle(
        tests=row,
        coverage={"<import>": {"m.py": (3,)}},
        static={"m.py": StaticFile("m.py", 4, (3, 4), (), ())},
        **common,  # type: ignore[arg-type]
    )
    b = input_from_bundle(base, source=tmp_path / "b", kind="python", repo_root=repo)
    n = input_from_bundle(now, source=tmp_path / "n", kind="python", repo_root=repo)
    c = compare(b, n)
    m = next(f for f in c.files if f.path == "m.py")
    assert m.status == "changed" and m.lost == () and m.compared_fns == 0
    assert not [f for f in c.functions if f.status in ("body unchanged", "body changed")]
    assert any("could not be matched" in note for note in c.notes)
    assert any("read from the local tree" in note for note in c.notes)


# -- the CLI -------------------------------------------------------------------------------------


def _cli():  # type: ignore[no-untyped-def]
    from coverset.cli import cli

    return cli


def test_cli_compare_with_itself_and_bad_bases(fixture_out: FixtureRun, tmp_path: Path) -> None:
    runner = CliRunner()
    out = tmp_path / "same"
    result = runner.invoke(
        _cli(),
        [
            "report",
            str(fixture_out.out),
            "--repo-root",
            str(fixture_out.project),
            "--compare",
            str(fixture_out.out),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "changes since base: 0 lines lost, 0 classes lost, 0 tests changed" in result.stderr
    changes = (out / "changes.html").read_text(encoding="utf-8")
    assert "No line ran in one run and not in the other" in changes
    assert "data-test-sets" not in changes
    missing = runner.invoke(
        _cli(),
        ["report", str(fixture_out.out), "--compare", str(tmp_path / "nope"), "--out", str(tmp_path / "x")],
    )
    assert missing.exit_code == 1 and "the base" in missing.output and "does not exist" in missing.output
    assert "Traceback" not in missing.output
    garbage = tmp_path / "garbage.tar.gz"
    garbage.write_bytes(b"not a tarball at all")
    bad = runner.invoke(
        _cli(), ["report", str(fixture_out.out), "--compare", str(garbage), "--out", str(tmp_path / "y")]
    )
    assert bad.exit_code == 1 and "cannot read the base" in bad.output, bad.output
    assert not (tmp_path / "y").exists()
    empty_tar = tmp_path / "empty.tar.gz"
    with tarfile.open(empty_tar, "w:gz"):
        pass
    worse = runner.invoke(
        _cli(), ["report", str(fixture_out.out), "--compare", str(empty_tar), "--out", str(tmp_path / "z")]
    )
    assert worse.exit_code == 1 and "base" in worse.output, worse.output
    both = runner.invoke(
        _cli(), ["report", str(fixture_out.out), "--compare", str(fixture_out.out), "--compare-run", "r"]
    )
    assert both.exit_code == 2


# -- the demo: PR #1 against main-6 ---------------------------------------------------------------

REFUND_TESTS = {
    "tests/test_refund.py::test_refund_partial",
    "tests/test_refund.py::test_refund_rejects_positive_total",
    "tests/test_refund.py::test_refund_splits_evenly_divisible_amounts",
}


@pytest.fixture(scope="module")
def pr1_vs_main(demo: DemoData, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, object]:
    assert demo.main_run, "the demo has a main baseline at PR #1's base"
    base = asyncio.run(load_run(demo.settings, demo.main_run))
    now = asyncio.run(load_run(demo.settings, demo.pr1_run))
    out = tmp_path_factory.mktemp("pr1-vs-main") / "out"
    result = build_report(now, out, base=base)
    return out, result.changes


def test_demo_pr1_against_main(pr1_vs_main: tuple[Path, object], demo: DemoData) -> None:
    from coverset.facts.store import facts_store
    from coverset.web.facts_view import run_facts

    out, c = pr1_vs_main
    assert c is not None
    added = {t.nodeid for t in c.tests_of("added")}  # type: ignore[attr-defined]
    assert added == REFUND_TESTS
    failing = [t for t in c.tests_of("added") if t.now_outcome == "failed"]  # type: ignore[attr-defined]
    assert [t.nodeid for t in failing] == ["tests/test_refund.py::test_refund_partial"]
    assert not c.tests_of("removed") and not c.tests_of("outcome")  # type: ignore[attr-defined]
    refund = [f for f in c.functions if f.qualname == "refund"]  # type: ignore[attr-defined]
    assert len(refund) == 1 and refund[0].status == "new" and refund[0].path == "src/ledger/billing/refund.py"
    assert [f.path for f in c.files if f.status == "added"] == ["src/ledger/billing/refund.py"]  # type: ignore[attr-defined]
    # nothing is reported lost that was not: check against the facts store directly
    store = facts_store(demo.settings)  # type: ignore[arg-type]
    base = run_facts(store, demo.repo_id, demo.main_run)
    now = run_facts(store, demo.repo_id, demo.pr1_run)
    really_lost = {
        path: base.union_lines()[path] - now.union_lines().get(path, set())
        for path in base.statics
        if path in now.statics
    }
    assert not any(really_lost.values())
    assert c.lines_lost == 0 and c.lines_not_rerun == 0  # type: ignore[attr-defined]
    assert c.classes_lost == 0 and not c.base_only  # type: ignore[attr-defined]
    assert not [f for f in c.functions if f.uncovered or f.status == "removed"]  # type: ignore[attr-defined]
    # PR #1's refund test passes a negative total into allocate_cents for the first time
    allocate = next(f for f in c.functions if f.qualname == "allocate_cents")  # type: ignore[attr-defined]
    assert "total_cents:negative" in allocate.classes_gained
    pages = all_pages(out)
    index = pages["index.html"]
    assert "0 lines lost, 0 classes lost, 3 tests changed" in re.sub(r"<[^>]+>", "", index)
    changes = pages["changes.html"]
    parse_page(changes)
    for nodeid in REFUND_TESTS:
        assert f'data-test-change="added" data-nodeid="{nodeid}"' in changes
    assert '<span class="oc oc-failed">failed</span>' in changes
    assert "data-test-sets" not in changes  # PR #1 ran every test of main-6


def test_demo_cli_compare_run(demo: DemoData, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = demo.settings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(settings.data_dir))  # type: ignore[attr-defined]
    monkeypatch.setenv("COVERSET_DATABASE_URL", str(settings.database_url))  # type: ignore[attr-defined]
    monkeypatch.setenv("COVERSET_BLOB_URL", str(settings.blob_url))  # type: ignore[attr-defined]  # "" = files
    runner = CliRunner()
    result = runner.invoke(
        _cli(),
        ["report", "--run", demo.pr1_run, "--compare-run", demo.main_run, "--out", str(tmp_path / "o")],
    )
    assert result.exit_code == 0, result.output
    assert "changes since base: 0 lines lost, 0 classes lost, 3 tests changed" in result.stderr
    assert (tmp_path / "o" / "changes.html").is_file()
    missing = runner.invoke(
        _cli(), ["report", "--run", demo.pr1_run, "--compare-run", "run_nope", "--out", str(tmp_path / "p")]
    )
    assert missing.exit_code == 1 and "cannot read the base run" in missing.output
    data = json.dumps(result.output)
    assert "Traceback" not in data


# -- review fixes: tests not selected, mismatched bases, own regions, classes of uncalled functions --


def test_a_deselected_test_is_not_selected_and_not_counted(tmp_path: Path) -> None:
    """A base test missing from this run while its file still has tests here was deselected (``-k``)
    or deleted from the file: listed as "not selected", never counted as a change."""
    base = bundle(
        {SAME: (1, 2)},
        [("t::a", "passed", 0.1), ("t::b", "passed", 0.1), ("u::c", "passed", 0.1, "u.ts")],
        {"t::a": {SAME: (1,)}, "t::b": {SAME: (2,)}, "u::c": {SAME: (2,)}},
        (),
    )
    now = bundle({SAME: (1, 2)}, [("t::a", "passed", 0.1)], {"t::a": {SAME: (1, 2)}}, ())
    c = compare(as_input(base, "b"), as_input(now, "n"))
    assert {(t.kind, t.nodeid) for t in c.tests} == {("not selected", "t::b"), ("removed", "u::c")}
    assert c.tests_changed == 1
    out = tmp_path / "out"
    build_report(as_input(now, "n"), out, base=as_input(base, "b"))
    changes = (out / "changes.html").read_text(encoding="utf-8")
    assert '<tr data-test-change="not selected" data-nodeid="t::b">' in changes
    assert ">not selected</span>" in changes


def test_a_base_of_another_repository_is_flagged() -> None:
    other = bundle({"lib/x.py": (1,)}, [("x::t", "passed", 0.1, "x.py")], {"x::t": {"lib/x.py": (1,)}}, ())
    c = compare(as_input(other, "b"), as_input(base_bundle(), "n"))
    assert len(c.warnings) == 1 and "share no file path and no test id" in c.warnings[0]
    assert "compared anyway" in c.warnings[0]
    assert not compare(as_input(base_bundle(), "b"), as_input(now_bundle(), "n")).warnings


def test_a_store_base_of_another_repository_is_flagged(pair: tuple[ReportInput, ReportInput]) -> None:
    import dataclasses

    base, now = pair
    stored = dataclasses.replace(
        base, commit="0" * 40, meta={**base.meta, "run": {"repository": "acme/elsewhere"}}
    )
    c = compare(stored, now)
    assert any("acme/elsewhere" in w and "'elsewhere'" in w for w in c.warnings)


def _py_project(root: Path, text: str, tests: str) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "mod.py").write_text(text, encoding="utf-8")
    (root / "test_mod.py").write_text(tests, encoding="utf-8")


def _neutral_rows(project: Path) -> tuple[FunctionRegion, ...]:
    from coverset.analysis.functions import index_functions

    return tuple(index_functions("pkg/mod.py", (project / "pkg" / "mod.py").read_text(encoding="utf-8")))


def test_a_python_base_uses_its_shipped_rows_over_the_local_text(tmp_path: Path) -> None:
    """M1: a ``--coverset-out`` base indexes its functions from its ``neutral/`` rows, not from the
    local text (this run's), so a changed file is still compared on unchanged bodies, and the
    base's own totals count its called functions."""
    import dataclasses

    from coverset.report.load import input_from_bundle

    base_text = "def add(a: int, b: int) -> int:\n    if a < 0:\n        return -1\n    return a + b\n"
    now_text = "X = 1\nY = 2\nZ = 3\n" + base_text
    old, new = tmp_path / "old", tmp_path / "new"
    _py_project(old, base_text, "")
    _py_project(new, now_text, "")
    base_rows = _neutral_rows(old)
    common = {"meta": {}, "collected": (), "run_arcs": {}, "sampling": (), "durations": {}, "types": {}}
    rows = (TestRow("t.py::neg", "t.py", "passed", 0.1, None, None, None, None, None),)
    add_b = base_rows[-1]
    base = NeutralBundle(
        tests=rows,
        coverage={"<import>": {"pkg/mod.py": (1,)}, "t.py::neg": {"pkg/mod.py": (2, 3)}},
        static={"pkg/mod.py": StaticFile("pkg/mod.py", 4, (1, 2, 3, 4), (), ())},
        shapes=(
            ShapeObs(add_b.triple, "a", "int|neg", "t.py::neg", 1, ("t.py::neg",), False, "-1", "t.py::neg"),
        ),
        functions=base_rows,
        **common,  # type: ignore[arg-type]
    )
    add_n = next(r for r in _neutral_rows(new) if r.qualname == "add")
    now = NeutralBundle(
        tests=rows,
        coverage={"<import>": {"pkg/mod.py": (1, 2, 3, 4)}, "t.py::neg": {"pkg/mod.py": (5, 7)}},
        static={"pkg/mod.py": StaticFile("pkg/mod.py", 7, (1, 2, 3, 4, 5, 6, 7), (), ())},
        shapes=(
            ShapeObs(add_n.triple, "a", "int|pos", "t.py::neg", 1, ("t.py::neg",), False, "1", "t.py::neg"),
        ),
        functions=(),
        **common,  # type: ignore[arg-type]
    )
    n = input_from_bundle(now, source=tmp_path / "n", kind="python", repo_root=new)
    # without its rows the base borrows this run's text: nothing in the changed file can be matched
    borrowed = input_from_bundle(
        dataclasses.replace(base, functions=()), source=tmp_path / "b", kind="python", repo_root=new
    )
    c0 = compare(borrowed, n)
    assert c0.lines_lost == 0 and any("could not be matched" in note for note in c0.notes)
    # with them (load_base passes prefer_shipped), the body is unchanged and line 6 (base 3) is lost
    b = input_from_bundle(base, source=tmp_path / "b", kind="python", repo_root=new, prefer_shipped=True)
    assert b.own_regions == frozenset({"pkg/mod.py"})
    c = compare(b, n)
    add = next(f for f in c.functions if f.qualname == "add")
    assert add.status == "body unchanged" and add.lost_lines == (6,)
    assert add.classes_lost == ("a:negative",)
    assert c.lines_lost == 1 and c.classes_lost == 1
    assert {r.key: r for r in c.totals}["called"].text == "1 → 1 (±0)"


def test_load_base_reads_the_neutral_copy(fixture_out: FixtureRun) -> None:
    from coverset.report.facts import bundle_regions
    from coverset.report.load import load_base, load_bundle, read_source

    assert (fixture_out.out / "neutral" / "manifest.json").is_file()
    plain = load_bundle(fixture_out.out, repo_root=fixture_out.project)
    base = load_base(fixture_out.out, repo_root=fixture_out.project)
    assert not plain.own_regions  # SOURCE itself is indexed from the local text, as before
    shipped = bundle_regions(read_source(fixture_out.out / "neutral").bundle)
    assert base.own_regions == {r.path for r in shipped} & set(base.facts.statics)
    triple = lambda r: (r.path, r.qualname, r.ordinal, r.def_line, r.end_line)  # noqa: E731
    assert {triple(r) for r in base.facts.regions} == {
        triple(r) for r in shipped if r.path in base.facts.statics
    }


def test_an_uncalled_function_loses_its_classes(tmp_path: Path) -> None:
    """M2: a function no test calls any more reports the classes it was passed as lost (or not
    re-run), never as "no longer expected"; with shapes off in this run, classes are not compared."""
    base = bundle(
        {SAME: tuple(range(1, 5))},
        [("t::keep", "passed", 0.1), ("t::gone", "passed", 0.1, "gone.ts")],
        {"<import>": {SAME: (1,)}, "t::keep": {SAME: (3,)}, "t::gone": {SAME: (4,)}},
        (F,),
        (shape(F, "int|pos", "t::keep"), shape(F, "int|zero", "t::gone")),
    )
    now = bundle({SAME: tuple(range(1, 5))}, [("t::keep", "passed", 0.1)], {"<import>": {SAME: (1,)}}, (F,))
    c = compare(as_input(base, "b"), as_input(now, "n"))
    f = next(x for x in c.functions if x.qualname == "f")
    assert f.uncovered and f.classes_lost == ("n:positive",) and f.classes_not_rerun == ("n:zero",)
    assert not f.classes_dropped
    off = dataclasses_replace_meta(as_input(now, "n"), {"shapes": "off"})
    c2 = compare(as_input(base, "b"), off)
    f2 = next(x for x in c2.functions if x.qualname == "f")
    assert not (f2.classes_lost or f2.classes_not_rerun or f2.classes_dropped or f2.classes_gained)
    assert any("Input classes are not compared" in note for note in c2.notes)


def test_a_class_is_dropped_only_when_the_declared_types_changed() -> None:
    import dataclasses

    cov = {"t::k": {SAME: (3,)}}
    now = bundle({SAME: (1, 2, 3)}, [("t::k", "passed", 0.1)], cov, (F,), (shape(F, "int|pos", "t::k"),))
    for declared, key, dropped in (({"kind": "bool"}, "bool|true", True), (INT, "int|neg", False)):
        base = dataclasses.replace(
            bundle({SAME: (1, 2, 3)}, [("t::k", "passed", 0.1)], cov, (F,), (shape(F, key, "t::k"),)),
            types={F.triple: {"n": declared}},
        )
        f = next(x for x in compare(as_input(base, "b"), as_input(now, "n")).functions if x.qualname == "f")
        missing = set(f.classes_lost) | set(f.classes_dropped)
        if dropped:  # declared bool then, int now: the bool class is no longer expected
            assert missing and set(f.classes_dropped) == missing, f
        else:  # the same declared types: a class missing now is lost, never dropped
            assert f.classes_lost == ("n:negative",) and not f.classes_dropped


def dataclasses_replace_meta(inp: ReportInput, meta: dict[str, object]) -> ReportInput:
    import dataclasses

    return dataclasses.replace(inp, meta={**inp.meta, "meta": meta})


def test_not_rerun_lines_of_a_changed_file_link_to_this_runs_lines(tmp_path: Path) -> None:
    """In a changed file, not-re-run lines are already in this run's numbering: they link too."""
    f0, f1 = region(MOVED, "p", 1, 3, "P"), region(MOVED, "p", 3, 5, "P")
    base = bundle(
        {MOVED: (1, 2, 3)},
        [("t::keep", "passed", 0.1), ("t::gone", "passed", 0.1, "gone.ts")],
        {"<import>": {MOVED: (1,)}, "t::gone": {MOVED: (2, 3)}},
        (f0,),
    )
    now = bundle(
        {MOVED: (1, 2, 3, 4, 5)}, [("t::keep", "passed", 0.1)], {"<import>": {MOVED: (1, 2, 3)}}, (f1,)
    )
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / MOVED).write_text("".join(f"line_{i}();\n" for i in range(1, 6)), encoding="utf-8")
    out = tmp_path / "out"
    result = build_report(as_input(now, "n", repo), out, base=as_input(base, "b"))
    assert result.changes is not None
    moved = next(f for f in result.changes.files if f.path == MOVED)
    assert moved.status == "changed" and moved.not_rerun == (4, 5)
    changes = (out / "changes.html").read_text(encoding="utf-8")
    assert f'<a class="line-ref not-rerun" href="files/{MOVED}.html#L4">4</a>' in changes


def test_cli_summary_counts_are_pluralised() -> None:
    from coverset.units import plural

    assert plural(1, "line") == "1 line" and plural(2, "line") == "2 lines"
    assert plural(1, "class", "classes") == "1 class" and plural(0, "class", "classes") == "0 classes"
