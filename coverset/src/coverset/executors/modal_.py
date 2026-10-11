"""``ModalExecutor``: shards in Modal Sandboxes, driven client-side (no functions are deployed).

All Modal objects live in the App ``coverset-runner`` of the Modal environment
``COVERSET_MODAL_ENVIRONMENT`` (default ``coverset``), looked up with ``create_if_missing=True``; that
creates an empty, persistent app record (``modal app list`` shows it as deployed) which is only a
container for sandboxes.

* **Environments** (:meth:`ModalExecutor.ensure_env`): one image per ``env_hash`` built from the recipe
  in :mod:`coverset.executors.modal_images`. Its object id is remembered in
  ``<data_dir>/modal/images.json`` and in ``envs.image_id``; :class:`~coverset.executors.base.EnvRef`
  carries it as ``location``. A stale id surfaces as ``NotFoundError`` from ``Sandbox.create`` and
  triggers one rebuild.
* **Trusted snapshots** (:meth:`ModalExecutor.ensure_snapshot`): only for commits reachable from the
  repo's default branch. A sandbox from the env image receives the ``git archive`` tarball, collects
  (``pytest --collect-only``) and byte-compiles the checkout with hash-checked pycs into
  ``PYTHONPYCACHEPREFIX``, then ``snapshot_filesystem(ttl=7 d)``; the image id and ``expires_at`` go
  into ``snapshots``, where the planner picks the newest ready one (it never waits for one).
* **Workers** (:meth:`ModalExecutor.acquire`): claimed from the warm pool first (pooled sandboxes are
  created from the trusted snapshot, never exec'd before they are claimed, serve one run and are then
  terminated), the rest created concurrently; capacity errors shrink the result instead of failing.
* **Guards**: every sandbox gets ``cpu=(0.125, COVERSET_SHARD_CPU_LIMIT)``,
  ``memory=(512, COVERSET_SHARD_MEMORY_MIB)``, ``timeout <= COVERSET_RUN_TIMEOUT_S`` (pooled sandboxes:
  ``COVERSET_RUN_TIMEOUT_S + COVERSET_POOL_TTL_S``, so a claimed one outlives the run's timeout),
  ``idle_timeout=COVERSET_POOL_TTL_S`` (None when it is 0), ``secrets=[]``,
  ``include_oidc_identity_token=False``, network per policy (``blocked`` unless the run says
  otherwise) and the tags ``{coverset_instance, repo, run, role, created, owner}``, never a ``name``.
  One counter per process caps the live sandboxes it creates (pool + shards + snapshot builds) at
  ``COVERSET_MAX_SANDBOXES``. Before creating anything the executor checks the daily budget
  (``COVERSET_MODAL_DAILY_BUDGET_USD``) against today's spend plus what the request may cost, and
  refuses with :class:`~coverset.executors.base.CapacityError` ("budget exhausted"). Today's spend
  is ``runs.cost_estimate`` plus ``executor_spend`` (pool idle time, snapshot builds, smoke tests,
  image builds, recorded here) plus the accrued time of this process's live non-shard sandboxes.
  Env image builds also count against ``COVERSET_MAX_IMAGE_BUILDS_PER_DAY``.
* **Reaping** (:meth:`ModalExecutor.reap`): lists sandboxes tagged with this installation's
  ``coverset_instance`` (or, with ``any_instance``, every coverset sandbox in the App) and terminates
  every one not tied to an active run, except pool, snapshot and smoke sandboxes younger than their
  natural lifetime, which may belong to another process (``all`` terminates every one of them).

Cost (:meth:`ModalExecutor.cost_usd`): Modal bills ``max(request, usage)`` per second at $0.142 per
core-hour and $0.024 per GiB-hour. Usage is not observable client-side, so a running shard is
estimated at its *limits* (conservative) and an idle pooled sandbox at its *requests* (0.125 core,
0.5 GiB), the floor Modal bills while nothing runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import os
import socket
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

from coverset.config import SandboxNetwork, Settings
from coverset.executors.base import (
    Capabilities,
    CapacityError,
    EnvRef,
    ExecutorError,
    LineCallback,
    ReapReport,
    Resources,
    RunResult,
    SnapshotRef,
    Stream,
    Worker,
    sanitize_env,
)
from coverset.executors.local import _Pump  # the shared line splitter (per-line cut, stdout cap)
from coverset.executors.modal_images import build_and_record, configure_builder, env_image, image_from_id
from coverset.lang.base import EnvSpec

__all__ = [
    "APP_NAME",
    "CPU_REQUEST",
    "CPU_USD_PER_CORE_S",
    "MEMORY_REQUEST_MIB",
    "MEM_USD_PER_GIB_S",
    "SNAPSHOT_TTL_S",
    "WORK_ROOT",
    "ModalExecutor",
    "ModalWorker",
    "SandboxSlots",
    "instance_id",
    "rate_usd_per_s",
    "sandbox_options",
    "shared_slots",
]

log = logging.getLogger(__name__)

APP_NAME = "coverset-runner"
WORK_ROOT = "/work"
SNAPSHOT_TTL_S = 7 * 24 * 3600
SNAPSHOT_MARGIN_S = 3600
"""A snapshot is recorded as expiring this long before Modal drops it, so runs never race the TTL."""
CPU_REQUEST = 0.125
MEMORY_REQUEST_MIB = 512
CPU_USD_PER_CORE_S = 0.142 / 3600
MEM_USD_PER_GIB_S = 0.024 / 3600
KILL_GRACE_S = 5.0
EXEC_BACKSTOP_S = 60
"""Added to an exec's own timeout for Modal's server-side exec timeout (the client kills first)."""
SNAPSHOT_COLLECT_TIMEOUT_S = 300.0
SNAPSHOT_SANDBOX_TIMEOUT_S = 900
SLOT_WAIT_S = 120.0
POOL_REAP_GRACE_S = 60
"""A non-``all`` reap keeps pool sandboxes younger than ``COVERSET_POOL_TTL_S`` plus this."""
BUILD_RESERVE_S = 120.0
"""Seconds of limit-rate spend an env image build must fit in the remaining budget before it starts."""
MAX_GET_FILE_BYTES = 1024 * 1024 * 1024
DB_RETRY_DELAYS_S = (1.0, 3.0, 10.0, 30.0)
PYCACHE_DIR = ".pycache"
INSTANCE_FILE = "instance_id"
IMAGES_FILE = "images.json"

_DRAIN_AFTER_EXIT_S = 5.0
_SIMPLE_OUTPUT_CAP = 64 * 1024
_CAPACITY_ERRORS = ("ResourceExhaustedError", "InternalFailure", "InternalError", "ConnectionError")
_SIGNAL_SCRIPT = """\
import os, sys
sig = int(sys.argv[1])
marks = [("COVERSET_EXEC_ID=" + i).encode() for i in sys.argv[2:]]
me = os.getpid()
hit = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == me:
        continue
    try:
        with open("/proc/" + pid + "/environ", "rb") as f:
            env = f.read().split(b"\\0")
    except OSError:
        continue
    if any(m in env for m in marks):
        try:
            os.kill(int(pid), sig)
            hit += 1
        except OSError:
            pass
print(hit)
"""
"""Signals every process whose environment carries one of the given ``COVERSET_EXEC_ID`` values.

Modal's ``ContainerProcess`` has no kill; every exec is started with a unique ``COVERSET_EXEC_ID``
in its (scrubbed) environment, which its children inherit, so this works like a process-group kill.
"""

_START_SHELL = 'mkdir -p "$HOME" "$TMPDIR" && exec "$@"'


def rate_usd_per_s(cpu: float, memory_mib: float) -> float:
    """Modal's per-second price of ``cpu`` cores and ``memory_mib`` MiB (the 2026 rate card)."""
    return cpu * CPU_USD_PER_CORE_S + (memory_mib / 1024.0) * MEM_USD_PER_GIB_S


# -- process-wide sandbox slots ----------------------------------------------------------------------


class SandboxSlots:
    """A counting semaphore over live sandboxes (pool + shards + snapshot builds), shared process-wide.

    One semaphore per process: ``serve`` and a concurrent CLI ``coverset run`` each count only their
    own sandboxes, so together they may hold up to twice ``COVERSET_MAX_SANDBOXES``.

    Thread-safe and loop-independent: acquiring never blocks the event loop (``try_take``) except in
    :meth:`take`, which polls for a free slot with a deadline.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = max(0, capacity)
        self.used = 0
        self._lock = threading.Lock()

    @property
    def free(self) -> int:
        """Slots not in use."""
        with self._lock:
            return max(0, self.capacity - self.used)

    def try_take(self, n: int) -> int:
        """Take up to ``n`` slots now; return how many were taken."""
        with self._lock:
            k = max(0, min(n, self.capacity - self.used))
            self.used += k
            return k

    def give_back(self, n: int = 1) -> None:
        """Return ``n`` slots."""
        with self._lock:
            self.used = max(0, self.used - n)

    async def take(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for one slot."""
        deadline = time.monotonic() + timeout_s
        while True:
            if self.try_take(1):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)


_SHARED_SLOTS: SandboxSlots | None = None
_SHARED_LOCK = threading.Lock()


def shared_slots(capacity: int) -> SandboxSlots:
    """The process-wide :class:`SandboxSlots` (the smallest ``capacity`` any caller asked for wins)."""
    global _SHARED_SLOTS
    with _SHARED_LOCK:
        if _SHARED_SLOTS is None:
            _SHARED_SLOTS = SandboxSlots(capacity)
        elif capacity < _SHARED_SLOTS.capacity:
            _SHARED_SLOTS.capacity = capacity
        return _SHARED_SLOTS


# -- small helpers ----------------------------------------------------------------------------------


def instance_id(data_dir: Path) -> str:
    """This installation's id (``<data_dir>/instance_id``, a UUID created once, atomically)."""
    path = data_dir / INSTANCE_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return str(uuid.UUID(text))
    except (OSError, ValueError):
        pass
    data_dir.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    tmp = path.with_name(f".{INSTANCE_FILE}.{uuid.uuid4().hex}")
    tmp.write_text(value + "\n", encoding="utf-8")
    try:
        os.link(tmp, path)  # fails if another process created it first
    except FileExistsError:
        return str(uuid.UUID(path.read_text(encoding="utf-8").strip()))
    finally:
        tmp.unlink(missing_ok=True)
    return value


def sandbox_options(
    settings: Settings,
    resources: Resources,
    tags: Mapping[str, str],
    *,
    idle_timeout_s: int | None,
    timeout_s: int | None = None,
    extra_timeout_s: int = 0,
) -> dict[str, Any]:
    """The keyword arguments of every ``Sandbox.create`` call (the cost and isolation guards).

    Limits are clamped to the configured ceilings; the timeout never exceeds ``run_timeout_s`` plus
    ``extra_timeout_s`` (a pooled sandbox's idle time before its claim: at most ``pool_ttl_s``).
    """
    cpu_limit = max(CPU_REQUEST, min(resources.cpu, settings.shard_cpu_limit))
    memory_limit = max(MEMORY_REQUEST_MIB, min(resources.memory_mib, settings.shard_memory_mib))
    timeout = int(min(timeout_s or resources.timeout_s, resources.timeout_s, settings.run_timeout_s))
    extra = max(0, min(int(extra_timeout_s), settings.pool_ttl_s))
    options: dict[str, Any] = {
        "cpu": (CPU_REQUEST, float(cpu_limit)),
        "memory": (MEMORY_REQUEST_MIB, int(memory_limit)),
        "timeout": max(1, timeout + extra),
        "idle_timeout": idle_timeout_s if idle_timeout_s and idle_timeout_s > 0 else None,
        "secrets": [],
        "include_oidc_identity_token": False,
        "tags": dict(tags),
        "verbose": False,
    }
    options.update(_network_options(resources.network, settings.sandbox_domain_allowlist))
    return options


def _network_options(network: SandboxNetwork, allowlist: list[str]) -> dict[str, Any]:
    if network == "open":
        return {"block_network": False}
    if network == "allowlist" and allowlist:
        return {"block_network": False, "outbound_domain_allowlist": list(allowlist)}
    return {"block_network": True}  # blocked, or an allowlist policy with nothing allowed


def _error_name(exc: BaseException) -> str:
    return type(exc).__name__


def _is_not_found(exc: BaseException) -> bool:
    return _error_name(exc) == "NotFoundError"


def _is_fs_not_found(exc: BaseException) -> bool:
    return _error_name(exc) in ("SandboxFilesystemNotFoundError", "NotFoundError", "FileNotFoundError")


def _now() -> datetime:
    return datetime.now(UTC)


def process_owner() -> str:
    """The ``owner`` tag of sandboxes this process creates: ``<hostname>:<pid>``."""
    return f"{(socket.gethostname() or 'localhost')[:64]}:{os.getpid()}"


def spend_kind(role: str) -> str:
    """The ``executor_spend.kind`` of a sandbox role that belongs to no run."""
    if role in ("pool", "snapshot", "smoke"):
        return role
    return "bench"


# -- live sandboxes ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Live:
    """A sandbox this process created and has not terminated."""

    sandbox: Any
    sandbox_id: str
    role: str
    repo: str
    run: str
    image_id: str
    network: SandboxNetwork
    cpu_limit: float
    memory_mib: int
    created_at: float = field(default_factory=time.monotonic)
    claimed: bool = False
    created_wall: int = field(default_factory=lambda: int(time.time()))
    """Creation time (UNIX seconds), also the sandbox's ``created`` tag."""

    def rate(self) -> float:
        """Estimated USD/s: requests while pooled and unclaimed, limits otherwise."""
        if self.role == "pool" and not self.claimed:
            return rate_usd_per_s(CPU_REQUEST, MEMORY_REQUEST_MIB)
        return rate_usd_per_s(self.cpu_limit, self.memory_mib)


@dataclass(slots=True)
class ExecutorStats:
    """Counters the benchmark script and the smoke command read."""

    created: int = 0
    terminated: int = 0
    pool_claims: int = 0
    images_built: int = 0
    rebuilds: int = 0
    capacity_errors: list[str] = field(default_factory=list)
    untracked_usd: float = 0.0
    """Estimated spend this process attributed to no run (pool idle time, snapshot builds, smoke
    tests, image builds). A statistic only: the budget reads ``executor_spend`` instead."""
    build_usd: float = 0.0
    """Estimated spend of the env image builds this process ran (limit rate x wall time)."""
    lifetime_usd: float = 0.0
    """Estimated spend of every terminated sandbox over its whole lifetime (all roles, rate card)."""
    sandbox_seconds: float = 0.0


class ModalExecutor:
    """Runs workers in Modal Sandboxes (see the module docstring)."""

    name = "modal"

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], ModuleType] | None = None,
        slots: SandboxSlots | None = None,
        budget_probe: Callable[[], Awaitable[float]] | None = None,
        verbose: bool = False,
        kill_grace_s: float = KILL_GRACE_S,
    ) -> None:
        self.settings = settings
        self.verbose = verbose
        self.kill_grace_s = kill_grace_s
        self.instance_id = instance_id(settings.data_dir)
        self.stats = ExecutorStats()
        self._factory = client_factory
        self._modal: ModuleType | None = None
        self._app: Any = None
        self._app_lock: asyncio.Lock | None = None
        self._build_locks: dict[str, asyncio.Lock] = {}
        self._slots = slots if slots is not None else shared_slots(settings.max_sandboxes)
        self._budget_probe = budget_probe
        self._live: dict[str, _Live] = {}
        self._pool: dict[str, list[_Live]] = {}
        self._images: dict[str, str] = {}
        self._remap: dict[str, str] = {}
        self._dead_images: set[str] = set()
        self._db: Any = None
        self._db_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self.owner = process_owner()
        self._spend_ids = itertools.count(1)
        self._pending_spend: dict[int, float] = {}
        """Non-run spend recorded but not yet in ``executor_spend`` (still counted by the budget)."""
        self._pending_builds: set[int] = set()
        self._lost_spend: dict[str, float] = {}
        """Per UTC day: spend whose ``executor_spend`` row could not be written (still counted)."""

    # -- Modal client ------------------------------------------------------------------------------

    @property
    def modal(self) -> ModuleType:
        """The ``modal`` module (or the injected fake), with the image builder version configured."""
        if self._modal is None:
            if self._factory is not None:
                module = self._factory()
                configure_builder(module)
            else:
                from coverset.executors.modal_images import import_modal

                module = import_modal()
            self._modal = module
        return self._modal

    async def app(self) -> Any:
        """The ``coverset-runner`` App in the configured environment (created on first use)."""
        if self._app is not None:
            return self._app
        if self._app_lock is None:
            self._app_lock = asyncio.Lock()
        async with self._app_lock:
            if self._app is None:
                try:
                    self._app = await self.modal.App.lookup.aio(
                        APP_NAME, create_if_missing=True, environment_name=self.settings.modal_environment
                    )
                except Exception as exc:
                    raise ExecutorError(
                        f"cannot reach Modal app {APP_NAME!r} in environment "
                        f"{self.settings.modal_environment!r}: {type(exc).__name__}: {exc}"
                    ) from exc
        return self._app

    # -- environments ------------------------------------------------------------------------------

    async def ensure_env(self, spec: EnvSpec) -> EnvRef:
        """The env image for ``spec``: cached id (memory, ``images.json``, ``envs``), else a build."""
        image_id = await self._known_image(spec)
        if image_id is None or image_id in self._dead_images:
            image_id = await self._build_env(spec, stale=image_id)
        else:
            self._touch_image(spec.env_hash)
        return EnvRef(env_hash=spec.env_hash, python=spec.python, location=image_id, spec=spec)

    async def cached_env(self, spec: EnvSpec) -> EnvRef | None:
        """The env image for ``spec`` if one is known (never builds): what an untrusted run may use."""
        image_id = await self._known_image(spec)
        if image_id is None or image_id in self._dead_images:
            return None
        self._touch_image(spec.env_hash)
        return EnvRef(env_hash=spec.env_hash, python=spec.python, location=image_id, spec=spec)

    async def _known_image(self, spec: EnvSpec) -> str | None:
        image_id = self._images.get(spec.env_hash) or self._cached_image(spec.env_hash)
        if image_id is None:
            image_id = await self._db_image_id(spec.env_hash)
            if image_id is not None:
                self._remember_image(spec, image_id)
        return image_id

    async def _build_env(self, spec: EnvSpec, *, stale: str | None = None) -> str:
        """Build the env image of ``spec`` (budget, daily build cap; the build is priced as spend).

        Modal's billing of image builds is not documented for the client, so a build is *estimated*
        at the shard limit rate for its wall time and recorded as ``executor_spend`` (kind ``build``).
        """
        lock = self._build_locks.setdefault(spec.env_hash, asyncio.Lock())
        async with lock:
            current = self._images.get(spec.env_hash)
            if current is not None and current != stale and current not in self._dead_images:
                return current
            if self._closed:
                raise CapacityError("the Modal executor is closed")
            await self._guard_budget(amount=self.cost_usd(self._default_resources(), BUILD_RESERVE_S))
            cap = self.settings.max_image_builds_per_day
            built_today = await self._db_builds_today() + len(self._pending_builds)
            if built_today >= cap:
                raise CapacityError(f"image build cap reached ({built_today} of {cap} today)")
            image = await asyncio.to_thread(env_image, spec, spec.plugin_wheel, modal=self.modal)
            app = await self.app()
            started = time.monotonic()
            try:  # a failed build counts too: Modal may have run (and billed) part of it
                image_id = await build_and_record(app, image, verbose=self.verbose, modal=self.modal)
            finally:
                seconds = time.monotonic() - started
                usd = self.cost_usd(self._default_resources(), seconds)
                self.stats.build_usd += usd
                self._record_spend("build", usd)
            self.stats.images_built += 1
            log.info("modal env image %s for %s built in %.1f s", image_id, spec.env_hash[:12], seconds)
            if stale is not None and stale != image_id:
                self._remap[stale] = image_id
            self._remember_image(spec, image_id)
            await self._record_env(spec, image_id)
            return image_id

    def _remember_image(self, spec: EnvSpec, image_id: str) -> None:
        self._images[spec.env_hash] = image_id
        self._dead_images.discard(image_id)
        self._write_image_cache(spec.env_hash, image_id, spec.python)

    # -- trusted snapshots ---------------------------------------------------------------------------

    async def ensure_snapshot(self, env: EnvRef, repo: str, sha: str) -> SnapshotRef | None:
        """A trusted snapshot of ``repo`` at ``sha`` (a default-branch commit) on ``env``.

        Reuses a ready, unexpired ``snapshots`` row for (repo, env, sha); otherwise builds one from
        the mirror's ``git archive``. Refuses commits not reachable from the default branch.
        """
        from coverset.orchestrator.repos import archive, default_branch_sha, merge_base, mirror_path

        repo_row = await self._find_repo(repo)
        if repo_row is None:
            raise ExecutorError(f"unknown repository {repo!r}")
        repo_id, default_branch = repo_row
        existing = await self._ready_snapshot(repo_id, env.env_hash, sha)
        if existing is not None:
            snapshot_id, image_id = existing
            return SnapshotRef(env=env, sha=sha, image_id=image_id, snapshot_id=snapshot_id)
        mirror = mirror_path(self.settings, repo_id)
        tip = await asyncio.to_thread(default_branch_sha, mirror, default_branch)
        if sha != tip and await asyncio.to_thread(merge_base, mirror, sha, tip) != sha:
            raise ExecutorError(
                f"refusing to snapshot {sha[:12]}: not on the default branch {default_branch!r} "
                "(trusted snapshots are built from default-branch commits only)"
            )
        tarball = await asyncio.to_thread(archive, mirror, sha)
        return await self.snapshot_from_archive(env, repo_id, sha, tarball)

    async def snapshot_from_archive(
        self, env: EnvRef, repo_id: int | str, sha: str, tarball: bytes
    ) -> SnapshotRef:
        """Build a snapshot image from ``tarball`` (trusted, default-branch source) and record it."""
        if self._closed:
            raise CapacityError("the Modal executor is closed")
        resources = Resources(
            cpu=self.settings.shard_cpu_limit,
            memory_mib=self.settings.shard_memory_mib,
            network="blocked",
            timeout_s=min(SNAPSHOT_SANDBOX_TIMEOUT_S, self.settings.run_timeout_s),
        )
        await self._guard_budget(amount=self.cost_usd(resources, resources.timeout_s))
        if not await self._slots.take(SLOT_WAIT_S):
            raise CapacityError(
                f"no sandbox slot for a snapshot build (max_sandboxes={self._slots.capacity})"
            )
        live = await self._create_or_rebuild(
            env, env.location, resources, role="snapshot", repo=str(repo_id), run="snapshot"
        )
        worker = ModalWorker(self, live, kill_grace_s=self.kill_grace_s)
        started = time.monotonic()
        try:
            await worker.put_tree(tarball, "repo")
            env_vars = {"PYTHONPATH": _pythonpath(worker, env.spec.source_root if env.spec else "")}
            collect = await worker.run(
                [
                    "python",
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--rootdir={worker.path('repo')}",
                ],
                cwd="repo",
                env=env_vars,
                timeout_s=SNAPSHOT_COLLECT_TIMEOUT_S,
                stdout_cap=self.settings.max_stdout_bytes,
            )
            if collect.timed_out:
                raise ExecutorError("collection timed out while warming the snapshot")
            await worker.run(
                ["python", "-m", "compileall", "-q", "-f", "--invalidation-mode", "checked-hash", "repo"],
                cwd="",
                env={},
                timeout_s=SNAPSHOT_COLLECT_TIMEOUT_S,
                stdout_cap=self.settings.max_stdout_bytes,
            )
            image = await live.sandbox.snapshot_filesystem.aio(timeout=300, ttl=SNAPSHOT_TTL_S)
            image_id = str(image.object_id)
        except ExecutorError:
            raise
        except Exception as exc:
            raise ExecutorError(f"snapshot build failed: {type(exc).__name__}: {exc}") from exc
        finally:
            await worker.release()
        log.info("trusted snapshot %s of %s built in %.1f s", image_id, sha[:12], time.monotonic() - started)
        expires = _now() + timedelta(seconds=SNAPSHOT_TTL_S - SNAPSHOT_MARGIN_S)
        snapshot_id = await self._record_snapshot(repo_id, env, sha, image_id, expires)
        return SnapshotRef(env=env, sha=sha, image_id=image_id, snapshot_id=snapshot_id)

    # -- workers -----------------------------------------------------------------------------------

    async def acquire(
        self,
        base: EnvRef | SnapshotRef,
        n: int,
        resources: Resources,
        ttl_s: int,
        labels: Mapping[str, str],
    ) -> list[Worker]:
        """Up to ``n`` workers: pooled ones first, the rest created concurrently.

        Raises :class:`CapacityError` only when no worker at all can be had (budget exhausted, no
        sandbox slots, or every create failed).
        """
        if n <= 0:
            return []
        if self._closed:
            raise CapacityError("the Modal executor is closed")
        repo = str(labels.get("repo", ""))
        run = str(labels.get("coverset_run", ""))
        await self._guard_budget(run, amount=self._run_exposure(n, resources))
        env = base.env if isinstance(base, SnapshotRef) else base
        image_id = self._current_image(base.image_id if isinstance(base, SnapshotRef) else base.location)
        claimed = await self._claim_pool(repo, image_id, resources, run, n)
        created: list[_Live] = []
        try:
            wanted = n - len(claimed)
            errors: list[BaseException] = []
            if wanted > 0:
                slots = self._slots.try_take(wanted)
                if slots < wanted:
                    message = (
                        f"max_sandboxes={self._slots.capacity} reached: {slots + len(claimed)} of {n} workers"
                    )
                    log.warning(message)
                    self.stats.capacity_errors.append(message)
                created, errors = await self._create_many(
                    image_id, slots, resources, role="shard", repo=repo, run=run, idle_timeout_s=ttl_s
                )
                if not created and errors and all(_is_not_found(e) for e in errors):
                    created, errors = await self._recover_image(base, env, slots, resources, repo, run, ttl_s)
        except BaseException:
            await self._abandon([*claimed, *created])
            raise
        workers: list[Worker] = [
            ModalWorker(self, live, kill_grace_s=self.kill_grace_s) for live in [*claimed, *created]
        ]
        if not workers:
            if errors:
                raise CapacityError(
                    f"no Modal sandbox could be created: {_error_name(errors[0])}: {errors[0]}"
                )
            raise CapacityError(f"no Modal sandbox slot free (max_sandboxes={self._slots.capacity})")
        return workers

    async def _recover_image(
        self,
        base: EnvRef | SnapshotRef,
        env: EnvRef,
        k: int,
        resources: Resources,
        repo: str,
        run: str,
        ttl_s: int,
    ) -> tuple[list[_Live], list[BaseException]]:
        """A create hit ``NotFoundError``: fall back from the snapshot to the env image, else rebuild once."""
        k = self._slots.try_take(k)
        if not k:
            return [], [CapacityError("no sandbox slots left")]
        if isinstance(base, SnapshotRef):
            log.warning("snapshot image %s is gone; falling back to the env image", base.image_id)
            self._dead_images.add(base.image_id)
            self._expire_snapshot_later(base.image_id)
            created, errors = await self._create_many(
                self._current_image(env.location),
                k,
                resources,
                role="shard",
                repo=repo,
                run=run,
                idle_timeout_s=ttl_s,
            )
            if created or not all(_is_not_found(e) for e in errors):
                return created, errors
            k = self._slots.try_take(k)
            if not k:
                return [], errors
        if env.spec is None:
            self._slots.give_back(k)
            return [], [ExecutorError(f"env image {env.location} is gone and its spec is unknown")]
        log.warning("env image %s is gone; rebuilding it once", env.location)
        self._dead_images.add(self._current_image(env.location))
        self.stats.rebuilds += 1
        try:
            new_id = await self._build_env(env.spec, stale=self._current_image(env.location))
        except ExecutorError as exc:
            self._slots.give_back(k)
            return [], [exc]
        except BaseException:
            self._slots.give_back(k)
            raise
        self._remap[env.location] = new_id
        return await self._create_many(
            new_id, k, resources, role="shard", repo=repo, run=run, idle_timeout_s=ttl_s
        )

    async def _create_or_rebuild(
        self, env: EnvRef, image_id: str, resources: Resources, *, role: str, repo: str, run: str
    ) -> _Live:
        """Create one sandbox (slot already taken); rebuild the env image once on ``NotFoundError``."""
        created, errors = await self._create_many(
            self._current_image(image_id),
            1,
            resources,
            role=role,
            repo=repo,
            run=run,
            idle_timeout_s=self.settings.pool_ttl_s,
        )
        if not created and errors and _is_not_found(errors[0]) and env.spec is not None:
            if not self._slots.try_take(1):
                raise CapacityError("no sandbox slot left")
            self.stats.rebuilds += 1
            self._dead_images.add(self._current_image(image_id))
            try:
                new_id = await self._build_env(env.spec, stale=self._current_image(image_id))
            except BaseException:
                self._slots.give_back(1)
                raise
            created, errors = await self._create_many(
                new_id, 1, resources, role=role, repo=repo, run=run, idle_timeout_s=self.settings.pool_ttl_s
            )
        if not created:
            error = errors[0] if errors else ExecutorError("unknown error")
            raise CapacityError(f"cannot create a {role} sandbox: {_error_name(error)}: {error}")
        return created[0]

    async def _create_many(
        self,
        image_id: str,
        k: int,
        resources: Resources,
        *,
        role: str,
        repo: str,
        run: str,
        idle_timeout_s: int | None,
        extra_timeout_s: int = 0,
    ) -> tuple[list[_Live], list[BaseException]]:
        """Create ``k`` sandboxes concurrently (``k`` slots already taken; failed ones are returned).

        If the caller is cancelled meanwhile, the creates still in flight are cancelled, every slot
        of a failed or cancelled create is given back, the sandboxes already created are terminated
        (which frees their slots) and the cancellation propagates: nothing is left behind.
        """
        if k <= 0:
            return [], []
        tasks = [
            asyncio.ensure_future(
                self._create_one(
                    image_id,
                    resources,
                    role=role,
                    repo=repo,
                    run=run,
                    idle_timeout_s=idle_timeout_s,
                    extra_timeout_s=extra_timeout_s,
                )
            )
            for _ in range(k)
        ]
        try:
            await asyncio.wait(tasks)
        except asyncio.CancelledError:
            cleanup = asyncio.ensure_future(self._abandon_creates(tasks))
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)
            raise
        created: list[_Live] = []
        errors: list[BaseException] = []
        for task in tasks:
            result: BaseException | _Live = (
                asyncio.CancelledError() if task.cancelled() else (task.exception() or task.result())
            )
            if isinstance(result, _Live):
                created.append(result)
                continue
            self._slots.give_back(1)
            errors.append(result)
            if isinstance(result, asyncio.CancelledError):
                continue
            message = f"{_error_name(result)}: {result}"
            if _error_name(result) in _CAPACITY_ERRORS:
                log.warning("sandbox create failed (capacity): %s", message)
                self.stats.capacity_errors.append(message)
            elif not _is_not_found(result):
                log.warning("sandbox create failed: %s", message)
        if any(isinstance(e, asyncio.CancelledError) for e in errors):
            await self._terminate_all(created)
            raise asyncio.CancelledError()
        return created, errors

    async def _abandon_creates(self, tasks: list[asyncio.Future[_Live]]) -> None:
        """The caller of :meth:`_create_many` was cancelled: undo every create (see there)."""
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        created = [t.result() for t in tasks if not t.cancelled() and t.exception() is None]
        self._slots.give_back(len(tasks) - len(created))
        await self._terminate_all(created)

    async def _abandon(self, lives: list[_Live]) -> None:
        """Terminate ``lives`` even while the caller is being cancelled (shielded)."""
        if not lives:
            return
        for entries in self._pool.values():
            entries[:] = [e for e in entries if e not in lives]
        cleanup = asyncio.ensure_future(self._terminate_all(lives))
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup)

    def _tags(self, repo: str, run: str, role: str, created: int) -> dict[str, str]:
        return {
            "coverset_instance": self.instance_id,
            "repo": repo,
            "run": run,
            "role": role,
            "created": str(created),
            "owner": self.owner,
        }

    async def _create_one(
        self,
        image_id: str,
        resources: Resources,
        *,
        role: str,
        repo: str,
        run: str,
        idle_timeout_s: int | None,
        extra_timeout_s: int = 0,
    ) -> _Live:
        modal = self.modal
        app = await self.app()
        created = int(time.time())
        options = sandbox_options(
            self.settings,
            resources,
            self._tags(repo, run, role, created),
            idle_timeout_s=idle_timeout_s,
            extra_timeout_s=extra_timeout_s,
        )
        image = image_from_id(image_id, modal=modal)
        sandbox = await modal.Sandbox.create.aio(app=app, image=image, **options)
        # Registered before anything else is awaited: from here on close() and reap() know it.
        live = _Live(
            sandbox=sandbox,
            sandbox_id=str(sandbox.object_id),
            role=role,
            repo=repo,
            run=run,
            image_id=image_id,
            network=resources.network,
            cpu_limit=options["cpu"][1],
            memory_mib=options["memory"][1],
            created_wall=created,
        )
        self._live[live.sandbox_id] = live
        self.stats.created += 1
        return live

    async def create_sandbox(
        self, image: Any, *, role: str, resources: Resources | None = None, repo: str = "-", run: str = "-"
    ) -> tuple[Any, Callable[[], Awaitable[None]]]:
        """One guarded sandbox from an arbitrary (lazy) image, for smoke tests and benchmarks.

        Returns the sandbox and an async ``terminate`` callback (call it in a ``finally``).
        """
        if self._closed:
            raise CapacityError("the Modal executor is closed")
        res = resources or Resources(
            cpu=self.settings.shard_cpu_limit,
            memory_mib=self.settings.shard_memory_mib,
            network="blocked",
            timeout_s=min(600, self.settings.run_timeout_s),
        )
        await self._guard_budget(amount=self.cost_usd(res, min(res.timeout_s, self.settings.run_timeout_s)))
        if not await self._slots.take(SLOT_WAIT_S):
            raise CapacityError(f"no sandbox slot (max_sandboxes={self._slots.capacity})")
        created = int(time.time())
        tags = self._tags(repo, run, role, created)
        options = sandbox_options(self.settings, res, tags, idle_timeout_s=self.settings.pool_ttl_s)
        try:
            sandbox = await self.modal.Sandbox.create.aio(app=await self.app(), image=image, **options)
        except BaseException:
            self._slots.give_back(1)
            raise
        live = _Live(
            sandbox=sandbox,
            sandbox_id=str(sandbox.object_id),
            role=role,
            repo=repo,
            run=run,
            image_id="-",
            network=res.network,
            cpu_limit=options["cpu"][1],
            memory_mib=options["memory"][1],
            created_wall=created,
        )
        self._live[live.sandbox_id] = live
        self.stats.created += 1

        async def terminate() -> None:
            await self._terminate(live)

        return sandbox, terminate

    def worker_for(self, sandbox: Any) -> ModalWorker:
        """A :class:`ModalWorker` over a sandbox this executor created (smoke tests, benchmarks)."""
        live = self._live.get(str(sandbox.object_id))
        if live is None:
            raise ExecutorError("not a sandbox of this executor")
        return ModalWorker(self, live, kill_grace_s=self.kill_grace_s)

    def _current_image(self, image_id: str) -> str:
        seen = set()
        while image_id in self._remap and image_id not in seen:
            seen.add(image_id)
            image_id = self._remap[image_id]
        return image_id

    async def _terminate(self, live: _Live) -> None:
        """Terminate one sandbox (idempotent per sandbox), free its slot and account its cost."""
        if self._live.pop(live.sandbox_id, None) is None:
            return
        seconds = max(0.0, time.monotonic() - live.created_at)
        if live.role != "shard":
            self._record_spend(spend_kind(live.role), live.rate() * seconds, live.sandbox_id)
        self.stats.lifetime_usd += live.rate() * seconds
        self.stats.sandbox_seconds += seconds
        try:
            await live.sandbox.terminate.aio()
        except Exception as exc:  # already gone, or a transient API error: the reaper catches leftovers
            log.warning("terminating sandbox %s failed: %s: %s", live.sandbox_id, _error_name(exc), exc)
        finally:
            self._slots.give_back(1)
            self.stats.terminated += 1

    async def _terminate_all(self, lives: list[_Live]) -> None:
        await asyncio.gather(*(self._terminate(live) for live in lives), return_exceptions=True)

    # -- the warm pool -----------------------------------------------------------------------------

    async def fill_pool(self, base: EnvRef | SnapshotRef, repo: str, size: int, ttl_s: int) -> int:
        """Top ``repo``'s pool up to ``size`` sandboxes on ``base``; return how many were created.

        Pooled sandboxes use the configured network policy and resource ceilings, an idle timeout of
        ``ttl_s`` (Modal terminates them if nobody claims them) and are never exec'd before a claim.
        """
        if size <= 0 or self._closed:
            return 0
        ttl = ttl_s or self.settings.pool_ttl_s
        try:
            await self._guard_budget(amount=self.idle_cost_usd(ttl or self.settings.run_timeout_s) * size)
        except CapacityError as exc:
            log.warning("not filling the pool: %s", exc)
            return 0
        image_id = self._current_image(base.image_id if isinstance(base, SnapshotRef) else base.location)
        entries = self._pool.setdefault(repo, [])
        stale = [e for e in entries if e.image_id != image_id or self._expired(e)]
        if stale:
            for entry in stale:
                entries.remove(entry)
            await self._terminate_all(stale)
        want = size - len(entries)
        k = self._slots.try_take(max(0, want))
        if k <= 0:
            return 0
        resources = Resources(
            cpu=self.settings.shard_cpu_limit,
            memory_mib=self.settings.shard_memory_mib,
            network=self.settings.sandbox_network,
            timeout_s=self.settings.run_timeout_s,
        )
        created, _ = await self._create_many(
            image_id,
            k,
            resources,
            role="pool",
            repo=repo,
            run="pool",
            idle_timeout_s=ttl,
            extra_timeout_s=ttl,  # a claimed sandbox must still survive the run's full timeout
        )
        if self._closed:  # closed while creating: close() already ran, so clean up here
            await self._terminate_all(created)
            return 0
        entries.extend(created)
        return len(created)

    async def drain_pool(self, repo: str | None = None) -> int:
        """Terminate pooled sandboxes of ``repo`` (all repos when None); return how many."""
        repos = [repo] if repo is not None else list(self._pool)
        doomed: list[_Live] = []
        for name in repos:
            doomed.extend(self._pool.pop(name, []))
        await self._terminate_all(doomed)
        return len(doomed)

    def pool_size(self, repo: str | None = None) -> int:
        """Pooled (unclaimed) sandboxes held for ``repo`` (or all repos)."""
        if repo is not None:
            return len(self._pool.get(repo, []))
        return sum(len(v) for v in self._pool.values())

    def _expired(self, entry: _Live) -> bool:
        ttl = self.settings.pool_ttl_s
        return ttl > 0 and time.monotonic() - entry.created_at >= ttl

    async def _claim_pool(
        self, repo: str, image_id: str, resources: Resources, run: str, n: int
    ) -> list[_Live]:
        """Take up to ``n`` live pooled sandboxes that match ``repo``, image, network and limits."""
        entries = self._pool.get(repo)
        if not entries:
            log.debug(
                "no pooled sandboxes for repo %r (pools: %s)",
                repo,
                {k: len(v) for k, v in self._pool.items()},
            )
            return []
        cpu = max(CPU_REQUEST, min(resources.cpu, self.settings.shard_cpu_limit))
        memory = max(MEMORY_REQUEST_MIB, min(resources.memory_mib, self.settings.shard_memory_mib))
        candidates: list[_Live] = []
        for entry in list(entries):
            if len(candidates) >= n:
                break
            if self._expired(entry):
                entries.remove(entry)
                await self._terminate(entry)
                continue
            if (
                entry.image_id == image_id
                and entry.network == resources.network
                and entry.cpu_limit == cpu
                and entry.memory_mib == memory
            ):
                entries.remove(entry)
                candidates.append(entry)
            else:
                log.debug(
                    "pooled %s does not match: image %s/%s network %s/%s cpu %s/%s memory %s/%s",
                    entry.sandbox_id, entry.image_id, image_id, entry.network, resources.network,
                    entry.cpu_limit, cpu, entry.memory_mib, memory,
                )  # fmt: skip
        if not candidates:
            return []
        try:
            alive = await asyncio.gather(*(self._still_running(e) for e in candidates))
        except BaseException:  # cancelled: the candidates left the pool, so terminate them
            await self._abandon(candidates)
            raise
        claimed: list[_Live] = []
        for entry, ok in zip(candidates, alive, strict=True):
            if not ok:
                log.debug("pooled %s is no longer running", entry.sandbox_id)
                await self._terminate(entry)
                continue
            entry.claimed = True
            entry.role = "shard"
            entry.run = run
            # The pooled time so far was idle (billed at requests) and belongs to no run.
            idle_s = time.monotonic() - entry.created_at
            idle_usd = rate_usd_per_s(CPU_REQUEST, MEMORY_REQUEST_MIB) * idle_s
            self._record_spend("pool", idle_usd, entry.sandbox_id)
            self.stats.lifetime_usd += idle_usd
            self.stats.sandbox_seconds += idle_s
            entry.created_at = time.monotonic()
            claimed.append(entry)
        try:
            await asyncio.gather(*(self._retag(e) for e in claimed), return_exceptions=True)
        except BaseException:  # cancelled before the caller got them: terminate them
            await self._abandon(claimed)
            raise
        self.stats.pool_claims += len(claimed)
        return claimed

    async def _still_running(self, entry: _Live) -> bool:
        try:
            return await entry.sandbox.poll.aio() is None
        except Exception:
            return False

    async def _retag(self, entry: _Live) -> None:
        tags = self._tags(entry.repo, entry.run, "shard", entry.created_wall)
        try:
            await entry.sandbox.set_tags.aio(tags)
        except Exception as exc:  # the reaper still keeps it: it is in this process's live set
            log.debug("retagging %s failed: %s", entry.sandbox_id, exc)

    # -- reaping -----------------------------------------------------------------------------------

    def natural_lifetime_s(self, role: str) -> int | None:
        """How long a sandbox of ``role`` may legitimately live without a run: pool sandboxes their
        idle TTL plus a grace, snapshot builds, smoke tests and benchmarks the run timeout; None for
        shards (they are judged by their run)."""
        if role == "shard":
            return None
        if role == "pool":
            return (self.settings.pool_ttl_s or self.settings.run_timeout_s) + POOL_REAP_GRACE_S
        return self.settings.run_timeout_s

    def _young(self, tags: Mapping[str, str], now: float) -> bool:
        """Whether a sandbox may still belong to another process (see :meth:`natural_lifetime_s`)."""
        lifetime = self.natural_lifetime_s(str(tags.get("role", "shard")))
        try:
            created = int(str(tags.get("created", "")))
        except ValueError:
            return False  # no creation time (an older coverset): treat it as old
        return lifetime is not None and now - created < lifetime

    async def reap(
        self, *, active_run_ids: set[str], all: bool = False, any_instance: bool = False
    ) -> ReapReport:
        """Terminate sandboxes tagged with this instance that no active run (or this process) holds.

        Pool, snapshot, smoke and benchmark sandboxes younger than :meth:`natural_lifetime_s` are kept:
        another process sharing the data dir (``serve``, ``coverset warm``, ``modal smoke``) may own
        them. With ``all``, terminate every one of them, including this process's pool and workers and
        other processes' young sandboxes. With ``any_instance``, look at every sandbox in the App that
        carries a ``coverset_instance`` tag (other data dirs: benchmarks, scratch demos) instead of only
        this installation's; their runs are unknown here, so their shards are terminated.
        """
        found = terminated = kept = 0
        errors: list[str] = []
        app = await self.app()
        mine = set() if all else set(self._live)
        filters = {} if any_instance else {"tags": {"coverset_instance": self.instance_id}}
        listing = self.modal.Sandbox.list.aio(app_id=app.app_id, **filters)
        now = time.time()
        async for sandbox in listing:
            sandbox_id = str(sandbox.object_id)
            try:
                if await sandbox.poll.aio() is not None:
                    continue  # finished; Modal lists terminated sandboxes for a few seconds
            except Exception:
                pass
            try:
                tags = await sandbox.get_tags.aio()
            except Exception:
                tags = {}
            if any_instance and "coverset_instance" not in tags:
                continue  # not a coverset sandbox
            found += 1
            if not all and (
                sandbox_id in mine or tags.get("run") in active_run_ids or self._young(tags, now)
            ):
                kept += 1
                continue
            try:
                live = self._live.get(sandbox_id)
                if live is not None:
                    for entries in self._pool.values():
                        if live in entries:
                            entries.remove(live)
                    await self._terminate(live)
                else:
                    await sandbox.terminate.aio()
                terminated += 1
            except Exception as exc:
                errors.append(f"{sandbox_id}: {_error_name(exc)}: {exc}")
        return ReapReport(found=found, terminated=terminated, kept=kept, errors=tuple(errors))

    async def list_running(self) -> list[dict[str, Any]]:
        """Running sandboxes tagged with this instance: ``{id, tags}`` each (for checks and the CLI)."""
        app = await self.app()
        rows: list[dict[str, Any]] = []
        async for sandbox in self.modal.Sandbox.list.aio(
            app_id=app.app_id, tags={"coverset_instance": self.instance_id}
        ):
            try:
                if await sandbox.poll.aio() is not None:
                    continue
            except Exception:
                pass
            try:
                tags = await sandbox.get_tags.aio()
            except Exception:
                tags = {}
            rows.append({"id": str(sandbox.object_id), "tags": dict(tags)})
        return rows

    # -- rate card, capabilities, lifecycle ----------------------------------------------------------

    def capabilities(self) -> Capabilities:
        """Egress follows the network policy; pools and filesystem snapshots are supported."""
        return Capabilities(
            name=self.name,
            egress=self.settings.sandbox_network != "blocked",
            warm_pool=True,
            fs_snapshot=True,
            max_workers=self.settings.max_sandboxes,
        )

    def cost_usd(self, resources: Resources, seconds: float) -> float:
        """Conservative estimate for a running shard: its CPU and memory *limits* for ``seconds``."""
        cpu = max(CPU_REQUEST, min(resources.cpu, self.settings.shard_cpu_limit))
        memory = max(MEMORY_REQUEST_MIB, min(resources.memory_mib, self.settings.shard_memory_mib))
        return rate_usd_per_s(cpu, memory) * max(0.0, seconds)

    def idle_cost_usd(self, seconds: float) -> float:
        """An idle (pooled, never exec'd) sandbox: billed at its requests, 0.125 core and 0.5 GiB."""
        return rate_usd_per_s(CPU_REQUEST, MEMORY_REQUEST_MIB) * max(0.0, seconds)

    def live_cost_usd(self) -> float:
        """Estimated spend of the sandboxes this process has running right now."""
        now = time.monotonic()
        return sum(live.rate() * (now - live.created_at) for live in self._live.values())

    @property
    def live_sandboxes(self) -> int:
        """Sandboxes this process created and has not terminated."""
        return len(self._live)

    async def close(self) -> None:
        """Terminate every sandbox this process holds (pool included) and flush pending DB writes.

        A closed executor creates nothing more: ``acquire`` and ``create_sandbox`` raise
        :class:`CapacityError`, ``fill_pool`` returns 0.
        """
        self._closed = True
        self._pool.clear()
        await self._terminate_all(list(self._live.values()))
        if self._db_tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*self._db_tasks, return_exceptions=True), timeout=15)
        if self._db is not None:
            with contextlib.suppress(Exception):
                await self._db.engine.dispose()
            self._db = None

    # -- budget ------------------------------------------------------------------------------------

    async def spent_today_usd(self, *, exclude_run: str | None = None) -> float:
        """Today's estimated Modal spend, as the orchestrator's budget sees it plus what it cannot see.

        :func:`coverset.orchestrator.budget.spent_today` sums ``runs.cost_estimate`` (finished runs'
        costs and in-flight runs' reservations, minus ``exclude_run``, the run asking for workers,
        exactly like ``budget.reserve``) and today's ``executor_spend`` rows (non-run spend of every
        process). On top come what only this process knows: the accrued time of the sandboxes it holds
        for no run (pool, snapshot builds, smoke tests) and non-run spend not yet written.
        """
        if self._budget_probe is not None:
            recorded = await self._budget_probe()
        else:
            recorded = await self._db_spent_today(exclude_run)
        now = time.monotonic()
        unattributed = sum(
            live.rate() * (now - live.created_at) for live in self._live.values() if live.role != "shard"
        )
        unwritten = sum(self._pending_spend.values()) + self._lost_spend.get(self._today(), 0.0)
        return recorded + unattributed + unwritten

    async def _guard_budget(self, run: str | None = None, *, amount: float = 0.0) -> None:
        """Refuse when today's spend has reached the budget or ``amount`` more would pass it."""
        budget = self.settings.modal_daily_budget_usd
        spent = await self.spent_today_usd(exclude_run=run or None)
        if spent >= budget or spent + max(0.0, amount) > budget:
            raise CapacityError(
                f"budget exhausted (${spent:.2f} of ${budget:.2f} today; this needs up to ${amount:.2f})"
            )

    def _run_exposure(self, n: int, resources: Resources) -> float:
        """What ``n`` more workers of a run may cost: their run timeout at the limit rate, at most
        the per-run cap (``COVERSET_MAX_RUN_COST_USD``, which the runner enforces live)."""
        from coverset.orchestrator.budget import run_cost_cap

        worst = n * self.cost_usd(resources, min(resources.timeout_s, self.settings.run_timeout_s))
        return min(worst, run_cost_cap(self.settings))

    def _default_resources(self) -> Resources:
        return Resources(
            cpu=self.settings.shard_cpu_limit,
            memory_mib=self.settings.shard_memory_mib,
            network="blocked",
            timeout_s=self.settings.run_timeout_s,
        )

    def _today(self) -> str:
        return self.settings.now().date().isoformat()

    def _record_spend(self, kind: str, usd: float, sandbox_id: str | None = None) -> None:
        """Persist non-run spend as an ``executor_spend`` row (in the background, retried); until it
        lands it is counted from memory by :meth:`spent_today_usd`."""
        usd = max(0.0, float(usd))
        if usd <= 0.0 and kind != "build":
            return
        self.stats.untracked_usd += usd
        token = next(self._spend_ids)
        self._pending_spend[token] = usd
        if kind == "build":
            self._pending_builds.add(token)
        day = self._today()

        async def write(session: Any) -> None:
            from coverset.orchestrator.budget import record_spend

            record_spend(session, self.settings, self.name, kind, usd, sandbox_id=sandbox_id)

        async def persist() -> None:
            try:
                for delay in (0.0, *DB_RETRY_DELAYS_S):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        await self._write_once(write)
                        return
                    except Exception as exc:
                        if "no such table" in str(exc):  # no database here (smoke, scratch dirs)
                            return
                        log.debug("recording %s spend failed: %s", kind, exc)
                log.warning("could not record $%.4f of %s spend; counting it in memory", usd, kind)
                self._lost_spend[day] = self._lost_spend.get(day, 0.0) + usd
            finally:
                self._pending_spend.pop(token, None)
                self._pending_builds.discard(token)

        try:
            task = asyncio.get_running_loop().create_task(persist())
        except RuntimeError:  # no event loop: keep it in memory for this process
            self._pending_spend.pop(token, None)
            self._pending_builds.discard(token)
            self._lost_spend[day] = self._lost_spend.get(day, 0.0) + usd
            return
        self._db_tasks.add(task)
        task.add_done_callback(self._db_tasks.discard)

    # -- persistence: images.json and the control-plane database ------------------------------------

    @property
    def _images_path(self) -> Path:
        return self.settings.data_dir / "modal" / IMAGES_FILE

    def image_records(self) -> dict[str, dict[str, Any]]:
        """``images.json``: ``env_hash -> {image_id, python, built_at, last_used}``."""
        try:
            data = json.loads(self._images_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _cached_image(self, env_hash: str) -> str | None:
        record = self.image_records().get(env_hash)
        image_id = record.get("image_id") if record else None
        if isinstance(image_id, str) and image_id:
            self._images[env_hash] = image_id
            return image_id
        return None

    def _write_image_cache(self, env_hash: str, image_id: str, python: str) -> None:
        records = self.image_records()
        stamp = _now().isoformat()
        previous = records.get(env_hash, {})
        built = previous.get("built_at") if previous.get("image_id") == image_id else stamp
        records[env_hash] = {"image_id": image_id, "python": python, "built_at": built, "last_used": stamp}
        self._save_image_records(records)

    def _touch_image(self, env_hash: str) -> None:
        records = self.image_records()
        if env_hash in records:
            records[env_hash]["last_used"] = _now().isoformat()
            self._save_image_records(records)

    def _save_image_records(self, records: Mapping[str, Any]) -> None:
        path = self._images_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot write %s: %s", path, exc)

    def stale_images(self, days: int = 30) -> list[dict[str, Any]]:
        """Env images not used for ``days`` days: candidates for manual deletion (Modal has no API)."""
        cutoff = _now() - timedelta(days=days)
        stale = []
        for env_hash, record in sorted(self.image_records().items()):
            try:
                last = datetime.fromisoformat(str(record.get("last_used")))
            except ValueError:
                last = None
            if last is None or last < cutoff:
                stale.append({"env_hash": env_hash, **record})
        return stale

    def _database(self) -> Any:
        if self._db is None:
            from coverset.db.session import Database

            self._db = Database.from_url(self.settings.database_url)
        return self._db

    async def _db_read(self, query: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            async with self._database().session() as session:
                return await query(session)
        except Exception as exc:  # no database (smoke tests, scratch dirs) or not migrated yet
            log.debug("modal executor database read failed: %s", exc)
            return None

    async def _db_spent_today(self, exclude_run: str | None = None) -> float:
        from coverset.orchestrator.budget import spent_today

        async def query(session: Any) -> float:
            return await spent_today(session, self.settings, "modal", exclude=exclude_run)

        value = await self._db_read(query)
        return float(value or 0.0)

    async def _db_builds_today(self) -> int:
        from coverset.orchestrator.budget import builds_today

        async def query(session: Any) -> int:
            return await builds_today(session, self.settings, self.name)

        value = await self._db_read(query)
        return int(value or 0)

    async def _db_image_id(self, env_hash: str) -> str | None:
        from sqlalchemy import select

        from coverset.db.models import Env

        async def query(session: Any) -> str | None:
            return await session.scalar(select(Env.image_id).where(Env.env_hash == env_hash))

        value = await self._db_read(query)
        return str(value) if value else None

    async def _find_repo(self, repo: str) -> tuple[int, str] | None:
        from sqlalchemy import select

        from coverset.db.models import Repo

        async def query(session: Any) -> tuple[int, str] | None:
            row = None
            if repo.isdigit():
                row = await session.get(Repo, int(repo))
            if row is None:
                row = await session.scalar(select(Repo).where(Repo.full_name == repo))
            return (row.id, row.default_branch) if row is not None else None

        return await self._db_read(query)

    async def _ready_snapshot(self, repo_id: int, env_hash: str, sha: str) -> tuple[int, str] | None:
        from sqlalchemy import select

        from coverset.db.models import Snapshot

        async def query(session: Any) -> tuple[int, str] | None:
            row = await session.scalar(
                select(Snapshot)
                .where(
                    Snapshot.repo_id == repo_id,
                    Snapshot.env_hash == env_hash,
                    Snapshot.sha == sha,
                    Snapshot.status == "ready",
                    Snapshot.image_id.is_not(None),
                )
                .order_by(Snapshot.created_at.desc())
                .limit(1)
            )
            if row is None or row.image_id is None or row.image_id in self._dead_images:
                return None
            if row.expires_at is not None and row.expires_at <= _now():
                return None
            return row.id, row.image_id

        return await self._db_read(query)

    async def _db_write(self, label: str, write: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run ``write`` in its own transaction now; on a locked database retry in the background.

        The planner may hold a write transaction while it plans (SQLite allows one writer), so a
        write that cannot get the lock within the busy timeout is retried after the planner commits.
        """
        try:
            return await self._write_once(write)
        except Exception as exc:
            if "no such table" in str(exc):
                log.debug("skipping %s: database not migrated", label)
                return None
            log.info("deferring %s: %s", label, exc)
        task = asyncio.get_running_loop().create_task(self._write_later(label, write))
        self._db_tasks.add(task)
        task.add_done_callback(self._db_tasks.discard)
        return None

    async def _write_once(self, write: Callable[[Any], Awaitable[Any]]) -> Any:
        async with self._database().session() as session:
            result = await write(session)
            await session.commit()
            return result

    async def _write_later(self, label: str, write: Callable[[Any], Awaitable[Any]]) -> None:
        for delay in DB_RETRY_DELAYS_S:
            await asyncio.sleep(delay)
            try:
                await self._write_once(write)
                return
            except Exception as exc:
                log.debug("%s still failing: %s", label, exc)
        log.warning("gave up on %s after %d attempts", label, len(DB_RETRY_DELAYS_S) + 1)

    async def _record_env(self, spec: EnvSpec, image_id: str) -> None:
        from sqlalchemy.dialects.sqlite import insert

        from coverset.db.models import Env

        async def write(session: Any) -> None:
            if session.bind.dialect.name == "sqlite":
                statement = insert(Env).values(
                    env_hash=spec.env_hash, python=spec.python, image_id=image_id, created_at=_now()
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[Env.env_hash], set_={"image_id": image_id}
                    )
                )
                return
            row = await session.get(Env, spec.env_hash)
            if row is None:
                session.add(
                    Env(env_hash=spec.env_hash, python=spec.python, image_id=image_id, created_at=_now())
                )
            else:
                row.image_id = image_id

        await self._db_write(f"recording env image {image_id}", write)

    async def _record_snapshot(
        self, repo_id: int | str, env: EnvRef, sha: str, image_id: str, expires_at: datetime
    ) -> int | None:
        from coverset.db.models import Env, Snapshot

        async def write(session: Any) -> int:
            if await session.get(Env, env.env_hash) is None:
                session.add(
                    Env(env_hash=env.env_hash, python=env.python, image_id=env.location, created_at=_now())
                )
                await session.flush()
            row = Snapshot(
                repo_id=int(repo_id),
                env_hash=env.env_hash,
                sha=sha,
                image_id=image_id,
                status="ready",
                created_at=_now(),
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

        if not str(repo_id).isdigit():
            return None
        return await self._db_write(f"recording snapshot {image_id}", write)

    def _expire_snapshot_later(self, image_id: str) -> None:
        from sqlalchemy import update

        from coverset.db.models import Snapshot

        async def write(session: Any) -> None:
            await session.execute(
                update(Snapshot).where(Snapshot.image_id == image_id).values(status="expired")
            )

        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(
                self._db_write(f"expiring snapshot {image_id}", write)
            )
            self._db_tasks.add(task)
            task.add_done_callback(self._db_tasks.discard)


def _pythonpath(worker: ModalWorker, source_root: str) -> str:
    root = worker.path("repo")
    if source_root in ("", "."):
        return root
    return f"{worker.path(f'repo/{source_root}')}:{root}"


# -- the worker -------------------------------------------------------------------------------------


class _ChunkStream:
    """Adapts a Modal output stream (async iterator of byte chunks) to ``read(n)`` for :class:`_Pump`."""

    def __init__(self, stream: Any) -> None:
        self._iter = stream.__aiter__()
        self._done = False

    async def read(self, _n: int) -> bytes:
        while not self._done:
            try:
                chunk = await self._iter.__anext__()
            except StopAsyncIteration:
                self._done = True
                break
            except Exception as exc:  # the sandbox went away mid-stream
                log.debug("output stream ended: %s", exc)
                self._done = True
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if chunk:
                return bytes(chunk)
        return b""


class ModalWorker:
    """One Modal sandbox: files under ``/work``, commands via ``exec`` with a scrubbed environment."""

    def __init__(self, executor: ModalExecutor, live: _Live, *, kill_grace_s: float = KILL_GRACE_S) -> None:
        self.id = live.sandbox_id
        self.root = WORK_ROOT
        self._executor = executor
        self._live = live
        self._sandbox = live.sandbox
        self._kill_grace_s = kill_grace_s
        self._execs: dict[str, Any] = {}
        self._cancelled: set[str] = set()
        self._dirs: set[str] = set()
        self._released = False

    @property
    def sandbox(self) -> Any:
        """The underlying ``modal.Sandbox``."""
        return self._sandbox

    def path(self, rel: str) -> str:
        """Absolute in-sandbox path of ``rel``; absolute paths and ``..`` segments are refused."""
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or "\0" in rel:
            raise ExecutorError(f"worker paths must be relative and inside the worker: {rel!r}")
        return str(PurePosixPath(WORK_ROOT).joinpath(*pure.parts)) if pure.parts else WORK_ROOT

    def child_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """The complete environment of a child: the fixed base plus the sanitised ``env``."""
        base = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": f"{WORK_ROOT}/home",
            "TMPDIR": f"{WORK_ROOT}/tmp",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPYCACHEPREFIX": f"{WORK_ROOT}/{PYCACHE_DIR}",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
        }
        return {**base, **sanitize_env(env)}

    def _check(self) -> None:
        if self._released:
            raise ExecutorError(f"worker {self.id} was released")

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
        """Exec ``cmd`` in ``cwd``; stream both outputs line by line; kill on timeout, cap or cancel.

        The command runs under ``env -i`` so nothing from the container's own environment leaks in,
        with a unique ``COVERSET_EXEC_ID`` that :meth:`cancel` uses to signal it and its children.
        """
        self._check()
        await self._mkdir(WORK_ROOT)  # exec needs an existing workdir; sub-directories come from put_tree
        exec_id = uuid.uuid4().hex
        child = {**self.child_env(env), "COVERSET_EXEC_ID": exec_id}
        argv = ["env", "-i", *(f"{k}={v}" for k, v in child.items()), "sh", "-c", _START_SHELL, "sh", *cmd]
        backstop = int(math.ceil(max(1.0, timeout_s))) + int(self._kill_grace_s * 2) + EXEC_BACKSTOP_S
        started = time.time()
        try:
            proc = await self._sandbox.exec.aio(*argv, workdir=self.path(cwd), timeout=backstop, text=False)
        except Exception as exc:
            raise ExecutorError(
                f"cannot exec {cmd[0]!r} in sandbox {self.id}: {_error_name(exc)}: {exc}"
            ) from exc
        self._execs[exec_id] = proc
        pump = _Pump(on_line, stdout_cap, on_overflow=lambda: self._spawn_kill(exec_id, proc))
        readers = [
            asyncio.create_task(pump.read(_ChunkStream(proc.stdout), "stdout")),  # type: ignore[arg-type]
            asyncio.create_task(pump.read(_ChunkStream(proc.stderr), "stderr")),  # type: ignore[arg-type]
        ]
        timed_out = False
        exit_code: int | None = None
        try:
            try:
                exit_code = await asyncio.wait_for(self._wait(proc), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                exit_code = await self._kill(exec_id, proc)
            _done, pending = await asyncio.wait(readers, timeout=_DRAIN_AFTER_EXIT_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._kill(exec_id, proc))
            for task in readers:
                task.cancel()
            raise
        finally:
            self._execs.pop(exec_id, None)
        cancelled = exec_id in self._cancelled
        self._cancelled.discard(exec_id)
        return RunResult(
            exit_code=exit_code,
            started_at=started,
            finished_at=time.time(),
            stdout_bytes=pump.total,
            truncated=pump.overflowed,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    async def _wait(self, proc: Any) -> int | None:
        try:
            return await proc.wait.aio()
        except Exception as exc:  # the sandbox was terminated or the exec vanished
            log.debug("exec wait failed in %s: %s", self.id, exc)
            return None

    def _spawn_kill(self, exec_id: str, proc: Any) -> None:
        asyncio.get_running_loop().create_task(self._kill(exec_id, proc))

    async def _kill(self, exec_id: str, proc: Any) -> int | None:
        """SIGTERM the exec's processes, SIGKILL them after the grace period; return the exit code."""
        await self._signal([exec_id], 15)
        try:
            return await asyncio.wait_for(self._wait(proc), timeout=self._kill_grace_s)
        except TimeoutError:
            pass
        await self._signal([exec_id], 9)
        try:
            return await asyncio.wait_for(self._wait(proc), timeout=self._kill_grace_s)
        except TimeoutError:
            return None

    async def _signal(self, exec_ids: list[str], signum: int) -> None:
        if not exec_ids or self._released:
            return
        try:
            proc = await self._sandbox.exec.aio(
                "python", "-c", _SIGNAL_SCRIPT, str(signum), *exec_ids, timeout=30, text=True
            )
            await asyncio.wait_for(proc.wait.aio(), timeout=30)
        except Exception as exc:
            log.debug("signal %d in %s failed: %s", signum, self.id, exc)

    async def _exec_simple(self, *argv: str, timeout_s: float = 120.0) -> tuple[int | None, str]:
        """Run a short helper command; return its exit code and (capped) stderr+stdout text."""
        chunks: list[str] = []
        size = 0

        def collect(text: str, _stream: Stream) -> None:
            nonlocal size
            if size < _SIMPLE_OUTPUT_CAP:
                chunks.append(text)
                size += len(text)

        result = await self.run(
            list(argv),
            cwd="",
            env={},
            timeout_s=timeout_s,
            on_line=collect,
            stdout_cap=_SIMPLE_OUTPUT_CAP * 16,
        )
        return result.exit_code, "\n".join(chunks)[-2000:]

    async def _mkdir(self, path: str) -> None:
        if path in self._dirs:
            return
        try:
            await self._sandbox.filesystem.make_directory.aio(path, create_parents=True)
        except Exception as exc:
            if _error_name(exc) != "SandboxFilesystemPathAlreadyExistsError":
                raise ExecutorError(f"cannot create {path} in sandbox {self.id}: {exc}") from exc
        self._dirs.add(path)

    async def put_tree(self, tar_bytes: bytes, dest: str) -> None:
        """Upload the ``.tar.gz`` and extract it into a fresh ``dest`` (any previous tree is removed).

        Replacing the tree matters for snapshot-based sandboxes, whose ``repo/`` holds the snapshot
        commit: files deleted since then must not linger.
        """
        self._check()
        target = self.path(dest)
        if target == WORK_ROOT:
            raise ExecutorError("put_tree needs a sub-directory of the worker root")
        archive = f"{WORK_ROOT}/.src-{uuid.uuid4().hex}.tar.gz"
        await self._mkdir(WORK_ROOT)
        try:
            await self._sandbox.filesystem.write_bytes.aio(tar_bytes, archive)
        except Exception as exc:
            raise ExecutorError(f"cannot upload the source archive: {_error_name(exc)}: {exc}") from exc
        script = (
            'rm -rf -- "$1" && mkdir -p -- "$1" && '
            'tar -xzf "$2" -C "$1" --no-same-owner --no-same-permissions; rc=$?; rm -f -- "$2"; exit $rc'
        )
        code, output = await self._exec_simple("sh", "-c", script, "sh", target, archive)
        if code != 0:
            raise ExecutorError(f"cannot extract the source archive (exit {code}): {output}")

    async def get_file(self, path: str) -> bytes:
        """Read a worker file (at most 1 GiB)."""
        self._check()
        target = self.path(path)
        fs = self._sandbox.filesystem
        try:
            info = await fs.stat.aio(target)
            size = getattr(info, "size", 0) or 0
            if size > MAX_GET_FILE_BYTES:
                raise ExecutorError(f"{path} is larger than {MAX_GET_FILE_BYTES} bytes")
            return bytes(await fs.read_bytes.aio(target))
        except ExecutorError:
            raise
        except Exception as exc:
            if _is_fs_not_found(exc):
                raise ExecutorError(f"no such file in the worker: {path}") from None
            raise ExecutorError(f"cannot read {path}: {_error_name(exc)}: {exc}") from exc

    async def write_file(self, path: str, data: bytes) -> None:
        """Create or replace a worker file (parent directories are created)."""
        self._check()
        target = self.path(path)
        parent = str(PurePosixPath(target).parent)
        await self._mkdir(parent)
        try:
            await self._sandbox.filesystem.write_bytes.aio(bytes(data), target)
        except Exception as exc:
            raise ExecutorError(f"cannot write {path}: {_error_name(exc)}: {exc}") from exc

    async def cancel(self) -> None:
        """SIGTERM every running exec (the plugin writes its bundle), SIGKILL after the grace period."""
        running = dict(self._execs)
        if not running:
            return
        self._cancelled.update(running)
        await self._signal(list(running), 15)
        waits = [asyncio.wait_for(self._wait(proc), timeout=self._kill_grace_s) for proc in running.values()]
        results = await asyncio.gather(*waits, return_exceptions=True)
        survivors = [i for i, r in zip(running, results, strict=True) if isinstance(r, BaseException)]
        if survivors:
            await self._signal(survivors, 9)

    async def release(self) -> None:
        """Terminate the sandbox (idempotent). Pooled sandboxes serve one run: they never return."""
        if self._released:
            return
        self._released = True
        await self._executor._terminate(self._live)
