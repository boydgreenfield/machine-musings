"""The executor interface: where a run's shards execute (a local subprocess pool, Modal Sandboxes, ...).

The control plane talks to every backend through two Protocols:

* :class:`Executor` builds (or finds) the dependency environment for an :class:`EnvSpec`
  (:meth:`Executor.ensure_env`), optionally a trusted filesystem snapshot of a default-branch
  commit (:meth:`Executor.ensure_snapshot`), and hands out up to ``n`` workers
  (:meth:`Executor.acquire`, which may return fewer: the planner re-shards to what it got).
* :class:`Worker` is one isolated place to run commands. Paths given to a worker are relative to its
  private root directory (:attr:`Worker.root`); :meth:`Worker.path` turns them into the absolute
  path to put on a command line inside the worker. Source arrives only by :meth:`Worker.put_tree`
  (a ``git archive`` tarball): workers never talk to a git remote and never see forge credentials.

Streaming: :meth:`Worker.run` calls ``on_line(text, stream)`` for every line of the child's stdout
and stderr as it arrives (``stream`` is ``"stdout"`` or ``"stderr"``; the newline is stripped). A
line longer than :data:`MAX_LINE_BYTES` is cut and the rest of it dropped; once the exec has written
``stdout_cap`` bytes in total it is killed and :attr:`RunResult.truncated` is set.

Environment variables a caller passes to :meth:`Worker.run` go through :func:`sanitize_env`, which
drops anything that could leak control-plane secrets into the child or redirect its interpreter.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from coverset.config import SandboxNetwork
from coverset.lang.base import EnvSpec

__all__ = [
    "DENIED_ENV_PREFIXES",
    "MAX_LINE_BYTES",
    "Capabilities",
    "CapacityError",
    "EnvRef",
    "Executor",
    "ExecutorError",
    "LineCallback",
    "PoolingExecutor",
    "ReapReport",
    "ReapingExecutor",
    "Resources",
    "RunResult",
    "SnapshotRef",
    "Stream",
    "Worker",
    "cache_root",
    "release_all",
    "sanitize_env",
]

MAX_LINE_BYTES = 1024 * 1024
"""Longest stdout/stderr line delivered to ``on_line``; the rest of a longer line is dropped."""

type Stream = Literal["stdout", "stderr"]
type LineCallback = Callable[[str, Stream], None]

DENIED_ENV_PREFIXES: tuple[str, ...] = (
    "COVERSET_GITHUB_",
    "COVERSET_ADMIN",
    "COVERSET_DATABASE",
    "ANTHROPIC_",
    "MODAL_",
    "AWS_",
    "GITHUB_",
    "PYTEST_",
    "COVERAGE_",
    "LD_",
    "DYLD_",
)
"""Environment-variable prefixes a worker never passes to a child, whoever asks for them."""

_DENIED_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "SSH_AUTH_SOCK",
    }
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_MAX_ENV_VALUE = 32 * 1024


class ExecutorError(RuntimeError):
    """An executor or worker operation failed (environment build, exec, file transfer)."""


class CapacityError(ExecutorError):
    """The backend has no capacity for another worker right now."""


@dataclass(frozen=True, slots=True)
class EnvRef:
    """A built dependency environment.

    ``location`` is backend-specific: a venv directory for the local executor, an image object id
    for Modal. ``spec`` is the :class:`EnvSpec` it was built from.
    """

    env_hash: str
    python: str
    location: str
    spec: EnvSpec | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """A trusted filesystem snapshot of one default-branch commit on top of an environment."""

    env: EnvRef
    sha: str
    image_id: str
    snapshot_id: int | None = None
    """``snapshots.id`` in the control-plane database, when recorded there."""


@dataclass(frozen=True, slots=True)
class Resources:
    """What one worker gets. Backends without the notion (the local executor) ignore it."""

    cpu: float = 2.0
    memory_mib: int = 2048
    network: SandboxNetwork = "blocked"
    timeout_s: int = 1800


@dataclass(frozen=True, slots=True)
class RunResult:
    """How one exec ended. Times are UNIX seconds (wall clock of the control plane)."""

    exit_code: int | None
    started_at: float
    finished_at: float
    stdout_bytes: int
    truncated: bool
    """The exec wrote more than ``stdout_cap`` bytes and was killed."""
    timed_out: bool = False
    cancelled: bool = False

    @property
    def seconds(self) -> float:
        """Wall-clock duration of the exec."""
        return max(0.0, self.finished_at - self.started_at)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a backend offers; the orchestrator adapts (pool maintenance, snapshots, budgets)."""

    name: str
    egress: bool
    """Workers can reach the network (always true locally: the local executor is not a sandbox)."""
    warm_pool: bool
    fs_snapshot: bool
    max_workers: int


@dataclass(frozen=True, slots=True)
class ReapReport:
    """What a reaper pass found and terminated."""

    found: int = 0
    terminated: int = 0
    kept: int = 0
    errors: tuple[str, ...] = ()


@runtime_checkable
class Worker(Protocol):
    """One isolated place to run a shard's commands (see the module docstring)."""

    id: str
    root: str
    """Absolute path of the worker's private root directory, as seen inside the worker."""

    def path(self, rel: str) -> str:
        """Absolute in-worker path of the worker-relative POSIX path ``rel``."""
        ...

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
        """Run ``cmd`` in ``cwd`` (worker-relative) and stream its output; never raises on a non-zero exit."""
        ...

    async def put_tree(self, tar_bytes: bytes, dest: str) -> None:
        """Extract a gzipped tarball into the worker-relative directory ``dest``."""
        ...

    async def get_file(self, path: str) -> bytes:
        """Read a worker-relative file; raises :class:`ExecutorError` when it is missing."""
        ...

    async def write_file(self, path: str, data: bytes) -> None:
        """Create or replace a worker-relative file (parent directories are created)."""
        ...

    async def cancel(self) -> None:
        """Stop every running exec (graceful signal, then a hard kill after a grace period)."""
        ...

    async def release(self) -> None:
        """Stop everything and free the worker; idempotent."""
        ...


@runtime_checkable
class Executor(Protocol):
    """A backend that builds environments and hands out workers (see the module docstring)."""

    name: str

    async def ensure_env(self, spec: EnvSpec) -> EnvRef:
        """Build (or find cached) the environment described by ``spec``."""
        ...

    async def ensure_snapshot(self, env: EnvRef, repo: str, sha: str) -> SnapshotRef | None:
        """A trusted snapshot of ``repo`` at ``sha`` on ``env``, or None when unsupported."""
        ...

    async def acquire(
        self,
        base: EnvRef | SnapshotRef,
        n: int,
        resources: Resources,
        ttl_s: int,
        labels: Mapping[str, str],
    ) -> list[Worker]:
        """Up to ``n`` workers on ``base`` (fewer when capacity is short; none raises nothing)."""
        ...

    def capabilities(self) -> Capabilities:
        """What this backend offers."""
        ...

    def cost_usd(self, resources: Resources, seconds: float) -> float:
        """Estimated cost of one worker with ``resources`` for ``seconds`` (the rate card)."""
        ...

    async def close(self) -> None:
        """Release everything the executor holds (workers, pools, clients)."""
        ...


@runtime_checkable
class PoolingExecutor(Executor, Protocol):
    """An executor with a warm pool of pre-created workers (Modal)."""

    async def fill_pool(self, base: EnvRef | SnapshotRef, repo: str, size: int, ttl_s: int) -> int:
        """Top the pool for ``repo`` up to ``size`` workers; return how many were created."""
        ...

    async def drain_pool(self, repo: str | None = None) -> int:
        """Terminate pooled workers (of ``repo``, or all); return how many."""
        ...


@runtime_checkable
class ReapingExecutor(Executor, Protocol):
    """An executor whose workers can outlive the control plane and must be reaped (Modal)."""

    async def reap(
        self, *, active_run_ids: set[str], all: bool = False, any_instance: bool = False
    ) -> ReapReport:
        """Terminate workers not tied to an active run (every worker with ``all``); with
        ``any_instance``, look beyond this installation's workers (every data dir's)."""
        ...


def sanitize_env(env: Mapping[str, object]) -> dict[str, str]:
    """Return the subset of ``env`` that may reach a child process.

    Names must be upper-case shell identifiers; values must be strings without NUL of at most 32 KiB;
    names in the deny list (``PATH``, ``HOME``, ``VIRTUAL_ENV``, ...) or with a denied prefix
    (:data:`DENIED_ENV_PREFIXES`) are dropped. ``PYTHONPATH`` and ``COVERSET_*`` plugin options pass.
    """
    clean: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            continue
        if name in _DENIED_ENV_NAMES or name.startswith(DENIED_ENV_PREFIXES):
            continue
        if not isinstance(value, str) or "\0" in value or len(value) > _MAX_ENV_VALUE:
            continue
        clean[name] = value
    return clean


async def release_all(workers: list[Worker]) -> None:
    """Release every worker, ignoring individual failures (used on error paths)."""
    for worker in workers:
        try:
            await worker.release()
        except Exception:  # best-effort cleanup
            continue


def cache_root() -> Path:
    """``~/.cache/coverset`` (``$XDG_CACHE_HOME/coverset`` when set): venvs and wheels live here."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "coverset"
