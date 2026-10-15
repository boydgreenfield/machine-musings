"""``coverset tests economy|redundant|minimal|tax`` over real pytest-coverset bundles.

The base is the plugin's fixture project; the head is a copy with the horror story added: a test
file that checks an environment flag parses ``"true"`` with a fixture, a helper class and three
copies of one test, every copy calling ``core.total`` exactly as ``test_total`` already does. It
covers no new line, so the tax is flagged and every copy repeats an existing test's flow.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from coverset.cli import cli
from coverset.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PROJECT = ROOT / "packages" / "pytest-coverset" / "tests" / "fixture_project"

BLOAT = """\
import pytest
from fixturepkg import core

TRUTHY = ("true", "1", "TRUE")


class _Env:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def env():
    sandbox = _Env()
    sandbox.set("FLAG", "true")
    yield sandbox
    sandbox.values.clear()


def _parse(value):
    text = value.strip()
    if text in TRUTHY:
        return True
    return False


def test_flag_is_true(env):
    value = env.get("FLAG")
    parsed = _parse(value)
    assert parsed is True
    assert core.total([3, -1, 2]) == 4
    assert core.total([0.0, 0.0]) == 0


def test_flag_is_true_again(env):
    value = env.get("FLAG")
    parsed = _parse(value)
    assert parsed is True
    assert core.total([3, -1, 2]) == 4
    assert core.total([0.0, 0.0]) == 0


def test_flag_is_true_once_more(env):
    value = env.get("FLAG")
    parsed = _parse(value)
    assert parsed is True
    assert core.total([3, -1, 2]) == 4
    assert core.total([0.0, 0.0]) == 0
"""


@dataclass(frozen=True)
class Projects:
    base: Path
    head: Path


def _run_plugin(project: Path) -> None:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COVERAGE_", "COV_CORE", "COVERSET_"))
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_coverset", "-q", "-p", "no:cacheprovider"]
        + ["--coverset-out", "out", "--coverset-source", "fixturepkg", "--coverset-neutral"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]


@pytest.fixture(scope="module")
def projects(tmp_path_factory: pytest.TempPathFactory) -> Projects:
    root = tmp_path_factory.mktemp("economy-cli")
    base, head = root / "base", root / "head"
    for target in (base, head):
        shutil.copytree(FIXTURE_PROJECT, target, ignore=shutil.ignore_patterns("__pycache__", "out"))
    (head / "tests" / "test_env_flag.py").write_text(BLOAT, encoding="utf-8")
    _run_plugin(base)
    _run_plugin(head)
    return Projects(base, head)


def invoke(*args: str) -> object:
    return CliRunner().invoke(cli, ["tests", *args], catch_exceptions=False)


def test_the_group_is_visible_with_its_four_commands() -> None:
    top = CliRunner().invoke(cli, ["--help"])
    assert top.exit_code == 0 and "tests" in top.output
    group = CliRunner().invoke(cli, ["tests", "--help"])
    assert group.exit_code == 0
    for name in ("economy", "redundant", "minimal", "tax"):
        assert name in group.output
    assert "what each test costs" in group.output


def test_economy_text_and_json(projects: Projects) -> None:
    text = invoke("economy", str(projects.head / "out"), "--repo-root", str(projects.head), "--limit", "5")
    assert text.exit_code == 0, text.output
    assert "Test economy · 27 tests" in text.output
    assert "minimal set" in text.output and "LEVERAGE" in text.output
    raw = invoke(
        "economy",
        str(projects.head / "out"),
        "--repo-root",
        str(projects.head),
        "--json",
        "--sort",
        "statements",
        "--limit",
        "0",
    )
    data = json.loads(raw.output)
    assert data["totals"]["tests"] == 27 and data["tests_total"] == 27
    sizes = [row["statements"] for row in data["tests"]]
    assert sizes == sorted(sizes, reverse=True)
    assert data["tests"][0]["nodeid"].startswith("tests/test_env_flag.py::")
    only = json.loads(
        invoke(
            "economy",
            str(projects.head / "out"),
            "--repo-root",
            str(projects.head),
            "--json",
            "--only",
            "grouped",
            "--limit",
            "0",
        ).output
    )
    assert only["tests"] and all(row["group_id"] for row in only["tests"])


def test_redundant_names_the_copies_as_candidates(projects: Projects) -> None:
    result = invoke("redundant", str(projects.head / "out"), "--repo-root", str(projects.head), "--json")
    data = json.loads(result.output)
    assert data["caveat"].startswith("Candidates only")
    group = next(g for g in data["groups"] if any("test_flag_is_true" in n for n in g["nodeids"]))
    assert "tests/test_core.py::test_total" in group["nodeids"]  # the copies repeat test_total's flow
    assert len(group["nodeids"]) == 4
    assert sum(1 for n in data["removable"] if "test_env_flag" in n) >= 2
    text = invoke("redundant", str(projects.head / "out"), "--repo-root", str(projects.head))
    assert "Candidates only" in text.output and "drop?" in text.output


def test_minimal_writes_an_argument_file_pytest_reads(projects: Projects, tmp_path: Path) -> None:
    target = tmp_path / "deselect.txt"
    result = invoke(
        "minimal",
        str(projects.head / "out"),
        "--repo-root",
        str(projects.head),
        "--write",
        str(target),
        "--json",
    )
    data = json.loads(result.output)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == sorted(f"--deselect={n}" for n in data["drop"])
    assert len(data["keep"]) + len(data["drop"]) + len(data["uncompared"]) == 27
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COVERSET_"))}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"@{target}"],
        cwd=projects.head,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-2000:]
    assert f"{len(lines)} deselected" in run.stdout


def test_tax_flags_the_horror_story(projects: Projects) -> None:
    args = [
        "tax",
        str(projects.head / "out"),
        "--repo-root",
        str(projects.head),
        "--compare",
        str(projects.base / "out"),
        "--base-root",
        str(projects.base),
    ]
    result = CliRunner().invoke(cli, ["tests", *args, "--check"])
    assert result.exit_code == 1, result.output  # --check: over the threshold
    first = result.output.splitlines()[0]
    assert first.startswith("Test tax: ")
    assert "for 0 newly covered lines (threshold 8)" in first
    assert "3 new tests duplicate existing flows" in first
    data = json.loads(invoke(*args, "--json").output)
    tax = data["tax"]
    assert tax["flagged"] and tax["statements_measured"] and tax["new_tests"] == 3
    assert tax["added_statements"] >= 20 and tax["newly_covered_lines"] == 0
    assert {d["duplicates"] for d in tax["duplicates"]} == {"tests/test_core.py::test_total"}
    assert data["line"].startswith("**Test tax: ")
    lenient = invoke(*args, "--threshold", "1000", "--json")
    assert not json.loads(lenient.output)["tax"]["flagged"]


def test_tax_without_the_base_sources_does_not_count_statements(projects: Projects) -> None:
    result = invoke(
        "tax",
        str(projects.head / "out"),
        "--repo-root",
        str(projects.head),
        "--compare",
        str(projects.base / "out"),
        "--json",
    )
    tax = json.loads(result.output)["tax"]
    assert tax["statements_measured"] is False and tax["flagged"] is False
    assert tax["duplicate_count"] == 3  # the flows are still compared


def test_usage_errors(
    projects: Projects, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    out = str(projects.head / "out")
    assert CliRunner().invoke(cli, ["tests", "economy"]).exit_code == 2
    assert CliRunner().invoke(cli, ["tests", "economy", out, "--run", "r"]).exit_code == 2
    assert CliRunner().invoke(cli, ["tests", "tax", out]).exit_code == 2  # SOURCE needs --compare
    both = ["tests", "tax", out, "--compare", out, "--base-ref", "HEAD", "--base-root", str(tmp_path)]
    assert CliRunner().invoke(cli, both).exit_code == 2
    bad_ref = CliRunner().invoke(
        cli,
        [
            "tests",
            "tax",
            out,
            "--repo-root",
            str(projects.head),
            "--compare",
            out,
            "--base-ref",
            "--upload-pack=x",
        ],
    )
    assert bad_ref.exit_code == 1 and "is not a commit" in bad_ref.output
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COVERSET_DATABASE_URL", settings.database_url)
    missing = CliRunner().invoke(cli, ["tests", "economy", "--run", "run_nope"])
    assert missing.exit_code == 1 and "no run" in missing.output
