"""Test doubles for the orchestrator: a scripted executor/worker and a tiny git repository.

The repository is the facts fixture's ``factspkg`` (``fixtures/bundles/minimal/repo``) plus a test
module and a ``[tool.coverset]`` config, so the fixture bundle's coverage matches the head source.
:class:`FakeWorker` interprets the commands the planner and runner send: ``--collect-only`` writes
``collect.json``; a shard or retry exec emits nonce-framed ``test`` events for the nodeids of its
args file with outcomes from a :class:`Script`, honours the STOP file and cancellation, and writes a
real ``bundle.tar.gz`` (the fixture bundle with ``results.jsonl`` rewritten to match).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from coverset.executors.base import (
    Capabilities,
    EnvRef,
    ExecutorError,
    LineCallback,
    Resources,
    RunResult,
    SnapshotRef,
    Worker,
)
from coverset.lang.base import EnvSpec

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = PROJECT_ROOT / "fixtures" / "bundles" / "minimal"
NODEIDS = (
    "tests/test_calc.py::test_add",
    "tests/test_calc.py::test_classify[neg]",
    "tests/test_calc.py::test_classify[pos]",
    "tests/test_calc.py::test_box_total",
    "tests/test_calc.py::test_zero_fails",
)
FAIL_SIG = "c0ffee0123456789"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "HOME": "/nonexistent",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
}
TEST_MODULE = """import pytest

from factspkg.calc import Box, add, classify


def test_add():
    assert add(1, 2) == 3


@pytest.mark.parametrize("n", [-3, 5], ids=["neg", "pos"])
def test_classify(n):
    assert classify(n)


def test_box_total():
    assert Box([1, 2]).total() == 3


def test_zero_fails():
    assert classify(0) == "pos"
"""
INVARIANTS = """invariants:
  - id: add_commutes
    scope: {kind: function, target: factspkg.calc.add}
    statement: add(a, b) equals add(b, a).
    severity: warning
    verify:
      - kind: test
        marker: add_commutes
"""


def pyproject(fail_fast: str = "finish_in_background", target_shard_seconds: float = 20.0) -> str:
    """The ``pyproject.toml`` of the test repository."""
    return (
        '[project]\nname = "factspkg"\nversion = "0"\n\n[tool.coverset]\nsource = ["factspkg"]\n'
        f'source_root = "src"\ntestpaths = ["tests"]\nfail_fast = "{fail_fast}"\n'
        f"target_shard_seconds = {target_shard_seconds}\nmax_shards = 4\n"
    )


def git(repo: Path, *args: str, date: str = "2026-10-01T12:00:00Z") -> str:
    """Run git in ``repo`` with a fixed identity and date; return stripped stdout."""
    env = {**GIT_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@dataclass
class TestRepo:
    """A working git repository with ``main`` and an optional PR branch."""

    __test__ = False

    path: Path
    main_sha: str
    pr_sha: str | None = None

    def commit(self, files: Mapping[str, str], message: str, *, branch: str = "main", hour: int = 1) -> str:
        """Commit ``files`` on ``branch`` (created from ``main`` if new) and return the SHA."""
        existing = git(self.path, "branch", "--list", branch)
        git(self.path, "checkout", "-q", branch if existing else "-b", *([] if existing else [branch]))
        for rel, text in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message, date=f"2026-10-01T{12 + hour:02d}:00:00Z")
        sha = git(self.path, "rev-parse", "HEAD")
        git(self.path, "checkout", "-q", "main")
        return sha


def make_repo(
    root: Path, *, fail_fast: str = "finish_in_background", target_shard_seconds: float = 20.0
) -> TestRepo:
    """The factspkg repository with one commit on ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "--initial-branch", "main")
    shutil.copytree(FIXTURE / "repo" / "src", root / "src")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(TEST_MODULE)
    (root / "pyproject.toml").write_text(pyproject(fail_fast, target_shard_seconds))
    (root / "invariants.yaml").write_text(INVARIANTS)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return TestRepo(root, git(root, "rev-parse", "HEAD"))


def pr_change(repo: TestRepo, *, branch: str = "pr-1", body: str = "return b + a", hour: int = 1) -> str:
    """Commit a one-line change to ``add`` (same line numbers) on ``branch``."""
    calc = (repo.path / "src/factspkg/calc.py").read_text().replace("    return a + b", f"    {body}")
    sha = repo.commit({"src/factspkg/calc.py": calc}, f"change add on {branch}", branch=branch, hour=hour)
    repo.pr_sha = sha
    return sha


def fake_env_spec(checkout: Path, config: Any) -> EnvSpec:
    """An :class:`EnvSpec` without ``uv export`` (the fake executor never builds anything)."""
    return EnvSpec(
        python="3.13",
        lock_hash="0" * 64,
        requirements_txt="pytest==9.1.1\n",
        plugin_wheel=None,
        source_root=config.source_root,
        env_hash="f" * 64,
    )


# -- scripts --------------------------------------------------------------------------------------

type Outcome = str


@dataclass
class Script:
    """What the fake tests do.

    ``outcomes`` maps nodeid → outcome for the first attempt (default ``passed``, except
    ``test_zero_fails`` which fails unless ``zero_passes``); ``retry_outcomes`` the same for retries
    (default: the first-attempt outcome). ``decide`` overrides both: ``(nodeid, env, retry) -> outcome``.
    ``delay_s`` is slept before each test; ``collected`` overrides the collection (nodeids the
    fixture does not know are synthesised); ``collect_delay_s`` is slept during collection;
    ``inject`` lines are emitted, nonce-framed, before a shard's first test; ``bundle_outcomes``
    overrides outcomes in the bundle's ``results.jsonl`` only (what the stream said stays).
    """

    outcomes: dict[str, Outcome] = field(default_factory=dict)
    retry_outcomes: dict[str, Outcome] = field(default_factory=dict)
    decide: Callable[[str, Mapping[str, str], bool], Outcome] | None = None
    delay_s: float = 0.0
    collected: tuple[str, ...] | None = None
    zero_passes: bool = False
    fail_sig: str = FAIL_SIG
    collect_delay_s: float = 0.0
    inject: list[str] = field(default_factory=list)
    bundle_outcomes: dict[str, Outcome] = field(default_factory=dict)
    envs: list[dict[str, str]] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)

    def outcome(self, nodeid: str, env: Mapping[str, str], retry: bool) -> Outcome:
        if self.decide is not None:
            return self.decide(nodeid, env, retry)
        first = self.outcomes.get(nodeid)
        if first is None:
            first = "failed" if nodeid.endswith("test_zero_fails") and not self.zero_passes else "passed"
        return self.retry_outcomes.get(nodeid, first) if retry else first


class FakeWorker:
    """A :class:`~coverset.executors.base.Worker` that scripts pytest-coverset's behaviour."""

    def __init__(self, script: Script, root: Path) -> None:
        self.id = f"fake-{uuid.uuid4().hex[:8]}"
        self.root = str(root)
        self._root = root
        self.script = script
        self.released = False
        self._cancelled = asyncio.Event()

    def path(self, rel: str) -> str:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise ExecutorError(f"bad path {rel!r}")
        return str(self._root.joinpath(*pure.parts))

    def _rel(self, absolute: str) -> Path:
        return Path(absolute)

    async def run(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: float,
        on_line: LineCallback | None = None,
        stdout_cap: int,
    ) -> RunResult:
        started = time.time()
        self.script.envs.append(dict(env))
        self.script.commands.append(list(cmd))
        emit = on_line or (lambda text, stream: None)
        prefix = f"\x1ecoverset {env.get('COVERSET_EVENT_NONCE', '-')} "
        out = Path(cmd[cmd.index("--coverset-out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        if "--collect-only" in cmd:
            if self.script.collect_delay_s:
                await asyncio.sleep(self.script.collect_delay_s)
            nodeids = self.script.collected if self.script.collected is not None else NODEIDS
            items = json.loads((FIXTURE / "bundle" / "collect.json").read_text())["items"]
            known = {i["nodeid"] for i in items}
            chosen = [i for i in items if i["nodeid"] in nodeids] + [
                {
                    "nodeid": n,
                    "file": n.split("::", 1)[0],
                    "line": k + 100,
                    "markers": [],
                    "invariant_ids": [],
                }
                for k, n in enumerate(nodeids)
                if n not in known
            ]
            (out / "collect.json").write_text(json.dumps({"items": chosen, "errors": []}))
            emit(prefix + json.dumps({"kind": "collected", "count": len(chosen)}), "stdout")
            return RunResult(0 if chosen else 5, started, time.time(), 10, False)
        args_file = Path(next(c for c in cmd if c.startswith("@"))[1:])
        nodeids = [line for line in args_file.read_text().splitlines() if line]
        stop_file = cmd[cmd.index("--coverset-stop-file") + 1] if "--coverset-stop-file" in cmd else None
        retry = "retry-" in out.name
        results: list[dict[str, Any]] = []
        exit_code = 0
        stopped = False
        emit("============================= test session starts ==============================", "stdout")
        if not retry:
            for line in self.script.inject:
                emit(prefix + line, "stdout")
        for nodeid in nodeids:
            if self._cancelled.is_set():
                return RunResult(-15, started, time.time(), 10, False, cancelled=True)
            if stop_file and os.path.exists(stop_file):
                stopped = True
                break
            if self.script.delay_s:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._cancelled.wait(), timeout=self.script.delay_s)
                if self._cancelled.is_set():
                    return RunResult(-15, started, time.time(), 10, False, cancelled=True)
            outcome = self.script.outcome(nodeid, env, retry)
            failed = outcome in ("failed", "error")
            row = {
                "nodeid": nodeid,
                "file": nodeid.split("::", 1)[0],
                "outcome": outcome,
                "duration_s": 0.01,
                "failed_phase": "call" if failed else None,
                "fail_sig": self.script.fail_sig if failed else None,
                "longrepr": "E   assert 'zero' == 'pos'" if failed else None,
                "start": started,
                "stop": started + 0.01,
            }
            results.append(row)
            emit(prefix + json.dumps({"kind": "test", **row}), "stdout")
            exit_code = 1 if failed else exit_code
        if stopped:
            exit_code = 2
        bundle = _bundle(
            [
                {**r, "outcome": self.script.bundle_outcomes[r["nodeid"]]}
                if r["nodeid"] in self.script.bundle_outcomes
                else r
                for r in results
            ]
        )
        (out / "bundle.tar.gz").write_bytes(bundle)
        emit(
            prefix + json.dumps({"kind": "bundle_written", "sha256": "0" * 64, "bytes": len(bundle)}),
            "stdout",
        )
        emit(prefix + json.dumps({"kind": "done", "exit_code": exit_code}), "stdout")
        return RunResult(exit_code, started, time.time(), 100, False)

    async def put_tree(self, tar_bytes: bytes, dest: str) -> None:
        target = Path(self.path(dest))
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
            tar.extractall(target, filter="data")

    async def get_file(self, path: str) -> bytes:
        target = Path(self.path(path))
        if not target.is_file():
            raise ExecutorError(f"missing {path}")
        return target.read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = Path(self.path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def cancel(self) -> None:
        self._cancelled.set()

    async def release(self) -> None:
        self.released = True
        self._cancelled.set()
        shutil.rmtree(self._root, ignore_errors=True)


def _bundle(results: list[dict[str, Any]]) -> bytes:
    """The fixture bundle with ``results.jsonl`` replaced by ``results``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for member in sorted((FIXTURE / "bundle").iterdir()):
            if member.name == "results.jsonl":
                data = "".join(json.dumps({"kind": "test", **r}) + "\n" for r in results).encode()
            else:
                data = member.read_bytes()
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeExecutor:
    """An :class:`~coverset.executors.base.Executor` handing out :class:`FakeWorker` objects."""

    name = "local"

    def __init__(self, script: Script | None = None, *, capacity: int = 8, rate_per_s: float = 0.0) -> None:
        self.script = script or Script()
        self.capacity = capacity
        self.rate_per_s = rate_per_s
        self.workers: list[FakeWorker] = []
        self.acquire_calls: list[int] = []
        self.fail_env: Exception | None = None

    async def ensure_env(self, spec: EnvSpec) -> EnvRef:
        if self.fail_env is not None:
            raise self.fail_env
        return EnvRef(env_hash=spec.env_hash, python=spec.python, location="/fake/venv", spec=spec)

    async def ensure_snapshot(self, env: EnvRef, repo: str, sha: str) -> SnapshotRef | None:
        return None

    async def acquire(
        self, base: EnvRef | SnapshotRef, n: int, resources: Resources, ttl_s: int, labels: Mapping[str, str]
    ) -> list[Worker]:
        self.acquire_calls.append(n)
        active = sum(1 for w in self.workers if not w.released)
        count = max(0, min(n, self.capacity - active))
        made = [
            FakeWorker(self.script, Path(tempfile.mkdtemp(prefix="coverset-fake-"))) for _ in range(count)
        ]
        self.workers += made
        return list(made)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="local", egress=True, warm_pool=False, fs_snapshot=False, max_workers=self.capacity
        )

    def cost_usd(self, resources: Resources, seconds: float) -> float:
        return self.rate_per_s * seconds

    async def close(self) -> None:
        for worker in self.workers:
            await worker.release()
