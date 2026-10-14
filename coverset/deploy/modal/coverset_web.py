"""coverset's control plane as a Modal web app: a stateless container.

Deploy with ``modal deploy -e coverset deploy/modal/coverset_web.py``.

Modal serves the Quart app through its ASGI gateway at a stable
``https://<workspace>-coverset--coverset-web.<region>.modal.run`` URL, and from inside the container
the run dispatcher creates shard sandboxes in the App ``coverset-runner`` exactly as ``coverset
serve`` does on a laptop (the Modal client is authenticated by the container runtime; no token is
stored). ``docs/deploy.md`` is the operator's guide; this docstring records the design.

**Nothing in the container must survive it.** The Modal Secret ``coverset-web`` names the state:
a Postgres ``COVERSET_DATABASE_URL``, an ``s3://`` ``COVERSET_BLOB_URL`` with
``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` for the bucket, and ``COVERSET_ADMIN_TOKEN`` (the
container serves a public URL). :func:`build_web_app` refuses to start without them
(:func:`missing_secrets`), with one line naming what is missing. The data dir, the cache (mirrors,
downloaded facts and sources) and the working directory are on the container's own disk
(:func:`container_environment`), so a restart or a redeploy loses no run, fact or setting. The
GitHub App key comes from the Secret (``COVERSET_GITHUB_APP_PRIVATE_KEY``, PEM text) because
nothing ``/setup`` writes to disk survives. One thing does not carry over yet:
``<data_dir>/instance_id``, the tag the reaper uses to find its own sandboxes, is new in every
container, so after a restart or a leader failover the earlier container's leftover sandboxes (warm-
pool ones included) are only stopped by ``coverset modal reap --any-instance`` (until the id lives in
the database).

**Containers.** ``min_containers=1`` keeps one warm so webhooks never wait for a cold start and the
background tasks (dispatcher, reaper, warm-pool maintainer, gc) keep running between requests;
``COVERSET_WEB_MAX_CONTAINERS`` (deploy time, default 1) lets Modal run more, and the leader lease
(``coverset.db.leader``) keeps the background loops in one of them while migrations serialise on an
advisory lock. ``@modal.concurrent`` lets each container take many requests at once (the app is
asyncio throughout, SSE streams included). With one container, deploy with ``--strategy
recreate``: with the default rolling strategy the new version waits for a free container slot that
the old, healthy container never gives up (observed 2026-10-13: the URL answered nothing for 25
minutes until the old container was stopped by hand).

The module is importable without a Modal token (``tests/test_deploy_hosted.py`` exercises it with
``modal`` stubbed out); only ``modal deploy`` talks to Modal.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

# The workspace default builder (2023.12) has no Python 3.13 debian_slim; coverset's executor sets
# the same override (executors/modal_images.configure_builder). An explicit value wins.
os.environ.setdefault("MODAL_IMAGE_BUILDER_VERSION", "2025.06")

import modal  # noqa: E402 - after the builder version, which the client reads at import

APP_NAME = "coverset-web"
ENVIRONMENT = "coverset"
SECRET_NAME = "coverset-web"
PYTHON_VERSION = "3.13"
UV_VERSION = "0.10.7"

LOCAL_DIR = Path("/root/coverset-data")
"""The container's own disk: data dir, cache and working directory (lost with the container)."""

WEB_CPU = float(os.environ.get("COVERSET_WEB_CPU", "0.5"))
"""Physical cores reserved for the web container (billed as max(reserved, used)); deploy-time knob."""
WEB_MEMORY_MIB = int(os.environ.get("COVERSET_WEB_MEMORY_MIB", "1024"))
"""Memory reserved for the web container, MiB (a request, not a hard limit); deploy-time knob."""
WEB_MAX_CONTAINERS = int(os.environ.get("COVERSET_WEB_MAX_CONTAINERS", "1"))
"""Deploy-time knob: containers Modal may run at once (the leader lease keeps the background loops
in one of them; with more than one, a default rolling deploy no longer waits for the only slot)."""
PRIVATE_KEY_ENV = "COVERSET_GITHUB_APP_PRIVATE_KEY"
"""The GitHub App's PEM text in the Secret; written to a file at start (:func:`write_private_key`)."""
REQUIRED_SECRETS = (
    "COVERSET_DATABASE_URL",
    "COVERSET_BLOB_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "COVERSET_ADMIN_TOKEN",
)
"""What the Secret must hold: the state's two URLs, the bucket's keys and the admin token."""
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
            # data dir, cache and key path the server uses (the Secret still overrides them).
            **container_environment(),
        }
    )


app = modal.App(APP_NAME)


def container_environment(local_dir: Path = LOCAL_DIR) -> dict[str, str]:
    """The ``COVERSET_*`` values the container fixes (the Secret may override the settings ones; the
    layout is :func:`build_web_app`'s): everything on the container's own disk."""
    return {
        "COVERSET_DATA_DIR": str(local_dir),
        "COVERSET_CACHE_DIR": str(local_dir / "cache"),
        "COVERSET_GITHUB_APP_PRIVATE_KEY_PATH": str(local_dir / "secrets" / "github-app.pem"),
        # Measured 2026-10-13: Modal's ASGI runtime already puts the real client address and
        # ``https`` in the scope and strips client-sent X-Forwarded-For/-Proto, so there is no
        # header to trust; leaving it off means a future pass-through could not be spoofed.
        "COVERSET_TRUST_PROXY": "false",
        "COVERSET_EXECUTOR": "modal",
        "COVERSET_MODAL_ENVIRONMENT": ENVIRONMENT,
        "COVERSET_ENV": "prod",
    }


def missing_secrets(environ: Mapping[str, str]) -> list[str]:
    """The :data:`REQUIRED_SECRETS` the environment (the Secret over the image) lacks, plus a URL of
    the wrong kind (a database that is not Postgres, a blob URL that is not ``s3://``)."""
    missing = [name for name in REQUIRED_SECRETS if not environ.get(name, "").strip()]
    database = environ.get("COVERSET_DATABASE_URL", "")
    if database and not database.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
        missing.append("COVERSET_DATABASE_URL (a Postgres URL)")
    blob = environ.get("COVERSET_BLOB_URL", "")
    if blob and not blob.startswith("s3://"):
        missing.append("COVERSET_BLOB_URL (an s3:// URL)")
    return missing


def write_private_key(environ: Mapping[str, str]) -> Path | None:
    """Write the GitHub App key from ``COVERSET_GITHUB_APP_PRIVATE_KEY`` (PEM text in the Secret) to
    ``COVERSET_GITHUB_APP_PRIVATE_KEY_PATH`` with mode 0600; None when the Secret holds no key.

    Nothing on the container's disk survives a restart, so the key that ``/setup`` would write to
    the working directory has to live in the Secret; this puts it where the settings look for it.
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
    local_dir: Path = LOCAL_DIR,
    create_app: Callable[[Any], Any] | None = None,
    load_settings: Callable[[], Any] = _fresh_settings,
) -> Any:
    """Check the Secret, lay the container out on its own disk, then build the coverset app.

    Raises :class:`RuntimeError` (the container does not start) when the Secret lacks what a
    stateless container needs (:func:`missing_secrets`).
    """
    missing = missing_secrets(environ)
    if missing:
        raise RuntimeError(
            f"coverset-web needs in the Modal Secret {SECRET_NAME!r}: {', '.join(missing)} "
            "(Postgres, an s3:// bucket with its keys, and the admin token; docs/modal.md)"
        )
    for key, value in container_environment(local_dir).items():
        environ.setdefault(key, value)
    local_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(local_dir)  # .env and secrets/ written by /setup: disposable, like the rest of the disk
    key_path = write_private_key(environ)
    settings = load_settings()
    if create_app is None:
        from coverset.app import create_app
    quart_app = create_app(settings)  # configures logging, so report the layout afterwards
    log.info("coverset-web: Postgres + object store; data dir and cache on the container's disk")
    if key_path is not None:
        log.info("coverset-web: GitHub App key written from the Secret to %s", key_path)
    return quart_app


@app.function(
    # The image is built from local files at deploy time; in the container it is already attached.
    image=build_image() if modal.is_local() else modal.Image.debian_slim(python_version=PYTHON_VERSION),
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    min_containers=1,
    max_containers=WEB_MAX_CONTAINERS,
    timeout=REQUEST_TIMEOUT_S,
    cpu=WEB_CPU,
    memory=WEB_MEMORY_MIB,
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_REQUESTS)
@modal.asgi_app(label=APP_NAME)
def web() -> Any:
    """Check the Secret, lay the container out and build the coverset app (its lifespan starts the
    background tasks).

    Modal calls this once per container and runs the returned app's ASGI lifespan when the container
    starts: Quart's ``before_serving`` hooks then migrate the database and start the dispatcher, gc,
    reaper and warm-pool maintainer; ``after_serving`` drains them.
    """
    return build_web_app(os.environ)
