"""The plugin's switch, the bundle files and their schemas, per-test coverage contexts."""

from __future__ import annotations

import json
import tarfile

import pytest
from support import NONCE, contexts, events, read_jsonl, read_shapes

from pytest_coverset.bundle import BUNDLE_MEMBERS

COVERAGE_FILE_FIELDS = {
    "executed_lines",
    "missing_lines",
    "excluded_lines",
    "executed_branches",
    "missing_branches",
    "functions",
    "classes",
    "summary",
}
RESULT_FIELDS = {
    "nodeid",
    "file",
    "outcome",
    "duration_s",
    "failed_phase",
    "fail_sig",
    "longrepr",
    "truncated",
    "reason",
    "start",
    "stop",
}


def test_inert_without_out(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess("--strict-markers")
    result.assert_outcomes(passed=24)
    assert NONCE not in (project.path / "stdout").read_text() and not events(project)
    assert not list(project.path.rglob("coverage.sqlite"))
    assert not list(project.path.rglob("bundle.tar.gz"))


def test_invariant_marker_registered(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess("--markers")
    result.stdout.fnmatch_lines(["@pytest.mark.invariant(id):*"])


def test_bundle_contents_and_schemas(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess("--coverset-out", "out", "--coverset-source", "fixturepkg")
    result.assert_outcomes(passed=24)
    out = project.path / "out"
    with tarfile.open(out / "bundle.tar.gz") as tar:
        assert sorted(tar.getnames()) == sorted(BUNDLE_MEMBERS)

    meta = json.loads((out / "meta.json").read_text())
    assert meta["tracer"] == "CTracer"
    assert meta["core"] == "ctrace"
    assert meta["tool_id"] == 4
    assert meta["shapes_disabled_reason"] is None
    assert meta["tracer_errors"] == 0
    assert meta["exit_code"] == 0
    assert meta["counts"]["passed"] == 24 and meta["counts"]["not_run"] == 0
    assert meta["hypothesis"]["deadline_ms"] == 200.0
    assert meta["git_root"] is None  # no .git: paths are relative to the rootdir
    assert (meta["rootdir"], meta["cwd"], meta["coverage_paths"]) == (".", ".", "repository")
    assert meta["timings"]["session_wall_s"] > 0
    assert {"python", "plugin_version", "coverage_version", "cwd", "rootdir"} <= meta.keys()

    results = read_jsonl(out / "results.jsonl")
    assert len(results) == 24
    assert all(set(row) == RESULT_FIELDS for row in results)
    assert {row["outcome"] for row in results} == {"passed"}
    assert all(row["file"].startswith("tests/") for row in results)

    collect = json.loads((out / "collect.json").read_text())
    items = {item["nodeid"]: item for item in collect["items"]}
    assert len(items) == 24 and collect["errors"] == []
    assert items["tests/test_core.py::test_scale_floats"]["hypothesis"] is True
    assert items["tests/test_core.py::test_total"]["hypothesis"] is False
    email = items["tests/test_core.py::test_validate_email[bob@eng.acme.com]"]
    assert email["invariant_ids"] == ["emails_have_domain"]
    assert {"invariant", "parametrize"} <= set(email["markers"])
    assert email["file"] == "tests/test_core.py" and email["line"] == 8  # first decorator line

    durations = json.loads((out / "durations.json").read_text())
    assert set(durations) == {row["nodeid"] for row in results}
    assert all(isinstance(v, float) for v in durations.values())

    links = read_jsonl(out / "invariant_links.jsonl")
    assert {
        "test_id": "tests/test_accounts.py::test_negative_deposit",
        "invariant_id": "balance_never_negative",
    } in links
    assert sum(1 for link in links if link["invariant_id"] == "emails_have_domain") == 4

    shapes = read_shapes(out)
    assert {"funcs", "shapes", "tests", "callers", "version"} <= shapes.header.keys()
    row = shapes.obs[0]
    assert set(row) == {"kind", "f", "p", "s", "c", "n", "t", "trunc", "ex", "ext"}

    written = [e for e in events(project) if e["kind"] == "bundle_written"]
    assert len(written) == 1 and written[0]["bytes"] == (out / "bundle.tar.gz").stat().st_size


def test_one_context_per_test(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_pipes="""
        import pytest
        from fixturepkg import core

        @pytest.mark.parametrize("text", ["a|b", "x||y"], ids=["a|b", "x||y"])
        def test_pipes(text):
            assert core.describe(text)
        """
    )
    result = project.runpytest_subprocess("--coverset-out", "out", "tests", "test_pipes.py")
    result.assert_outcomes(passed=26)
    recorded = contexts(project.path / "out" / "coverage.sqlite")
    nodeids = [row["nodeid"] for row in read_jsonl(project.path / "out" / "results.jsonl")]
    assert "" in recorded
    assert sorted(c for c in recorded if c) == sorted(nodeids)
    assert "test_pipes.py::test_pipes[a|b]" in recorded and "test_pipes.py::test_pipes[x||y]" in recorded


def test_coverage_json_fields(project: pytest.Pytester) -> None:
    project.runpytest_subprocess("--coverset-out", "out", "--coverset-source", "fixturepkg").assert_outcomes(
        passed=24
    )
    report = json.loads((project.path / "out" / "coverage.json").read_text())
    assert report["meta"]["format"] == 3 and report["meta"]["branch_coverage"] is True
    core = report["files"]["src/fixturepkg/core.py"]
    assert core.keys() >= COVERAGE_FILE_FIELDS
    assert "validate_email" in core["functions"] and "" in core["functions"]
    assert "Account" in report["files"]["src/fixturepkg/accounts.py"]["classes"]
    region = core["functions"]["validate_email"]
    assert {
        "executed_lines",
        "missing_lines",
        "executed_branches",
        "missing_branches",
        "start_line",
    } <= region.keys()


def test_paths_agree_between_coverage_and_shapes(project: pytest.Pytester) -> None:
    (project.path / ".git").mkdir()
    project.runpytest_subprocess("--coverset-out", "out", "--coverset-source", "fixturepkg").assert_outcomes(
        passed=24
    )
    out = project.path / "out"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["git_root"] == meta["repo_root"]
    covered = set(json.loads((out / "coverage.json").read_text())["files"])
    traced = {func[0] for func in read_shapes(out).header["funcs"]}
    assert traced <= covered
    assert {"src/fixturepkg/core.py", "src/fixturepkg/accounts.py", "src/fixturepkg/full.py"} <= traced


def test_foreign_tracer_is_a_usage_error(project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    project.makepyfile(foreign_tracer="import sys\nsys.settrace(lambda *args: None)\n")
    monkeypatch.setenv("PYTHONPATH", str(project.path))
    result = project.runpytest_subprocess("-p", "foreign_tracer", "--coverset-out", "out")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*--coverset-out cannot measure coverage: another tracer is already installed*"]
    )


def test_collection_errors_still_write_a_bundle(project: pytest.Pytester) -> None:
    project.makepyfile(test_broken="import does_not_exist\n")
    result = project.runpytest_subprocess(
        "--coverset-out", "out", "--continue-on-collection-errors", "tests", "test_broken.py"
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = project.path / "out"
    collect = json.loads((out / "collect.json").read_text())
    assert [error["nodeid"] for error in collect["errors"]] == ["test_broken.py"]
    assert "does_not_exist" in collect["errors"][0]["longrepr"]
    assert len(read_jsonl(out / "results.jsonl")) == 24
    kinds = [e["kind"] for e in events(project)]
    assert kinds[0] == "collected" and kinds[-2:] == ["bundle_written", "done"]
