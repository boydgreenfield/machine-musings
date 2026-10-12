"""pytest-coverset's ``--coverset-neutral`` bundle against the reader, the validator and the converter.

For a run of the plugin's fixture project, of a project with every outcome (failures, errors,
skips, xfail/xpass and tests a stop file kept from running), and of a git repository whose tests
pytest runs from a subdirectory:

* ``coverset_model.io.validate`` finds no problem in ``<out>/neutral`` nor in ``neutral.tar.gz``;
* ``coverset_model.io.read_bundle(<out>/neutral)`` equals ``read_python_bundle(<out>)`` (the
  control plane's reading of the Python bundle from the same run) dataclass by dataclass: tests,
  collected items, durations, static files, run arcs, coverage, shapes, sampling and ``meta``
  (``meta.neutral`` aside: ``meta.json`` holds the export's final outcome, the manifest the data
  files' counters). One documented difference is applied to the converter's side: coverage keeps
  only statement lines (the neutral format drops the rest, ``docs/neutral-bundle.md``). Sampling
  rows need no adjustment: both readers carry the plugin's ``cap``;
* the shipped function regions equal the control plane's index of the measured sources;
* the archive reads back exactly as the directory.
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


def assert_equivalent(project: Path, *, functions: bool = True) -> tuple[NeutralBundle, NeutralBundle]:
    out = project / "out"
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


@pytest.fixture(scope="module")
def subdir_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A git repository whose tests pytest runs from ``app/`` (rootdir ``app``, git root above)."""
    project = tmp_path_factory.mktemp("conformance") / "subdir"
    app = project / "app"
    (app / "subpkg").mkdir(parents=True)
    (project / ".git").mkdir()  # find_git_root only looks for the directory
    (app / "subpkg" / "__init__.py").write_text("")
    (app / "subpkg" / "mod.py").write_text(MIXED_MODULE)
    (app / "tests").mkdir()
    (app / "tests" / "test_sub.py").write_text(
        "from subpkg.mod import classify\n\n\ndef test_pos():\n    assert classify(3) == 'pos'\n"
    )
    (app / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\ntestpaths = ["tests"]\n'
    )
    proc = run_pytest(app, "--coverset-out=out", "--coverset-source=subpkg", "--coverset-neutral")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return app


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


def test_a_subdirectory_run_reads_like_the_python_bundle(subdir_run: Path) -> None:
    """coverage.py names files relative to the directory pytest ran in; the converter reads those
    names as git-root-relative, and so does the neutral export, so the two readings agree."""
    out = subdir_run / "out"
    assert model_io.validate(out / "neutral") == []
    neutral, python = assert_equivalent(subdir_run, functions=False)
    assert set(neutral.static) == set(python.static) == {"subpkg/__init__.py", "subpkg/mod.py"}
    assert neutral.tests[0].file == "app/tests/test_sub.py"  # results are git-root-relative


@pytest.mark.parametrize("run", ["fixture_run", "mixed_run", "subdir_run"])
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
