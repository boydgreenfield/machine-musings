"""The shard argv from ``PythonAdapter.test_command`` keeps the target repo's pytest configuration.

pytest finds its configuration file before entry-point plugins register their options. A value
option written as two words (``--coverset-out /work/run/out``) is then read as a positional path,
and the paths under the worker's sibling ``run/`` directory move the inferred rootdir out of the repo:
no configfile is found and ``xfail_strict``, ``addopts``, ``markers``, ``pythonpath`` and the rest are
silently dropped. These tests run the exact shard argv (plus the runner's ``--rootdir``) against a
worker-shaped layout with the real plugin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coverset.lang.python import PythonAdapter

PYPROJECT = """\
[tool.pytest.ini_options]
xfail_strict = true
pythonpath = ["src"]
markers = ["slow: slow tests"]
"""

TESTS = """\
import pytest
from inipkg import answer


@pytest.mark.xfail(reason="strict xfail must fail when it passes")
def test_xpass():
    assert answer() == 42
"""


def _worker_layout(root: Path) -> tuple[Path, Path]:
    repo, run = root / "repo", root / "run"
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "inipkg").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (repo / "src" / "inipkg" / "__init__.py").write_text("def answer():\n    return 42\n")
    (repo / "tests" / "test_ini.py").write_text(TESTS)
    (run / "out").mkdir(parents=True)
    (run / "shard.args").write_text("tests/test_ini.py::test_xpass\n")
    (run / "shard.scores.json").write_text(json.dumps({"tests/test_ini.py::test_xpass": 1}))
    (run / "full-trace.txt").write_text("src/inipkg/__init__.py\n")
    return repo, run


def test_ini_options_survive_the_shard_argv(tmp_path: Path) -> None:
    pytest.importorskip("pytest_coverset")
    repo, run = _worker_layout(tmp_path)
    extra = {
        "python": sys.executable,
        "sources": ["inipkg"],
        "full_trace_file": str(run / "full-trace.txt"),
        "shapes": "on",
        "repr_examples": "scalars",
        "stop_file": str(run / "STOP"),
    }
    cmd = [
        *PythonAdapter().test_command(
            str(run / "shard.args"), str(run / "shard.scores.json"), str(run / "out"), extra
        ),
        f"--rootdir={repo}",
        "-v",  # cancels the argv's -q so the header (rootdir, configfile) is printed
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COVERAGE_", "COVERSET_"))}
    proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=120, check=False)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output[-3000:]
    assert "configfile: pyproject.toml" in output
    assert "XPASS(strict)" in output
    meta = json.loads((run / "out" / "meta.json").read_text())
    assert meta["configfile"] == str(repo / "pyproject.toml")
    assert meta["counts"]["failed"] == 1


def test_the_split_form_loses_the_configfile(tmp_path: Path) -> None:
    """Control: the same argv with ``--opt value`` words drops the repo's configuration (why ``=`` is used)."""
    pytest.importorskip("pytest_coverset")
    repo, run = _worker_layout(tmp_path)
    joined = PythonAdapter().test_command(
        str(run / "shard.args"),
        str(run / "shard.scores.json"),
        str(run / "out"),
        {"python": sys.executable, "full_trace_file": str(run / "full-trace.txt")},
    )
    split = [
        part for word in joined for part in (word.split("=", 1) if word.startswith("--coverset") else [word])
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COVERAGE_", "COVERSET_"))}
    env["PYTHONPATH"] = str(repo / "src")  # the lost `pythonpath` setting would otherwise fail the import
    proc = subprocess.run(
        [*split, f"--rootdir={repo}", "-v"], cwd=repo, env=env, capture_output=True, text=True, timeout=120
    )
    output = proc.stdout + proc.stderr
    assert "configfile:" not in output
    assert proc.returncode == 0 and "1 xpassed" in output, output[-3000:]
