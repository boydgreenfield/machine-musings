"""``coverset doctor``: the readiness report, its findings and fixes, the suggested config and the CLI.

Most tests run with ``--no-collect`` semantics (``collect=False``) and a fake ``uv`` for the sources
that need a resolver, so they are offline. The ones that build an environment and collect for real
are marked ``network`` and ``slow``.
"""

from __future__ import annotations

import json
import shutil
import socket
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from coverset.cli import cli
from coverset.commands.doctor import append_config, diagnose, render_text, worktree_archive
from coverset.lang.python import PythonAdapter, read_tool_config

ROOT = Path(__file__).parents[1]
REPOS = ROOT / "fixtures" / "repos"
DEMO_APP = ROOT / "examples" / "demo-app"


def fake_adapter(tmp_path: Path) -> PythonAdapter:
    script = tmp_path / "fake-uv"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write('attrs==26.1.0\\npytest==9.1.1\\n')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return PythonAdapter(uv=str(script), cache_dir=tmp_path / "cache")


def codes(report: object, level: str | None = None) -> list[str]:
    return [f.code for f in report.findings if level is None or f.level == level]  # type: ignore[attr-defined]


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv for uv export")
def test_the_demo_app_is_ready() -> None:
    report = diagnose(DEMO_APP, collect=False)
    assert report.verdict == "ready" and report.language == "python"
    assert report.environment["source"] == "uv.lock" and report.environment["requirements"] == 8
    assert report.environment["first_requirements"][1] == "hypothesis==6.168.5"
    assert report.source == {
        "source_root": "src",
        "how_root": "[tool.coverset]",
        "packages": ["ledger"],
        "how_packages": "[tool.coverset]",
    }
    assert report.install["mode"] == "none" and report.collection.skipped == "--no-collect"
    assert report.commands["collect"][-1] == "tests"
    assert report.commands["shard"][:4] == ["python", "-m", "pytest", "@run/shard.args"]
    assert "--coverset-source=ledger" in report.commands["shard"]
    text = render_text(report)
    assert "Verdict: ready" in text and "uv.lock  (uv.lock is committed)" in text


def test_requirements_repo_gets_a_warning_and_its_notes(tmp_path: Path) -> None:
    report = diagnose(REPOS / "requirements-only", adapter=fake_adapter(tmp_path), collect=False)
    assert report.verdict == "ready, with warnings"
    assert "no-lock" in codes(report, "warning")
    assert report.environment["inputs"] == ["requirements-dev.txt", "requirements.txt"]
    notes = [f.message for f in report.findings if f.code == "env-note"]
    assert any("--index-url" in note for note in notes) and any("project itself" in note for note in notes)
    assert 'requirements = ["requirements-dev.txt", "requirements.txt"]' in report.suggested_config


def test_the_entry_point_project_is_installed(tmp_path: Path) -> None:
    report = diagnose(REPOS / "src-entry-points", adapter=fake_adapter(tmp_path), collect=False)
    assert report.install["mode"] == "wheel" and report.install["build_requires"] == ["setuptools>=61"]
    assert "install" in codes(report, "info")
    assert 'install = "wheel"' in report.suggested_config


def test_suggested_config_round_trips_through_the_parser(tmp_path: Path) -> None:
    repo = write(
        tmp_path / "repo",
        {
            "pkg/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "requirements-dev.txt": "pytest\n",
            "setup.cfg": "[tool:pytest]\naddopts = -ra --lf -x --cov=pkg\n",
        },
    )
    report = diagnose(repo, adapter=fake_adapter(tmp_path), collect=False)
    errors = [f for f in report.findings if f.code == "addopts" and f.level == "error"]
    assert {f.message.split("'")[1] for f in errors} == {"--lf", "--cov=pkg"}
    assert any(f.code == "addopts" and f.level == "warning" and "'-x'" in f.message for f in report.findings)
    # pytest_args are not forwarded to real runs yet: the fix is the repository's own addopts
    assert errors[0].fix is not None and "remove it from addopts in setup.cfg" in errors[0].fix
    assert "-ra" in errors[0].fix and "override-ini" not in errors[0].fix
    assert report.verdict == "not ready"
    section = tomllib.loads(report.suggested_config)["tool"]["coverset"]
    assert "pytest_args" not in section
    assert "# edit addopts in setup.cfg (not here) to read: -ra" in report.suggested_config
    config = read_tool_config(report.suggested_config, repo_root=repo)
    assert config.source == ("pkg",) and config.pytest_args == ()


def test_the_doctor_runs_exactly_what_the_planner_and_runner_run(tmp_path: Path) -> None:
    """pytest_args are not forwarded by the planner or the runner yet, so the doctor must not add
    them to its collect or shard commands (a verdict about a run that never happens), and says so."""
    repo = write(
        tmp_path / "repo",
        {
            "pkg/__init__.py": "",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "requirements-dev.txt": "pytest\n",
            "pyproject.toml": '[tool.coverset]\npytest_args = ["-p", "no:randomly"]\n',
        },
    )
    report = diagnose(repo, adapter=fake_adapter(tmp_path), collect=False)
    assert "no:randomly" not in report.commands["collect"]
    assert "no:randomly" not in report.commands["shard"]
    assert report.commands["collect"][-1] == "tests"
    pending = [f for f in report.findings if f.code == "pytest-args-pending"]
    assert pending and pending[0].level == "warning" and "-p no:randomly" in pending[0].message
    assert report.tests["pytest_args"] == ["-p", "no:randomly"]
    section = tomllib.loads(report.suggested_config)["tool"]["coverset"]
    assert section["pytest_args"] == ["-p", "no:randomly"]  # kept, so the section round-trips


def test_the_collect_command_matches_the_planner_source() -> None:
    """``_collect_argv`` mirrors ``planner._collect``: the same words, in the same order."""
    import inspect

    from coverset.commands.doctor import _collect_argv
    from coverset.lang.python import CoversetConfig
    from coverset.orchestrator import planner

    source = inspect.getsource(planner._collect)
    start = source.index("cmd = [")
    planner_words = source[start : source.index("]", source.index("*config.testpaths"))]
    argv = _collect_argv("REPO", "OUT", CoversetConfig(testpaths=("tests",), pytest_args=("-ra",)))
    assert argv[-1] == "tests" and "-ra" not in argv
    for word in (
        "--collect-only",
        "no:cacheprovider",
        "--coverset-shapes=off",
        "--continue-on-collection-errors",
    ):
        assert word in planner_words and word in argv
    assert "pytest_args" not in planner_words  # update the doctor when the planner starts passing them


def test_a_repository_without_python_or_sources(tmp_path: Path) -> None:
    empty = diagnose(tmp_path, collect=False)
    assert empty.verdict == "not ready" and codes(empty) == ["not-python"]
    write(tmp_path / "ts", {"package.json": "{}"})
    typescript = diagnose(tmp_path / "ts", collect=False)
    assert typescript.language == "typescript" and "upload mode" in (typescript.findings[0].fix or "")
    nosource = write(tmp_path / "nosource", {"tools/run.py": "", "requirements.txt": "attrs\n"})
    report = diagnose(nosource, adapter=fake_adapter(tmp_path), collect=False)
    assert "no-source" in codes(report, "error") and "no-testpaths" in codes(report, "warning")


def test_an_environment_that_cannot_be_described(tmp_path: Path) -> None:
    repo = write(
        tmp_path / "repo", {"pkg/__init__.py": "", "setup.py": "import x\nsetup(install_requires=x.deps)\n"}
    )
    report = diagnose(repo, collect=False)
    assert "environment" in codes(report, "error") and report.environment["error"]
    assert report.commands == {} and report.collection.skipped == "--no-collect"


def test_write_config_appends_outside_the_repository_only(tmp_path: Path) -> None:
    report = diagnose(REPOS / "poetry", collect=False)
    out = tmp_path / "out" / "poetry.toml"
    append_config(report, REPOS / "poetry", out)
    append_config(report, REPOS / "poetry", out)
    assert out.read_text().count("[tool.coverset]") == 2
    with pytest.raises(Exception, match="inside the repository"):
        append_config(report, REPOS / "poetry", REPOS / "poetry" / "suggested.toml")
    assert not (REPOS / "poetry" / "suggested.toml").exists()


def test_the_default_config_target_is_never_inside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without settings the data dir is ``./data``: from the repository's root that is inside it, so the
    default falls back to coverset's cache, and a bad --config-out fails before the diagnosis runs."""
    import coverset.commands.common as common
    from coverset.commands import doctor

    repo = write(tmp_path / "repo", {"pkg/__init__.py": "", "requirements.txt": "attrs\n"})
    monkeypatch.chdir(repo)
    monkeypatch.delenv("COVERSET_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    def no_settings() -> None:
        raise RuntimeError("no settings")

    monkeypatch.setattr(common, "load", no_settings)
    target = doctor._default_config_out(repo.resolve())
    assert target == (tmp_path / "xdg" / "coverset" / "doctor" / "repo.toml").resolve()
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    assert (
        doctor._default_config_out(repo.resolve()) == (tmp_path / "data" / "doctor" / "repo.toml").resolve()
    )

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("diagnose ran before the target was checked")

    monkeypatch.setattr(doctor, "diagnose", must_not_run)
    result = CliRunner().invoke(
        cli,
        [
            "doctor",
            "--repo",
            str(repo),
            "--no-collect",
            "--write-config",
            "--config-out",
            str(repo / "x.toml"),
        ],
    )
    assert result.exit_code != 0 and "inside the repository" in result.output
    assert not (repo / "x.toml").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_working_tree_archive_and_lfs_detection(tmp_path: Path) -> None:
    repo = write(
        tmp_path / "repo",
        {
            "pyproject.toml": '[project]\nname = "app"\nversion = "1"\ndependencies = []\n',
            "app/__init__.py": "",
            "tests/test_app.py": "def test_x():\n    pass\n",
            "requirements-dev.txt": "./vendor/blob-1.0-py3-none-any.whl\n",
            "vendor/blob-1.0-py3-none-any.whl": "PK real wheel bytes",
            ".gitattributes": "*.whl filter=lfs diff=lfs merge=lfs -text\n",
            ".gitignore": "ignored/\n",
            "ignored/big.bin": "x" * 1000,
        },
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tarball, files, size = worktree_archive(repo)
    assert files == 7 and size < 1000  # tracked and untracked files, nothing git ignores
    report = diagnose(repo, adapter=fake_adapter(tmp_path), collect=False)
    assert "lfs-local-packages" in codes(report, "error")
    assert report.size["lfs_files"] == 1


def test_the_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--repo", str(REPOS / "poetry"), "--no-collect", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "ready" and payload["environment"]["source"] == "poetry"
    out = tmp_path / "suggested.toml"
    result = runner.invoke(
        cli,
        [
            "doctor",
            "--repo",
            str(REPOS / "poetry"),
            "--no-collect",
            "--write-config",
            "--config-out",
            str(out),
        ],
    )
    assert result.exit_code == 0 and "appended to" in result.output and out.is_file()
    failing = runner.invoke(cli, ["doctor", "--repo", str(tmp_path), "--no-collect"])
    assert failing.exit_code == 1 and "Verdict: not ready" in failing.output
    assert "doctor" in runner.invoke(cli, ["--help"]).output


# -- collecting for real (network) ---------------------------------------------------------------------------


def _online() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=3):
            return True
    except OSError:
        return False


needs_index = pytest.mark.skipif(shutil.which("uv") is None or not _online(), reason="needs uv and the index")


@pytest.mark.network
@pytest.mark.slow
@needs_index
def test_collection_installs_the_project_and_finds_the_tests(tmp_path: Path) -> None:
    report = diagnose(REPOS / "src-entry-points", adapter=PythonAdapter(cache_dir=tmp_path / "cache"))
    collection = report.collection
    assert collection.ran and collection.exit_code == 0 and collection.collected == 3
    assert collection.imports == [{"package": "entry_app", "ok": True, "error": ""}]
    assert report.verdict == "ready, with warnings"  # no lock


@pytest.mark.network
@pytest.mark.slow
@needs_index
def test_collection_explains_a_package_that_needs_installing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(
        REPOS / "src-entry-points", repo, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info")
    )
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + '\n[tool.coverset]\ninstall = "none"\n')
    report = diagnose(repo, adapter=PythonAdapter(cache_dir=tmp_path / "cache"))
    failed = [f for f in report.findings if f.code == "import"]
    assert (
        failed
        and "PackageNotFoundError" in failed[0].message
        and 'install = "wheel"' in (failed[0].fix or "")
    )
    assert "collect-errors" in codes(report, "error")  # every test module imports the package
    assert report.verdict == "not ready"


def test_a_rust_repository_is_pointed_at_upload_mode(tmp_path: Path) -> None:
    write(tmp_path, {"Cargo.toml": '[package]\nname = "r"\n', "src/main.rs": "fn main() {}\n"})
    report = diagnose(tmp_path, collect=False)
    assert report.language == "rust" and "docs/rust.md" in (report.findings[0].fix or "")
