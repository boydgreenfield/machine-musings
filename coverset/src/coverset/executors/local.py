"""``LocalExecutor``: shards as scrubbed subprocesses on this machine. A convenience, not a sandbox.

The test code runs with the control plane's user id, can read anything that user can read and can
reach the network. What the executor does guarantee:

* **Cached environments.** One virtualenv per ``env_hash`` under ``~/.cache/coverset/venvs/``,
  built by ``uv venv --relocatable`` + ``uv pip install -r requirements.txt`` + the pytest-coverset
  wheel in a staging directory under an exclusive ``flock``, marked ready, then renamed into place
  atomically, so concurrent runs (in-process or from another CLI) never see a half-built venv. The
  project under test is never installed: tests import it through ``PYTHONPATH``.
* **A scrubbed child environment** built from scratch: ``PATH=<venv>/bin:/usr/bin:/bin``, ``HOME``
  and ``TMPDIR`` inside the worker's private directory, ``PYTHONUNBUFFERED=1``, ``LANG``, ``TZ``,
  plus what the caller passes after :func:`~coverset.executors.base.sanitize_env` (``PYTHONPATH``,
  ``COVERSET_*`` plugin options and the run's ``extra_env``). Nothing is inherited from the
  control plane's environment, so tokens such as ``COVERSET_GITHUB_*``, ``ANTHROPIC_API_KEY`` or
  ``MODAL_TOKEN_*`` never reach a test.
* **Process-group control.** Every exec starts a new session; cancelling sends ``SIGTERM`` to the
  group (pytest-coverset then writes its bundle) and ``SIGKILL`` after a 5 s grace period.
* **Cleanup.** Each worker owns a ``tempfile.mkdtemp(prefix="coverset-run-")`` directory outside the
  project, removed on release; executor start-up removes leftovers older than an hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from coverset.config import Settings
from coverset.executors.base import (
    MAX_LINE_BYTES,
    Capabilities,
    EnvRef,
    ExecutorError,
    LineCallback,
    Resources,
    RunResult,
    SnapshotRef,
    Stream,
    Worker,
    cache_root,
    sanitize_env,
)
from coverset.executors.env_export import file_lock
from coverset.lang.base import EnvSpec

__all__ = [
    "KILL_GRACE_S",
    "READY_MARKER",
    "RUN_DIR_PREFIX",
    "LocalExecutor",
    "LocalWorker",
    "VenvBuilder",
    "sweep_stale_run_dirs",
    "uv_venv_builder",
]

RUN_DIR_PREFIX = "coverset-run-"
READY_MARKER = ".coverset-ready.json"
KILL_GRACE_S = 5.0
STALE_RUN_DIR_S = 3600.0
MAX_GET_FILE_BYTES = 1024 * 1024 * 1024
UV_TIMEOUT_S = 900
_READ_CHUNK = 64 * 1024
_DRAIN_AFTER_EXIT_S = 5.0

type VenvBuilder = Callable[[EnvSpec, Path], None]
"""Builds a venv for ``spec`` in the (empty, existing) directory it is given; raises on failure."""


class LocalExecutor:
    """Runs workers as local subprocesses (see the module docstring)."""

    name = "local"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_workers: int | None = None,
        cache_dir: Path | None = None,
        temp_dir: Path | None = None,
        venv_builder: VenvBuilder | None = None,
        kill_grace_s: float = KILL_GRACE_S,
        sweep: bool = True,
    ) -> None:
        default_workers = settings.local_max_workers if settings is not None else min(8, os.cpu_count() or 1)
        self.max_workers = max_workers or default_workers
        self.venvs_dir = (cache_dir or cache_root()) / "venvs"
        self.temp_dir = Path(os.path.realpath(temp_dir or tempfile.gettempdir()))
        self.kill_grace_s = kill_grace_s
        self._builder = venv_builder or uv_venv_builder
        self._workers: dict[str, LocalWorker] = {}
        if sweep:
            sweep_stale_run_dirs(self.temp_dir)

    # -- environments ------------------------------------------------------------------------------

    async def ensure_env(self, spec: EnvSpec) -> EnvRef:
        """The cached venv for ``spec``, built under a lock on first use."""
        return await asyncio.to_thread(self.ensure_env_sync, spec)

    def ensure_env_sync(self, spec: EnvSpec) -> EnvRef:
        """Synchronous :meth:`ensure_env` (also used by ``coverset warm``)."""
        self.venvs_dir.mkdir(parents=True, exist_ok=True)
        final = self.venvs_dir / spec.env_hash
        if not (final / READY_MARKER).is_file():
            with file_lock(self.venvs_dir / f"{spec.env_hash}.lock"):
                if not (final / READY_MARKER).is_file():
                    self._build(spec, final)
        return EnvRef(env_hash=spec.env_hash, python=spec.python, location=str(final), spec=spec)

    def _build(self, spec: EnvSpec, final: Path) -> None:
        staging = Path(tempfile.mkdtemp(dir=self.venvs_dir, prefix=f".{spec.env_hash[:16]}-"))
        try:
            self._builder(spec, staging)
            marker = {"env_hash": spec.env_hash, "python": spec.python, "built_at": time.time()}
            (staging / READY_MARKER).write_text(json.dumps(marker), encoding="utf-8")
            if final.exists():  # a half-built venv from a crashed builder (no ready marker)
                shutil.rmtree(final)
            os.rename(staging, final)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def ensure_snapshot(self, env: EnvRef, repo: str, sha: str) -> SnapshotRef | None:
        """Local workers start from a fresh directory every time: no snapshots."""
        return None

    # -- workers -----------------------------------------------------------------------------------

    async def acquire(
        self,
        base: EnvRef | SnapshotRef,
        n: int,
        resources: Resources,
        ttl_s: int,
        labels: Mapping[str, str],
    ) -> list[Worker]:
        """Up to ``n`` workers, limited by ``local_max_workers`` minus the workers already out."""
        env = base.env if isinstance(base, SnapshotRef) else base
        venv = Path(env.location)
        if not (venv / READY_MARKER).is_file():
            raise ExecutorError(f"environment {env.env_hash[:12]} is not built")
        count = max(0, min(n, self.max_workers - len(self._workers)))
        workers: list[Worker] = []
        for _ in range(count):
            root = Path(os.path.realpath(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX, dir=self.temp_dir)))
            worker = LocalWorker(
                venv=venv,
                root=root,
                worker_id=f"local-{uuid.uuid4().hex[:12]}",
                kill_grace_s=self.kill_grace_s,
                on_release=self._forget,
            )
            self._workers[worker.id] = worker
            workers.append(worker)
        return workers

    def _forget(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)

    @property
    def active_workers(self) -> int:
        """Workers acquired and not yet released."""
        return len(self._workers)

    def capabilities(self) -> Capabilities:
        """Egress is unrestricted and there is no pool or snapshot support."""
        return Capabilities(
            name=self.name, egress=True, warm_pool=False, fs_snapshot=False, max_workers=self.max_workers
        )

    def cost_usd(self, resources: Resources, seconds: float) -> float:
        """Local runs are free."""
        return 0.0

    async def close(self) -> None:
        """Release every outstanding worker."""
        for worker in list(self._workers.values()):
            await worker.release()


class LocalWorker:
    """One private directory plus the subprocesses running in it."""

    def __init__(
        self,
        *,
        venv: Path,
        root: Path,
        worker_id: str,
        kill_grace_s: float = KILL_GRACE_S,
        on_release: Callable[[str], None] | None = None,
    ) -> None:
        self.id = worker_id
        self.root = str(root)
        self._root = root
        self._venv = venv
        self._kill_grace_s = kill_grace_s
        self._on_release = on_release
        self._procs: set[asyncio.subprocess.Process] = set()
        self._cancelled: set[int] = set()
        self._released = False
        (root / "home").mkdir(exist_ok=True)
        (root / "tmp").mkdir(exist_ok=True)

    def path(self, rel: str) -> str:
        """Absolute path of ``rel``; absolute paths and ``..`` segments are refused."""
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or "\0" in rel:
            raise ExecutorError(f"worker paths must be relative and inside the worker: {rel!r}")
        return str(self._root.joinpath(*pure.parts)) if pure.parts else self.root

    def child_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The complete environment of a child: the fixed base plus the sanitised ``env``."""
        base = {
            "PATH": f"{self._venv / 'bin'}:/usr/bin:/bin",
            "HOME": str(self._root / "home"),
            "TMPDIR": str(self._root / "tmp"),
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
        }
        return {**base, **sanitize_env(env)}

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
        """Run ``cmd`` in its own session; stream lines; kill on timeout, cap or :meth:`cancel`."""
        if self._released:
            raise ExecutorError(f"worker {self.id} was released")
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.path(cwd),
                env=self.child_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutorError(f"cannot start {cmd[0]!r}: {exc}") from exc
        self._procs.add(proc)
        pump = _Pump(on_line, stdout_cap, on_overflow=lambda: self._spawn_kill(proc))
        readers = [
            asyncio.create_task(pump.read(proc.stdout, "stdout")),  # type: ignore[arg-type]
            asyncio.create_task(pump.read(proc.stderr, "stderr")),  # type: ignore[arg-type]
        ]
        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                await self._kill(proc)
            done, pending = await asyncio.wait(readers, timeout=_DRAIN_AFTER_EXIT_S)
            if pending:  # a grandchild still holds the pipes open
                _signal_group(proc.pid, signal.SIGKILL)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            await self._kill(proc)
            for task in readers:
                task.cancel()
            raise
        finally:
            self._procs.discard(proc)
        cancelled = proc.pid in self._cancelled
        self._cancelled.discard(proc.pid)
        return RunResult(
            exit_code=proc.returncode,
            started_at=started,
            finished_at=time.time(),
            stdout_bytes=pump.total,
            truncated=pump.overflowed,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def _spawn_kill(self, proc: asyncio.subprocess.Process) -> None:
        asyncio.get_running_loop().create_task(self._kill(proc))

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the process group, then SIGKILL it after the grace period."""
        if proc.returncode is None:
            _signal_group(proc.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=self._kill_grace_s)
        _signal_group(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=self._kill_grace_s)

    async def put_tree(self, tar_bytes: bytes, dest: str) -> None:
        """Extract a ``.tar.gz`` into ``dest`` with the ``data`` filter (no links out, no devices)."""
        target = Path(self.path(dest))
        await asyncio.to_thread(_extract_tar, tar_bytes, target)

    async def get_file(self, path: str) -> bytes:
        """Read a regular file without following symlinks."""
        return await asyncio.to_thread(_read_regular, Path(self.path(path)))

    async def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` atomically (temp file in the same directory, then rename)."""
        target = Path(self.path(path))
        await asyncio.to_thread(_write_atomic, target, data)

    async def cancel(self) -> None:
        """SIGTERM every running exec's process group; SIGKILL after the grace period."""
        procs = list(self._procs)
        for proc in procs:
            self._cancelled.add(proc.pid)
        await asyncio.gather(*(self._kill(proc) for proc in procs), return_exceptions=True)

    async def release(self) -> None:
        """Stop everything and delete the worker directory (idempotent)."""
        if self._released:
            return
        self._released = True
        try:
            await self.cancel()
        finally:
            await asyncio.to_thread(shutil.rmtree, self._root, True)
            if self._on_release is not None:
                self._on_release(self.id)


class _Pump:
    """Splits two byte streams into lines for ``on_line``, enforcing the per-line and total caps."""

    def __init__(self, on_line: LineCallback | None, cap: int, *, on_overflow: Callable[[], None]) -> None:
        self._on_line = on_line
        self._cap = cap
        self._on_overflow = on_overflow
        self.total = 0
        self.overflowed = False

    async def read(self, stream: asyncio.StreamReader, name: Stream) -> None:
        buffer = bytearray()
        discarding = False
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                break
            if self.overflowed:
                continue  # keep draining so the child never blocks on a full pipe
            self.total += len(chunk)
            if self.total > self._cap:
                self.overflowed = True
                self._on_overflow()
                continue
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > MAX_LINE_BYTES:
                        if not discarding:
                            self._emit(bytes(buffer[:MAX_LINE_BYTES]), name)
                        discarding = True
                        buffer.clear()
                    break
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if discarding:
                    discarding = False
                    continue
                self._emit(line[:MAX_LINE_BYTES], name)
        if buffer and not discarding and not self.overflowed:
            self._emit(bytes(buffer[:MAX_LINE_BYTES]), name)

    def _emit(self, line: bytes, name: Stream) -> None:
        if self._on_line is None:
            return
        text = line.decode("utf-8", "replace").rstrip("\r")
        self._on_line(text, name)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _extract_tar(data: bytes, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(target, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ExecutorError(f"cannot extract the source archive: {exc}") from exc


def _read_regular(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ExecutorError(f"no such file in the worker: {path.name}") from None
    except OSError as exc:
        raise ExecutorError(f"cannot read {path.name}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ExecutorError(f"{path.name} is not a regular file")
        if info.st_size > MAX_GET_FILE_BYTES:
            raise ExecutorError(f"{path.name} is larger than {MAX_GET_FILE_BYTES} bytes")
        return handle.read()


def _write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def sweep_stale_run_dirs(temp_dir: Path, *, max_age_s: float = STALE_RUN_DIR_S) -> list[Path]:
    """Remove ``coverset-run-*`` directories in ``temp_dir`` (owned by us) older than ``max_age_s``."""
    removed: list[Path] = []
    cutoff = time.time() - max_age_s
    try:
        entries = list(temp_dir.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if not entry.name.startswith(RUN_DIR_PREFIX):
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mtime > cutoff:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry)
    return removed


def uv_venv_builder(spec: EnvSpec, target: Path) -> None:
    """Build a relocatable venv in ``target`` with uv: requirements, then the plugin wheel (with deps).

    The wheel is installed *with* its dependencies (``coverage``), resolved against the already
    installed requirements, because a target repo's lockfile need not pin coverage at all.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise ExecutorError("uv is not installed or not on PATH")
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    python = str(target / "bin" / "python")
    _uv(
        uv,
        ["venv", "--quiet", "--relocatable", "--allow-existing", "--python", spec.python, str(target)],
        env,
    )
    requirements = target / ".coverset-requirements.txt"
    requirements.write_text(spec.requirements_txt, encoding="utf-8")
    if any(line.strip() and not line.lstrip().startswith("#") for line in spec.requirements_txt.splitlines()):
        _uv(uv, ["pip", "install", "--quiet", "--python", python, "-r", str(requirements)], env)
    if spec.plugin_wheel is not None:
        _uv(uv, ["pip", "install", "--quiet", "--python", python, str(spec.plugin_wheel)], env)


def _uv(uv: str, args: list[str], env: Mapping[str, str]) -> None:
    try:
        proc = subprocess.run(
            [uv, *args],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=UV_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutorError(f"uv {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[-800:]
        raise ExecutorError(f"uv {' '.join(args[:2])} failed: {stderr}")
