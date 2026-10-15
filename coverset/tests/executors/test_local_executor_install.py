"""LocalExecutor: installing the project (``[tool.coverset] install``) and local packages for a run.

The install goes into a side directory per (environment, commit), never into the shared venv; the
run's workers see it through ``COVERSET_SITE_DIRS`` and the venv's ``_coverset_site_dirs.pth``. The
fake-installer tests run offline; the end-to-end ones build a real venv with uv and are marked
``network`` (they skip without the package index).
"""

from __future__ import annotations

import io
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from coverset.config import Settings
from coverset.executors.base import Resources
from coverset.executors.env_export import build_env_spec, build_plugin_wheel
from coverset.executors.local import (
    SITE_DIRS_ENV,
    SITE_DIRS_PTH,
    LocalExecutor,
    uv_project_installer,
)
from coverset.lang.base import EnvSpec
from coverset.lang.python import PythonAdapter

REPOS = Path(__file__).parents[2] / "fixtures" / "repos"


def spec(install: str = "wheel", requirements: str = "", env_hash: str = "e" * 64) -> EnvSpec:
    return EnvSpec("3.13", "l", requirements, None, "src", env_hash, env_source="pyproject", install=install)


def fake_venv_builder(env: EnvSpec, target: Path) -> None:
    (target / "bin").mkdir()
    (target / "bin" / "python").symlink_to(sys.executable)
    (target / "lib" / "python3.13" / "site-packages").mkdir(parents=True)


@pytest.fixture
def installs() -> list[tuple[str, Path]]:
    return []


@pytest.fixture
def executor(tmp_path: Path, installs: list[tuple[str, Path]]) -> LocalExecutor:
    def installer(env: EnvSpec, venv: Path, checkout: Path, site: Path) -> None:
        installs.append((env.env_hash, checkout))
        time.sleep(0.05)
        (site / "marker.txt").write_text("installed")

    (tmp_path / "tmp").mkdir()
    return LocalExecutor(
        max_workers=2,
        cache_dir=tmp_path / "cache",
        temp_dir=tmp_path / "tmp",
        venv_builder=fake_venv_builder,
        project_installer=installer,
        sweep=False,
    )


async def test_install_once_per_environment_and_commit(
    executor: LocalExecutor, installs: list[tuple[str, Path]], tmp_path: Path
) -> None:
    env = await executor.ensure_env(spec())
    first = await executor.install_project(env, tmp_path, sha="a" * 40, run_id="run-1")
    again = await executor.install_project(env, tmp_path, sha="a" * 40, run_id="run-2")
    other = await executor.install_project(env, tmp_path, sha="b" * 40, run_id="run-3")
    assert first is not None and first == again != other
    assert (first / "marker.txt").read_text() == "installed" and len(installs) == 2
    hook = Path(env.location) / "lib" / "python3.13" / "site-packages" / SITE_DIRS_PTH
    assert SITE_DIRS_ENV in hook.read_text()


def test_concurrent_installs_build_once(
    executor: LocalExecutor, installs: list[tuple[str, Path]], tmp_path: Path
) -> None:
    env = executor.ensure_env_sync(spec())
    results: list[Path] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(executor.install_project_sync(env, tmp_path, "c" * 40))
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1 and len(installs) == 1


async def test_nothing_to_install_returns_none(
    executor: LocalExecutor, installs: list, tmp_path: Path
) -> None:  # type: ignore[type-arg]
    env = await executor.ensure_env(spec(install="none", env_hash="f" * 64))
    assert await executor.install_project(env, tmp_path, sha="a" * 40, run_id="r") is None
    local = spec(install="none", requirements="# coverset-local: file w/x-1.whl x\n", env_hash="d" * 64)
    env = await executor.ensure_env(local)
    assert await executor.install_project(env, tmp_path, sha="a" * 40, run_id="r") is not None
    assert len(installs) == 1


@pytest.mark.security
async def test_only_the_runs_workers_see_the_install_and_callers_cannot_name_one(
    executor: LocalExecutor, tmp_path: Path
) -> None:
    env = await executor.ensure_env(spec())
    site = await executor.install_project(env, tmp_path, sha="a" * 40, run_id="run-1")
    (mine,) = await executor.acquire(env, 1, Resources(), 60, {"coverset_run": "run-1"})
    (theirs,) = await executor.acquire(env, 1, Resources(), 60, {"coverset_run": "run-2"})
    try:
        assert mine.child_env({})[SITE_DIRS_ENV] == str(site)  # type: ignore[attr-defined]
        assert SITE_DIRS_ENV not in theirs.child_env({SITE_DIRS_ENV: "/etc"})  # type: ignore[attr-defined]
        assert mine.child_env({SITE_DIRS_ENV: "/etc"})[SITE_DIRS_ENV] == str(site)  # type: ignore[attr-defined]
    finally:
        await mine.release()
        await theirs.release()


# -- end to end with uv (network) ----------------------------------------------------------------------------


def _online() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=3):
            return True
    except OSError:
        return False


needs_index = pytest.mark.skipif(shutil.which("uv") is None or not _online(), reason="needs uv and the index")


def _tar(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(root.rglob("*")):
            if "__pycache__" not in path.parts and path.is_file():
                tar.add(path, arcname=str(path.relative_to(root)))
    return buffer.getvalue()


@pytest.mark.network
@pytest.mark.slow
@needs_index
async def test_an_entry_point_project_passes_only_when_installed(tmp_path: Path) -> None:
    """``src-entry-points`` reads its version and plugins from installed metadata: it needs ``wheel``."""
    repo = REPOS / "src-entry-points"
    wheel = build_plugin_wheel()
    adapter = PythonAdapter(cache_dir=tmp_path / "compile")
    env_spec = build_env_spec(adapter, repo, {}, wheel)
    assert env_spec.install == "wheel" and "setuptools==" in env_spec.requirements_txt
    (tmp_path / "tmp").mkdir()
    executor = LocalExecutor(
        max_workers=1, cache_dir=tmp_path / "cache", temp_dir=tmp_path / "tmp", sweep=False
    )
    env = await executor.ensure_env(env_spec)
    checkout = tmp_path / "checkout"
    shutil.copytree(repo, checkout, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"))
    site = await executor.install_project(env, checkout, sha="1" * 40, run_id="e2e")
    assert site is not None and (site / "_coverset_fallback.pth").is_file()
    assert any(p.name.startswith("entry_app") and p.name.endswith(".dist-info") for p in site.iterdir())
    lines: list[str] = []
    (worker,) = await executor.acquire(env, 1, Resources(), 60, {"coverset_run": "e2e"})
    try:
        await worker.put_tree(_tar(repo), "repo")
        env_vars = {"PYTHONPATH": f"{worker.path('repo/src')}:{worker.path('repo')}"}
        result = await worker.run(
            ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd="repo",
            env=env_vars,
            timeout_s=120,
            on_line=lambda text, _s: lines.append(text),
            stdout_cap=1 << 20,
        )
        assert result.exit_code == 0, "\n".join(lines)
        assert any("3 passed" in line for line in lines)
        # the source still wins: a change in the worker's checkout is what the tests import
        probe = await worker.run(
            ["python", "-c", "import entry_app, sys; print(entry_app.__file__)"],
            cwd="repo",
            env=env_vars,
            timeout_s=60,
            on_line=lambda text, _s: lines.append(text),
            stdout_cap=1 << 20,
        )
        assert probe.exit_code == 0 and lines[-1].startswith(worker.path("repo/src"))
    finally:
        await worker.release()
    assert not (repo / "src" / "entry_app.egg-info").exists()  # the fixture was never built in place


@pytest.mark.network
@pytest.mark.slow
@needs_index
def test_local_wheels_are_installed_with_their_markers(tmp_path: Path) -> None:
    """A committed wheel (``# coverset-local:``) is installed; one whose marker does not apply is not."""
    project = tmp_path / "tool"
    (project / "toolpkg").mkdir(parents=True)
    (project / "toolpkg" / "__init__.py").write_text("NAME = 'tool'\n")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "toolpkg"\nversion = "1.0"\n[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
    )
    checkout = tmp_path / "checkout"
    (checkout / "wheels").mkdir(parents=True)
    built = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(checkout / "wheels"), str(project)],
        capture_output=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip(f"uv build failed: {built.stderr[-300:]!r}")
    (wheel,) = (checkout / "wheels").glob("*.whl")
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "--quiet", "--python", sys.executable, str(venv)], check=True)
    rel = f"wheels/{wheel.name}"
    requirements = f"# coverset-local: file {rel} toolpkg\n# coverset-local: file {rel} toolpkg ; sys_platform == 'nonexistent'\n"
    env_spec = spec(install="none", requirements=requirements)
    site = tmp_path / "site"
    site.mkdir()
    uv_project_installer(env_spec, venv, checkout, site)
    assert (site / "toolpkg" / "__init__.py").is_file() and (site / "_coverset_fallback.pth").is_file()


# -- the Modal snapshot step (same contract, in the trusted snapshot) ----------------------------------------


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"modal_environment": "coverset-test", "max_sandboxes": 8}


async def _snapshot(settings: Settings, fake_modal: Any, env_spec: EnvSpec) -> Any:
    from coverset.db.models import Repo
    from coverset.db.session import Database, init_db
    from coverset.executors.modal_ import ModalExecutor, SandboxSlots

    await init_db(settings)
    database = Database.from_url(settings.database_url)
    async with database.session() as session:
        repo = Repo(forge="local", external_id="r", full_name="local/r", default_branch="main")
        session.add(repo)
        await session.commit()
        repo_id = repo.id
    await database.engine.dispose()

    async def zero() -> float:
        return 0.0

    executor = ModalExecutor(
        settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4), budget_probe=zero
    )
    try:
        env = await executor.ensure_env(env_spec)
        files = {"src/entry_app/__init__.py": b"", "pyproject.toml": b"[project]\nname='entry-app'\n"}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return await executor.snapshot_from_archive(env, repo_id, "c" * 40, buffer.getvalue())
    finally:
        await executor.close()


async def test_modal_snapshot_installs_the_project_and_local_packages_before_collecting(
    settings: Settings, fake_modal: Any, world: Any
) -> None:
    requirements = "pytest==9.1.1\n# coverset-local: file w/x-1.whl x ; sys_platform == 'linux'\n"
    await _snapshot(settings, fake_modal, spec(install="wheel", requirements=requirements))
    sandbox = world.sandboxes[-1]
    commands = [r["cmd"] for r in sandbox.execs if r.get("cmd", [""])[0] == "python"]
    pip = ["python", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--no-index", "--quiet"]
    assert commands[0] == [*pip, 'x @ file:///work/repo/w/x-1.whl ; sys_platform == "linux"']
    assert commands[1] == [*pip, "/work/repo"]
    assert commands[2][:2] == ["python", "-c"] and commands[2][3:] == ["/work/repo/src", "-"]
    assert commands[3][:4] == ["python", "-m", "pytest", "--collect-only"]  # installed before collection
    install_env = next(r["env"] for r in sandbox.execs if r.get("cmd") == commands[1])
    assert install_env["SETUPTOOLS_SCM_PRETEND_VERSION"] == "0.0.0+coverset"


async def test_modal_snapshot_without_an_install_runs_no_pip(
    settings: Settings, fake_modal: Any, world: Any
) -> None:
    await _snapshot(settings, fake_modal, spec(install="none", requirements="pytest==9.1.1\n"))
    commands = [r["cmd"] for r in world.sandboxes[-1].execs if r.get("cmd", [""])[0] == "python"]
    assert not any("pip" in cmd for cmd in commands)


async def test_a_failed_snapshot_install_fails_the_snapshot(
    settings: Settings, fake_modal: Any, world: Any
) -> None:
    from coverset.executors.base import ExecutorError

    world.handler = lambda cmd, env, workdir: (
        world.Plan(stdout=[b"error: no backend\n"], exit_code=1) if "pip" in cmd else None
    )
    with pytest.raises(ExecutorError, match="installing the project for the snapshot failed.*no backend"):
        await _snapshot(settings, fake_modal, spec(install="wheel", requirements="pytest==9.1.1\n"))
    assert world.sandboxes[-1].terminated


# -- the planner's hook ---------------------------------------------------------------------------------------


async def test_the_planner_installs_for_trusted_runs_on_executors_that_can(
    settings: Settings, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from coverset.db.session import Database, init_db
    from coverset.executors.base import EnvRef
    from coverset.lang.python import CoversetConfig
    from coverset.orchestrator.planner import _ensure_env

    await init_db(settings)
    database = Database.from_url(settings.database_url)
    installed: list[tuple[str, str, Path]] = []

    class Stub:
        async def ensure_env(self, env_spec: EnvSpec) -> EnvRef:
            return EnvRef(env_spec.env_hash, env_spec.python, "venv", env_spec)

        async def install_project(self, env: EnvRef, checkout: Path, *, sha: str, run_id: str) -> None:
            installed.append((sha, run_id, checkout))

    async def plan(trusted: bool, install: str, run_id: str) -> None:
        run = SimpleNamespace(trusted=trusted, head_sha="a" * 40, id=run_id, approved_by=None)
        builder = lambda checkout, config: spec(install=install, env_hash=run_id.ljust(64, "0"))  # noqa: E731
        await _ensure_env(
            database, settings, Stub(), PythonAdapter(), tmp_path, CoversetConfig(), builder, run
        )  # type: ignore[arg-type]

    try:
        await plan(True, "wheel", "trusted")
        await plan(False, "wheel", "untrusted")
        await plan(True, "none", "nothing")
    finally:
        await database.engine.dispose()
    assert installed == [("a" * 40, "trusted", tmp_path)]


def test_old_installs_are_pruned_when_a_new_one_is_built(
    executor: LocalExecutor, installs: list[tuple[str, Path]], tmp_path: Path
) -> None:
    import os

    env = executor.ensure_env_sync(spec())
    old = executor.install_project_sync(env, tmp_path, "1" * 40).parent
    recent = executor.install_project_sync(env, tmp_path, "2" * 40).parent
    stale = time.time() - 4 * 24 * 3600
    os.utime(old / ".coverset-ready.json", (stale, stale))
    newest = executor.install_project_sync(env, tmp_path, "3" * 40).parent
    assert not old.exists() and recent.is_dir() and newest.is_dir()
