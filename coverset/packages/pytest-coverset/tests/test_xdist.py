"""pytest-xdist: workers run the whole plugin, the controller merges one bundle equal to a serial one.

Every run is a subprocess (``pytester.runpytest_subprocess``) with real xdist workers. The merged
bundle of the fixture project is compared with a serial bundle of the same project as sets: coverage
rows per (context, file, arc), results without their timings, decoded shape rows (examples included),
``coverage.json`` without its timestamp, ``collect.json``, and the neutral export's coverage rows.
"""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from support import NONCE, contexts, coverage_rows, events, read_jsonl, read_shapes

from pytest_coverset.bundle import BUNDLE_MEMBERS
from pytest_coverset.merge import (
    ShapesMerge,
    WorkerOutput,
    merge_collect,
    merge_results,
    natural_key,
    read_workers,
)
from pytest_coverset.outcomes import TestResult

pytest.importorskip("xdist")

from pytest_coverset.parallel import XDIST_ENV, WorkerToken, _local_spec  # noqa: E402

TIMING_FIELDS = {"duration_s", "start", "stop"}


def run(project: pytest.Pytester, out: str, *args: str) -> pytest.RunResult:
    return project.runpytest_subprocess(
        f"--coverset-out={out}", "--coverset-source=fixturepkg", "--coverset-neutral", *args
    )


def results(out: Path) -> set[str]:
    rows = read_jsonl(out / "results.jsonl")
    return {json.dumps({k: v for k, v in r.items() if k not in TIMING_FIELDS}, sort_keys=True) for r in rows}


def shape_rows(out: Path) -> set[tuple[Any, ...]]:
    """Decoded shape rows: header indexes replaced by what they point at."""
    shapes = read_shapes(out)
    header = shapes.header
    tests = header["tests"]
    found: set[tuple[Any, ...]] = set()
    for row in shapes.obs:
        caller = json.dumps(header["callers"][row["c"]], sort_keys=True)
        example_test = tests[row["ext"]] if row["ext"] is not None else None
        found.add(
            (
                "obs",
                tuple(header["funcs"][row["f"]]),
                row["p"],
                header["shapes"][row["s"]],
                caller,
                row["n"],
                tuple(sorted(tests[t] for t in row["t"])),
                row["trunc"],
                row["ex"],
                example_test,
            )
        )
    for row in shapes.sampling:
        found.add(
            (
                "sampling",
                tuple(header["funcs"][row["f"]]),
                row["calls"],
                tuple(sorted(tests[t] for t in row["tests"])),
                row["truncated"],
                row["cap"],
            )
        )
    return found


def coverage_report(out: Path) -> dict[str, Any]:
    report = json.loads((out / "coverage.json").read_text())
    report["meta"].pop("timestamp", None)
    return report


def neutral_coverage(out: Path) -> set[str]:
    with gzip.open(out / "neutral" / "coverage.jsonl.gz", "rt", encoding="utf-8") as fh:
        return {json.dumps(json.loads(line), sort_keys=True) for line in fh}


def meta(out: Path) -> dict[str, Any]:
    return json.loads((out / "meta.json").read_text())


@pytest.mark.parametrize("dist", ["load", "loadfile"])
def test_the_merged_bundle_equals_the_serial_one(project: pytest.Pytester, dist: str) -> None:
    run(project, "serial").assert_outcomes(passed=24)
    run(project, "par", "-n", "2", f"--dist={dist}").assert_outcomes(passed=24)
    serial, par = project.path / "serial", project.path / "par"

    assert coverage_rows(par / "coverage.sqlite") == coverage_rows(serial / "coverage.sqlite")
    assert results(par) == results(serial)
    assert shape_rows(par) == shape_rows(serial)
    assert coverage_report(par) == coverage_report(serial)
    assert json.loads((par / "collect.json").read_text()) == json.loads((serial / "collect.json").read_text())
    assert (par / "invariant_links.jsonl").read_text() == (serial / "invariant_links.jsonl").read_text()
    assert neutral_coverage(par) == neutral_coverage(serial)
    with tarfile.open(par / "bundle.tar.gz") as tar:
        assert sorted(tar.getnames()) == sorted(BUNDLE_MEMBERS)

    info = meta(par)
    assert info["errors"] == [] and info["exit_code"] == 0
    assert info["counts"] == meta(serial)["counts"]
    assert info["coverage_paths"] == "repository" and info["tracer"] == "CTracer"
    assert info["neutral"]["status"] == "written"
    assert info["functions_traced"] == meta(serial)["functions_traced"]
    assert info["xdist"]["role"] == "controller" and info["xdist"]["dist"] == dist
    workers = info["xdist"]["workers"]
    assert [w["id"] for w in workers] == ["gw0", "gw1"]
    assert sum(w["tests"] for w in workers) == 24 and all(w["finished"] for w in workers)
    assert info["timings"]["context_switches"] == 24
    assert meta(serial)["xdist"] is None
    assert not (par / "workers").exists()  # removed after a clean merge


def test_the_controller_streams_every_event(project: pytest.Pytester) -> None:
    run(project, "out", "-n", "2").assert_outcomes(passed=24)
    seen = events(project)
    kinds = [e["kind"] for e in seen]
    assert kinds[0] == "collected" and seen[0]["count"] == 24
    assert kinds[-3:] == ["bundle_written", "neutral_written", "done"]
    tests = {e["nodeid"]: e for e in seen if e["kind"] == "test"}
    rows = {r["nodeid"]: r for r in read_jsonl(project.path / "out" / "results.jsonl")}
    assert tests.keys() == rows.keys()
    for nodeid, row in rows.items():
        event = {k: v for k, v in tests[nodeid].items() if k not in {"kind", "t"}}
        assert {k: v for k, v in event.items() if k not in TIMING_FIELDS} == {
            k: v for k, v in row.items() if k not in TIMING_FIELDS
        }


def test_failures_fold_the_same_on_the_controller(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_mixed="""
        import pytest

        class Boom(Exception):
            pass

        @pytest.fixture
        def broken():
            raise Boom("setup")

        def test_fails():
            raise Boom("call")

        def test_errors(broken):
            pass

        def test_passes():
            pass

        @pytest.mark.xfail(strict=True)
        def test_xpass_strict():
            pass
        """
    )
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    pytester.runpytest_subprocess("--coverset-out=out", "-n", "2")
    rows = {r["nodeid"]: r for r in read_jsonl(pytester.path / "out" / "results.jsonl")}
    streamed = {e["nodeid"]: e for e in events(pytester) if e["kind"] == "test"}
    assert {r["outcome"] for r in rows.values()} == {"failed", "error", "passed"}
    for nodeid, row in rows.items():
        assert streamed[nodeid]["fail_sig"] == row["fail_sig"]
        assert streamed[nodeid]["outcome"] == row["outcome"]


def test_a_crashed_worker_keeps_the_rest_and_says_so(pytester: pytest.Pytester) -> None:
    """The tests a worker finished before it died keep their results and their coverage.

    One worker runs the file in order, so ``test_a``..``test_c`` finish (well inside a second, the
    serial commit interval) before ``test_dies`` kills the process: their lines must already be
    committed to the worker's data file, or the merge would show passed tests covering nothing.
    """
    pytester.makepyfile(
        crashpkg="""
        def double(x):
            return x * 2

        def halve(x):
            return x / 2
        """,
        test_crash="""
        import os
        import crashpkg

        def test_a():
            assert crashpkg.double(2) == 4

        def test_b():
            assert crashpkg.halve(2) == 1

        def test_c():
            assert crashpkg.double(3) == 6

        def test_dies():
            crashpkg.double(1)
            os._exit(3)
        """,
    )
    result = pytester.runpytest_subprocess("--coverset-out=out", "--coverset-source=crashpkg", "-n", "1")
    assert result.ret == 1
    out = pytester.path / "out"
    rows = {r["nodeid"].split("::")[-1]: r for r in read_jsonl(out / "results.jsonl")}
    assert rows["test_dies"]["outcome"] == "failed"  # the controller's row: no worker recorded it
    assert {rows[n]["outcome"] for n in ("test_a", "test_b", "test_c")} == {"passed"}
    measured = set(contexts(out / "coverage.sqlite"))
    assert {f"test_crash.py::{n}" for n in ("test_a", "test_b", "test_c")} <= measured
    by_test = {
        ctx for ctx, path, *_ in coverage_rows(out / "coverage.sqlite") if path.endswith("crashpkg.py")
    }
    assert {f"test_crash.py::{n}" for n in ("test_a", "test_b", "test_c")} <= by_test
    info = meta(out)
    assert any("did not finish" in e for e in info["errors"])
    worker = info["xdist"]["workers"][0]
    assert worker["id"] == "gw0" and worker["finished"] is False
    assert (out / "workers").is_dir()  # kept for a look when the merge had problems
    assert (out / "workers" / ".coverset-xdist").is_file()
    assert (out / "bundle.tar.gz").is_file()


def test_a_crashed_run_is_cleared_by_the_next(pytester: pytest.Pytester) -> None:
    """Worker directories a crashed run kept (they carry the marker) are replaced by the next run."""
    pytester.makepyfile(test_ok="def test_ok():\n    pass\n")
    stale = pytester.path / "out" / "workers" / "gw7"
    stale.mkdir(parents=True)
    (stale.parent / ".coverset-xdist").write_text("0" * 32 + "\n")
    pytester.runpytest_subprocess("--coverset-out=out", "-n", "1").assert_outcomes(passed=1)
    assert not (pytester.path / "out" / "workers").exists()


def test_a_foreign_workers_directory_is_never_removed(pytester: pytest.Pytester) -> None:
    """``workers/`` in the out directory that coverset did not create (no marker) is a usage error."""
    pytester.makepyfile(test_ok="def test_ok():\n    pass\n")
    tasks = pytester.path / "workers" / "tasks.py"
    tasks.parent.mkdir()
    tasks.write_text("CELERY = True\n")
    result = pytester.runpytest_subprocess("--coverset-out=.", "-n", "2")
    assert result.ret == 4
    assert "was not created by coverset" in result.stderr.str()
    assert tasks.read_text() == "CELERY = True\n"


def test_an_empty_workers_directory_is_reused(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_ok="def test_ok():\n    pass\n")
    (pytester.path / "out" / "workers").mkdir(parents=True)
    pytester.runpytest_subprocess("--coverset-out=out", "-n", "1").assert_outcomes(passed=1)
    assert meta(pytester.path / "out")["errors"] == []


def test_local_switch_with_workers(project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    (project.path / "pyproject.toml").write_text(
        (project.path / "pyproject.toml").read_text() + '\n[tool.coverset]\nsource = ["fixturepkg"]\n'
    )
    monkeypatch.setenv("COVERSET", "1")  # popped by the controller: workers learn it from the token
    result = project.runpytest_subprocess("-n", "2")
    result.assert_outcomes(passed=24)
    lines = [line for line in result.outlines if line.startswith("coverset: wrote")]
    assert lines == [
        "coverset: wrote .coverset (24 tests, 4 files); `coverset report .coverset` for the pages"
    ]
    assert not events(project)
    assert meta(project.path / ".coverset")["xdist"]["role"] == "controller"
    assert len(coverage_rows(project.path / ".coverset" / "coverage.sqlite")) > 0


def test_no_workers_is_a_serial_run(project: pytest.Pytester) -> None:
    run(project, "out", "-n", "0").assert_outcomes(passed=24)
    assert meta(project.path / "out")["xdist"] is None


def test_remote_workers_are_a_usage_error(project: pytest.Pytester) -> None:
    result = run(project, "out", "--tx", "ssh=nobody@localhost//python=python3", "--dist=load")
    assert result.ret == 4
    assert "supports local workers only" in result.stderr.str()


def test_shapes_off_with_workers(project: pytest.Pytester) -> None:
    run(project, "out", "-n", "2", "--coverset-shapes=off").assert_outcomes(passed=24)
    info = meta(project.path / "out")
    assert info["shapes"] == "off" and info["functions_traced"] == 0 and info["errors"] == []
    assert read_shapes(project.path / "out").obs == []


# -- the pieces, without workers ------------------------------------------------------------------


def test_the_token_is_removed_and_needs_a_worker_id(tmp_path: Path) -> None:
    token = WorkerToken(out=str(tmp_path), local=True, events=False, run="r")
    env = {XDIST_ENV: token.dumps(), "PYTEST_XDIST_WORKER": "gw3"}
    popped = WorkerToken.pop(env)
    assert popped == (token, "gw3") and XDIST_ENV not in env
    assert token.worker_dir("gw3") == tmp_path / "workers" / "gw3"
    assert WorkerToken.pop({XDIST_ENV: token.dumps()}) is None  # not a worker
    assert WorkerToken.pop({XDIST_ENV: token.dumps(), "PYTEST_XDIST_WORKER": "../x"}) is None
    assert WorkerToken.pop({XDIST_ENV: "{", "PYTEST_XDIST_WORKER": "gw0"}) is None
    relative = WorkerToken(out="rel", local=False, events=False, run="r").dumps()
    assert WorkerToken.pop({XDIST_ENV: relative, "PYTEST_XDIST_WORKER": "gw0"}) is None


@pytest.mark.parametrize(
    ("spec", "ok"),
    [
        ("popen", True),
        ("4*popen", True),
        ("popen//python=python3.12", True),
        ("popen//chdir=/elsewhere", False),
        ("ssh=host//python=python3", False),
        ("socket=127.0.0.1:8888", False),
    ],
)
def test_only_local_worker_specs(spec: str, ok: bool) -> None:
    assert _local_spec(spec) is ok


def test_worker_ids_sort_naturally() -> None:
    assert sorted(["gw10", "gw2", "gw1"], key=natural_key) == ["gw1", "gw2", "gw10"]


def _result(nodeid: str, outcome: str = "passed") -> TestResult:
    return TestResult(nodeid, "t.py", outcome, 0.1, None, None, None, False, None, None, None)


def test_worker_rows_win_and_the_controller_fills_gaps(tmp_path: Path) -> None:
    a = WorkerOutput("gw0", tmp_path, results=[_result("t.py::b")])
    b = WorkerOutput("gw1", tmp_path, results=[_result("t.py::a", "failed")])
    fallback = {"t.py::a": _result("t.py::a", "error"), "t.py::c": _result("t.py::c", "failed")}
    merged = merge_results([a, b], fallback, ["t.py::a", "t.py::b", "t.py::c"])
    assert [(r.nodeid, r.outcome) for r in merged] == [
        ("t.py::a", "failed"),
        ("t.py::b", "passed"),
        ("t.py::c", "failed"),
    ]


def _shapes_file(path: Path, tests: list[str], rows: list[dict[str, Any]]) -> Path:
    header = {
        "kind": "header",
        "version": 1,
        "funcs": [["src/m.py", "f", 1]],
        "shapes": ["int|pos"],
        "tests": tests,
        "callers": [{"kind": "test", "nodeid": "<import>"}, *({"kind": "test", "nodeid": t} for t in tests)],
    }
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in (header, *rows):
            fh.write(json.dumps(row) + "\n")
    return path


def test_shapes_merge_counts_import_time_once_and_keeps_the_serial_example(tmp_path: Path) -> None:
    def obs(c: int, n: int, t: list[int], ex: str, ext: int | None) -> dict[str, Any]:
        return {
            "kind": "obs",
            "f": 0,
            "p": "x",
            "s": 0,
            "c": c,
            "n": n,
            "t": t,
            "trunc": False,
            "ex": ex,
            "ext": ext,
        }

    first = _shapes_file(
        tmp_path / "a.gz",
        ["t.py::late"],
        [
            obs(0, 2, [], "1", None),
            obs(1, 3, [0], "30", 0),
            {"kind": "func_sampling", "f": 0, "calls": 5, "tests": [0], "truncated": False, "cap": None},
        ],
    )
    second = _shapes_file(
        tmp_path / "b.gz",
        ["t.py::early"],
        [
            obs(0, 2, [], "1", None),
            obs(1, 4, [0], "40", 0),
            {"kind": "func_sampling", "f": 0, "calls": 6, "tests": [0], "truncated": True, "cap": "sample"},
        ],
    )
    merged = ShapesMerge(order={"t.py::early": 0, "t.py::late": 1})
    merged.add_file(first, "gw0")
    merged.add_file(second, "gw1")
    out = tmp_path / "out"
    out.mkdir()
    merged.write(out / "shapes.jsonl.gz")
    decoded = read_shapes(out)
    by_caller = {decoded.header["callers"][r["c"]]["nodeid"]: r for r in decoded.obs}
    assert by_caller["<import>"]["n"] == 2  # every worker imported the module: once, not twice
    assert by_caller["t.py::late"]["n"] == 3 and by_caller["t.py::early"]["n"] == 4
    assert all(r["trunc"] is True for r in decoded.obs)  # one worker capped the function
    (sampling,) = decoded.sampling
    assert sampling["calls"] == 5 + 6 - 2 and sampling["truncated"] and sampling["cap"] == "sample"
    assert sorted(decoded.header["tests"][t] for t in sampling["tests"]) == ["t.py::early", "t.py::late"]
    assert merged.problems == []


def test_a_broken_shapes_file_is_a_problem_not_a_failure(tmp_path: Path) -> None:
    broken = tmp_path / "broken.gz"
    broken.write_bytes(b"not gzip")
    merged = ShapesMerge(order={})
    merged.add_file(broken, "gw0")
    merged.add_file(tmp_path / "missing.gz", "gw1")
    assert len(merged.problems) == 1 and merged.problems[0].startswith("gw0:")


def test_the_stop_file_stops_every_worker(pytester: pytest.Pytester) -> None:
    body = "".join(f"def test_{i:02d}():\n    __import__('time').sleep(0.05)\n\n\n" for i in range(20))
    pytester.makepyfile(
        test_s="import pathlib\n\n\ndef test_make_stop():\n    pathlib.Path('STOP').write_text('x')\n\n\n"
        + body
    )
    result = pytester.runpytest_subprocess("--coverset-out=out", "--coverset-stop-file=STOP", "-n", "2")
    assert result.ret == 2
    info = meta(pytester.path / "out")
    assert info["exit_code"] == 2 and info["stop_requested"] is True
    assert info["counts"]["not_run"] > 0


def test_reading_a_half_written_worker_directory(tmp_path: Path) -> None:
    gw = tmp_path / "workers" / "gw0"
    gw.mkdir(parents=True)
    good = json.dumps(_result("t.py::a").as_dict())
    (gw / "results.jsonl").write_text(good + "\n{torn line\n" + json.dumps({"nodeid": "t.py::b"}) + "\n")
    (gw / "collect.json").write_text("{not json")
    (tmp_path / "workers" / "gw10").mkdir()
    (tmp_path / "workers" / "gw2").mkdir()
    workers = read_workers(tmp_path / "workers")
    assert [w.id for w in workers] == ["gw0", "gw2", "gw10"]
    first = workers[0]
    assert [r.nodeid for r in first.results] == ["t.py::a"]
    assert first.meta is None and first.collect is None
    assert any("did not finish" in p for p in first.problems)
    assert any(p.startswith("results.jsonl line 2") for p in first.problems)
    assert any(p.startswith("results.jsonl line 3") for p in first.problems)
    assert any(p.startswith("collect.json") for p in first.problems)


def test_collections_merge_in_the_first_workers_order(tmp_path: Path) -> None:
    def item(nodeid: str) -> dict[str, Any]:
        return {
            "nodeid": nodeid,
            "file": "t.py",
            "line": 1,
            "markers": [],
            "invariant_ids": [],
            "hypothesis": False,
        }

    error = {"nodeid": "bad.py", "longrepr": "boom", "truncated": False}
    a = WorkerOutput(
        "gw0", tmp_path, collect={"items": [item("t.py::b"), item("t.py::a")], "errors": [error]}
    )
    b = WorkerOutput(
        "gw1", tmp_path, collect={"items": [item("t.py::a"), item("t.py::c")], "errors": [error]}
    )
    items, errors = merge_collect([a, b, WorkerOutput("gw2", tmp_path)])
    assert [i.nodeid for i in items] == ["t.py::b", "t.py::a", "t.py::c"]
    assert errors == [error]
