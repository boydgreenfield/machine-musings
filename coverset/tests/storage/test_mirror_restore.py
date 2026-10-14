"""Mirrors in the object-store cache come back on demand; local mirrors are never re-cloned."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from coverset.config import Settings
from coverset.db.models import Repo
from coverset.orchestrator import repos


class FakeDb:
    def __init__(self, rows: dict[int, Repo]) -> None:
        self.rows = rows

    @asynccontextmanager
    async def session(self) -> Any:
        class Session:
            async def get(_self, model: type, key: int) -> Repo | None:  # noqa: N805
                return self.rows.get(key)

        yield Session()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(work), "add", "a.py"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one"],
        check=True,
    )
    return work


def _repo(source: Path) -> Repo:
    return Repo(
        id=7, forge="local", external_id="x", full_name="t/demo", settings_json={"source": str(source)}
    )


async def test_a_cache_mirror_is_cloned_again(tmp_path: Path, source: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data", blob_url="s3://bucket")
    mirror = await repos.restore_mirror(settings, 7, db=FakeDb({7: _repo(source)}), forge=None)
    assert mirror == settings.mirrors_dir / "7.git" and (mirror / "HEAD").is_file()
    assert settings.mirrors_dir.parent == settings.cache_dir


async def test_the_local_default_never_clones_behind_the_operators_back(tmp_path: Path, source: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data")
    assert await repos.restore_mirror(settings, 7, db=FakeDb({7: _repo(source)}), forge=None) is None
    assert not (settings.mirrors_dir / "7.git").exists()


async def test_a_failed_clone_is_not_retried_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data", blob_url="s3://bucket")
    attempts: list[int] = []

    async def failing(*args: Any, **kwargs: Any) -> Path:
        attempts.append(1)
        raise repos.RepoError("no credentials")

    monkeypatch.setattr(repos, "ensure_mirror_async", failing)
    db = FakeDb({7: _repo(tmp_path / "missing")})
    assert await repos.restore_mirror(settings, 7, db=db, forge=None) is None
    assert await repos.restore_mirror(settings, 7, db=db, forge=None) is None
    assert attempts == [1]
    assert await repos.restore_mirror(settings, 8, db=db, forge=None) is None, "an unknown repository"


def test_a_lock_left_by_an_ended_event_loop_does_not_break_the_next(tmp_path: Path, source: Path) -> None:
    """Each ``asyncio.run`` is a new loop (CLI steps, tests); a restore lock that an ended loop left
    held must not block the next loop's restore forever, nor fail it with "bound to a different
    event loop" (what the hosted e2e hit when a page restored a mirror in a second app)."""
    settings = Settings.for_tests(data_dir=tmp_path / "data", blob_url="s3://bucket")
    db = FakeDb({7: _repo(source)})

    async def hold_the_lock_and_end() -> None:
        assert await repos.restore_mirror(settings, 7, db=db, forge=None) is not None  # registers the lock
        _loop, lock = repos._restore_locks[(str(settings.mirrors_dir), 7)]
        await lock.acquire()  # never released: the loop ends with the lock held

    asyncio.run(hold_the_lock_and_end())
    shutil.rmtree(settings.mirrors_dir / "7.git")
    # Bounded: with one lock for every loop this waited forever on the ended loop's holder.
    mirror = asyncio.run(asyncio.wait_for(repos.restore_mirror(settings, 7, db=db, forge=None), 20))
    assert mirror is not None and (mirror / "HEAD").is_file()
