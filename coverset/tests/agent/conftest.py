"""Fixtures for the agent tests: the demo app measured by ``pytest --coverset`` in a git work tree.

``demo_app`` copies ``examples/demo-app`` into a temporary directory, commits it on ``main``, adds
one untested function on a ``feature`` branch (``parse_minutes`` in ``durations/parse.py``) and runs
the demo's 97 tests with the plugin (``--coverset``: the bundle goes to ``.coverset`` with its
neutral copy). That gives every agent tool something real to answer: the three README functions,
property-based tests, parametrized families, and a diff against ``main`` for the patch tools. It
runs once per session (about 3 s).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from agent_helpers import DemoApp

ROOT = Path(__file__).resolve().parents[2]
DEMO_APP = ROOT / "examples" / "demo-app"

NEW_FUNCTION = '''

def parse_minutes(text: str) -> int:
    """Whole minutes in a duration string such as ``"1h30m"``."""
    seconds = parse_duration(text)
    if seconds < 0:
        raise ValueError("negative duration")
    return seconds // 60
'''


def git_env() -> dict[str, str]:
    """A git environment without the user's config (no hooks, signing or aliases)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="coverset tests",
        GIT_AUTHOR_EMAIL="tests@coverset.invalid",
        GIT_COMMITTER_NAME="coverset tests",
        GIT_COMMITTER_EMAIL="tests@coverset.invalid",
    )
    return env


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], env=git_env(), check=True, capture_output=True, timeout=60
    )


def run_coverset_pytest(project: Path) -> subprocess.CompletedProcess[str]:
    """``pytest --coverset`` in ``project`` with this interpreter (the plugin is installed here)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COVERAGE_", "COV_CORE", "COVERSET_"))
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "--coverset"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="session")
def demo_app(tmp_path_factory: pytest.TempPathFactory) -> DemoApp:
    """The demo app on a ``feature`` branch with one new untested function, measured."""
    if shutil.which("git") is None:
        pytest.skip("the agent tests need git")
    project = tmp_path_factory.mktemp("agent-demo") / "demo-app"
    shutil.copytree(
        DEMO_APP, project, ignore=shutil.ignore_patterns("__pycache__", ".venv", ".coverset", ".pytest_cache")
    )
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "demo app")
    git(project, "checkout", "-q", "-b", "feature")
    parse = project / "src" / "ledger" / "durations" / "parse.py"
    parse.write_text(parse.read_text(encoding="utf-8") + NEW_FUNCTION, encoding="utf-8")
    git(project, "commit", "-q", "-am", "parse_minutes")
    result = run_coverset_pytest(project)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert (project / ".coverset" / "coverage.sqlite").is_file()
    return DemoApp(project, project / ".coverset")
