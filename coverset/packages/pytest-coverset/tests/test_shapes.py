"""The sys.monitoring shape tracer, end to end through pytest subprocess runs of the fixture project."""

from __future__ import annotations

import json

import pytest
from support import coverage_rows, read_shapes

ACCOUNT = "obj:fixturepkg.accounts.Account"
SAVINGS = "obj:fixturepkg.accounts.SavingsAccount"


def run_with_shapes(project: pytest.Pytester, *extra: str) -> pytest.RunResult:
    (project.path / "full.txt").write_text("src/fixturepkg/full.py\n")
    result = project.runpytest_subprocess(
        "--coverset-out",
        "out",
        "--coverset-source",
        "fixturepkg",
        "--coverset-full-trace",
        "full.txt",
        *extra,
    )
    result.assert_outcomes(passed=24)
    return result


def test_recorded_shapes(project: pytest.Pytester) -> None:
    run_with_shapes(project)
    shapes = read_shapes(project.path / "out")
    meta = json.loads((project.path / "out" / "meta.json").read_text())
    assert meta["tracer_errors"] == 0

    floats = shapes.keys("scale", "x")
    assert "float|nan" in floats
    assert {"float|inf", "float|inf|inf-neg"} & floats

    emails = shapes.keys("validate_email", "email")
    for sub in ("plain", "multi-label-domain", "plus-tag"):
        assert (
            f"str|ascii|kind:email/{sub}|len:11-100" in emails
            or f"str|ascii|kind:email/{sub}|len:2-10" in emails
        )
    by_example = {
        row["ex"]: shapes.header["shapes"][row["s"]] for row in shapes.rows("validate_email", "email")
    }
    assert "kind:email/plain" in by_example["'c***@example.co.uk'"]
    assert "kind:email/multi-label-domain" in by_example["'b***@eng.acme.com'"]
    assert "kind:email/plus-tag" in by_example["'d***@x.io'"]

    durations = shapes.keys("parse_duration", "text")
    for sub in ("multi-unit", "multi-letter-unit", "decimal", "signed", "trailing-garbage"):
        assert any(f"kind:quantity/{sub}|" in key for key in durations), sub

    assert "list|any_neg|elem:int|len:2-10|order:unsorted" in shapes.keys("total", "values")
    assert {
        "list|elem:tuple|len:2-10|order:unsorted|pair:lt",
        "list|elem:tuple|len:1|pair:gt",
    } <= shapes.keys("merge_intervals", "intervals")

    assert {ACCOUNT, SAVINGS} <= shapes.keys("Account.deposit", "<receiver>")
    assert shapes.keys("Account.from_email", "<receiver>") == {ACCOUNT}
    assert shapes.keys("Account.deposit_money", "money") == {"obj:fixturepkg.accounts.Money|dataclass"}
    assert all(row["ex"] is None for row in shapes.rows("Account.deposit", "<receiver>"))

    assert shapes.params("describe") == {"*args", "**kwargs", "return"}
    assert shapes.keys("describe", "*args") == {"tuple|elem:mixed|len:2-10|order:n/a"}
    assert shapes.keys("describe", "**kwargs") == {"dict|elem:int|key:str|len:1"}
    assert shapes.params("double") == {"n", "return"}
    assert shapes.params("timed.<locals>.wrapper") == {"*args", "**kwargs", "return"}
    assert shapes.params("countdown") == {"n"}

    assert shapes.keys("validate_email", "return") >= {"str|ascii|len:11-100", "str|ascii|len:2-10"}
    assert shapes.keys("parse_duration", "return") == {"int|pos"}
    assert shapes.keys("Account.__init__", "return") == {"NoneType"}

    assert [row["ex"] for row in shapes.rows("login", "password")] == ["<redacted:len=14>"]

    callers = shapes.header["callers"]
    tests = shapes.header["tests"]
    via_signup = [
        row for row in shapes.rows("validate_email", "email") if callers[row["c"]].get("qualname") == "signup"
    ]
    assert len(via_signup) == 1
    assert callers[via_signup[0]["c"]] == {
        "kind": "func",
        "path": "src/fixturepkg/core.py",
        "qualname": "signup",
        "line": 21,
    }
    assert [tests[t] for t in via_signup[0]["t"]] == ["tests/test_core.py::test_signup"]
    direct = [row for row in shapes.rows("validate_email", "email") if callers[row["c"]]["kind"] == "test"]
    assert {callers[row["c"]]["nodeid"] for row in direct} >= {
        "tests/test_core.py::test_validate_email[bob@eng.acme.com]"
    }
    double_callers = {callers[row["c"]].get("qualname") for row in shapes.rows("double")}
    assert double_callers == {"timed.<locals>.wrapper"}


def test_adaptive_cap_and_full_trace(project: pytest.Pytester) -> None:
    run_with_shapes(project)
    shapes = read_shapes(project.path / "out")
    hot = shapes.sampling_of("hot")
    assert hot["truncated"] is True and hot["cap"] == "sample"
    assert 64 < hot["calls"] < 80
    assert all(row["trunc"] for row in shapes.rows("hot"))
    full = shapes.sampling_of("hot_full")
    assert full == {
        "kind": "func_sampling",
        "f": full["f"],
        "calls": 300,
        "tests": full["tests"],
        "truncated": False,
        "cap": None,
    }
    assert not any(row["trunc"] for row in shapes.rows("hot_full"))
    assert shapes.sampling_of("validate_email")["truncated"] is False
    meta = json.loads((project.path / "out" / "meta.json").read_text())
    assert meta["functions_truncated"] == 1 and meta["full_trace"] == ["src/fixturepkg/full.py"]


def test_hard_sample_cap(project: pytest.Pytester) -> None:
    run_with_shapes(project, "--coverset-shape-samples", "10", "--coverset-shape-settle", "1000")
    shapes = read_shapes(project.path / "out")
    hot = shapes.sampling_of("hot")
    assert hot["calls"] == 10 and hot["truncated"] is True
    assert shapes.sampling_of("hot_full")["calls"] == 300
    assert shapes.sampling_of("validate_email")["truncated"] is False


def test_repr_examples_off(project: pytest.Pytester) -> None:
    run_with_shapes(project, "--coverset-repr-examples", "off")
    assert all(row["ex"] is None for row in read_shapes(project.path / "out").obs)


def test_shapes_off_writes_header_only(project: pytest.Pytester) -> None:
    run_with_shapes(project, "--coverset-shapes", "off")
    shapes = read_shapes(project.path / "out")
    assert shapes.obs == [] and shapes.header["funcs"] == []
    meta = json.loads((project.path / "out" / "meta.json").read_text())
    assert meta["shapes_disabled_reason"] == "disabled by --coverset-shapes off" and meta["tool_id"] is None


def test_coverage_identical_with_and_without_shapes(project: pytest.Pytester) -> None:
    (project.path / "full.txt").write_text("src/fixturepkg/full.py\n")
    common = ("--coverset-source", "fixturepkg", "--coverset-full-trace", "full.txt")
    project.runpytest_subprocess("--coverset-out", "on", "--coverset-shapes", "on", *common).assert_outcomes(
        passed=24
    )
    project.runpytest_subprocess(
        "--coverset-out", "off", "--coverset-shapes", "off", *common
    ).assert_outcomes(passed=24)
    with_shapes = coverage_rows(project.path / "on" / "coverage.sqlite")
    without = coverage_rows(project.path / "off" / "coverage.sqlite")
    assert with_shapes == without
    assert len({row[0] for row in with_shapes}) == 25


@pytest.mark.parametrize(
    ("taken", "expected_tool", "reason_fragment"),
    [((4,), 5, None), ((4, 5), 2, None), ((4, 5, 2), None, "no free sys.monitoring tool id")],
)
def test_tool_id_fallback(
    project: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    taken: tuple[int, ...],
    expected_tool: int | None,
    reason_fragment: str | None,
) -> None:
    claims = "".join(f"sys.monitoring.use_tool_id({tid}, 'other-{tid}')\n" for tid in taken)
    project.makepyfile(occupy_tools="import sys\n" + claims)
    monkeypatch.setenv("PYTHONPATH", str(project.path))
    run_with_shapes(project, "-p", "occupy_tools")
    meta = json.loads((project.path / "out" / "meta.json").read_text())
    assert meta["tool_id"] == expected_tool
    if reason_fragment is None:
        assert meta["shapes_disabled_reason"] is None
        assert read_shapes(project.path / "out").obs
    else:
        assert reason_fragment in meta["shapes_disabled_reason"]
        assert read_shapes(project.path / "out").obs == []
