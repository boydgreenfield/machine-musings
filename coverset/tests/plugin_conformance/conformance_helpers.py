"""Run pytest-coverset in a subprocess of the workspace interpreter and compare what it writes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "packages" / "pytest-coverset"
PLUGIN_TESTS = PLUGIN / "tests"
FIXTURE_PROJECT = PLUGIN_TESTS / "fixture_project"
DEMO_SRC = ROOT / "examples" / "demo-app" / "src"
NONCE = "conformance-nonce"
_SCRUBBED_ENV = (
    "COVERAGE_PROCESS_START",
    "COVERSET_NEUTRAL",
    "COVERSET_SHARD",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
)


def copy_fixture_project(target: Path) -> Path:
    """A copy of the plugin's fixture project at ``target`` (no ``__pycache__``)."""
    shutil.copytree(FIXTURE_PROJECT, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def run_pytest(
    project: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``python -m pytest -q -p no:cacheprovider ARGS`` in ``project`` with the workspace interpreter."""
    environ = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    environ["COVERSET_EVENT_NONCE"] = NONCE
    environ["PYTHONDONTWRITEBYTECODE"] = "1"
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=project,
        env=environ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
