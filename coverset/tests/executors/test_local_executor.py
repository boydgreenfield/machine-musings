"""LocalExecutor: scrubbed environments, venv cache locking, kill paths, cleanup and file transfer."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

from coverset.executors.base import MAX_LINE_BYTES, Resources, sanitize_env
from coverset.executors.base import ExecutorError as BaseExecutorError
from coverset.executors.local import (
    READY_MARKER,
    RUN_DIR_PREFIX,
    LocalExecutor,
    sweep_stale_run_dirs,
)
from coverset.lang.base import EnvSpec

LEAKY = {
    "COVERSET_GITHUB_TOKEN": "ghp_" + "x" * 36,
    "COVERSET_GITHUB_WEBHOOK_SECRET": "webhook-secret-123",
    "ANTHROPIC_API_KEY": "sk-ant-leak",
    "MODAL_TOKEN_ID": "ak-leak",
    "MODAL_TOKEN_SECRET": "as-leak",
    "VIRTUAL_ENV": "/somewhere/.venv",
    "PYTEST_ADDOPTS": "-x",
    "COVERAGE_PROCESS_START": "/tmp/.coveragerc",
}


def spec(env_hash: str = "e" * 64) -> EnvSpec:
    return EnvSpec(
        python="3.13",
        lock_hash="l",
        requirements_txt="",
        plugin_wheel=None,
        source_root="src",
        env_hash=env_hash,
    )


def fake_builder(calls: list[str] | None = None, delay: float = 0.0):  # type: ignore[no-untyped-def]
    def build(env: EnvSpec, target: Path) -> None:
        if calls is not None:
            calls.append(env.env_hash)
        time.sleep(delay)
        (target / "bin").mkdir()
        (target / "bin" / "python").symlink_to(sys.executable)

    return build


@pytest.fixture
def executor(tmp_path: Path) -> LocalExecutor:
    return LocalExecutor(
        max_workers=2,
        cache_dir=tmp_path / "cache",
        temp_dir=tmp_path / "tmp",
        venv_builder=fake_builder(),
        kill_grace_s=0.5,
    )


@pytest.fixture(autouse=True)
def _tmp_dir(tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()


async def _worker(executor: LocalExecutor):  # type: ignore[no-untyped-def]
    env = await executor.ensure_env(spec())
    (worker,) = await executor.acquire(env, 1, Resources(), 60, {})
    return worker


async def test_child_environment_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, executor: LocalExecutor
) -> None:
    for key, value in LEAKY.items():
        monkeypatch.setenv(key, value)
    worker = await _worker(executor)
    lines: list[str] = []
    script = "import json, os; print(json.dumps(dict(os.environ)))"
    result = await worker.run(
        ["python", "-c", script],
        cwd="",
        env={
            "COVERSET_EVENT_NONCE": "abc123",
            "PYTHONPATH": worker.path("repo/src"),
            "COVERSET_DEMO_RUN": "4",
            "COVERSET_GITHUB_TOKEN": "passed-explicitly",
            "MODAL_TOKEN_SECRET": "passed-explicitly",
            "PATH": "/evil",
        },
        timeout_s=30,
        on_line=lambda text, stream: lines.append(text),
        stdout_cap=1 << 20,
    )
    assert result.exit_code == 0
    child = json.loads(lines[-1])
    for key in LEAKY:
        assert key not in child, key
    assert child["COVERSET_EVENT_NONCE"] == "abc123" and child["COVERSET_DEMO_RUN"] == "4"
    assert child["PYTHONPATH"] == worker.path("repo/src")
    assert child["PATH"].split(":") == [
        str(Path(executor.venvs_dir / ("e" * 64) / "bin")),
        "/usr/bin",
        "/bin",
    ]
    assert child["HOME"].startswith(worker.root) and child["TMPDIR"].startswith(worker.root)
    assert child["PYTHONUNBUFFERED"] == "1" and child["TZ"] == "UTC" and "LANG" in child
    assert set(child) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == {
        "PATH", "HOME", "TMPDIR", "PYTHONUNBUFFERED", "LANG", "TZ",
        "COVERSET_EVENT_NONCE", "PYTHONPATH", "COVERSET_DEMO_RUN",
    }  # fmt: skip
    await worker.release()


def test_sanitize_env_rules() -> None:
    assert sanitize_env(
        {
            "OK_NAME": "1",
            "lower": "x",
            "BAD-NAME": "x",
            "NUL": "a\0b",
            "NUM": 3,
            "LD_PRELOAD": "x",
            "HOME": "/h",
        }
    ) == {"OK_NAME": "1"}


def test_venv_cache_builds_once_under_the_lock(tmp_path: Path) -> None:
    calls: list[str] = []
    first = LocalExecutor(
        cache_dir=tmp_path / "c", temp_dir=tmp_path / "tmp", venv_builder=fake_builder(calls, 0.3)
    )
    second = LocalExecutor(
        cache_dir=tmp_path / "c", temp_dir=tmp_path / "tmp", venv_builder=fake_builder(calls, 0.3)
    )
    refs = []
    threads = [
        threading.Thread(target=lambda e=e: refs.append(e.ensure_env_sync(spec()))) for e in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == ["e" * 64]
    assert refs[0].location == refs[1].location
    final = Path(refs[0].location)
    assert (final / READY_MARKER).is_file() and (final / "bin" / "python").exists()
    leftovers = [p.name for p in (tmp_path / "c" / "venvs").iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_failed_venv_build_leaves_nothing(tmp_path: Path) -> None:
    def broken(env: EnvSpec, target: Path) -> None:
        (target / "partial").write_text("x")
        raise BaseExecutorError("pip failed")

    executor = LocalExecutor(cache_dir=tmp_path / "c", temp_dir=tmp_path / "tmp", venv_builder=broken)
    with pytest.raises(BaseExecutorError):
        executor.ensure_env_sync(spec())
    venvs = tmp_path / "c" / "venvs"
    assert [p for p in venvs.iterdir() if not p.name.endswith(".lock")] == []


async def test_timeout_kills_the_process_group(executor: LocalExecutor) -> None:
    worker = await _worker(executor)
    marker = Path(worker.path("tmp/grandchild.pid"))
    script = (
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(marker)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    )
    started = time.monotonic()
    result = await worker.run(["python", "-c", script], cwd="", env={}, timeout_s=1.0, stdout_cap=1 << 20)
    assert result.timed_out and time.monotonic() - started < 10
    grandchild = int(marker.read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)
    await worker.release()


async def test_sigterm_ignoring_process_gets_sigkill(executor: LocalExecutor) -> None:
    worker = await _worker(executor)
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    task = asyncio.create_task(
        worker.run(["python", "-c", script], cwd="", env={}, timeout_s=60, stdout_cap=1 << 20)
    )
    await asyncio.sleep(0.5)
    started = time.monotonic()
    await worker.cancel()
    result = await asyncio.wait_for(task, timeout=10)
    assert result.cancelled and result.exit_code is not None and result.exit_code < 0
    assert time.monotonic() - started < 5
    await worker.release()


async def test_stdout_cap_and_line_cap(executor: LocalExecutor) -> None:
    worker = await _worker(executor)
    lines: list[str] = []
    long_line = f"import sys; sys.stdout.write('x' * {MAX_LINE_BYTES + 10} + '\\nshort\\n')"
    result = await worker.run(
        ["python", "-c", long_line],
        cwd="",
        env={},
        timeout_s=30,
        on_line=lambda t, s: lines.append(t),
        stdout_cap=1 << 24,
    )
    assert result.exit_code == 0 and not result.truncated
    assert len(lines[0]) == MAX_LINE_BYTES and lines[1] == "short"
    flood = "import sys\nwhile True: sys.stdout.write('y' * 65536)"
    result = await worker.run(["python", "-c", flood], cwd="", env={}, timeout_s=30, stdout_cap=1 << 20)
    assert result.truncated and result.exit_code != 0
    await worker.release()


async def test_files_in_and_out(executor: LocalExecutor) -> None:
    worker = await _worker(executor)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("pkg/mod.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    await worker.put_tree(buffer.getvalue(), "repo")
    assert await worker.get_file("repo/pkg/mod.py") == b"print('hi')\n"
    await worker.write_file("run/STOP", b"stop")
    assert Path(worker.path("run/STOP")).read_bytes() == b"stop"
    for bad in ("../escape", "/etc/passwd"):
        with pytest.raises(BaseExecutorError):
            worker.path(bad)
    os.symlink("/etc/hosts", worker.path("run/link"))
    with pytest.raises(BaseExecutorError):
        await worker.get_file("run/link")
    with pytest.raises(BaseExecutorError):
        await worker.get_file("run/missing")
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tar:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(BaseExecutorError):
        await worker.put_tree(evil.getvalue(), "repo")
    root = Path(worker.root)
    await worker.release()
    assert not root.exists()


async def test_acquire_respects_max_workers_and_release(executor: LocalExecutor) -> None:
    env = await executor.ensure_env(spec())
    workers = await executor.acquire(env, 5, Resources(), 60, {})
    assert len(workers) == 2 and executor.active_workers == 2
    assert all(Path(w.root).name.startswith(RUN_DIR_PREFIX) for w in workers)
    assert await executor.acquire(env, 1, Resources(), 60, {}) == []
    await workers[0].release()
    assert len(await executor.acquire(env, 1, Resources(), 60, {})) == 1
    await executor.close()
    assert executor.active_workers == 0
    caps = executor.capabilities()
    assert (caps.egress, caps.warm_pool, caps.fs_snapshot, caps.max_workers) == (True, False, False, 2)
    assert executor.cost_usd(Resources(), 3600) == 0.0


def test_startup_sweep_removes_old_run_dirs(tmp_path: Path) -> None:
    temp = tmp_path / "sweep"
    temp.mkdir()
    old, new, other = temp / f"{RUN_DIR_PREFIX}old", temp / f"{RUN_DIR_PREFIX}new", temp / "unrelated-old"
    for directory in (old, new, other):
        directory.mkdir()
        (directory / "f").write_text("x")
    stale = time.time() - 7200
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    removed = sweep_stale_run_dirs(temp)
    assert removed == [old] and not old.exists() and new.exists() and other.exists()
    LocalExecutor(cache_dir=tmp_path / "c", temp_dir=temp, venv_builder=fake_builder())
    assert new.exists()
