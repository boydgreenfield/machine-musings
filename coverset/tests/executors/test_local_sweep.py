"""The LocalExecutor's start-up sweep never deletes a live worker of another process."""

from __future__ import annotations

import os
import time
from pathlib import Path

from coverset.executors.base import EnvRef, Resources
from coverset.executors.local import (
    READY_MARKER,
    RUN_DIR_PREFIX,
    WORKER_LOCK,
    LocalExecutor,
    sweep_stale_run_dirs,
)


def _env(tmp_path: Path) -> EnvRef:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / READY_MARKER).write_text("{}")
    return EnvRef(env_hash="e" * 64, python="3.13", location=str(venv))


def _age(root: Path, seconds: float) -> None:
    old = time.time() - seconds
    for path in (root, root / "run", root / "repo"):
        if path.exists():
            os.utime(path, (old, old))


async def test_a_held_worker_survives_another_executors_sweep(tmp_path: Path) -> None:
    temp = tmp_path / "t"
    temp.mkdir()
    first = LocalExecutor(temp_dir=temp, sweep=False, max_workers=2)
    [worker] = await first.acquire(_env(tmp_path), 1, Resources(), 600, {"coverset_run": "run_a"})
    root = Path(worker.root)
    (root / "run").mkdir()
    (root / "repo").mkdir()
    assert (root / WORKER_LOCK).is_file()
    _age(root, 2 * 3600)  # a run longer than the sweep's hour: nothing touches these directories
    LocalExecutor(temp_dir=temp)  # another process starting up sweeps
    assert root.is_dir()
    await worker.release()
    assert not root.exists()
    await first.close()


def test_a_dead_workers_directory_is_swept(tmp_path: Path) -> None:
    stale = tmp_path / f"{RUN_DIR_PREFIX}dead"
    (stale / "run").mkdir(parents=True)
    (stale / WORKER_LOCK).write_text("")  # nobody holds it: its process is gone
    _age(stale, 2 * 3600)
    legacy = tmp_path / f"{RUN_DIR_PREFIX}legacy"  # from before locks
    legacy.mkdir()
    _age(legacy, 2 * 3600)
    assert sorted(sweep_stale_run_dirs(tmp_path)) == sorted([stale, legacy])
    assert not stale.exists() and not legacy.exists()


def test_recent_activity_keeps_an_unlocked_directory(tmp_path: Path) -> None:
    busy = tmp_path / f"{RUN_DIR_PREFIX}busy"
    (busy / "run").mkdir(parents=True)
    _age(busy, 2 * 3600)
    os.utime(busy / "run", None)  # its run/ changed just now
    assert sweep_stale_run_dirs(tmp_path) == []
    assert busy.is_dir()
