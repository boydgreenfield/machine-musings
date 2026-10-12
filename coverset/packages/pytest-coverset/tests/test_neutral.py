"""The language-neutral bundle (``--coverset-neutral``): files, manifest, archive, early exits,
equivalence with a run without the flag, and the function-region rules."""

from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
import textwrap
from pathlib import Path
from typing import Any

import pytest
from support import NONCE, coverage_rows, events, read_jsonl, read_shapes

from pytest_coverset import __version__
from pytest_coverset.neutral import (
    DATA_FILES,
    MEMBERS,
    _Static,
    env_enabled,
    shard_from_env,
)
from pytest_coverset.regions import MODULE_QUALNAME, func_key, index_functions

COMMON = ("--coverset-source", "fixturepkg")
TIMING_FIELDS = ("duration_s", "start", "stop")


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Rows of a neutral ``.jsonl`` or ``.jsonl.gz`` file."""
    data = path.read_bytes()
    if path.name.endswith(".gz"):
        data = gzip.decompress(data)
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def run_neutral(project: pytest.Pytester, *args: str, out: str = "out") -> Path:
    project.runpytest_subprocess("--coverset-out", out, *COMMON, *args).assert_outcomes(passed=24)
    return project.path / out


def test_off_by_default(project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COVERSET_NEUTRAL", raising=False)
    stale = project.path / "out" / "neutral"
    stale.mkdir(parents=True)
    (stale / "tests.jsonl").write_text("{}\n")
    (stale.parent / "neutral.tar.gz").write_bytes(b"an archive from an earlier run")
    out = run_neutral(project)
    assert not (out / "neutral").exists() and not (out / "neutral.tar.gz").exists()
    assert "neutral_written" not in [e["kind"] for e in events(project)]
    assert json.loads((out / "meta.json").read_text())["neutral"] == {"requested": False}


def test_writes_every_file_the_manifest_and_the_archive(project: pytest.Pytester) -> None:
    out = run_neutral(project, "--coverset-neutral")
    neutral = out / "neutral"
    assert sorted(p.name for p in neutral.iterdir()) == sorted(MEMBERS)

    manifest = json.loads((neutral / "manifest.json").read_text())
    meta = json.loads((out / "meta.json").read_text())
    assert manifest["format"] == "coverset-neutral" and manifest["version"] == 1
    assert manifest["language"] == "python"
    assert manifest["producer"] == {"name": "pytest-coverset", "version": __version__}
    assert manifest["shard"] is None
    assert len(manifest["created_at"]) == 20 and manifest["created_at"].endswith("Z")
    outcome = meta.pop("neutral")
    assert outcome["status"] == "written" and outcome["requested"] is True
    assert {k: v for k, v in manifest["meta"].items() if k != "neutral"} == meta
    assert manifest["meta"]["neutral"]["rows"] == outcome["rows"]
    assert outcome["dropped"] == manifest["meta"]["neutral"]["dropped"]

    kinds = [e["kind"] for e in events(project)]
    assert kinds[-3:] == ["bundle_written", "neutral_written", "done"]
    written = next(e for e in events(project) if e["kind"] == "neutral_written")
    archive = out / "neutral.tar.gz"
    assert written["path"] == str(archive) and written["bytes"] == archive.stat().st_size
    assert written["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert written["rows"]["tests.jsonl.gz"] == 24 and written["seconds"] >= 0
    assert (outcome["sha256"], outcome["bytes"]) == (written["sha256"], written["bytes"])
    assert written["dropped"] == outcome["dropped"] and written["rows"] == outcome["rows"]
    with tarfile.open(archive) as tar:
        assert sorted(tar.getnames()) == sorted(MEMBERS)
        for member in tar.getmembers():
            assert member.isreg() and "/" not in member.name
            assert tar.extractfile(member).read() == (neutral / member.name).read_bytes()  # type: ignore[union-attr]
    with tarfile.open(out / "bundle.tar.gz") as tar:
        assert not any(name.startswith("neutral") for name in tar.getnames())


def test_rows_follow_the_specification(project: pytest.Pytester) -> None:
    out = run_neutral(project, "--coverset-neutral")
    neutral = out / "neutral"
    tests = read_rows(neutral / "tests.jsonl.gz")
    collect = json.loads((out / "collect.json").read_text())["items"]
    assert [row["id"] for row in tests] == [item["nodeid"] for item in collect]
    results = {row["nodeid"]: row for row in read_jsonl(out / "results.jsonl")}
    for row in tests:
        result = results[row["id"]]
        assert row["outcome"] == result["outcome"] and row["message"] == result["longrepr"]
        assert (row["start"], row["stop"], row["duration_s"]) == (
            result["start"],
            result["stop"],
            result["duration_s"],
        )
    inv = next(row for row in tests if row["invariants"])
    assert "invariant" in inv["markers"]
    assert any(row["property"] for row in tests)
    links = read_rows(neutral / "links.jsonl")
    assert {(r["test"], r["invariant"]) for r in links} == {
        (r["id"], i) for r in tests for i in r["invariants"]
    }

    files = {row["path"]: row for row in read_rows(neutral / "files.jsonl.gz")}
    report = json.loads((out / "coverage.json").read_text())["files"]
    assert set(files) == set(report)
    for path, row in files.items():
        entry = report[path]
        assert row["statements"] == sorted(entry["executed_lines"] + entry["missing_lines"])
        assert row["branches"] == sorted(entry["executed_branches"] + entry["missing_branches"])
        assert row["n_lines"] == len((project.path / path).read_text().splitlines())
    arcs = {row["file"]: row["arcs"] for row in read_rows(neutral / "arcs.jsonl.gz")}
    assert arcs == {path: sorted(entry["executed_branches"]) for path, entry in report.items()}

    coverage = read_rows(neutral / "coverage.jsonl.gz")
    assert coverage[0]["test"] == "<import>"
    assert {row["test"] for row in coverage} <= {row["id"] for row in tests} | {"<import>"}
    for row in coverage:
        assert row["lines"] == sorted(set(row["lines"]))
        assert set(row["lines"]) <= set(files[row["file"]]["statements"])
        assert row["arcs"] == sorted(row["arcs"]) and all(len(arc) == 2 for arc in row["arcs"])
    executed = {
        path: {line for row in coverage if row["file"] == path for line in row["lines"]} for path in files
    }
    assert executed == {path: set(entry["executed_lines"]) for path, entry in report.items()}

    functions = read_rows(neutral / "functions.jsonl.gz")
    assert {row["path"] for row in functions} == set(files)
    assert all(row["key"] == func_key(row["path"], row["qualname"], row["ordinal"]) for row in functions)
    assert all("types" not in row for row in functions)
    wrapper = next(row for row in functions if row["qualname"] == "timed.<locals>.wrapper")
    assert wrapper["kind"] == "function" and wrapper["params"] == ["*args", "**kwargs"]


def test_shapes_are_self_contained_rows(project: pytest.Pytester) -> None:
    out = run_neutral(project, "--coverset-neutral")
    plugin = read_shapes(out)
    rows = read_rows(out / "neutral" / "shapes.jsonl.gz")
    obs = [r for r in rows if r["kind"] == "obs"]
    sampling = [r for r in rows if r["kind"] == "sampling"]
    assert len(sampling) == len(plugin.sampling)
    header = plugin.header
    import_callers = {
        i for i, c in enumerate(header["callers"]) if c == {"kind": "test", "nodeid": "<import>"}
    }
    expected = [r for r in plugin.obs if r["c"] not in import_callers]
    assert len(obs) == len(expected) and len(expected) < len(plugin.obs)
    for raw, row in zip(expected, obs, strict=True):
        assert row["func"] == header["funcs"][raw["f"]] and row["shape"] == header["shapes"][raw["s"]]
        caller = header["callers"][raw["c"]]
        assert row["caller"] == (
            {"test": caller["nodeid"]}
            if caller["kind"] == "test"
            else [caller["path"], caller["qualname"], caller["line"]]
        )
        assert row["tests"] == [header["tests"][i] for i in raw["t"]]
        assert row["samples"] == raw["n"] and row["example"] == raw["ex"]
    assert all(r["cap"] is None or r["truncated"] for r in sampling)


def test_environment_switch_and_shard(project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVERSET_NEUTRAL", "1")
    monkeypatch.setenv("COVERSET_SHARD", "3")
    out = run_neutral(project)
    manifest = json.loads((out / "neutral" / "manifest.json").read_text())
    assert manifest["shard"] == 3 and manifest["meta"]["neutral"]["requested"] is True


def test_the_export_changes_nothing_else(project: pytest.Pytester) -> None:
    on = run_neutral(project, "--coverset-neutral", out="on")
    off = run_neutral(project, out="off")

    def results(out: Path) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in row.items() if k not in TIMING_FIELDS}
            for row in read_jsonl(out / "results.jsonl")
        ]

    def shapes(out: Path) -> tuple[Any, ...]:
        decoded = read_shapes(out)
        return (decoded.header, decoded.obs, decoded.sampling)

    assert results(on) == results(off)
    assert coverage_rows(on / "coverage.sqlite") == coverage_rows(off / "coverage.sqlite")
    assert shapes(on) == shapes(off)
    for name in ("collect.json", "invariant_links.jsonl", "coverage.json"):
        if name == "coverage.json":
            strip = lambda d: {**d, "meta": {}}  # noqa: E731 - the report's timestamp differs
            assert strip(json.loads((on / name).read_text())) == strip(json.loads((off / name).read_text()))
        else:
            assert (on / name).read_text() == (off / name).read_text()


def test_stopped_run_lists_not_run_tests_with_a_null_outcome(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    stop = pytester.path / "STOP"
    pytester.makepyfile(
        test_stop=f"""
        import pathlib

        def test_first():
            pass

        def test_makes_stop():
            pathlib.Path({str(stop)!r}).write_text("stop")

        def test_never():
            pass
        """
    )
    result = pytester.runpytest_subprocess(
        "--coverset-out", "out", "--coverset-neutral", "--coverset-stop-file", str(stop)
    )
    assert result.ret == pytest.ExitCode.INTERRUPTED
    rows = {
        row["id"].rpartition("::")[2]: row for row in read_rows(pytester.path / "out/neutral/tests.jsonl.gz")
    }
    assert rows["test_first"]["outcome"] == "passed" and rows["test_makes_stop"]["outcome"] == "passed"
    assert rows["test_never"]["outcome"] is None and rows["test_never"]["duration_s"] is None
    assert [e["kind"] for e in events(pytester)][-2:] == ["neutral_written", "done"]


@pytest.mark.parametrize("case", ["conftest", "scores"])
def test_early_exits_still_write_the_neutral_bundle(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    args = ["--coverset-out", "out", "--coverset-neutral"]
    if case == "conftest":
        (pytester.path / "tests").mkdir()
        (pytester.path / "tests" / "conftest.py").write_text('raise ImportError("conftest boom")\n')
        (pytester.path / "tests" / "test_ok.py").write_text("def test_ok():\n    pass\n")
    else:
        pytester.makepyfile(test_one="def test_one():\n    pass\n")
        args += ["--coverset-scores", "missing.json"]
    result = pytester.runpytest_subprocess(*args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert [e["kind"] for e in events(pytester)][-3:] == ["bundle_written", "neutral_written", "done"]
    neutral = pytester.path / "out" / "neutral"
    assert sorted(p.name for p in neutral.iterdir()) == sorted(MEMBERS)
    manifest = json.loads((neutral / "manifest.json").read_text())
    meta = json.loads((pytester.path / "out" / "meta.json").read_text())
    assert meta.pop("neutral")["status"] == "written"
    assert {k: v for k, v in manifest["meta"].items() if k != "neutral"} == meta
    assert manifest["meta"]["exit_code"] == 4
    for name in DATA_FILES:
        read_rows(neutral / name)  # every file is well-formed, even when empty


def test_switches() -> None:
    assert env_enabled({"COVERSET_NEUTRAL": "1"}) and env_enabled({"COVERSET_NEUTRAL": "True"})
    assert not env_enabled({}) and not env_enabled({"COVERSET_NEUTRAL": "0"})
    assert shard_from_env({"COVERSET_SHARD": "7"}) == 7
    assert shard_from_env({}) is None
    assert shard_from_env({"COVERSET_SHARD": "-1"}) is None
    assert shard_from_env({"COVERSET_SHARD": "x"}) is None
    assert shard_from_env({"COVERSET_SHARD": "99999999"}) is None


def test_first_lines_map_continuations_and_keep_statements_only() -> None:
    facts = _Static(
        path="m.py",
        n_lines=20,
        statements=(2, 3, 7, 10),
        excluded=(5,),
        branches=(),
        executed_arcs=(),
        anchors=(2, 3, 5, 7, 10),
        anchor_is_statement=(True, True, False, True, True),
    )
    # 1: before the first statement (a docstring line); 4: continuation of 3; 6: inside the
    # excluded statement at 5; 8, 9: continuation of 7; 11: continuation of 10; 30: beyond the file.
    assert facts.first_lines([1, 4, 6, 8, 9, 11, 2, 0, -3]) == [2, 3, 7, 10]


REGION_SOURCE = textwrap.dedent(
    '''\
    """Module docstring."""
    from typing import overload

    @overload
    def parse(x: int) -> int: ...
    @overload
    def parse(x: str) -> str: ...
    def parse(x):
        return x


    class Account:
        """Doc only."""


    class Box:
        @property
        def value(self):
            return 1

        @value.setter
        def value(self, v, /, *rest, k=1, **kw):
            pass


    if True:
        def outer():
            """Docstring only."""
    else:
        async def outer():
            def inner():
                return lambda: 1
            return inner
    '''
)


def test_regions_follow_the_control_plane_rules() -> None:
    regions = index_functions("pkg/m.py", REGION_SOURCE)
    by_name = {(r.qualname, r.ordinal): r for r in regions}
    assert regions[0].qualname == MODULE_QUALNAME and regions[0].kind == "module"
    assert (regions[0].def_line, regions[0].body_start, regions[0].end_line) == (1, 2, 33)
    assert [by_name["parse", i].def_line for i in range(3)] == [4, 6, 8]
    assert by_name["parse", 2].body_start == 9
    assert by_name["parse", 0].body_hash == by_name["parse", 1].body_hash != by_name["parse", 2].body_hash
    assert by_name["parse", 0].sig_hash != by_name["parse", 1].sig_hash
    account = by_name["Account", 0]
    assert (account.kind, account.def_line, account.body_start, account.end_line) == ("class", 12, 13, 13)
    setter = by_name["Box.value", 1]
    assert setter.kind == "method" and setter.params == ("self", "v", "*rest", "k", "**kw")
    assert setter.def_line == 21 and setter.body_start == 23
    assert by_name["outer", 0].body_start == by_name["outer", 0].def_line == 27
    assert by_name["outer.<locals>.inner", 0].kind == "function"
    assert by_name["outer", 1].end_line == 33
    assert all(r.func_key == func_key("pkg/m.py", r.qualname, r.ordinal) for r in regions)
    assert func_key("a.py", "f", 0) == hashlib.blake2b(b"a.py\0f\x000", digest_size=8).hexdigest()


def test_regions_of_a_file_that_does_not_parse() -> None:
    [module] = index_functions("bad.py", "def broken(:\n    pass\n")
    assert (module.qualname, module.kind, module.def_line, module.end_line) == ("<module>", "module", 1, 2)
