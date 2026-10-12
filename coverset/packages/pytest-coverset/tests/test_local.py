"""Repository-relative paths from any working directory, ``--coverset``/``COVERSET=1`` and the
output directory's safety rules."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from support import coverage_rows, events, read_jsonl, read_shapes

from pytest_coverset.local import (
    GITIGNORE_TEXT,
    display_path,
    pop_switch,
    sources_from_pyproject,
    summary_line,
)
from pytest_coverset.paths import coverage_name, rel_dir

SOURCES = {
    "src/fixturepkg/__init__.py",
    "src/fixturepkg/accounts.py",
    "src/fixturepkg/core.py",
    "src/fixturepkg/full.py",
}


def neutral_rows(path: Path) -> list[dict[str, Any]]:
    data = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def sqlite_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
        return {row[0] for row in con.execute("select path from file")}


def every_path(out: Path) -> dict[str, set[str]]:
    """Every file path the bundle and its neutral export name, by where it was found."""
    neutral = out / "neutral"
    return {
        "coverage.json": set(json.loads((out / "coverage.json").read_text())["files"]),
        "coverage.sqlite": sqlite_names(out / "coverage.sqlite"),
        "results": {row["file"] for row in read_jsonl(out / "results.jsonl")},
        "collect": {item["file"] for item in json.loads((out / "collect.json").read_text())["items"]},
        "shapes": {func[0] for func in read_shapes(out).header["funcs"]},
        "neutral files": {row["path"] for row in neutral_rows(neutral / "files.jsonl.gz")},
        "neutral coverage": {row["file"] for row in neutral_rows(neutral / "coverage.jsonl.gz")},
        "neutral arcs": {row["file"] for row in neutral_rows(neutral / "arcs.jsonl.gz")},
        "neutral functions": {row["path"] for row in neutral_rows(neutral / "functions.jsonl.gz")},
        "neutral tests": {row["file"] for row in neutral_rows(neutral / "tests.jsonl.gz")},
    }


# -- repository-relative paths -------------------------------------------------------------------


def test_a_subdirectory_run_writes_repository_paths(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From ``tests/`` (git root and rootdir one level up) every path is the git-root-relative one,
    and coverage is row for row what a run from the root records."""
    (project.path / ".git").mkdir()
    args = ("--coverset-source=fixturepkg", "--coverset-neutral")
    project.runpytest_subprocess(f"--coverset-out={project.path / 'root'}", *args).assert_outcomes(passed=24)
    monkeypatch.chdir(project.path / "tests")
    project.runpytest_subprocess(f"--coverset-out={project.path / 'sub'}", *args).assert_outcomes(passed=24)

    root, sub = project.path / "root", project.path / "sub"
    paths = every_path(sub)
    tests = {"tests/test_accounts.py", "tests/test_core.py"}
    for where in ("coverage.json", "coverage.sqlite", "neutral files", "neutral arcs", "neutral functions"):
        assert paths[where] == SOURCES, where
    assert paths["neutral coverage"] <= SOURCES and "src/fixturepkg/core.py" in paths["neutral coverage"]
    assert paths["shapes"] <= SOURCES and "src/fixturepkg/core.py" in paths["shapes"]
    assert paths["results"] == paths["collect"] == paths["neutral tests"] == tests
    assert every_path(root) == paths
    assert coverage_rows(sub / "coverage.sqlite") == coverage_rows(root / "coverage.sqlite")

    meta, root_meta = (json.loads((out / "meta.json").read_text()) for out in (sub, root))
    assert (meta["cwd"], meta["rootdir"], meta["coverage_paths"]) == ("tests", ".", "repository")
    assert (root_meta["cwd"], root_meta["rootdir"]) == (".", ".")
    assert meta["errors"] == [] and meta["neutral"]["dropped"] == root_meta["neutral"]["dropped"]


def test_a_package_below_the_git_root_names_files_from_the_root(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rootdir ``app`` (its own pyproject.toml), git root above it, pytest run from ``app``."""
    monkeypatch.delenv("COVERSET", raising=False)
    (pytester.path / ".git").mkdir()
    app = pytester.path / "app"
    (app / "apppkg").mkdir(parents=True)
    (app / "apppkg" / "__init__.py").write_text("def one():\n    return 1\n")
    (app / "tests").mkdir()
    (app / "tests" / "test_app.py").write_text(
        "from apppkg import one\n\n\ndef test_one():\n    assert one() == 1\n"
    )
    (app / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    monkeypatch.chdir(app)
    pytester.runpytest_subprocess("--coverset-out=out", "--coverset-source=apppkg").assert_outcomes(passed=1)
    out = app / "out"
    assert set(json.loads((out / "coverage.json").read_text())["files"]) == {"app/apppkg/__init__.py"}
    assert sqlite_names(out / "coverage.sqlite") == {"app/apppkg/__init__.py"}
    assert {row["file"] for row in read_jsonl(out / "results.jsonl")} == {"app/tests/test_app.py"}
    assert [row["nodeid"] for row in read_jsonl(out / "results.jsonl")] == ["tests/test_app.py::test_one"]
    meta = json.loads((out / "meta.json").read_text())
    assert (meta["cwd"], meta["rootdir"]) == ("app", "app")


def test_coverage_name_and_rel_dir(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    tests = str(root / "tests")
    assert coverage_name("x.py", tests, root) == "tests/x.py"
    assert coverage_name(str(root / "src" / "pkg" / "m.py"), tests, root) == "src/pkg/m.py"
    assert coverage_name("src/pkg/m.py", str(root), root) == "src/pkg/m.py"
    assert coverage_name("/elsewhere/site.py", tests, root) == "/elsewhere/site.py"
    assert coverage_name("<string>", tests, root) == "<string>"
    assert rel_dir(root, root) == "." and rel_dir(root / "src" / "pkg", root) == "src/pkg"
    assert rel_dir(root.parent, root) is None


# -- --coverset and COVERSET=1 ---------------------------------------------------------------------


def test_coverset_flag_uses_the_local_defaults(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess("--coverset")
    result.assert_outcomes(passed=24)
    out = project.path / ".coverset"
    assert (out / ".gitignore").read_text() == GITIGNORE_TEXT
    assert (out / "bundle.tar.gz").is_file() and (out / "neutral.tar.gz").is_file()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["sources"] == ["fixturepkg"]  # inferred under src/, as the control plane infers
    assert meta["neutral"]["status"] == "written"
    lines = [line for line in result.outlines if line.startswith("coverset: ")]
    assert lines == [
        "coverset: wrote .coverset (24 tests, 4 files); `coverset report .coverset` for the pages"
    ]
    assert [e["kind"] for e in events(project)][-1] == "done"


def test_coverset_reads_tool_coverset_and_explicit_options_win(project: pytest.Pytester) -> None:
    pyproject = project.path / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + '\n[tool.coverset]\nsource = ["fixturepkg.core"]\n')
    project.runpytest_subprocess("--coverset").assert_outcomes(passed=24)
    meta = json.loads((project.path / ".coverset" / "meta.json").read_text())
    assert meta["sources"] == ["fixturepkg.core"]
    assert set(json.loads((project.path / ".coverset" / "coverage.json").read_text())["files"]) == {
        "src/fixturepkg/core.py"
    }

    project.runpytest_subprocess("--coverset", "--coverset-out=elsewhere", "--coverset-source=fixturepkg")
    meta = json.loads((project.path / "elsewhere" / "meta.json").read_text())
    assert meta["sources"] == ["fixturepkg"] and meta["neutral"]["status"] == "written"


def test_coverset_from_a_subdirectory_writes_under_the_rootdir(
    project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.path / ".git").mkdir()
    monkeypatch.chdir(project.path / "tests")
    result = project.runpytest_subprocess("--coverset")
    result.assert_outcomes(passed=24)
    assert (project.path / ".coverset" / "bundle.tar.gz").is_file()
    assert not (project.path / "tests" / ".coverset").exists()
    assert [line for line in result.outlines if line.startswith("coverset: ")] == [
        "coverset: wrote ../.coverset (24 tests, 4 files); `coverset report ../.coverset` for the pages"
    ]


def test_coverset_environment_switch(project: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """``COVERSET=1`` is ``--coverset``, and the tests' own subprocesses do not inherit it."""
    project.makepyfile(test_env="import os\n\n\ndef test_env():\n    assert 'COVERSET' not in os.environ\n")
    monkeypatch.setenv("COVERSET", "1")
    project.runpytest_subprocess("tests", "test_env.py").assert_outcomes(passed=25)
    assert (project.path / ".coverset" / "neutral.tar.gz").is_file()

    monkeypatch.setenv("COVERSET", "0")
    (project.path / ".coverset" / "bundle.tar.gz").unlink()
    project.runpytest_subprocess("tests").assert_outcomes(passed=24)
    assert not (project.path / ".coverset" / "bundle.tar.gz").exists()


def test_coverset_without_sources_is_a_clear_usage_error(project: pytest.Pytester) -> None:
    pyproject = project.path / "pyproject.toml"
    base = pyproject.read_text()
    pyproject.write_text(base + '\n[tool.coverset]\nsource_root = "tests"\n')
    result = project.runpytest_subprocess("--coverset")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--coverset found no package to measure under*--coverset-source=PKG*"])
    assert not (project.path / ".coverset").exists()

    pyproject.write_text(base + "\n[tool.coverset]\nsource = 3\n")
    result = project.runpytest_subprocess("--coverset")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*[[]tool.coverset[]] source in*must be a module name*"])


def test_out_without_sources_still_measures_in_repo_code(project: pytest.Pytester) -> None:
    """The runner's flags keep their meaning: ``--coverset-out`` alone reads no pyproject, writes no
    neutral bundle and prints no summary."""
    result = project.runpytest_subprocess("--coverset-out=out")
    result.assert_outcomes(passed=24)
    meta = json.loads((project.path / "out" / "meta.json").read_text())
    assert meta["sources"] == [] and meta["neutral"] == {"requested": False}
    assert not any(line.startswith("coverset: ") for line in result.outlines)


def test_sources_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "src" / "alpha").mkdir(parents=True)
    (tmp_path / "src" / "alpha" / "__init__.py").write_text("")
    (tmp_path / "src" / "tests").mkdir()
    (tmp_path / "src" / "tests" / "__init__.py").write_text("")
    (tmp_path / "src" / "notapkg").mkdir()
    assert sources_from_pyproject(tmp_path) == ("alpha",)  # no pyproject.toml: inferred under src
    (tmp_path / "pyproject.toml").write_text('[tool.coverset]\nsource = "beta"\n')
    assert sources_from_pyproject(tmp_path) == ("beta",)
    bad = (
        '[tool.coverset]\nsource = ["-x"]\n',
        "[tool.coverset]\nsource = []\n",
        '[tool.coverset]\nsource_root = "../up"\n',
        "[tool]\ncoverset = 1\n",
        "[",
    )
    for text in bad:
        (tmp_path / "pyproject.toml").write_text(text)
        with pytest.raises(pytest.UsageError):
            sources_from_pyproject(tmp_path)


def test_pop_switch_and_summary_helpers(tmp_path: Path) -> None:
    env = {"COVERSET": " Yes "}
    assert pop_switch(env) and env == {}
    assert not pop_switch({"COVERSET": "0"}) and not pop_switch({})
    root = tmp_path.resolve()
    (root / "tests").mkdir()
    assert display_path(root / ".coverset", str(root), str(root)) == ".coverset"
    assert display_path(root / ".coverset", str(root / "tests"), str(root)) == "../.coverset"
    assert display_path(root / ".coverset", "/", str(root)) == str(root / ".coverset")
    assert summary_line("out dir", 1, 1, 2) == (
        "coverset: wrote out dir (1 test, 1 file); `coverset report 'out dir'` for the pages "
        "(2 errors in out dir/meta.json)"
    )


# -- the output directory --------------------------------------------------------------------------


def test_gitignore_only_in_a_directory_the_plugin_created(project: pytest.Pytester) -> None:
    project.runpytest_subprocess("--coverset-out=made/out", "--coverset-source=fixturepkg")
    assert (project.path / "made" / "out" / ".gitignore").read_text() == GITIGNORE_TEXT
    assert not (project.path / "made" / ".gitignore").exists()

    (project.path / "mine").mkdir()
    project.runpytest_subprocess("--coverset-out=mine", "--coverset-source=fixturepkg")
    assert (project.path / "mine" / "bundle.tar.gz").is_file()
    assert not (project.path / "mine" / ".gitignore").exists()


@pytest.mark.parametrize("target", ["dir", "missing"])
def test_a_symlinked_output_directory_is_refused(project: pytest.Pytester, target: str) -> None:
    real = project.path / "real"
    if target == "dir":
        real.mkdir()
    os.symlink(real, project.path / "link")
    result = project.runpytest_subprocess("--coverset-out=link", "--coverset-source=fixturepkg")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*coverset output directory*link is a symlink*"])
    assert not real.exists() or list(real.iterdir()) == []
    assert not events(project)


def test_a_file_as_output_directory_is_refused(project: pytest.Pytester) -> None:
    (project.path / "afile").write_text("x")
    result = project.runpytest_subprocess("--coverset-out=afile", "--coverset-source=fixturepkg")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*coverset output directory*afile exists and is not a directory*"])
