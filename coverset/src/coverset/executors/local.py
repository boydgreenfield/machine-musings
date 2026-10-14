"""``LocalExecutor``: shards as scrubbed subprocesses on this machine. A convenience, not a sandbox.

The test code runs with the control plane's user id, can read anything that user can read and can
reach the network. What the executor does guarantee:

* **Cached environments.** One virtualenv per ``env_hash`` under ``~/.cache/coverset/venvs/``,
  built by ``uv venv --relocatable`` + ``uv pip install -r requirements.txt`` + the pytest-coverset
  wheel *without its dependencies* (the requirements already carry an exact ``coverage`` pin, see
  :func:`~coverset.executors.env_export.build_env_spec`, so the repo's locked ``pytest`` is never
  upgraded behind its back) in a staging directory under an exclusive ``flock``, marked ready, then
  renamed into place atomically, so concurrent runs (in-process or from another CLI) never see a
  half-built venv. ``uv`` runs with ``--no-config`` and an allow-listed environment (``PATH``,
  ``HOME``, cache directories, ``UV_*``, proxy and TLS variables): an sdist build hook never sees the
  control plane's secrets. The project under test is imported through ``PYTHONPATH``; it is
  installed only when its :class:`~coverset.lang.base.EnvSpec` says ``install`` is ``wheel`` or
  ``editable`` (``[tool.coverset] install``), and then never into the shared venv:
  :meth:`LocalExecutor.install_project` builds it once per (environment, commit) with
  ``uv pip install --no-deps --no-build-isolation --target`` into
  ``~/.cache/coverset/installs/<key>/site``, writes coverset's import fallback there (compiled
  extensions and generated modules, see :mod:`coverset.lang.python_env`), and the run's workers get
  that directory as a site directory: a one-line ``_coverset_site_dirs.pth`` in the venv calls
  ``site.addsitedir`` on ``COVERSET_SITE_DIRS``, which only the executor sets. It lands after
  ``PYTHONPATH`` on ``sys.path``, so the checkout still wins for every pure-Python module.
* **A scrubbed child environment** built from scratch: ``PATH=<venv>/bin:/usr/bin:/bin``, ``HOME``
  and ``TMPDIR`` inside the worker's private directory, ``PYTHONUNBUFFERED=1``, ``LANG``, ``TZ``,
  plus what the caller passes after :func:`~coverset.executors.base.sanitize_env` (``PYTHONPATH``,
  ``COVERSET_*`` plugin options and the run's ``extra_env``). Nothing is inherited from the
  control plane's environment, so tokens such as ``COVERSET_GITHUB_*``, ``ANTHROPIC_API_KEY`` or
  ``MODAL_TOKEN_*`` never reach a test.
* **Process-group control.** Every exec starts a new session; cancelling sends ``SIGTERM`` to the
  group (pytest-coverset then writes its bundle) and ``SIGKILL`` after a 5 s grace period. When an
  exec ends (normally or not) its whole group gets ``SIGKILL``, and so does every group the worker
  ever started when it is released, so background processes a test suite leaves behind do not
  outlive their exec (a child that calls ``setsid`` itself escapes; this is not a sandbox).
* **Waiting for capacity.** ``acquire`` hands out at most ``local_max_workers`` workers at once. A
  run that holds none waits (up to ``acquire_wait_s``) for one to be released instead of failing;
  a run that already holds workers gets only what is free right now (the planner re-shards).
* **Released means gone.** After ``release`` every worker method raises
  :class:`~coverset.executors.base.ExecutorError`; nothing recreates the deleted directory.
* **Cleanup.** Each worker owns a ``tempfile.mkdtemp(prefix="coverset-run-")`` directory outside the
  project, removed on release; executor start-up removes leftovers older than an hour. A worker
  holds an exclusive ``flock`` on ``<root>/.worker.lock`` from acquire to release, and the sweep
  skips a directory whose lock is held or whose ``run/`` or ``repo/`` changed within the hour, so
  another process's start-up never deletes a live worker of a long run.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections import OrderedDict
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
from coverset.lang.python_env import BUILD_ENV, FALLBACK_WRITER, install_needed, local_packages

__all__ = [
    "KILL_GRACE_S",
    "READY_MARKER",
    "RUN_DIR_PREFIX",
    "LocalExecutor",
    "LocalWorker",
    "ProjectInstaller",
    "VenvBuilder",
    "sweep_stale_run_dirs",
    "uv_environment",
    "uv_project_installer",
    "uv_venv_builder",
]

RUN_DIR_PREFIX = "coverset-run-"
WORKER_LOCK = ".worker.lock"
"""File in a worker root that its executor holds an exclusive ``flock`` on while the worker lives."""
READY_MARKER = ".coverset-ready.json"
KILL_GRACE_S = 5.0
ACQUIRE_WAIT_S = 600.0
"""How long a run holding no worker waits for a local worker to be released."""
UV_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NIX_SSL_CERT_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SYSTEMROOT",
    }
)
"""Variables of the control plane's environment that ``uv`` may see when it builds a venv (plus ``UV_*``)."""
UV_ENV_DENIED = frozenset({"UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR_OVERRIDE"})
STALE_RUN_DIR_S = 3600.0
MAX_GET_FILE_BYTES = 1024 * 1024 * 1024
UV_TIMEOUT_S = 900
_READ_CHUNK = 64 * 1024
_DRAIN_AFTER_EXIT_S = 5.0

log = logging.getLogger(__name__)

type VenvBuilder = Callable[[EnvSpec, Path], None]
"""Builds a venv for ``spec`` in the (empty, existing) directory it is given; raises on failure."""
type ProjectInstaller = Callable[[EnvSpec, Path, Path, Path], None]
"""``(spec, venv, checkout, site)``: installs the project of ``checkout`` into the empty ``site``."""
SITE_DIRS_ENV = "COVERSET_SITE_DIRS"
"""Child variable naming the project's install directories; set only by the executor, never passed."""
SITE_DIRS_PTH = "_coverset_site_dirs.pth"
_SITE_DIRS_HOOK = (
    "import os, site; [site.addsitedir(p) for p in os.environ.get("
    f"{SITE_DIRS_ENV!r}, '').split(os.pathsep) if p]\n"
)
MAX_REMEMBERED_INSTALLS = 256
INSTALL_MAX_AGE_S = 3 * 24 * 3600.0
"""Project installs unused for this long are deleted when another one is built."""


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
        project_installer: ProjectInstaller | None = None,
        kill_grace_s: float = KILL_GRACE_S,
        sweep: bool = True,
        acquire_wait_s: float = ACQUIRE_WAIT_S,
    ) -> None:
        default_workers = settings.local_max_workers if settings is not None else min(8, os.cpu_count() or 1)
        self.max_workers = max_workers or default_workers
        self.venvs_dir = (cache_dir or cache_root()) / "venvs"
        self.installs_dir = (cache_dir or cache_root()) / "installs"
        self._installer = project_installer or uv_project_installer
        self._site_dirs: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self.temp_dir = Path(os.path.realpath(temp_dir or tempfile.gettempdir()))
        self.kill_grace_s = kill_grace_s
        self._builder = venv_builder or uv_venv_builder
        self.acquire_wait_s = acquire_wait_s
        self._workers: dict[str, LocalWorker] = {}
        self._run_of: dict[str, str] = {}
        self._freed: asyncio.Condition | None = None
        self._freed_loop: asyncio.AbstractEventLoop | None = None
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

    async def install_project(self, env: EnvRef, checkout: Path, *, sha: str, run_id: str) -> Path | None:
        """Install the project of ``checkout`` for ``run_id``'s workers when ``env.spec.install`` asks.

        The planner calls this with its control-plane checkout of the head commit (the
        LocalExecutor never runs untrusted pull requests). Returns the site directory, or None for
        ``install = "none"``.
        """
        if env.spec is None or not install_needed(env.spec):
            return None
        site = await asyncio.to_thread(self.install_project_sync, env, checkout, sha)
        self._site_dirs[run_id] = (str(site),)
        self._site_dirs.move_to_end(run_id)
        while len(self._site_dirs) > MAX_REMEMBERED_INSTALLS:
            self._site_dirs.popitem(last=False)
        return site

    def install_project_sync(self, env: EnvRef, checkout: Path, sha: str) -> Path:
        """The install directory for (environment, ``sha``, mode), built under a lock on first use."""
        spec = env.spec
        if spec is None:
            raise ExecutorError("install_project needs the environment's spec")
        key = hashlib.sha256(f"{spec.env_hash}\0{sha}\0{spec.install}".encode()).hexdigest()[:32]
        final = self.installs_dir / key
        self.installs_dir.mkdir(parents=True, exist_ok=True)
        if not (final / READY_MARKER).is_file():
            with file_lock(self.installs_dir / f"{key}.lock"):
                if not (final / READY_MARKER).is_file():
                    staging = Path(tempfile.mkdtemp(dir=self.installs_dir, prefix=f".{key[:16]}-"))
                    try:
                        (staging / "site").mkdir()
                        self._installer(spec, Path(env.location), checkout, staging / "site")
                        marker = {"env_hash": spec.env_hash, "sha": sha, "install": spec.install}
                        (staging / READY_MARKER).write_text(json.dumps(marker), encoding="utf-8")
                        if final.exists():
                            shutil.rmtree(final)
                        os.rename(staging, final)
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
                    _prune_installs(self.installs_dir, keep=final)
        else:
            os.utime(final / READY_MARKER)  # recently used: the pruning below keeps it
        _write_site_dirs_hook(Path(env.location))
        return final / "site"

    # -- workers -----------------------------------------------------------------------------------

    async def acquire(
        self,
        base: EnvRef | SnapshotRef,
        n: int,
        resources: Resources,
        ttl_s: int,
        labels: Mapping[str, str],
    ) -> list[Worker]:
        """Up to ``n`` workers, limited by ``local_max_workers`` minus the workers already out.

        When none is free and the caller (``labels["coverset_run"]``) holds no worker yet, waits up
        to ``acquire_wait_s`` for a release; a caller that already holds workers never waits.
        """
        env = base.env if isinstance(base, SnapshotRef) else base
        venv = Path(env.location)
        if not (venv / READY_MARKER).is_file():
            raise ExecutorError(f"environment {env.env_hash[:12]} is not built")
        run = labels.get("coverset_run", "")
        site_dirs = self._site_dirs.get(run, ()) if run else ()
        holds = bool(run) and run in self._run_of.values()
        if n > 0 and not holds and self.max_workers - len(self._workers) <= 0:
            freed = self._condition()
            async with freed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        freed.wait_for(lambda: self.max_workers - len(self._workers) > 0),
                        timeout=self.acquire_wait_s,
                    )
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
                site_dirs=site_dirs,
            )
            self._workers[worker.id] = worker
            self._run_of[worker.id] = run
            workers.append(worker)
        return workers

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._freed is None or self._freed_loop is not loop:
            self._freed, self._freed_loop = asyncio.Condition(), loop
        return self._freed

    def _forget(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)
        self._run_of.pop(worker_id, None)
        freed, loop = self._freed, self._freed_loop
        if freed is None or loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self._notify(freed))
        else:
            loop.call_soon_threadsafe(lambda: loop.create_task(self._notify(freed)))

    @staticmethod
    async def _notify(freed: asyncio.Condition) -> None:
        async with freed:
            freed.notify_all()

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
        site_dirs: tuple[str, ...] = (),
    ) -> None:
        self.id = worker_id
        self._site_dirs = site_dirs
        self.root = str(root)
        self._root = root
        self._venv = venv
        self._kill_grace_s = kill_grace_s
        self._on_release = on_release
        self._procs: set[asyncio.subprocess.Process] = set()
        self._groups: set[int] = set()
        self._cancelled: set[int] = set()
        self._released = False
        self._lock_fd: int | None = _hold_lock(root / WORKER_LOCK)
        (root / "home").mkdir(exist_ok=True)
        (root / "tmp").mkdir(exist_ok=True)

    def _check(self) -> None:
        if self._released:
            raise ExecutorError(f"worker {self.id} was released")

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
        clean = sanitize_env(env)
        clean.pop(SITE_DIRS_ENV, None)  # only the executor names install directories
        if self._site_dirs:
            base[SITE_DIRS_ENV] = os.pathsep.join(self._site_dirs)
        return {**base, **clean}

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
        """Run ``cmd`` in its own session; stream lines; kill on timeout, cap or :meth:`cancel`.

        The exec's process group is killed when it ends, whatever is left in it.
        """
        self._check()
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
        self._groups.add(proc.pid)
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
            _signal_group(proc.pid, signal.SIGKILL)  # whatever the exec left running in its group
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
            if proc.returncode is not None:
                _signal_group(proc.pid, signal.SIGKILL)
                self._groups.discard(proc.pid)
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
        self._check()
        target = Path(self.path(dest))
        await asyncio.to_thread(_extract_tar, tar_bytes, target)

    async def get_file(self, path: str) -> bytes:
        """Read a regular file without following symlinks."""
        self._check()
        return await asyncio.to_thread(_read_regular, Path(self.path(path)))

    async def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` atomically (temp file in the same directory, then rename)."""
        self._check()
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
            for group in list(self._groups):  # leftovers of execs that ended
                _signal_group(group, signal.SIGKILL)
            self._groups.clear()
            await asyncio.to_thread(shutil.rmtree, self._root, True)
            if self._lock_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._lock_fd)  # closing the descriptor drops the flock
                self._lock_fd = None
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
        self.callback_errors = 0

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
        try:
            self._on_line(text, name)
        except Exception:  # a failing callback must not stop the reader (the pipe would fill up)
            self.callback_errors += 1
            if self.callback_errors == 1:
                log.exception("on_line raised; later errors of this exec are not logged")


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


def _hold_lock(path: Path) -> int | None:
    """Open ``path`` and take an exclusive ``flock`` on it; the descriptor holds it until closed."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        log.warning("cannot create the worker lock %s: %s", path, exc)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        log.warning("cannot lock the worker directory %s: %s", path.parent, exc)
        return None
    return fd


def _try_lock(path: Path) -> int | None:
    """A descriptor holding ``path``'s lock, or None when another holder has it (a live worker).

    A missing lock file (a directory from before locks, or one a test deleted) is not "held": the
    caller falls back to the modification times.
    """
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return -1
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _recently_used(entry: Path, cutoff: float) -> bool:
    """Whether ``entry`` or its ``run/`` or ``repo/`` directory changed after ``cutoff``."""
    for path in (entry, entry / "run", entry / "repo"):
        with contextlib.suppress(OSError):
            if path.lstat().st_mtime > cutoff:
                return True
    return False


def sweep_stale_run_dirs(temp_dir: Path, *, max_age_s: float = STALE_RUN_DIR_S) -> list[Path]:
    """Remove ``coverset-run-*`` directories in ``temp_dir`` (owned by us) older than ``max_age_s``.

    A directory is left alone while its worker lock is held (a live worker of any process) or when
    it, its ``run/`` or its ``repo/`` changed within ``max_age_s``.
    """
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
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or _recently_used(entry, cutoff):
            continue
        fd = _try_lock(entry / WORKER_LOCK)
        if fd is None:
            continue
        try:
            shutil.rmtree(entry, ignore_errors=True)
        finally:
            if fd >= 0:
                os.close(fd)
        removed.append(entry)
    return removed


def uv_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allow-listed environment ``uv`` builds venvs with (no tokens, no ``VIRTUAL_ENV``)."""
    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if (key in UV_ENV_NAMES or key.startswith("UV_")) and key not in UV_ENV_DENIED
    }


def uv_venv_builder(spec: EnvSpec, target: Path) -> None:
    """Build a relocatable venv in ``target`` with uv: requirements, then the plugin wheel (no deps).

    The requirements already hold the plugin's runtime dependencies at exact versions (the repo's
    own ``pytest`` and ``coverage`` pins, or the ``coverage`` pin coverset adds, which is part of
    ``env_hash``), so installing the wheel with ``--no-deps`` can never change the locked
    environment. uv runs with ``--no-config`` and :func:`uv_environment`.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise ExecutorError("uv is not installed or not on PATH")
    env = uv_environment()
    python = str(target / "bin" / "python")
    _uv(
        uv,
        [
            "venv",
            "--no-config",
            "--quiet",
            "--relocatable",
            "--allow-existing",
            "--python",
            spec.python,
            str(target),
        ],
        env,
    )
    requirements = target / ".coverset-requirements.txt"
    requirements.write_text(spec.requirements_txt, encoding="utf-8")
    if any(line.strip() and not line.lstrip().startswith("#") for line in spec.requirements_txt.splitlines()):
        _uv(
            uv, ["pip", "install", "--no-config", "--quiet", "--python", python, "-r", str(requirements)], env
        )
    if spec.plugin_wheel is not None:
        _uv(
            uv,
            [
                "pip",
                "install",
                "--no-config",
                "--quiet",
                "--no-deps",
                "--python",
                python,
                str(spec.plugin_wheel),
            ],
            env,
        )


def uv_project_installer(spec: EnvSpec, venv: Path, checkout: Path, site: Path) -> None:
    """Install the project of ``checkout`` into ``site`` (``--target``) and write the import fallback.

    ``--no-deps`` keeps the locked environment as it is and ``--no-build-isolation`` builds with the
    backend the environment already holds (``EnvSpec`` adds ``[build-system] requires`` whenever the
    project is installed). ``editable`` is installed the same way here: a worker's checkout lives in
    a fresh directory per run, so an editable link would point at a directory that is gone.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise ExecutorError("uv is not installed or not on PATH")
    python = str(venv / "bin" / "python")
    env = {**uv_environment(), **BUILD_ENV}
    pip = [
        "pip",
        "install",
        "--no-config",
        "--quiet",
        "--no-deps",
        "--no-build-isolation",
        "--python",
        python,
    ]
    local = [package.requirement(str(checkout)) for package in local_packages(spec.requirements_txt)]
    if local:  # editable local packages are copied too: the checkout is temporary
        _uv(uv, [*pip, "--target", str(site), *local], env)
    if spec.install != "none":
        _uv(uv, [*pip, "--target", str(site), str(checkout)], env)
    source = checkout if spec.source_root in ("", ".") else checkout / spec.source_root
    try:
        proc = subprocess.run(
            [python, "-c", FALLBACK_WRITER, str(source), str(site)],
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutorError(f"writing the import fallback failed: {exc}") from exc
    if proc.returncode != 0:
        raise ExecutorError(
            f"writing the import fallback failed: {proc.stderr.decode('utf-8', 'replace')[-400:]}"
        )


def _prune_installs(installs_dir: Path, *, keep: Path, max_age_s: float = INSTALL_MAX_AGE_S) -> None:
    """Delete project installs not used for ``max_age_s`` (one per commit adds up), except ``keep``.

    A directory whose lock another process holds is being built and is skipped.
    """
    cutoff = time.time() - max_age_s
    for entry in installs_dir.iterdir() if installs_dir.is_dir() else []:
        if entry == keep or not entry.is_dir() or entry.name.startswith("."):
            continue
        marker = entry / READY_MARKER
        try:
            if marker.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        fd = _try_lock(installs_dir / f"{entry.name}.lock")
        if fd is None:
            continue
        try:
            shutil.rmtree(entry, ignore_errors=True)
        finally:
            if fd >= 0:
                os.close(fd)


def _write_site_dirs_hook(venv: Path) -> None:
    """Make ``venv`` honour ``COVERSET_SITE_DIRS`` (idempotent, atomic; harmless when it is unset)."""
    for packages in venv.glob("lib/python*/site-packages"):
        hook = packages / SITE_DIRS_PTH
        try:
            if hook.read_text(encoding="utf-8") == _SITE_DIRS_HOOK:
                continue
        except OSError:
            pass
        tmp = packages / f".{SITE_DIRS_PTH}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(_SITE_DIRS_HOOK, encoding="utf-8")
        os.replace(tmp, hook)


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
