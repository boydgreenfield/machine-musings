"""The Rust converter (capture → neutral bundle) over the committed fixture, with no cargo involved.

``fixtures/bundles/neutral-rust/capture`` is what ``coverset rust run`` left behind for
``examples/rust-app`` (cargo-nextest 0.9.148, rustc 1.98.1, one LLVM profile per test), with its
paths made relative; ``bundle/`` is the converter's output, rebuilt by
``scripts/make_rust_fixture.py --from-capture``. The line sets asserted here are LLVM's own: they
were measured, not written by hand.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from coverset.lang.rust_llvm import (
    CAPTURE_FILE,
    CaptureError,
    _failure,
    _function_digest,
    _hits_only,
    _libtest_failure,
    _measured,
    convert_capture,
    parse_junit,
    parse_lcov,
    profile_key,
    write_neutral,
)
from coverset_model.io import read_bundle, validate
from coverset_model.neutral import func_key
from coverset_model.types import to_json

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "bundles" / "neutral-rust"
CAPTURE = FIXTURE / "capture"
DATA = Path(__file__).resolve().parent / "data"
MONEY, DURATIONS, ERROR = "src/money.rs", "src/durations.rs", "src/error.rs"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_rust_fixture", ROOT / "scripts" / "make_rust_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle():  # noqa: ANN201
    return convert_capture(CAPTURE)


def test_the_fixture_validates_and_is_what_the_converter_writes() -> None:
    assert validate(FIXTURE / "bundle") == []
    assert validate(FIXTURE / "bundle.tar.gz") == []
    assert _script().main(["--check"]) == 0
    total = sum(p.stat().st_size for p in FIXTURE.rglob("*") if p.is_file())
    assert total < 200 * 1024


def test_ids_are_deterministic(bundle, tmp_path: Path) -> None:  # noqa: ANN001
    again = convert_capture(CAPTURE)
    assert again == bundle
    ids = [c.nodeid for c in bundle.collected]
    assert ids == sorted(ids) and len(ids) == len(set(ids)) == 16
    assert "ledger money::tests::refuses_to_overdraw" in ids
    assert "ledger::ledger_flow a_failed_transfer_reports_the_shortfall" in ids
    for region in bundle.functions:
        assert region.func_key == func_key(region.path, region.qualname, region.ordinal)
    write_neutral(bundle, tmp_path / "a")
    write_neutral(again, tmp_path / "b")
    for name in ("tests.jsonl", "coverage.jsonl", "functions.jsonl", "files.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert profile_key("ledger", "x") == profile_key("ledger", "x") != profile_key("ledger::x", "")


def test_function_ranges_come_from_the_sources(bundle) -> None:  # noqa: ANN001
    regions = {(r.path, r.qualname): r for r in bundle.functions}
    assert len(regions) == 12
    allocate = regions[(MONEY, "allocate_cents")]
    assert (allocate.def_line, allocate.body_start, allocate.end_line) == (19, 20, 38)
    assert allocate.params == ("total_cents", "weights") and allocate.kind == "function"
    post = regions[(MONEY, "Ledger::post")]
    assert (post.def_line, post.body_start, post.end_line, post.kind) == (64, 65, 75, "method")
    fmt = regions[(ERROR, "<LedgerError as fmt::Display>::fmt")]
    assert fmt.kind == "method" and fmt.params == ("f",)
    assert not any("tests::" in qualname for _, qualname in regions)  # test functions are not regions
    types = bundle.types[allocate.triple]
    assert to_json(types["weights"]) == {"kind": "list", "elem": {"kind": "int"}}
    assert to_json(types["return"])["arms"][1] == {
        "kind": "enum",
        "name": "LedgerError",
        "members": ["NoWeights", "BadDuration", "BadEmail", "Overdrawn"],
    }
    currency = bundle.types[regions[(MONEY, "format_cents")].triple]["currency"]
    assert currency is not None and currency.members == ("Usd", "Eur", "Jpy")
    llvm = bundle.meta["llvm_functions"]
    assert llvm["instrumented"] == llvm["regions"] == 12
    assert llvm["unexplained"] == [] and llvm["not_instrumented"] == []
    assert llvm["monomorphised"] == {"src/intervals.rs::largest": 2}  # f64 and i32


def test_line_sets_per_test(bundle) -> None:  # noqa: ANN001
    cov = bundle.coverage
    # the early return of allocate_cents, then its closing brace: nothing else of the function
    assert cov["ledger money::tests::refuses_zero_weights"] == {MONEY: (19, 20, 21, 22, 38)}
    # the transfer's first posting fails, so `self.post(to, cents)?` (85) and `Ok(())` (86) never run
    transfer = cov["ledger::ledger_flow a_failed_transfer_reports_the_shortfall"][MONEY]
    assert {83, 84, 87} <= set(transfer) and not {85, 86} & set(transfer)
    # the unit test that formats the error reaches the Display impl in another file
    assert cov["ledger money::tests::refuses_to_overdraw"][ERROR] == (19, 20, 24, 25, 28)
    assert "ledger durations::tests::parses_days" not in cov  # ignored: never ran
    money = bundle.static[MONEY]
    assert 22 in money.statements and not set(money.statements) & set(range(90, 124))
    assert set(money.excluded) <= set(range(90, 124)) and 116 in money.excluded  # the test module
    for lines in (lines for per_file in cov.values() for lines in per_file.values()):
        assert list(lines) == sorted(set(lines))


def test_outcomes_files_and_lines_of_tests(bundle) -> None:  # noqa: ANN001
    collected = {c.nodeid: c for c in bundle.collected}
    results = {t.nodeid: t for t in bundle.tests}
    unit = collected["ledger money::tests::refuses_to_overdraw"]
    assert (unit.file, unit.line) == (MONEY, 115)  # the #[test] attribute line
    flow = collected["ledger::ledger_flow signup_rejects_a_bad_domain"]
    assert (flow.file, flow.line) == ("tests/ledger_flow.rs", 39)
    ignored = collected["ledger durations::tests::parses_days"]
    assert ignored.markers == ("ignore",) and results[ignored.nodeid].outcome == "skipped"
    assert sorted(t.outcome for t in bundle.tests) == ["passed"] * 15 + ["skipped"]
    assert bundle.run_arcs == {} and bundle.shapes == () and bundle.sampling == ()
    assert bundle.meta["runner"] == "nextest" and "arcs" in bundle.meta


def test_the_capture_reads_against_a_moved_root(tmp_path: Path) -> None:
    repo = tmp_path / "elsewhere"
    shutil.copytree(FIXTURE / "repo", repo)
    moved = convert_capture(CAPTURE, root=repo)
    assert moved.coverage == convert_capture(CAPTURE).coverage
    empty = tmp_path / "empty"
    empty.mkdir()
    assert convert_capture(CAPTURE, root=empty).functions == ()  # no sources: no regions, no statements


def test_a_bad_capture_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="cannot read"):
        convert_capture(tmp_path)
    (tmp_path / CAPTURE_FILE).write_text(json.dumps({"format": "something-else"}))
    with pytest.raises(CaptureError, match="not a coverset Rust capture"):
        convert_capture(tmp_path)
    (tmp_path / CAPTURE_FILE).write_text(json.dumps({"format": "coverset-rust-capture", "version": 9}))
    with pytest.raises(CaptureError, match="version"):
        convert_capture(tmp_path)
    capture = tmp_path / "capture"
    shutil.copytree(CAPTURE, capture)
    doc = json.loads((capture / CAPTURE_FILE).read_text())
    doc["root"] = str(FIXTURE / "repo")
    doc["tests"][1]["export"] = "../../../../etc/passwd"
    (capture / CAPTURE_FILE).write_text(json.dumps(doc))
    with pytest.raises(CaptureError, match="leaves the capture directory"):
        convert_capture(capture)
    doc["tests"][1]["export"] = None
    doc["tests"][2]["duration_s"] = "slow"
    (capture / CAPTURE_FILE).write_text(json.dumps(doc))
    with pytest.raises(CaptureError, match="malformed capture"):
        convert_capture(capture)
    doc["tests"][2]["duration_s"] = 0.1
    doc["tests"][2]["outcome"] = "exploded"  # not an outcome of the format: the test never ran
    (capture / CAPTURE_FILE).write_text(json.dumps(doc))
    odd = convert_capture(capture)
    name = f"{doc['tests'][2]['binary_id']} {doc['tests'][2]['name']}"
    assert name in {c.nodeid for c in odd.collected} and name not in {t.nodeid for t in odd.tests}


def test_parse_lcov_and_hits_only() -> None:
    text = "SF:/x/src/a.rs\nFN:3,_RNvf\nDA:3,2\nDA:4,0\nDA:5,1\nLF:3\nLH:2\nend_of_record\nSF:src/b.rs\nDA:1,0\nend_of_record\n"
    assert parse_lcov(text) == {"/x/src/a.rs": {3: 2, 4: 0, 5: 1}, "src/b.rs": {1: 0}}
    assert _hits_only(text) == "SF:/x/src/a.rs\nDA:3,2\nDA:5,1\nend_of_record\nSF:src/b.rs\nend_of_record\n"
    assert parse_lcov("DA:1,1\nSF:a\nDA:x,1\nDA:2,3\nDA:2,1\n") == {"a": {2: 4}}  # merged repeats


def test_parse_junit_of_a_real_failing_run() -> None:
    results = parse_junit((DATA / "nextest-junit.xml").read_text(encoding="utf-8"))
    failed = results[("ledger::ledger_flow", "deliberately_fails")]
    outcome, duration, message, sig, start = failed
    assert outcome == "failed" and duration is not None and start is not None
    assert message is not None and message.startswith("thread 'deliberately_fails' panicked at")
    assert "(6" not in message.split("\n", 1)[0]  # the thread id is gone: messages are stable
    assert "remainder <goes> first" in message
    assert sig == _failure("thread 't' (1) panicked at tests/ledger_flow.rs:48:5:\nother text", "x")[1]
    passed = [v for v in results.values() if v[0] == "passed"]
    assert len(passed) == 15 and all(v[2] is None for v in passed)


def test_failure_signatures_hash_the_location_not_the_message() -> None:
    a = _failure("thread 'a' (12) panicked at src/x.rs:3:9:\nleft: 1", "t1")
    b = _failure("thread 'b' (99) panicked at src/x.rs:3:1:\nleft: 2", "t2")
    c = _failure("thread 'a' (12) panicked at src/x.rs:4:9:\nleft: 1", "t1")
    assert a[1] == b[1] != c[1]
    assert a[0] == "thread 'a' panicked at src/x.rs:3:9:\nleft: 1"
    assert _failure(None, "t1")[1] == _failure("", "t1")[1] != _failure(None, "t2")[1]
    assert len(_failure("x" * 20_000, "t")[0] or "") == 8192


def test_libtest_failure_section() -> None:
    output = (
        "running 1 test\ntest m::t ... FAILED\n\nfailures:\n\n---- m::t stdout ----\n\n"
        "thread 'm::t' panicked at src/m.rs:9:5:\nboom\n\n\nfailures:\n    m::t\n\ntest result: FAILED."
    )
    assert _libtest_failure(output, "m::t") == "thread 'm::t' panicked at src/m.rs:9:5:\nboom"
    assert _libtest_failure("segfault", "m::t") == "segfault"


def test_what_is_measured() -> None:
    assert _measured("src/lib.rs", ["."])
    assert _measured("crates/core/src/x.rs", ["crates/core", "crates/cli"])
    assert not _measured("tests/flow.rs", ["."])
    assert not _measured("crates/core/benches/b.rs", ["crates/core"])
    assert not _measured("build.rs", ["."]) and not _measured("src/data.txt", ["."])
    assert not _measured("vendor/x.rs", ["crates/core"])
    # A workspace whose root is a package: each file is judged by the most specific root.
    roots = [".", "crates/alpha", "crates/beta"]
    assert not _measured("crates/beta/tests/flow.rs", roots)
    assert not _measured("crates/beta/benches/b.rs", roots)
    assert not _measured("crates/alpha/build.rs", roots)
    assert _measured("crates/alpha/src/util.rs", roots) and _measured("src/main.rs", roots)
    assert not _measured("tests/root_flow.rs", roots)
    assert _measured("crates/alphabet/src/x.rs", [".", "crates/alpha"])  # a prefix, not a parent


def test_the_function_digest_tells_binaries_from_monomorphisations() -> None:
    def record(name: str, line: int) -> dict:  # type: ignore[type-arg]
        return {
            "name": name,
            "count": 1,
            "filenames": ["src/a.rs"],
            "regions": [[line, 1, line + 3, 2, 1, 0, 0, 0]],
        }

    lib = {"data": [{"functions": [record("_RNvA", 1), record("_RINvBd", 9), record("_RINvBl", 9)]}]}
    flow = {"data": [{"functions": [record("_RNvA2", 1), record("_RNCNvA", 2)]}]}
    rows = {(r["line"]): r for r in _function_digest([lib, flow])}
    assert (rows[1]["binaries"], rows[1]["instances"], rows[1]["count"]) == (2, 1, 2)
    assert (rows[9]["binaries"], rows[9]["instances"]) == (1, 2)
    assert rows[2]["closure"] and not rows[1]["closure"]
    assert rows[1]["end_line"] == 4


def test_the_written_bundle_reads_back(bundle, tmp_path: Path) -> None:  # noqa: ANN001
    write_neutral(bundle, tmp_path / "out", created_at="2026-10-13T12:00:00Z", compress=True)
    assert (tmp_path / "out" / "files.jsonl.gz").is_file()
    back = read_bundle(tmp_path / "out")
    assert back.meta["bundle"]["language"] == "rust"
    assert back.meta["bundle"]["producer"]["name"] == "coverset-rust"
    assert back.coverage == bundle.coverage and back.functions == bundle.functions
