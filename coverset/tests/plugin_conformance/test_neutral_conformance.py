"""pytest-coverset's ``--coverset-neutral`` bundle against the reader, the validator and the converter.

For a run of the plugin's fixture project, of a project with every outcome (failures, errors,
skips, xfail/xpass and tests a stop file kept from running), of the fixture project run from its
``tests/`` directory, and of a git repository whose package (and pytest's rootdir) is a
subdirectory:

* ``coverset_model.io.validate`` finds no problem in ``<out>/neutral`` nor in ``neutral.tar.gz``;
* ``coverset_model.io.read_bundle(<out>/neutral)`` equals ``read_python_bundle(<out>)`` (the
  control plane's reading of the Python bundle from the same run) dataclass by dataclass: tests,
  collected items, durations, static files, run arcs, coverage, shapes, sampling and ``meta``
  (``meta.neutral`` aside: ``meta.json`` holds the export's final outcome, the manifest the data
  files' counters). One documented difference is applied to the converter's side: coverage keeps
  only statement lines (the neutral format drops the rest, ``docs/neutral-bundle.md``). Sampling
  rows need no adjustment: both readers carry the plugin's ``cap``;
* the shipped function regions equal the control plane's index of the measured sources;
* the archive reads back exactly as the directory;
* every path is repository-relative whatever the working directory: the strict subdirectory tests
  compare each path with the git-root-relative one. When the plugin could not rewrite the coverage
  file names (``meta.coverage_paths == "cwd"``), both readers join ``meta.cwd`` in front and still
  agree; a bundle with the old absolute ``meta.cwd``/``meta.rootdir`` reads as it always did.
"""

from __future__ import annotations

import dataclasses
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from conformance_helpers import copy_fixture_project, run_pytest

model_io = pytest.importorskip("coverset_model.io")

from coverset.analysis.functions import index_functions  # noqa: E402
from coverset.facts.convert import read_python_bundle  # noqa: E402
from coverset_model.neutral import NeutralBundle  # noqa: E402

MIXED_MODULE = '''
"""A module with branches."""


def classify(n):
    if n < 0:
        return "neg"
    if n == 0:
        return "zero"
    return "pos"


def explode(x):
    raise ValueError(f"bad {x}")
'''

MIXED_TESTS = """
import pathlib

import pytest

from mixpkg.mod import classify, explode


@pytest.fixture
def broken():
    raise RuntimeError("setup failed")


@pytest.mark.invariant("sign_is_total")
def test_pos():
    assert classify(3) == "pos"


def test_fails():
    assert classify(-1) == "zero"


def test_raises():
    explode(1)


def test_setup_error(broken):
    pass


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass


@pytest.mark.xfail(reason="known")
def test_xfail():
    assert classify(0) == "pos"


@pytest.mark.xfail(reason="fixed already")
def test_xpass():
    assert classify(0) == "zero"


def test_stop():
    pathlib.Path(STOP).write_text("stop")


def test_never_runs():
    assert classify(5) == "pos"
"""


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = copy_fixture_project(tmp_path_factory.mktemp("conformance") / "fixture")
    proc = run_pytest(project, "--coverset-out=out", "--coverset-source=fixturepkg", "--coverset-neutral")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return project


@pytest.fixture(scope="module")
def mixed_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("conformance") / "mixed"
    (project / "src" / "mixpkg").mkdir(parents=True)
    (project / "src" / "mixpkg" / "__init__.py").write_text("")
    (project / "src" / "mixpkg" / "mod.py").write_text(MIXED_MODULE)
    (project / "tests").mkdir()
    stop = project / "STOP"
    (project / "tests" / "test_mixed.py").write_text(f"STOP = {str(stop)!r}\n" + textwrap.dedent(MIXED_TESTS))
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n'
    )
    proc = run_pytest(
        project,
        "--coverset-out=out",
        "--coverset-source=mixpkg",
        f"--coverset-stop-file={stop}",
        env={"COVERSET_NEUTRAL": "1", "COVERSET_SHARD": "2"},
    )
    assert proc.returncode == 2, proc.stdout[-3000:] + proc.stderr[-3000:]
    return project


def _converter_view(python: NeutralBundle) -> NeutralBundle:
    """The converter's bundle with the documented difference applied (module docstring)."""
    coverage: dict[str, dict[str, tuple[int, ...]]] = {}
    for test, by_path in python.coverage.items():
        kept = {
            path: tuple(line for line in lines if line in set(python.static[path].statements))
            for path, lines in by_path.items()
        }
        kept = {path: lines for path, lines in kept.items() if lines}
        if kept:
            coverage[test] = kept
    return dataclasses.replace(python, coverage=coverage)


def _sorted(rows: Any, key: Any = repr) -> list[Any]:
    return sorted(rows, key=key)


def assert_equivalent(
    project: Path, *, out: Path | None = None, functions: bool = True
) -> tuple[NeutralBundle, NeutralBundle]:
    """``project`` is the repository root (sources are read under it); ``out`` the bundle directory."""
    out = out or project / "out"
    neutral = model_io.read_bundle(out / "neutral")
    python = _converter_view(read_python_bundle(out, source_root=project if functions else None))

    assert _sorted(neutral.tests) == _sorted(python.tests)
    assert _sorted(neutral.collected) == _sorted(python.collected)
    assert neutral.durations == python.durations
    assert neutral.static == python.static
    assert neutral.run_arcs == python.run_arcs
    assert neutral.coverage == python.coverage
    assert _sorted(neutral.shapes) == _sorted(python.shapes)
    assert _sorted(neutral.sampling) == _sorted(python.sampling)
    assert all(s.cap is None or s.truncated for s in neutral.sampling)
    plain_meta = {k: v for k, v in neutral.meta.items() if k not in model_io.READER_META_KEYS}
    meta = json.loads((out / "meta.json").read_text())
    outcome, shipped = meta.pop("neutral"), plain_meta.pop("neutral")
    assert plain_meta == meta
    assert outcome["status"] == "written"
    assert (shipped["rows"], shipped["dropped"]) == (outcome["rows"], outcome["dropped"])
    assert neutral.meta["convert"]["dropped"] == {}

    if not functions:
        return neutral, python
    expected = [
        region
        for path in sorted(neutral.static)
        for region in index_functions(path, (project / path).read_text(encoding="utf-8"))
    ]
    assert _sorted(neutral.functions) == _sorted(expected)
    assert dict(neutral.types) == {}
    return neutral, python


SUB_TEST = "from subpkg.mod import classify\n\n\ndef test_pos():\n    assert classify(3) == 'pos'\n"
BREAK_REMAP = """
from pytest_coverset.coverage_ctx import CoverageContext


def _fail(self, mapper):
    raise OSError("simulated: the coverage data cannot be rewritten")


CoverageContext.remap_files = _fail
"""


def _package_below_root(project: Path) -> Path:
    """A git repository at ``project`` whose package, tests and pyproject.toml live in ``app/``."""
    app = project / "app"
    (app / "subpkg").mkdir(parents=True)
    (project / ".git").mkdir()  # find_git_root only looks for the directory
    (app / "subpkg" / "__init__.py").write_text("")
    (app / "subpkg" / "mod.py").write_text(MIXED_MODULE)
    (app / "tests").mkdir()
    (app / "tests" / "test_sub.py").write_text(SUB_TEST)
    (app / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\ntestpaths = ["tests"]\n'
    )
    return app


@pytest.fixture(scope="module")
def subdir_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """pytest run from ``app/`` (rootdir ``app``, git root above); returns the git root."""
    project = tmp_path_factory.mktemp("conformance") / "subdir"
    app = _package_below_root(project)
    proc = run_pytest(app, "--coverset-out=../out", "--coverset-source=subpkg", "--coverset-neutral")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return project


@pytest.fixture(scope="module")
def tests_cwd_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture project, a git repository, run from its ``tests/`` directory."""
    project = copy_fixture_project(tmp_path_factory.mktemp("conformance") / "testscwd")
    (project / ".git").mkdir()
    proc = run_pytest(
        project / "tests", "--coverset-out=../out", "--coverset-source=fixturepkg", "--coverset-neutral"
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return project


@pytest.fixture(scope="module")
def unrewritten_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """As ``subdir_run``, with the coverage file-name rewrite failing (``coverage_paths: cwd``)."""
    project = tmp_path_factory.mktemp("conformance") / "unrewritten"
    app = _package_below_root(project)
    (project / "break_remap.py").write_text(BREAK_REMAP)
    proc = run_pytest(
        app,
        "-p",
        "break_remap",
        "--coverset-out=../out",
        "--coverset-source=subpkg",
        "--coverset-neutral",
        env={"PYTHONPATH": str(project)},
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return project


def test_fixture_project_validates(fixture_run: Path) -> None:
    out = fixture_run / "out"
    assert model_io.validate(out / "neutral") == []
    assert model_io.validate(out / "neutral.tar.gz") == []


def test_fixture_project_reads_like_the_python_bundle(fixture_run: Path) -> None:
    neutral, python = assert_equivalent(fixture_run)
    assert len(neutral.tests) == 24 and len(neutral.collected) == 24
    assert "<import>" in neutral.coverage and len(neutral.coverage) == 25
    assert neutral.shapes and neutral.sampling and neutral.functions
    assert any(item.invariant_ids for item in neutral.collected)
    assert neutral.meta["bundle"]["language"] == "python"
    assert neutral.meta["bundle"]["producer"]["name"] == "pytest-coverset"
    assert neutral.meta["bundle"]["shard"] is None


def test_every_outcome_reads_like_the_python_bundle(mixed_run: Path) -> None:
    out = mixed_run / "out"
    assert model_io.validate(out / "neutral") == []
    neutral, _ = assert_equivalent(mixed_run)
    outcomes = {row.nodeid.rpartition("::")[2]: row.outcome for row in neutral.tests}
    assert outcomes == {
        "test_pos": "passed",
        "test_fails": "failed",
        "test_raises": "failed",
        "test_setup_error": "error",
        "test_skipped": "skipped",
        "test_xfail": "xfailed",
        "test_xpass": "xpassed",
        "test_stop": "passed",
    }
    assert any(item.nodeid.endswith("::test_never_runs") for item in neutral.collected)
    failed = next(row for row in neutral.tests if row.nodeid.endswith("::test_raises"))
    assert failed.fail_sig and "ValueError" in (failed.longrepr or "")
    assert neutral.meta["bundle"]["shard"] == 2


APP_SOURCES = {"app/subpkg/__init__.py", "app/subpkg/mod.py"}
FIXTURE_SOURCES = {
    "src/fixturepkg/__init__.py",
    "src/fixturepkg/accounts.py",
    "src/fixturepkg/core.py",
    "src/fixturepkg/full.py",
}


def _paths(bundle: NeutralBundle) -> dict[str, set[str]]:
    """Every file path a bundle names, by where it appears."""
    return {
        "static": set(bundle.static),
        "run_arcs": set(bundle.run_arcs),
        "coverage": {path for by_path in bundle.coverage.values() for path in by_path},
        "tests": {row.file for row in bundle.tests},
        "collected": {item.file for item in bundle.collected},
        "shapes": {obs.func[0] for obs in bundle.shapes},
        "sampling": {row.func[0] for row in bundle.sampling},
        "functions": {region.path for region in bundle.functions},
    }


def test_a_run_from_the_tests_directory_names_every_path_from_the_git_root(tests_cwd_run: Path) -> None:
    """From ``tests/`` coverage.py names the sources by absolute path (they lie outside the working
    directory): the plugin makes them git-root-relative, and both readers read them that way."""
    out = tests_cwd_run / "out"
    assert model_io.validate(out / "neutral") == []
    neutral, python = assert_equivalent(tests_cwd_run)
    tests = {"tests/test_accounts.py", "tests/test_core.py"}
    for name, paths in (("neutral", _paths(neutral)), ("python", _paths(python))):
        assert paths["static"] == paths["run_arcs"] == FIXTURE_SOURCES, name
        # an empty or docstring-only module has no statement a test executes
        assert paths["coverage"] <= FIXTURE_SOURCES and "src/fixturepkg/core.py" in paths["coverage"], name
        assert paths["tests"] == paths["collected"] == tests, name
        assert paths["shapes"] and paths["shapes"] <= FIXTURE_SOURCES, name
        assert paths["sampling"] and paths["sampling"] <= FIXTURE_SOURCES, name
    assert _paths(neutral)["functions"] == FIXTURE_SOURCES
    meta = json.loads((out / "meta.json").read_text())
    assert (meta["cwd"], meta["rootdir"], meta["coverage_paths"]) == ("tests", ".", "repository")
    assert len(neutral.tests) == 24 and len(neutral.coverage) == 25


def test_a_package_below_the_git_root_reads_like_the_python_bundle(subdir_run: Path) -> None:
    """rootdir and working directory ``app/``: coverage names (``subpkg/mod.py`` to coverage.py)
    and test files are ``app/...`` in both readings."""
    out = subdir_run / "out"
    assert model_io.validate(out / "neutral") == []
    neutral, python = assert_equivalent(subdir_run)
    for name, paths in (("neutral", _paths(neutral)), ("python", _paths(python))):
        assert paths["static"] == APP_SOURCES and paths["coverage"] == {"app/subpkg/mod.py"}, name
        assert paths["tests"] == paths["collected"] == {"app/tests/test_sub.py"}, name
        assert paths["shapes"] == {"app/subpkg/mod.py"}, name
    assert neutral.tests[0].nodeid == "tests/test_sub.py::test_pos"  # nodeids stay rootdir-relative
    meta = json.loads((out / "meta.json").read_text())
    assert (meta["cwd"], meta["rootdir"], meta["coverage_paths"]) == ("app", "app", "repository")


def test_without_the_rewrite_both_readers_join_meta_cwd(unrewritten_run: Path) -> None:
    out = unrewritten_run / "out"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["coverage_paths"] == "cwd" and meta["cwd"] == "app"
    assert any(error.startswith("coverage paths: OSError: simulated") for error in meta["errors"])
    assert set(json.loads((out / "coverage.json").read_text())["files"]) == {
        "subpkg/__init__.py",
        "subpkg/mod.py",
    }
    assert model_io.validate(out / "neutral") == []
    neutral, python = assert_equivalent(unrewritten_run)
    assert set(neutral.static) == set(python.static) == APP_SOURCES
    assert _paths(neutral)["coverage"] == _paths(python)["coverage"] == {"app/subpkg/mod.py"}


def test_old_bundles_read_as_before(unrewritten_run: Path, tmp_path: Path) -> None:
    """Bundles written before ``meta.coverage_paths`` recorded absolute ``cwd`` and ``rootdir``:
    nothing is joined, so coverage names and nodeid-derived files read exactly as they always did."""
    old = tmp_path / "old"
    old.mkdir()
    for name in ("coverage.sqlite", "coverage.json", "results.jsonl", "collect.json", "meta.json"):
        (old / name).write_bytes((unrewritten_run / "out" / name).read_bytes())
    meta = json.loads((old / "meta.json").read_text())
    meta.pop("coverage_paths")
    meta["cwd"] = meta["rootdir"] = str(unrewritten_run / "app")
    (old / "meta.json").write_text(json.dumps(meta))
    rows = [json.loads(line) for line in (old / "results.jsonl").read_text().splitlines()]
    (old / "results.jsonl").write_text("".join(json.dumps({**r, "file": None}) + "\n" for r in rows))
    bundle = read_python_bundle(old)
    assert set(bundle.static) == {"subpkg/__init__.py", "subpkg/mod.py"}
    assert {row.file for row in bundle.tests} == {"tests/test_sub.py"}

    meta["cwd"], meta["rootdir"] = "app", "app"  # the same bundle with the new keys
    (old / "meta.json").write_text(json.dumps(meta))
    bundle = read_python_bundle(old)
    assert set(bundle.static) == APP_SOURCES
    assert {row.file for row in bundle.tests} == {"app/tests/test_sub.py"}


@pytest.mark.parametrize(
    "run", ["fixture_run", "mixed_run", "subdir_run", "tests_cwd_run", "unrewritten_run"]
)
def test_archive_reads_back_as_the_directory(run: str, request: pytest.FixtureRequest) -> None:
    out = request.getfixturevalue(run) / "out"
    from_dir = model_io.read_bundle(out / "neutral")
    from_tar = model_io.read_bundle(out / "neutral.tar.gz")
    for field in dataclasses.fields(NeutralBundle):
        if field.name != "meta":
            assert getattr(from_tar, field.name) == getattr(from_dir, field.name), field.name
    tar_meta, dir_meta = dict(from_tar.meta), dict(from_dir.meta)
    tar_convert, dir_convert = tar_meta.pop("convert"), dir_meta.pop("convert")
    assert tar_meta == dir_meta
    assert tar_convert["bundle_sha256"] == dir_convert["bundle_sha256"]
    assert tar_convert["rows"] == dir_convert["rows"] and tar_convert["dropped"] == dir_convert["dropped"]
