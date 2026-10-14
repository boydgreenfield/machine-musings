"""coverset's control plane as a Modal web app.

Deploy with ``modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py``.

One container (``min_containers=1``, ``max_containers=1``) serves the Quart app through Modal's ASGI
gateway at a stable ``https://<workspace>-coverset--coverset-web.<region>.modal.run`` URL, and from inside
that container the run dispatcher creates shard sandboxes in the App ``coverset-runner`` exactly as
``coverset serve`` does on a laptop (the Modal client is authenticated by the container runtime; no
token is stored). ``docs/deploy.md`` is the operator's guide; this docstring records the design.

**Exactly one container.** coverset keeps its control-plane state in SQLite and runs singleton
background tasks (dispatcher, reaper, warm-pool maintainer, gc). ``max_containers=1`` keeps a
single writer; ``min_containers=1`` keeps it warm so webhooks never wait for a cold start and the
background tasks keep running between requests. ``@modal.concurrent`` lets that one container take
many requests at once (the app is asyncio throughout, SSE streams included). Redeploys must use
``--strategy recreate``: with the default rolling strategy the new version waits for a free
container slot that the old, healthy container never gives up (observed 2026-10-13: the URL
answered nothing for 25 minutes until the old container was stopped by hand).

**Where state lives.** Modal Volumes are eventually consistent, last-writer-wins file stores with
explicit ``commit()``/``reload()`` and *no file locking* ("nor is distributed file locking
supported", modal.com/docs/guide/volumes, read 2026-10-13); the docs give no SQLite guidance beyond
an example that builds the database on local disk and copies the finished file onto the Volume.
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
on temporary directories); only ``modal deploy`` talks to Modal.
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
import uuid
from collections.abc import Callable
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
            # live database and the Volume too (the Secret still overrides them).
            **container_environment(),
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
    """Create ``<data_dir>/instance_id`` (the reaper's sandbox tag) if missing, without hard links.

    coverset creates it with ``os.link`` so concurrent processes agree on one id, but Modal's
    (v1) Volumes refuse hard links with EPERM. This container is the only writer, so an exclusive
    create is enough; the executor then just reads the file.
    """
    path = data_dir / "instance_id"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return str(uuid.UUID(text))
    except (OSError, ValueError):
        pass
    data_dir.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    staging = path.with_name(f".instance_id.{uuid.uuid4().hex}")
    staging.write_text(value + "\n", encoding="utf-8")
    os.replace(staging, path)
    return value


def container_environment() -> dict[str, str]:
    """The ``COVERSET_*`` values this deployment fixes (the Secret may override any of them)."""
    return {
        "COVERSET_DATA_DIR": str(VOLUME_MOUNT),
        "COVERSET_DATABASE_URL": f"sqlite+aiosqlite:///{LOCAL_DB}",
        # Measured 2026-10-13: Modal's ASGI runtime already puts the real client address and
        # ``https`` in the scope and strips client-sent X-Forwarded-For/-Proto, so there is no
        # header to trust; leaving it off means a future pass-through could not be spoofed.
        "COVERSET_TRUST_PROXY": "false",
        "COVERSET_EXECUTOR": "modal",
        "COVERSET_MODAL_ENVIRONMENT": ENVIRONMENT,
        "COVERSET_ENV": "prod",
    }


@app.function(
    # The image is built from local files at deploy time; in the container it is already attached.
    image=build_image() if modal.is_local() else modal.Image.debian_slim(python_version=PYTHON_VERSION),
    volumes={str(VOLUME_MOUNT): volume},
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    min_containers=1,
    max_containers=1,
    timeout=REQUEST_TIMEOUT_S,
    cpu=WEB_CPU,
    memory=WEB_MEMORY_MIB,
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_REQUESTS)
@modal.asgi_app(label=APP_NAME)
def web() -> Any:
    """Restore the database, then build the coverset app (its lifespan starts the background tasks).

    Modal calls this once per container and runs the returned app's ASGI lifespan when the container
    starts: Quart's ``before_serving`` hooks then migrate the database and start the dispatcher, gc,
    reaper, warm-pool maintainer and the backup timer; ``after_serving`` drains them on shutdown.
    """
    for key, value in container_environment().items():
        os.environ.setdefault(key, value)
    os.chdir(VOLUME_MOUNT)  # .env and secrets/ written by /setup persist on the Volume
    from coverset.app import create_app
    from coverset.config import get_settings

    settings = get_settings()
    backup: SQLiteVolumeBackup | None = None
    outcome = "COVERSET_DATABASE_URL is overridden: no SQLite volume backups"
    if settings.database_url == container_environment()["COVERSET_DATABASE_URL"]:
        backup = SQLiteVolumeBackup(LOCAL_DB, BACKUP_DIR, commit=volume.commit)
        outcome = backup.restore()  # before create_app, which may touch the database file
    ensure_instance_id(settings.data_dir)
    quart_app = create_app(settings)  # configures logging, so report the restore afterwards
    log.info("coverset-web database: %s", outcome)
    if backup is not None:
        install_backups(quart_app, backup)
    return quart_app
