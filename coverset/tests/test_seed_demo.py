"""The seeded demo repository is byte-stable and its branches behave as the demo needs.

The expected SHAs change whenever anything under examples/demo-app or examples/demo-prs
changes; that is intended. Re-run ``uv run python scripts/seed_demo.py`` and update them.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"

EXPECTED_COMMITS = {
    "main-1": "5598d93ba74ad627a19b2d1f1a47d621a08d55c2",
    "main-2": "415127f7ce1143a7a66c22c5aa6f753a64c88221",
    "main-3": "ca9ab713fa46283402286cb186a1cfae05f0f5f4",
    "main-4": "bf3c1c40e3d83000031566702e260ff7bc8034f9",
    "main-5": "d0e6be8c81905e016c1a44f626f4b8ab473d6756",
    "main-6": "77e37dc692c5ec51c62ff80a2fdc5aa135583bc1",
}
EXPECTED_BRANCHES = {
    "main": "77e37dc692c5ec51c62ff80a2fdc5aa135583bc1",
    "pr-1-partial-refunds": "2bb99b1de9818b9646784e085b02dd6f2c9b0c78",
    "pr-2-subdomain-signups": "517d7d3cfd2929d1edad804b8ab7ccc507cf86c6",
}

# A pytest plugin, loaded into the demo suite's own environment, that records which tests each
# @pytest.mark.invariant id links to.
LINK_DUMP_PLUGIN = """
import json
import os


def pytest_collection_modifyitems(items):
    links = {}
    for item in items:
        for mark in item.iter_markers("invariant"):
            links.setdefault(mark.args[0], []).append(item.originalname)
    with open(os.environ["LINK_DUMP_OUT"], "w", encoding="utf-8") as handle:
        json.dump(links, handle)
"""

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None or shutil.which("uv") is None, reason="needs git and uv"),
]


@dataclass(frozen=True)
class SuiteResult:
    """Outcome of one run of the demo test suite."""

    returncode: int
    tests: int
    failed: list[str]


def _load_seed_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed_demo = _load_seed_module()


@pytest.fixture(scope="module")
def demo_git(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly seeded bare demo repository."""
    out = tmp_path_factory.mktemp("seed") / "demo.git"
    seed_demo.seed(out)
    return out


@pytest.fixture(scope="module")
def demo_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One virtualenv shared by every checkout, which all use the same uv.lock."""
    return tmp_path_factory.mktemp("venv") / "venv"


def _git(git_dir: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        capture_output=True,
        check=True,
        env=seed_demo.git_env(),
    ).stdout


def _checkout(git_dir: Path, ref: str, dest: Path) -> Path:
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(_git(git_dir, "archive", "--format=tar", ref))) as archive:
        archive.extractall(dest, filter="data")
    return dest


def _suite_env(venv: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "COVERSET_DEMO_RUN"}
    }
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    return env


def _run_suite(checkout: Path, venv: Path, demo_run: str | None = None) -> SuiteResult:
    env = _suite_env(venv)
    if demo_run is not None:
        env["COVERSET_DEMO_RUN"] = demo_run
    report = checkout / "junit.xml"
    command = [
        "uv",
        "run",
        "--locked",
        "--project",
        str(checkout),
        "python",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]
    proc = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=120)
    assert report.exists(), proc.stdout + proc.stderr
    cases = ET.parse(report).getroot().iter("testcase")
    failed = []
    total = 0
    for case in cases:
        total += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(case.attrib["name"])
    return SuiteResult(returncode=proc.returncode, tests=total, failed=sorted(failed))


def _invariant_links(checkout: Path, venv: Path, plugin_dir: Path) -> dict[str, list[str]]:
    """Collect the suite at ``checkout`` and return ``{invariant id: [test function names]}``."""
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "coverset_link_dump.py").write_text(LINK_DUMP_PLUGIN, encoding="utf-8")
    out = plugin_dir / "links.json"
    env = {**_suite_env(venv), "PYTHONPATH": str(plugin_dir), "LINK_DUMP_OUT": str(out)}
    command = ["uv", "run", "--locked", "--project", str(checkout), "python", "-m", "pytest", "-q"]
    command += ["--collect-only", "-p", "no:cacheprovider", "-p", "coverset_link_dump"]
    proc = subprocess.run(command, cwd=checkout, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _init_work_tree(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, env=seed_demo.git_env())
    return path


def test_seed_produces_the_expected_shas(demo_git: Path) -> None:
    manifest = json.loads(seed_demo.shas_path(demo_git).read_text(encoding="utf-8"))
    assert manifest["commits"] == EXPECTED_COMMITS
    assert manifest["branches"] == EXPECTED_BRANCHES
    assert [(pr["number"], pr["branch"], pr["head_sha"]) for pr in manifest["prs"]] == [
        (1, "pr-1-partial-refunds", EXPECTED_BRANCHES["pr-1-partial-refunds"]),
        (2, "pr-2-subdomain-signups", EXPECTED_BRANCHES["pr-2-subdomain-signups"]),
    ]
    assert {pr["base_sha"] for pr in manifest["prs"]} == {EXPECTED_BRANCHES["main"]}


def test_reseeding_replaces_a_seeded_repository(tmp_path: Path) -> None:
    out = tmp_path / "demo.git"
    first = seed_demo.seed(out)
    (out / "stale").write_text("left over from an older seed")
    second = seed_demo.seed(out)
    assert first == second
    assert second.branches == EXPECTED_BRANCHES
    assert not (out / "stale").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["demo.git", "demo.git.shas.json"]


def test_seeding_into_an_empty_directory_is_allowed(tmp_path: Path) -> None:
    out = tmp_path / "demo.git"
    out.mkdir()
    assert seed_demo.seed(out).branches == EXPECTED_BRANCHES


@pytest.mark.parametrize("bare_repo", [False, True], ids=["plain-directory", "bare-repo-without-manifest"])
def test_seeding_refuses_to_replace_a_directory_it_did_not_make(tmp_path: Path, bare_repo: bool) -> None:
    out = tmp_path / "precious"
    if bare_repo:
        subprocess.run(["git", "init", "--quiet", "--bare", str(out)], check=True, env=seed_demo.git_env())
    else:
        out.mkdir()
    (out / "notes.txt").write_text("keep me")
    with pytest.raises(ValueError, match="refusing to replace"):
        seed_demo.seed(out)
    assert (out / "notes.txt").read_text() == "keep me"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["precious"]


def test_failed_rebuild_keeps_the_previous_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "demo.git"
    seed_demo.seed(out)
    manifest = seed_demo.shas_path(out).read_text(encoding="utf-8")

    def broken_build(*args: object) -> None:
        raise RuntimeError("git fast-import failed")

    monkeypatch.setattr(seed_demo, "_build", broken_build)
    with pytest.raises(RuntimeError, match="fast-import"):
        seed_demo.seed(out)
    assert _git(out, "rev-parse", "main").decode().strip() == EXPECTED_BRANCHES["main"]
    assert seed_demo.shas_path(out).read_text(encoding="utf-8") == manifest
    assert sorted(path.name for path in tmp_path.iterdir()) == ["demo.git", "demo.git.shas.json"]


def test_read_tree_skips_what_the_demo_gitignore_excludes(tmp_path: Path) -> None:
    root = _init_work_tree(tmp_path / "app")
    shutil.copyfile(seed_demo.DEMO_APP / ".gitignore", root / ".gitignore")
    files = [
        "src/ledger/__init__.py",
        ".env",
        ".env.local",
        "coverage.json",
        "coverage.xml",
        "htmlcov/index.html",
        "junit.xml",
        ".coverset/bundle/results.jsonl",
        ".venv/bin/python",
        ".hypothesis/examples/x",
        "secrets/token.txt",
        "deploy.pem",
    ]
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x\r\n")
    tree = seed_demo.read_tree(root)
    assert sorted(tree) == [".gitignore", "src/ledger/__init__.py"]
    assert tree["src/ledger/__init__.py"] == b"x\n"


def test_read_tree_refuses_secret_looking_files_git_would_add(tmp_path: Path) -> None:
    root = _init_work_tree(tmp_path / "app")
    (root / ".env").write_text("TOKEN=x\n")
    with pytest.raises(ValueError, match="secret-looking"):
        seed_demo.read_tree(root)


def test_main_has_six_commits_with_fixed_identity_and_dates(demo_git: Path) -> None:
    log = _git(demo_git, "log", "--reverse", "--format=%an <%ae>|%cn <%ce>|%aI|%cI", "main").decode()
    lines = log.splitlines()
    assert len(lines) == 6
    for hour, line in enumerate(lines):
        stamp = f"2026-10-01T{12 + hour:02d}:00:00+00:00"
        assert (
            line == f"Demo Author <demo@coverset.invalid>|Demo Author <demo@coverset.invalid>|{stamp}|{stamp}"
        )
    modes = {row.split()[0] for row in _git(demo_git, "ls-tree", "-r", "main").decode().splitlines()}
    assert modes == {"100644"}


@pytest.mark.parametrize("branch", ["pr-1-partial-refunds", "pr-2-subdomain-signups"])
def test_pr_branch_is_one_commit_on_main(demo_git: Path, branch: str) -> None:
    assert _git(demo_git, "rev-list", "--count", f"main..{branch}").strip() == b"1"
    assert _git(demo_git, "rev-parse", f"{branch}^").decode().strip() == EXPECTED_BRANCHES["main"]


def test_main_suite_passes(demo_git: Path, demo_venv: Path, tmp_path: Path) -> None:
    result = _run_suite(_checkout(demo_git, "main", tmp_path / "main"), demo_venv)
    assert result.failed == []
    assert result.returncode == 0
    assert result.tests > 0


@pytest.mark.parametrize(
    ("demo_run", "failed"),
    [("4", ["test_hold_survives_clock_skew"]), ("5", [])],
    ids=["history-run-4", "its-retry"],
)
def test_flaky_test_fails_once_and_passes_on_retry(
    demo_git: Path, demo_venv: Path, tmp_path: Path, demo_run: str, failed: list[str]
) -> None:
    result = _run_suite(_checkout(demo_git, "main", tmp_path / "main"), demo_venv, demo_run=demo_run)
    assert result.failed == failed


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("main", {"cents_conserved": 2, "durations_consume_whole_input": 2, "merge_idempotent": 1}),
        (
            "pr-1-partial-refunds",
            {"cents_conserved": 3, "durations_consume_whole_input": 2, "merge_idempotent": 1},
        ),
        (
            "pr-2-subdomain-signups",
            {"cents_conserved": 2, "durations_consume_whole_input": 2, "merge_idempotent": 1},
        ),
    ],
)
def test_invariant_markers(
    demo_git: Path, demo_venv: Path, tmp_path: Path, branch: str, expected: dict[str, int]
) -> None:
    """No test links signup_rejects_existing_domain, so it is untested (never stale) on PR #2."""
    links = _invariant_links(
        _checkout(demo_git, branch, tmp_path / "checkout"), demo_venv, tmp_path / "plugin"
    )
    assert {inv: len(set(names)) for inv, names in links.items()} == expected
    if branch == "pr-1-partial-refunds":
        assert "test_refund_partial" in links["cents_conserved"]


def test_refunds_pr_fails_only_the_partial_refund_test(
    demo_git: Path, demo_venv: Path, tmp_path: Path
) -> None:
    result = _run_suite(_checkout(demo_git, "pr-1-partial-refunds", tmp_path / "pr1"), demo_venv)
    assert result.failed == ["test_refund_partial"]


def test_subdomain_pr_passes(demo_git: Path, demo_venv: Path, tmp_path: Path) -> None:
    result = _run_suite(_checkout(demo_git, "pr-2-subdomain-signups", tmp_path / "pr2"), demo_venv)
    assert result.failed == []
    assert result.returncode == 0
