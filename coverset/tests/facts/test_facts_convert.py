"""Bundle conversion: numbits, coverage.sqlite/json, first lines, shapes, collect and links."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import numpy as np
import pytest
from coverage.numbits import nums_to_numbits
from facts_helpers import (
    FIXTURE_DIR,
    MODULE_PATH,
    T_ADD,
    T_BOX,
    T_FAIL,
    T_NEG,
    TESTS,
    build_minimal_bundle,
    measure,
)
from hypothesis import given
from hypothesis import strategies as st

from coverset.facts.convert import (
    first_lines,
    is_shape_key,
    numbits_to_lines,
    read_python_bundle,
    tracer_mismatch,
)
from coverset.lang.base import IMPORT_CONTEXT
from coverset_model.io import safe_rel_path


@given(st.sets(st.integers(min_value=1, max_value=20_000), max_size=300))
def test_numbits_round_trip(lines: set[int]) -> None:
    decoded = numbits_to_lines(nums_to_numbits(lines))
    assert decoded.tolist() == sorted(lines)
    assert decoded.dtype.kind == "i"


def test_numbits_empty_blob() -> None:
    assert numbits_to_lines(b"").tolist() == []
    assert numbits_to_lines(np.zeros(4, np.uint8).tobytes()).tolist() == []


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("src/a.py", True),
        ("a.py", True),
        ("/etc/passwd", False),
        ("../a.py", False),
        ("src/../a.py", False),
        ("./a.py", False),
        ("a//b.py", False),
        ("a\\b.py", False),
        ("C:/x.py", False),
        ("a\0b", False),
        ("", False),
        (3, False),
    ],
)
def test_safe_rel_path(value: object, ok: bool) -> None:
    assert (safe_rel_path(value) == value) is ok


# -- reading a real bundle ----------------------------------------------------------------------


def test_contexts_become_nodeids_and_import(minimal_bundle: Path) -> None:
    bundle = read_python_bundle(minimal_bundle)
    assert set(bundle.coverage) == {IMPORT_CONTEXT, *TESTS}
    calc_import = bundle.coverage[IMPORT_CONTEXT][MODULE_PATH]
    assert {4, 8, 17, 25, 26, 29} <= set(calc_import)  # def/class lines run at import time
    assert bundle.coverage[T_ADD] == {MODULE_PATH: (5,)}
    assert bundle.coverage[T_NEG][MODULE_PATH] == (10, 11)
    assert {30, 31, 32, 33, 10, 12, 14} <= set(bundle.coverage[T_BOX][MODULE_PATH])


def test_static_facts_from_coverage_json(minimal_bundle: Path) -> None:
    bundle = read_python_bundle(minimal_bundle)
    calc = bundle.static[MODULE_PATH]
    assert 18 in calc.statements and 5 in calc.statements
    assert set(calc.excluded) == {21, 22}
    assert not set(calc.excluded) & set(calc.statements)
    assert set(calc.arcs_possible) == {(10, 11), (10, 12), (12, 13), (12, 14), (31, 32), (31, 33)}
    assert calc.n_lines == 33  # largest line seen without a source root
    assert set(bundle.run_arcs[MODULE_PATH]) == set(calc.arcs_possible)
    assert bundle.static["src/factspkg/__init__.py"].statements == ()


def test_n_lines_from_source_root(minimal_bundle: Path) -> None:
    bundle = read_python_bundle(minimal_bundle, source_root=minimal_bundle.parent / "repo")
    assert bundle.static[MODULE_PATH].n_lines == 33
    assert bundle.static["src/factspkg/__init__.py"].n_lines == 1


def test_results_collect_durations_meta(minimal_bundle: Path) -> None:
    bundle = read_python_bundle(minimal_bundle)
    by_id = {row.nodeid: row for row in bundle.tests}
    assert set(by_id) == set(TESTS)
    assert by_id[T_FAIL].outcome == "failed" and by_id[T_FAIL].failed_phase == "call"
    assert by_id[T_ADD].outcome == "passed" and by_id[T_ADD].file == "tests/test_calc.py"
    collected = {item.nodeid: item for item in bundle.collected}
    assert collected[T_ADD].invariant_ids == ("add_commutes",)
    assert collected[T_NEG].markers == ("parametrize",)
    assert bundle.durations[T_ADD] == pytest.approx(0.01)
    convert = bundle.meta["convert"]
    assert convert["dropped"] == {} and convert["missing_files"] == []
    assert len(convert["bundle_sha256"]) == 64
    assert bundle.meta["tracer"] == "CTracer"


def test_shapes_decoded_per_addendum_b(minimal_bundle: Path) -> None:
    bundle = read_python_bundle(minimal_bundle)
    classify = (MODULE_PATH, "classify", 8)
    total = (MODULE_PATH, "Box.total", 29)
    neg = [o for o in bundle.shapes if o.func == classify and o.shape_key == "int|neg"]
    assert len(neg) == 1
    assert neg[0].caller == T_NEG and neg[0].tests == (T_NEG,) and neg[0].example == "-3"
    from_total = [o for o in bundle.shapes if o.caller == total]
    assert len(from_total) == 1 and from_total[0].samples == 2 and from_total[0].tests == (T_BOX,)
    receiver = [o for o in bundle.shapes if o.param == "<receiver>"]
    assert receiver[0].shape_key == "obj:factspkg.calc.Box" and receiver[0].example is None
    sampling = {s.func: s for s in bundle.sampling}
    assert sampling[classify].truncated and sampling[classify].calls == 5
    assert not sampling[total].truncated


def test_committed_fixture_matches_generator(tmp_path: Path) -> None:
    committed = read_python_bundle(FIXTURE_DIR / "bundle")
    fresh = read_python_bundle(build_minimal_bundle(tmp_path))
    assert committed.coverage == fresh.coverage
    assert committed.static == fresh.static
    assert committed.shapes == fresh.shapes
    assert committed.tests == fresh.tests
    with tarfile.open(FIXTURE_DIR / "bundle.tar.gz") as tar:
        assert sorted(tar.getnames()) == sorted(p.name for p in (FIXTURE_DIR / "bundle").iterdir())


def test_line_coverage_numbits_path(tmp_path: Path) -> None:
    """Without branch coverage coverage.py stores numbits in line_bits; they decode the same way."""
    bundle_dir = tmp_path / "b"
    bundle_dir.mkdir()
    db = sqlite3.connect(bundle_dir / "coverage.sqlite")
    db.executescript(
        "CREATE TABLE file (id integer primary key, path text, unique (path));"
        "CREATE TABLE context (id integer primary key, context text, unique (context));"
        "CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);"
    )
    db.execute("INSERT INTO file VALUES (1, 'src/m.py'), (2, '/abs/evil.py')")
    db.execute("INSERT INTO context VALUES (1, ''), (2, 't.py::t')")
    db.execute(
        "INSERT INTO line_bits VALUES (1, 1, ?), (1, 2, ?), (2, 2, ?)",
        [
            nums_to_numbits([1, 2]),
            nums_to_numbits([5, 9, 70]),
            nums_to_numbits([1]),
        ],
    )
    db.commit()
    db.close()
    (bundle_dir / "results.jsonl").write_text(json.dumps({"nodeid": "t.py::t", "outcome": "passed"}) + "\n")
    bundle = read_python_bundle(bundle_dir)
    assert bundle.coverage == {IMPORT_CONTEXT: {"src/m.py": (1, 2)}, "t.py::t": {"src/m.py": (5, 9, 70)}}
    assert bundle.static == {}
    assert bundle.meta["convert"]["dropped"]["bad_paths"] == 1
    assert "coverage.json" in bundle.meta["convert"]["missing_files"]


# -- first lines of multi-line statements ---------------------------------------------------------

MULTILINE_SOURCE = """import functools


def noop(*a, **k):
    return None


def deco(f):
    @functools.wraps(f)
    def inner(*a, **k):
        return f(*a, **k)
    return inner


def paren():
    (
        noop()
    )
    return 1


def lst():
    x = [
        noop(y)
        for y in range(2)
    ]
    return x


@deco
@deco
def decorated(
    a,
    b=2,
):
    return (a
            + b)


def flow(n):
    try:
        if n:
            noop()
        else:
            noop(
                n,
            )
    except ValueError:
        pass
    finally:
        noop()
    match n:
        case 0:
            return "zero"
        case _:
            pass
    while n > 0:
        n -= 1
    else:
        noop()
    g = (lambda q:
         q + 1)
    return g(
        n
    )


def excluded():  # pragma: no cover
    return (
        1
    )


class K:
    attr = noop(
        1,
    )

    def meth(self):
        return {
            k: v
            for k, v in {}.items()
        }
"""

MULTILINE_CALLS = {
    "t::paren": lambda m: m.paren(),
    "t::lst": lambda m: m.lst(),
    "t::decorated": lambda m: m.decorated(1),
    "t::flow0": lambda m: m.flow(0),
    "t::flow2": lambda m: m.flow(2),
    "t::excluded": lambda m: m.excluded(),
    "t::meth": lambda m: m.K().meth(),
}


def test_per_test_lines_are_statement_first_lines(tmp_path: Path) -> None:
    """Per-test lines agree with coverage.py's own (translated) executed lines, test by test."""
    bundle_dir, _ = measure(tmp_path, MULTILINE_SOURCE, MULTILINE_CALLS, package="mlpkg")
    bundle = read_python_bundle(bundle_dir)
    path = "src/mlpkg/calc.py"
    statements = set(bundle.static[path].statements)
    union = {line for by_path in bundle.coverage.values() for line in by_path.get(path, ())}
    report = json.loads((bundle_dir / "coverage.json").read_text())["files"][path]
    assert union & statements == set(report["executed_lines"]) & statements
    paren = set(bundle.coverage["t::paren"][path])
    assert {16, 19} <= paren and 17 not in paren  # `(` starts the statement; `noop()` is line 17
    assert bundle.meta["convert"]["dropped"] == {}


def test_first_lines_maps_continuations_and_keeps_excluded_lines() -> None:
    statements, excluded = (5, 6, 9, 20), (12, 13)
    raw = np.array([7, 9, 8, 13, 14, 3, 25])
    assert first_lines(raw, statements, excluded).tolist() == [3, 6, 9, 13, 14, 20]
    assert first_lines(np.array([4, 2]), (), ()).tolist() == [2, 4]


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        ("int|pos", True),
        ("NoneType", True),
        ("str|ascii|kind:email/multi-label-domain|len:11-100", True),
        ("list|elem:obj:pkg.mod.f.<locals>.C|len:2-10|order:n/a", True),
        ("obj:ledger.util.money.Money|dataclass", True),
        ("int|zero|big", False),  # features must be sorted (canonical)
        ("integer|pos", False),
        ("obj:", False),
        ("int||pos", False),
        ("int|po s", False),
        ("int|\x00", False),
        ("", False),
        (7, False),
    ],
)
def test_is_shape_key(key: object, ok: bool) -> None:
    assert is_shape_key(key) is ok


def test_invariant_links_accept_the_plugin_and_legacy_keys(bundle_copy: Path) -> None:
    (bundle_copy / "invariant_links.jsonl").write_text(
        json.dumps({"test_id": T_ADD, "invariant_id": "add_commutes"})
        + "\n"
        + json.dumps({"nodeid": T_NEG, "invariant_id": "sign_total"})
        + "\n"
    )
    bundle = read_python_bundle(bundle_copy)
    collected = {item.nodeid: item for item in bundle.collected}
    assert collected[T_ADD].invariant_ids == ("add_commutes",)
    assert collected[T_NEG].invariant_ids == ("sign_total",)
    assert bundle.meta["convert"]["dropped"] == {}


@pytest.mark.security
def test_paths_outside_the_head_tree_are_dropped(minimal_bundle: Path) -> None:
    head = {MODULE_PATH, "src/factspkg/__init__.py", "tests/test_calc.py"}
    full = read_python_bundle(minimal_bundle, head_paths=head)
    assert full.meta["convert"]["dropped"] == {} and full.meta["convert"]["head_checked"]
    narrowed = read_python_bundle(minimal_bundle, head_paths=head - {"src/factspkg/__init__.py"})
    assert set(narrowed.static) == {MODULE_PATH}
    assert all("src/factspkg/__init__.py" not in by_path for by_path in narrowed.coverage.values())
    assert narrowed.meta["convert"]["dropped"]["paths_not_in_head"] >= 2  # coverage.json + sqlite
    no_tests = read_python_bundle(minimal_bundle, head_paths={MODULE_PATH})
    assert no_tests.tests == ()


@pytest.mark.security
def test_n_lines_never_follows_a_symlink_out_of_the_checkout(minimal_bundle: Path, tmp_path: Path) -> None:
    repo = Path(shutil.copytree(minimal_bundle.parent / "repo", tmp_path / "repo"))
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n" * 1000)
    (repo / MODULE_PATH).unlink()
    os.symlink(outside, repo / MODULE_PATH)
    bundle = read_python_bundle(minimal_bundle, source_root=repo)
    assert bundle.static[MODULE_PATH].n_lines == 33  # the fallback, not 1000


@pytest.mark.parametrize("tracer", ["PyTracer", "SysMonitor"])
def test_a_non_c_tracer_is_flagged(bundle_copy: Path, tracer: str) -> None:
    meta = json.loads((bundle_copy / "meta.json").read_text())
    (bundle_copy / "meta.json").write_text(json.dumps({**meta, "tracer": tracer}))
    convert = read_python_bundle(bundle_copy).meta["convert"]
    assert convert["tracer_mismatch"] is True and convert["tracer"] == tracer


def test_the_c_tracer_or_no_tracer_is_not_flagged(minimal_bundle: Path, bundle_copy: Path) -> None:
    assert "tracer_mismatch" not in read_python_bundle(minimal_bundle).meta["convert"]
    meta = json.loads((bundle_copy / "meta.json").read_text())
    del meta["tracer"]
    (bundle_copy / "meta.json").write_text(json.dumps(meta))
    assert "tracer_mismatch" not in read_python_bundle(bundle_copy).meta["convert"]


def test_tracer_mismatch_clips_untrusted_names() -> None:
    assert tracer_mismatch({"tracer": "CTracer"}) is None and tracer_mismatch({"tracer": "ctrace"}) is None
    assert tracer_mismatch({}) is None and tracer_mismatch(None) is None
    assert (
        tracer_mismatch({"tracer": "Py\x1b[31mTracer" + "x" * 200}) == "Py[31mTracer" + "x" * 51
    )  # clipped to 64, then non-printables dropped
    assert tracer_mismatch({"tracer": "\n"}) == "an unnamed tracer"
