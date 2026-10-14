"""coverset's control plane as a Modal web app.

Deploy with ``modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py``.

One container (``min_containers=1``, ``max_containers=1``) serves the Quart app through Modal's ASGI
gateway at a stable ``https://<workspace>-coverset--coverset-web.<region>.modal.run`` URL, and from inside
that container the run dispatcher creates shard sandboxes in the App ``coverset-runner`` exactly as
``coverset serve`` does on a laptop (the Modal client is authenticated by the container runtime; no
token is stored). ``docs/deploy.md`` is the operator's guide; this docstring records the design.

**Two modes, chosen from the Secret at container start** (:func:`plan_container`):

* **Hosted** (the Secret holds a Postgres ``COVERSET_DATABASE_URL`` and an ``s3://``
  ``COVERSET_BLOB_URL``, plus ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` for the bucket): the
  container is stateless. The Volume restore/backup machinery below is skipped entirely, the data
  dir, the cache (mirrors, downloaded facts) and the working directory are on the container's own
  disk, and a restart or a redeploy loses no run, fact or setting. The GitHub App key comes from the
  Secret (``COVERSET_GITHUB_APP_PRIVATE_KEY``, PEM text) because nothing ``/setup`` writes to disk
  survives. One thing does not carry over yet: ``<data_dir>/instance_id``, the tag the reaper uses
  to find its own sandboxes, is new in every container, so after a restart or a leader failover
  the earlier container's leftover sandboxes (warm-pool ones included) are only stopped by
  ``coverset modal reap --any-instance`` (until the id lives in the database).
  Deploy with ``COVERSET_WEB_STATE=hosted`` in the deploying shell to mount no Volume at all; the
  image's environment then carries the hosted layout too (:func:`image_environment`), so a
  ``modal container exec ... coverset ...`` shell sees the data dir, cache and key path the server
  uses.
* **Volume** (anything else, the default): SQLite on the container's disk, backed up to the Volume,
  as described below. This is the mode the first deployment ran in on 2026-10-13.

**One container by default.** coverset runs singleton background tasks (dispatcher, reaper,
warm-pool maintainer, gc), and in the Volume mode SQLite allows one writer. ``max_containers=1``
keeps both true (a hosted deploy may raise it with ``COVERSET_WEB_MAX_CONTAINERS``: on Postgres the
leader lease keeps the loops in one container); ``min_containers=1`` keeps it warm so webhooks
never wait for a cold start and the background tasks keep running between requests.
``@modal.concurrent`` lets that one container take many requests at once (the app is asyncio
throughout, SSE streams included). With one container, redeploys must use ``--strategy recreate``:
with the default rolling strategy the new version waits for a free container slot that the old,
healthy container never gives up (observed 2026-10-13: the URL answered nothing for 25 minutes
until the old container was stopped by hand).

**Where state lives in the Volume mode.** Modal Volumes are eventually consistent,
last-writer-wins file stores with explicit ``commit()``/``reload()`` and *no file locking* ("nor
is distributed file locking supported", modal.com/docs/guide/volumes, read 2026-10-13); the docs
give no SQLite guidance beyond an example that builds the database on local disk and copies the
finished file onto the Volume.
Measured inside this container on 2026-10-13: ``flock``, ``lockf``, ``O_EXCL`` and ``rename`` work on
the mounted (v1) Volume, hard links fail with EPERM, and WAL-mode SQLite *runs* there but 200
single-row commits take 235 ms against 9 ms on local disk (26x), while what a background commit
captures of a database mid-transaction, and what another container would see, is undocumented. So
the live database does not sit on the Volume. Instead:

* the **live database** is on the container's local disk (``/root/coverset-data/coverset.db``);
* at startup :meth:`SQLiteVolumeBackup.restore` copies the newest good backup from the Volume
  (``/data/backup/coverset.db``, falling back to ``coverset.db.prev``) after ``PRAGMA quick_check``;
* every :data:`BACKUP_INTERVAL_S` seconds, *only when the database changed* (``PRAGMA data_version``),
  :meth:`SQLiteVolumeBackup.backup_if_dirty` takes a consistent online copy with SQLite's backup API
  (safe while the app writes), switches it to a self-contained rollback-journal file, copies it to
  the Volume under a temporary name, rotates ``coverset.db`` → ``coverset.db.prev``, renames the new
  file into place and calls ``volume.commit()``; once per UTC day it also keeps a dated copy
  (:data:`DAILY_KEEP` of them) so a bad restore cannot rotate every good generation away;
* on shutdown (ASGI lifespan ``shutdown``, which Quart turns into ``after_serving``) one last backup
  runs, and Modal commits the Volume as the container exits.

The recovery-point objective is therefore *the backup interval* (60 s) for a crash, an OOM kill or
a host failure, and zero for an orderly stop or a ``--strategy recreate`` redeploy: the old
container stops (final backup, ``volume.commit()``) before the new one mounts the Volume and
restores. A rolling redeploy, if it ever got a second slot, would overlap the two containers and
lose the old one's last writes; that is one more reason for ``recreate``. Runs in flight when the
container stops are abandoned at the next start (``COVERSET_REQUEUE_ON_BOOT`` re-queues them).

Everything else stays directly on the Volume because it is written once or can be rebuilt:
Parquet facts (immutable files), git mirrors (re-fetched if lost), run scratch, ``instance_id``
(the reaper's sandbox tag) and the image cache. The working directory is the Volume root, so the
``.env`` and ``secrets/github-app.pem`` that the ``/setup`` GitHub App flow writes persist across
restarts. Modal commits the Volume in the background every few seconds and at exit.

The module is importable without a Modal token (``tests/test_deploy.py`` exercises the backup logic
on temporary directories, ``tests/test_deploy_hosted.py`` the mode branching with ``modal`` stubbed
out); only ``modal deploy`` talks to Modal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The workspace default builder (2023.12) has no Python 3.13 debian_slim; coverset's executor sets
# the same override (executors/modal_images.configure_builder). An explicit value wins.
os.environ.setdefault("MODAL_IMAGE_BUILDER_VERSION", "2025.06")

import modal  # noqa: E402 - after the builder version, which the client reads at import

APP_NAME = "coverset-web"
ENVIRONMENT = "coverset"
VOLUME_NAME = "coverset-data"
SECRET_NAME = "coverset-web"
PYTHON_VERSION = "3.13"
UV_VERSION = "0.10.7"

VOLUME_MOUNT = Path("/data")
"""The Volume: ``COVERSET_DATA_DIR`` (facts, mirrors, runs, instance id) and the working directory."""
BACKUP_DIR = VOLUME_MOUNT / "backup"
LOCAL_DIR = Path("/root/coverset-data")
"""Container-local disk: the live SQLite database (lost with the container, restored from backups)."""
LOCAL_DB = LOCAL_DIR / "coverset.db"

BACKUP_INTERVAL_S = 60.0
"""How often the database is copied to the Volume when it changed: the crash recovery point."""
DAILY_KEEP = 7
"""Dated daily copies kept under ``backup/daily`` besides the two rolling generations."""

WEB_CPU = float(os.environ.get("COVERSET_WEB_CPU", "0.5"))
"""Physical cores reserved for the web container (billed as max(reserved, used)); deploy-time knob."""
WEB_MEMORY_MIB = int(os.environ.get("COVERSET_WEB_MEMORY_MIB", "1024"))
"""Memory reserved for the web container, MiB (a request, not a hard limit); deploy-time knob."""
WEB_STATE = os.environ.get("COVERSET_WEB_STATE", "volume").strip().lower()
"""Deploy-time knob: ``volume`` (default) mounts the Volume ``coverset-data``; ``hosted`` mounts nothing,
for a Secret that names Postgres and a bucket (the container is then stateless)."""
WEB_MAX_CONTAINERS = int(os.environ.get("COVERSET_WEB_MAX_CONTAINERS", "1")) if WEB_STATE == "hosted" else 1
"""Deploy-time knob, hosted only: containers Modal may run at once. On Postgres the leader lease keeps
the background loops in one of them and migrations serialise on an advisory lock; with more than one,
a default (rolling) deploy no longer waits for the only slot. The Volume mode is always one."""
PRIVATE_KEY_ENV = "COVERSET_GITHUB_APP_PRIVATE_KEY"
"""The GitHub App's PEM text in the Secret; written to a file at start (:func:`write_private_key`)."""
REQUEST_TIMEOUT_S = 24 * 3600
"""Per-input timeout. Every HTTP request is one input, so this caps one SSE stream, not the container."""
MAX_CONCURRENT_REQUESTS = 256

_HERE = Path(__file__).resolve()
PROJECT_ROOT = _HERE.parents[2] if len(_HERE.parents) > 2 else _HERE.parent
"""The coverset checkout (inside the container this module is ``/root/coverset_web.py``)."""
log = logging.getLogger("coverset.deploy.modal")

_SYNC_ENV = {
    "UV_PROJECT_ENVIRONMENT": "/app/.venv",
    "UV_COMPILE_BYTECODE": "1",
    "UV_LINK_MODE": "copy",
    "UV_PYTHON_DOWNLOADS": "never",
}
_DEPENDENCY_FILES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "packages/coverset-model/pyproject.toml",
    "packages/coverset-model/README.md",
    "packages/pytest-coverset/pyproject.toml",
    "packages/pytest-coverset/README.md",
)


def build_image() -> modal.Image:
    """The web image: the same contents as the project's Dockerfile, built natively by Modal.

    Dependencies are synced from the lock before the sources are added, so a code change rebuilds
    only the last layers. The project is installed editable into ``/app/.venv`` (the executors find
    the plugin sources and ``uv.lock`` relative to ``src/coverset``), the venv comes first on
    ``PATH`` (how Modal's own ``uv_sync`` activates one), and the plugin wheel is pre-built. The
    build context is the Dockerfile's: ``.dockerignore`` is reused as the ignore list.
    """
    image = (
        modal.Image.debian_slim(python_version=PYTHON_VERSION)
        .apt_install("git", "ca-certificates")
        .pip_install(f"uv=={UV_VERSION}")
    )
    for rel in _DEPENDENCY_FILES:
        image = image.add_local_file(PROJECT_ROOT / rel, f"/app/{rel}", copy=True)
    image = image.run_commands(
        "cd /app && uv sync --frozen --no-dev --no-install-project --no-install-workspace", env=_SYNC_ENV
    )
    image = image.add_local_dir(
        PROJECT_ROOT,
        "/app",
        copy=True,
        ignore=modal.FilePatternMatcher.from_file(PROJECT_ROOT / ".dockerignore"),
    )
    return image.run_commands(
        "cd /app && uv sync --frozen --no-dev",
        "/app/.venv/bin/python -c 'from coverset.executors.env_export import build_plugin_wheel; "
        "print(build_plugin_wheel())'",
        env=_SYNC_ENV,
    ).env(
        {
            "PATH": "/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
            # The same values web() sets, so a `modal container exec ... coverset ...` shell sees the
            # live database and the Volume, or in a hosted deploy the hosted layout (the Secret
            # still overrides them).
            **image_environment(),
        }
    )


app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


class SQLiteVolumeBackup:
    """Restore the live SQLite database from a Volume directory, and back it up there when it changed.

    ``commit`` is called after every backup (``volume.commit`` in the container, nothing in tests).
    Thread-safe: the timer and the shutdown hook may race; one backup runs at a time.
    """

    def __init__(
        self,
        live: Path,
        backup_dir: Path,
        *,
        commit: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.live = live
        self.backup_dir = backup_dir
        self.commit = commit
        self.clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._backed_up_version: int | None = None
        self.backups = 0
        self.last_backup: dict[str, Any] | None = None

    @property
    def current(self) -> Path:
        """The newest backup on the Volume."""
        return self.backup_dir / "coverset.db"

    @property
    def previous(self) -> Path:
        """The generation before :attr:`current`."""
        return self.backup_dir / "coverset.db.prev"

    def restore(self) -> str:
        """Copy the newest backup that passes ``quick_check`` to :attr:`live`; return what happened.

        A live database that already exists is kept (the same container restarting its app). A
        backup that fails the check is renamed ``*.corrupt-<time>`` so later rotations cannot push a
        good generation out behind it. With no usable backup the app starts on an empty database
        (migrations create it) and the daily copies, if any, are left for a manual restore.
        """
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if self.live.exists():
            outcome = f"kept the existing local database {self.live}"
        else:
            outcome = "no backup on the volume: starting with an empty database"
            for candidate in (self.current, self.previous):
                if not candidate.is_file():
                    continue
                if self._restore_from(candidate):
                    outcome = f"restored {candidate.name} ({candidate.stat().st_size} bytes)"
                    break
                stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
                quarantined = candidate.with_name(f"{candidate.name}.corrupt-{stamp}")
                os.replace(candidate, quarantined)
                log.error("backup %s failed its integrity check; kept as %s", candidate, quarantined.name)
                outcome = "every backup failed its integrity check: starting with an empty database"
        if self.live.exists():
            self._backed_up_version = self._data_version()  # identical to the backup: not dirty
        return outcome

    def _restore_from(self, backup: Path) -> bool:
        staging = self.live.with_name(self.live.name + ".restore")
        shutil.copyfile(backup, staging)
        try:
            with sqlite3.connect(staging) as check:
                ok = check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            check.close()
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            staging.unlink(missing_ok=True)
            return False
        os.replace(staging, self.live)
        return True

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.live, isolation_level=None, check_same_thread=False)
        return self._conn

    def _data_version(self) -> int:
        """``PRAGMA data_version`` on this object's own connection: it changes whenever *another*
        connection (the app's) commits, which is exactly "the database changed since the last look"."""
        return int(self._connection().execute("PRAGMA data_version").fetchone()[0])

    def backup_if_dirty(self, *, force: bool = False) -> bool:
        """Back the live database up to the Volume when it changed (or ``force``); True if it did."""
        with self._lock:
            if not self.live.exists():
                return False
            version = self._data_version()
            if not force and version == self._backed_up_version and self.current.is_file():
                return False
            started = time.monotonic()
            size = self._write_backup()
            self._backed_up_version = version
            self.backups += 1
            elapsed = time.monotonic() - started
            self.last_backup = {"at": self.clock().isoformat(), "bytes": size, "seconds": round(elapsed, 3)}
            log.info("coverset-web database backed up: %d bytes in %.2f s", size, elapsed)
            return True

    def _write_backup(self) -> int:
        snapshot = self.live.with_name(self.live.name + ".snapshot")
        snapshot.unlink(missing_ok=True)
        target = sqlite3.connect(snapshot)
        try:
            self._connection().backup(target)
            target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal to lose
        finally:
            target.close()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        incoming = self.backup_dir / "coverset.db.tmp"
        shutil.copyfile(snapshot, incoming)
        with open(incoming, "rb") as handle:
            os.fsync(handle.fileno())
        if self.current.exists():
            os.replace(self.current, self.previous)
        os.replace(incoming, self.current)
        self._keep_daily(snapshot)
        size = snapshot.stat().st_size
        snapshot.unlink(missing_ok=True)
        meta = {"at": self.clock().isoformat(), "bytes": size, "source": str(self.live)}
        (self.backup_dir / "backup.json").write_text(json.dumps(meta), encoding="utf-8")
        if self.commit is not None:
            self.commit()
        return size

    def _keep_daily(self, snapshot: Path) -> None:
        daily = self.backup_dir / "daily"
        daily.mkdir(exist_ok=True)
        today = daily / f"coverset-{self.clock():%Y%m%d}.db"
        if not today.exists():
            shutil.copyfile(snapshot, today)
        for stale in sorted(daily.glob("coverset-*.db"))[:-DAILY_KEEP]:
            stale.unlink(missing_ok=True)

    def close(self) -> None:
        """Close this object's read connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


async def backup_forever(
    backup: SQLiteVolumeBackup, stop: asyncio.Event, *, interval_s: float = BACKUP_INTERVAL_S
) -> None:
    """Call :meth:`SQLiteVolumeBackup.backup_if_dirty` every ``interval_s`` until ``stop`` is set."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(backup.backup_if_dirty)
        except Exception:
            log.exception("coverset-web database backup failed (will retry)")


def install_backups(
    quart_app: Any, backup: SQLiteVolumeBackup, *, interval_s: float = BACKUP_INTERVAL_S
) -> None:
    """Run :func:`backup_forever` while the app serves, and one last backup when it stops."""

    @quart_app.before_serving
    async def _start_backups() -> None:
        log.info("coverset-web lifespan startup: backups every %.0f s", interval_s)
        quart_app.add_background_task(backup_forever, backup, quart_app.shutdown_event, interval_s=interval_s)

    @quart_app.after_serving
    async def _final_backup() -> None:
        log.info("coverset-web lifespan shutdown: final database backup")
        try:
            await asyncio.to_thread(backup.backup_if_dirty)
        finally:
            backup.close()


def ensure_instance_id(data_dir: Path) -> str:
    """``<data_dir>/instance_id`` (the reaper's sandbox tag), created if missing.

    Kept for callers of this module only: ``web()`` no longer pre-creates the file, because
    :func:`coverset.executors.modal_.instance_id` now falls back to an exclusive create when the
    Volume refuses its ``os.link`` with EPERM.
    """
    from coverset.executors.modal_ import instance_id

    return instance_id(data_dir)


def container_environment(
    *, local_dir: Path = LOCAL_DIR, volume_mount: Path = VOLUME_MOUNT
) -> dict[str, str]:
    """The ``COVERSET_*`` values the Volume mode fixes (the Secret may override any of them)."""
    return {
        "COVERSET_DATA_DIR": str(volume_mount),
        "COVERSET_DATABASE_URL": f"sqlite+aiosqlite:///{local_dir / LOCAL_DB.name}",
        # Measured 2026-10-13: Modal's ASGI runtime already puts the real client address and
        # ``https`` in the scope and strips client-sent X-Forwarded-For/-Proto, so there is no
        # header to trust; leaving it off means a future pass-through could not be spoofed.
        "COVERSET_TRUST_PROXY": "false",
        "COVERSET_EXECUTOR": "modal",
        "COVERSET_MODAL_ENVIRONMENT": ENVIRONMENT,
        "COVERSET_ENV": "prod",
    }


def hosted_layout(local_dir: Path = LOCAL_DIR) -> dict[str, str]:
    """Where the hosted mode keeps its disposable files: all on the container's own disk."""
    return {
        "COVERSET_DATA_DIR": str(local_dir),
        "COVERSET_CACHE_DIR": str(local_dir / "cache"),
        "COVERSET_GITHUB_APP_PRIVATE_KEY_PATH": str(local_dir / "secrets" / "github-app.pem"),
    }


def image_environment(state: str = WEB_STATE, *, local_dir: Path = LOCAL_DIR) -> dict[str, str]:
    """The image's ``COVERSET_*`` values: :func:`container_environment`, plus, for a
    ``COVERSET_WEB_STATE=hosted`` deploy, :func:`hosted_layout`.

    ``web()`` applies the layout in its own process (:func:`plan_container`); putting it in the
    image too gives ``modal container exec`` shells the same view, so ``coverset deploy check``
    there checks the server's data dir and cache instead of a ``/data`` that has no Volume behind
    it. The key path equals the relative default ``/setup`` uses from the hosted working directory.
    """
    values = container_environment(local_dir=local_dir)
    if state == "hosted":
        values.update(hosted_layout(local_dir))
    return values


def secret_is_hosted(environ: Mapping[str, str]) -> bool:
    """True when the environment (the Secret over the image's defaults) names Postgres and a bucket:
    the same rule as :attr:`coverset.config.Settings.hosted`, read before Settings exist."""
    url = environ.get("COVERSET_DATABASE_URL", "")
    return url.startswith(("postgresql://", "postgresql+", "postgres://")) and bool(
        environ.get("COVERSET_BLOB_URL")
    )


@dataclass(frozen=True)
class ContainerPlan:
    """How :func:`build_web_app` lays the container out: the stateless hosted mode or the Volume mode."""

    hosted: bool
    environment: dict[str, str]
    """Values that replace what the image or the Secret set."""
    workdir: Path
    """The working directory (``.env`` and ``secrets/`` of the ``/setup`` flow land here)."""
    backups: bool
    """Restore and back up the local SQLite database from and to the Volume."""


def plan_container(
    environ: Mapping[str, str],
    *,
    local_dir: Path = LOCAL_DIR,
    volume_mount: Path = VOLUME_MOUNT,
) -> ContainerPlan:
    """Choose the mode from the environment.

    **Hosted** (Postgres URL and blob URL in the Secret): nothing needs the Volume. The data dir and
    the cache are on the container's own disk (``local_dir``; a disposable cache of mirrors and
    downloads), the working directory too, and the SQLite backup machinery is skipped entirely.

    **Volume** (anything else): the data dir and the working directory are the Volume, and the
    database is backed up to it when it is the container-local SQLite file this module defaults to.
    """
    if secret_is_hosted(environ):
        layout = hosted_layout(local_dir)
        values = {"COVERSET_DATA_DIR": layout["COVERSET_DATA_DIR"]}
        if not environ.get("COVERSET_CACHE_DIR"):
            values["COVERSET_CACHE_DIR"] = layout["COVERSET_CACHE_DIR"]
        if environ.get(PRIVATE_KEY_ENV):
            values["COVERSET_GITHUB_APP_PRIVATE_KEY_PATH"] = layout["COVERSET_GITHUB_APP_PRIVATE_KEY_PATH"]
        return ContainerPlan(hosted=True, environment=values, workdir=local_dir, backups=False)
    local_url = container_environment(local_dir=local_dir)["COVERSET_DATABASE_URL"]
    return ContainerPlan(
        hosted=False,
        environment={},
        workdir=volume_mount,
        backups=environ.get("COVERSET_DATABASE_URL", local_url) == local_url,
    )


def write_private_key(environ: Mapping[str, str]) -> Path | None:
    """Write the GitHub App key from ``COVERSET_GITHUB_APP_PRIVATE_KEY`` (PEM text in the Secret) to
    ``COVERSET_GITHUB_APP_PRIVATE_KEY_PATH`` with mode 0600; None when the Secret holds no key.

    In the hosted mode nothing on disk survives a restart, so the key that ``/setup`` wrote to the
    working directory has to live in the Secret; this puts it where the settings look for it.
    """
    pem = environ.get(PRIVATE_KEY_ENV, "").strip()
    if not pem:
        return None
    path = Path(environ.get("COVERSET_GITHUB_APP_PRIVATE_KEY_PATH", "secrets/github-app.pem"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(pem + "\n")
    return path


def _fresh_settings() -> Any:
    from coverset.config import get_settings

    get_settings.cache_clear()  # the environment just changed
    return get_settings()


def build_web_app(
    environ: MutableMapping[str, str],
    *,
    volume_mounted: bool,
    local_dir: Path = LOCAL_DIR,
    volume_mount: Path = VOLUME_MOUNT,
    commit: Callable[[], None] | None = None,
    create_app: Callable[[Any], Any] | None = None,
    load_settings: Callable[[], Any] = _fresh_settings,
) -> Any:
    """Lay the container out (:func:`plan_container`), then build the coverset app.

    Volume mode restores the database before ``create_app`` (which may touch the file) and installs
    the backup timer; hosted mode builds the app straight away. A deployment without the Volume
    (``COVERSET_WEB_STATE=hosted``) whose Secret is not hosted refuses to start rather than keep
    its only database on a disk that disappears with the container.
    """
    for key, value in container_environment(local_dir=local_dir, volume_mount=volume_mount).items():
        environ.setdefault(key, value)
    plan = plan_container(environ, local_dir=local_dir, volume_mount=volume_mount)
    if not plan.hosted and not volume_mounted:
        raise RuntimeError(
            "this deployment has no Volume (COVERSET_WEB_STATE=hosted) but the Secret does not name "
            "Postgres and a bucket (COVERSET_DATABASE_URL=postgresql+asyncpg://..., COVERSET_BLOB_URL=s3://...)"
        )
    environ.update(plan.environment)
    plan.workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(plan.workdir)  # .env and secrets/ written by /setup: the Volume, or (hosted) disposable
    key_path = write_private_key(environ)
    settings = load_settings()
    if create_app is None:
        from coverset.app import create_app
    backup: SQLiteVolumeBackup | None = None
    if plan.hosted:
        outcome = "hosted: Postgres + object store, no Volume backups; cache on the container's disk"
    elif plan.backups:
        backup = SQLiteVolumeBackup(local_dir / LOCAL_DB.name, volume_mount / BACKUP_DIR.name, commit=commit)
        outcome = backup.restore()  # before create_app, which may touch the database file
    else:
        outcome = "COVERSET_DATABASE_URL is overridden: no SQLite volume backups"
    quart_app = create_app(settings)  # configures logging, so report the layout afterwards
    log.info("coverset-web state: %s", outcome)
    if key_path is not None:
        log.info("coverset-web: GitHub App key written from the Secret to %s", key_path)
    if backup is not None:
        install_backups(quart_app, backup)
    return quart_app


@app.function(
    # The image is built from local files at deploy time; in the container it is already attached.
    image=build_image() if modal.is_local() else modal.Image.debian_slim(python_version=PYTHON_VERSION),
    # COVERSET_WEB_STATE=hosted (deploy time) mounts no Volume: Postgres and the bucket hold the state.
    volumes={} if WEB_STATE == "hosted" else {str(VOLUME_MOUNT): volume},
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    min_containers=1,
    # One container unless the hosted deploy asks for more (the leader lease then picks the one
    # that runs the dispatcher, reaper, warm-pool maintainer and gc).
    max_containers=WEB_MAX_CONTAINERS,
    timeout=REQUEST_TIMEOUT_S,
    cpu=WEB_CPU,
    memory=WEB_MEMORY_MIB,
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_REQUESTS)
@modal.asgi_app(label=APP_NAME)
def web() -> Any:
    """Lay the container out and build the coverset app (its lifespan starts the background tasks).

    Modal calls this once per container and runs the returned app's ASGI lifespan when the container
    starts: Quart's ``before_serving`` hooks then migrate the database and start the dispatcher, gc,
    reaper, warm-pool maintainer and (Volume mode) the backup timer; ``after_serving`` drains them.
    """
    return build_web_app(
        os.environ, volume_mounted=WEB_STATE != "hosted" and VOLUME_MOUNT.is_dir(), commit=volume.commit
    )
