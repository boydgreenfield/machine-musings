"""Test doubles and builders shared by the analysis tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from coverset.analysis.functions import FunctionRegion
from coverset.lang.base import CollectedItem, ShapeObs

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-10-01T12:00:00Z",
    "GIT_COMMITTER_DATE": "2026-10-01T12:00:00Z",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


@dataclass
class FakePriorRun:
    """A :class:`~coverset.lang.base.PriorRun` over in-memory per-test line sets."""

    run_id: str
    regions: list[FunctionRegion]
    lines: dict[str, dict[str, set[int]]]
    """nodeid → path → executed lines."""
    module_lines: dict[str, set[int]] = field(default_factory=dict)
    p50: dict[str, float] = field(default_factory=dict)

    def functions(self) -> list[FunctionRegion]:
        return list(self.regions)

    def tests_touching(self, path: str, start: int, end: int) -> set[str]:
        return {
            nodeid
            for nodeid, by_path in self.lines.items()
            if any(start <= line <= end for line in by_path.get(path, ()))
        }

    def test_lines(self, nodeid: str) -> dict[str, set[int]]:
        return self.lines.get(nodeid, {})

    def module_level_lines(self, path: str) -> set[int]:
        return self.module_lines.get(path, set())

    def durations(self) -> dict[str, float]:
        return dict(self.p50)


def item(nodeid: str, *, invariants: Iterable[str] = (), hypothesis: bool = False) -> CollectedItem:
    """A collected item whose file is the nodeid's path part."""
    return CollectedItem(nodeid, nodeid.split("::", 1)[0], 1, (), tuple(invariants), hypothesis)


def obs(
    param: str,
    shape_key: str,
    *,
    tests: Iterable[str] = ("tests/test_x.py::test_a",),
    example: str | None = None,
    func: tuple[str, str, int] = ("src/pkg/mod.py", "f", 1),
    truncated: bool = False,
    samples: int = 1,
) -> ShapeObs:
    """One shape observation for ``param``."""
    tests = tuple(tests)
    return ShapeObs(func, param, shape_key, tests[0], samples, tests, truncated, example, tests[0])


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a hermetic identity and config."""
    proc = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def commit_tree(repo: Path, files: Mapping[str, str | None], message: str) -> str:
    """Write (or delete, for None) ``files`` in ``repo``, commit them, and return the new SHA."""
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            git(repo, "rm", "-q", rel)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def tree_at(repo: Path, sha: str, paths: Iterable[str]) -> dict[str, str | None]:
    """Source text of ``paths`` at ``sha`` (None when absent)."""
    result: dict[str, str | None] = {}
    for path in paths:
        proc = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            cwd=repo,
            env=GIT_ENV,
            capture_output=True,
            text=True,
            check=False,
        )
        result[path] = proc.stdout if proc.returncode == 0 else None
    return result
