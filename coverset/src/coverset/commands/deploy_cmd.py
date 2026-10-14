"""``coverset deploy check``: is this environment ready to serve coverset on a public URL?

Run it where ``coverset serve`` will run (a container, a VM, ``modal container exec`` into the web
container) with the same environment. Every row is ``PASS``, ``WARN`` or ``FAIL`` with the reason
and, for anything not passing, what to change; the exit status is 1 when any row fails. The checks
read configuration and local state, connect to the database (``check_database``: server version,
migrated revision, the background-task lease holder) and the object store (``check_store``: a write-read-list-delete of one small
probe object and a conditional write), plus one ``GET <public url>/healthz`` (skip it with
``--offline``), and never print a secret: tokens are reported by length and source, not value.

Four rows describe where the state lives: ``database``, ``object store``, ``cache dir`` (the
disposable local cache against its bound) and ``state``: *hosted* when a bucket is set (nothing
under the data dir must survive a restart), or blobs as files under the data dir, which warns
behind a public URL (that directory must then be a disk that survives restarts).

The checks are a pure function of a :class:`~coverset.config.Settings`, an environment mapping and
a URL prober (:func:`deployment_checks`), so tests drive them with hermetic settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import click

from coverset.config import MIN_ADMIN_TOKEN_CHARS, Settings, is_private_host

if TYPE_CHECKING:
    from coverset.forge.credentials import GitHubCredentials

__all__ = ["Check", "deploy_group", "deployment_checks", "probe_healthz"]

Status = Literal["pass", "warn", "fail"]
Prober = Callable[[str], tuple[bool, str]]
"""``prober(url) -> (ok, detail)``: fetch ``url`` and say whether it answered like coverset's /healthz."""

LOW_DISK_BYTES = 1024**3
"""Free space under the data dir below which the check warns (mirrors and facts grow with use)."""
PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Check:
    """One row of the report."""

    name: str
    status: Status
    detail: str


def probe_healthz(url: str) -> tuple[bool, str]:
    """GET ``url`` and accept only coverset's ``{"status": "ok", ...}`` with HTTP 200."""
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_S) as response:  # noqa: S310 - http(s) only
            status, body = response.status, response.read(4096)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"unreachable: {getattr(exc, 'reason', exc)}"
    try:
        payload = json.loads(body)
    except ValueError:
        return False, f"HTTP {status} but not coverset's /healthz JSON"
    if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
        return True, f"HTTP 200, coverset {payload.get('version', '?')}"
    return False, f"HTTP {status}: {str(payload)[:80]}"


def _in_modal_container(environ: Mapping[str, str]) -> bool:
    """True inside a Modal container, where the runtime authenticates the Modal client.

    ``MODAL_IS_REMOTE=1`` is in every process of the container (a ``modal container exec`` shell
    too, where ``modal.is_local()`` is still True because the function never started there).
    """
    if environ.get("MODAL_IS_REMOTE") == "1":
        return True
    try:
        import modal
    except ImportError:  # pragma: no cover - modal is a dependency
        return False
    return not modal.is_local()


def _profile_has_token() -> bool:
    """True when the Modal client's config (``~/.modal.toml`` profile or env) holds a token."""
    try:
        from modal.config import config
    except ImportError:  # pragma: no cover - modal is a dependency
        return False
    return bool(config.get("token_id") and config.get("token_secret"))


def _modal_token_source(environ: Mapping[str, str], in_modal: bool) -> str | None:
    """Where the Modal client gets credentials, or None when it has none."""
    if in_modal:
        return "the Modal container runtime"
    if environ.get("MODAL_TOKEN_ID") and environ.get("MODAL_TOKEN_SECRET"):
        return "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET"
    if _profile_has_token():
        return "the active profile in ~/.modal.toml"
    return None


DatabaseProbe = Callable[[str], Any]
"""``check_database(url)`` (``coverset.db.session``): a report that unpacks as ``(status, detail)``."""
StoreProbe = Callable[[Settings], Any]
"""``check_store(settings)`` (``coverset.storage``): ``(status, detail)`` of the bucket probe."""


def _resolve(result: Any) -> Any:
    """A probe's result, awaited when the probe was a coroutine function."""
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _database_check(settings: Settings, probe: DatabaseProbe | None = None) -> Check:
    """Server version, the migrated revision against the packaged head, the pool and the
    background-task lease holder, from :func:`coverset.db.session.check_database` (``probe``
    replaces it in tests). It never creates anything and reports an unreachable server or a
    missing database as a failing row."""
    if probe is None:
        from coverset.db.session import check_database

        probe = check_database
    try:
        report = _resolve(probe(settings.database_url))
    except Exception as exc:  # noqa: BLE001 - any driver error is a report row, not a crash
        return Check("database", "fail", f"cannot read the schema revision: {type(exc).__name__}: {exc}")
    status, detail = report
    return Check("database", status, detail)


def _store_check(settings: Settings, probe: StoreProbe | None = None) -> Check:
    """The blob store, from :func:`coverset.storage.check_store` (``probe`` replaces it in tests):
    local files or a bucket, probed with a write-read-list-delete of one small object and a
    conditional write that must be refused (a store that ignores ``If-Match`` warns)."""
    from coverset.storage import blob_url_of

    if probe is None:
        from coverset.storage import check_store

        probe = check_store
    try:
        status, detail = _resolve(probe(settings))
    except Exception as exc:  # noqa: BLE001 - credentials, DNS, permissions: a row, not a crash
        return Check(
            "object store", "fail", f"{blob_url_of(settings).split('?', 1)[0]}: {type(exc).__name__}: {exc}"
        )
    return Check("object store", status, detail)


CACHE_SCAN_LIMIT = 100_000
"""Files the cache-size walk counts before it stops and reports a lower bound."""


def _cache_size(root: Path) -> tuple[int, int, bool]:
    """``(bytes, files, complete)`` under ``root``; symlinks are not followed and the walk stops after
    :data:`CACHE_SCAN_LIMIT` files so a huge cache cannot stall the check."""
    total = files = 0
    for current, _dirs, names in os.walk(root):
        for name in names:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(current, name)).st_size
            files += 1
            if files >= CACHE_SCAN_LIMIT:
                return total, files, False
    return total, files, True


def _cache_bound(settings: Settings) -> int | None:
    """The cache's size bound in bytes (``COVERSET_CACHE_MAX_GB``), or None when this build has none."""
    configured = getattr(settings, "cache_max_gb", None)
    if configured is not None:
        return int(float(configured) * 1_000_000_000)
    try:
        from coverset.facts.cache import cache_max_bytes
    except ImportError:
        return None
    return cache_max_bytes()


def _cache_check(settings: Settings) -> Check:
    """The disposable local cache (facts, fetched sources, mirrors): writable, its size against its
    bound (``COVERSET_CACHE_MAX_GB``, which gc and start-up evict to) and the free disk under it."""
    cache = settings.cache_dir or settings.data_dir / "cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".deploy-check-"):
            pass
    except OSError as exc:
        return Check("cache dir", "fail", f"{cache} is not writable: {exc}")
    size, files, complete = _cache_size(cache)
    free = shutil.disk_usage(cache).free
    bound = _cache_bound(settings)
    amount = f"{size / 1e9:.2f} GB in {files} files" + ("" if complete else "+")
    limit = f"bound {bound / 1e9:g} GB" if bound is not None else "no bound"
    detail = f"{cache}: {amount} ({limit}), {free / 1024**3:.1f} GiB free; disposable"
    status: Status = "pass"
    if free < LOW_DISK_BYTES:
        status = "warn"
    elif bound is not None and bound > size + free:
        status = "warn"
        detail += "; the bound is larger than the disk can hold (lower COVERSET_CACHE_MAX_GB)"
    return Check("cache dir", status, detail)


def _state_check(settings: Settings) -> Check:
    """Where the state that must survive a restart lives, and whether that fits the public URL."""
    if settings.hosted:
        return Check(
            "state",
            "pass",
            f"hosted: Postgres + object store; nothing under {settings.data_dir} must survive a restart",
        )
    if is_private_host(urlsplit(settings.public_url).hostname):
        return Check("state", "pass", f"local: Postgres + blobs as files under {settings.data_dir}")
    return Check(
        "state",
        "warn",
        f"blobs are files under {settings.data_dir} behind the public {settings.public_url}: that "
        "directory must be a disk that survives restarts; set COVERSET_BLOB_URL for a stateless app "
        "(docs/deploy.md)",
    )


def _data_dir_check(settings: Settings) -> list[Check]:
    data_dir = settings.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".deploy-check-"):
            pass
    except OSError as exc:
        return [Check("data dir", "fail", f"{data_dir} is not writable: {exc}")]
    checks = [Check("data dir", "pass", f"{data_dir} is writable")]
    free = shutil.disk_usage(data_dir).free
    status: Status = "warn" if free < LOW_DISK_BYTES else "pass"
    checks.append(Check("disk", status, f"{free / 1024**3:.1f} GiB free under {data_dir}"))
    return checks


def _stored_credentials(settings: Settings) -> GitHubCredentials | None:
    """The App ``/setup`` stored, as the server would load it; None when the database cannot be read
    (the ``database`` row says why)."""
    from coverset.db.session import Database
    from coverset.forge.credentials import load

    async def read() -> GitHubCredentials:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                return await load(session, settings)
        finally:
            await database.engine.dispose()

    try:
        return asyncio.run(read())
    except Exception:  # noqa: BLE001 - unreachable or unmigrated: reported by the database row
        return None


def _github_checks(
    settings: Settings, environ: Mapping[str, str], stored: GitHubCredentials | None = None
) -> list[Check]:
    """The forge mode, the App and the webhook secret, from the environment or (``stored``) the App
    ``/setup`` stored in the database (None: not read)."""
    from coverset.forge.credentials import effective_owners, from_environment
    from coverset.forge.registry import forge_mode
    from coverset.forge.signing import webhook_secret

    credentials = from_environment(settings)
    environment = credentials.source == "environment"
    if not environment and stored is not None:
        credentials = stored
    try:
        mode = forge_mode(settings, environ, credentials)
    except ValueError as exc:
        return [Check("github", "fail", str(exc))]
    checks: list[Check] = []
    key_set = settings.secret_key is not None and bool(settings.secret_key.get_secret_value())
    if credentials.problem:
        checks.append(Check("github", "fail", f"the stored GitHub App cannot be used: {credentials.problem}"))
    elif mode == "fake":
        detail = "no App or token: fake forge only (create the App at <public url>/setup"
        detail += ")" if key_set else "; it needs COVERSET_SECRET_KEY to store the App)"
        if stored is None and not environment:
            detail += "; the database was not read"
        checks.append(Check("github", "warn", detail))
    elif mode == "github-app":
        app = credentials.app
        assert app is not None
        where = "from the environment" if environment else "stored by /setup"
        if not app.pem:
            checks.append(
                Check(
                    "github",
                    "fail",
                    f"App {app.app_id}: COVERSET_GITHUB_APP_PRIVATE_KEY (the PEM text) is not set",
                )
            )
        else:
            owners = ", ".join(effective_owners(settings, credentials)) or "-"
            checks.append(Check("github", "pass", f"App {app.app_id} ({where}), allowed owners: {owners}"))
        if not app.client_id or not app.client_secret:
            checks.append(
                Check("github login", "warn", "no App client id/secret: GitHub sign-in is unavailable")
            )
    else:
        checks.append(Check("github", "pass", "token mode (commit statuses and comments)"))
    _secret, problem = webhook_secret(settings, credentials)
    if problem is None:
        checks.append(Check("webhook secret", "pass", "set (webhooks are verified)"))
    else:
        status: Status = "warn" if mode == "fake" else "fail"
        checks.append(Check("webhook secret", status, f"{problem}: POST /webhooks/github answers 503"))
    return checks


def deployment_checks(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    prober: Prober | None = probe_healthz,
    in_modal: bool | None = None,
    check_database: bool = True,
    check_store: bool = True,
    database_probe: DatabaseProbe | None = None,
    store_probe: StoreProbe | None = None,
) -> list[Check]:
    """Every readiness row for ``settings`` (``prober=None`` skips the network probe).

    ``check_database`` / ``check_store`` turn the rows that connect to the database and the bucket
    off; ``database_probe`` / ``store_probe`` replace ``check_database`` / ``check_store`` (tests).
    """
    env = os.environ if environ is None else environ
    modal_container = _in_modal_container(env) if in_modal is None else in_modal
    public = urlsplit(settings.public_url)
    public_is_private = is_private_host(public.hostname)
    checks: list[Check] = []

    checks.append(
        Check("environment", "pass" if settings.env == "prod" else "warn", f"COVERSET_ENV={settings.env}")
    )
    if modal_container:
        checks.append(Check("bind", "pass", "served by Modal's ASGI gateway (COVERSET_BIND is unused)"))
    elif settings.is_loopback and not public_is_private:
        checks.append(
            Check(
                "bind",
                "warn",
                f"{settings.bind} is loopback: only a proxy or tunnel on this host can reach it",
            )
        )
    else:
        checks.append(Check("bind", "pass", settings.bind))

    if public.scheme == "https":
        checks.append(Check("public url", "pass", settings.public_url))
    elif public_is_private:
        checks.append(
            Check("public url", "warn", f"{settings.public_url} is local only: GitHub cannot reach it")
        )
    else:
        checks.append(
            Check("public url", "warn", f"{settings.public_url} is not https: cookies are not Secure")
        )
    if prober is not None:
        ok, detail = prober(f"{settings.public_url}/healthz")
        checks.append(Check("reachable", "pass" if ok else "warn", f"GET /healthz: {detail}"))

    token = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if token:
        checks.append(Check("admin token", "pass", f"set ({len(token)} characters)"))
    elif public_is_private and settings.is_loopback:
        checks.append(Check("admin token", "warn", "unset: open mode (fine on a laptop, never on a server)"))
    else:
        checks.append(
            Check(
                "admin token",
                "fail",
                f"unset: set COVERSET_ADMIN_TOKEN (at least {MIN_ADMIN_TOKEN_CHARS} characters)",
            )
        )
    if settings.dev_login:
        checks.append(
            Check(
                "dev sign-in",
                "fail" if settings.env == "prod" else "warn",
                "COVERSET_DEV_LOGIN=true does not belong in a deployment (it is inert outside open mode on a loopback bind, but remove it)",
            )
        )

    secret_key = settings.secret_key.get_secret_value() if settings.secret_key else ""
    if secret_key:
        status: Status = "pass" if len(secret_key) >= 32 else "warn"
        checks.append(
            Check("secret key", status, f"COVERSET_SECRET_KEY ({len(secret_key)} characters) + admin token")
        )
    elif token:
        checks.append(
            Check("secret key", "pass", "derived from the admin token (rotating it signs everyone out)")
        )
    else:
        key_file = settings.data_dir / "secret_key"
        if settings.hosted:  # the data dir is disposable: every new container would sign everyone out
            detail = f"random key file {key_file}, lost with the container: set COVERSET_SECRET_KEY"
        else:
            detail = f"random key file {key_file}"
        checks.append(Check("secret key", "warn", detail))

    if settings.trust_proxy:
        checks.append(
            Check("proxy", "pass", "client address and scheme from X-Forwarded-For/-Proto (nearest hop)")
        )
    elif modal_container:
        checks.append(Check("proxy", "pass", "Modal's gateway passes the client address and scheme itself"))
    elif public.scheme == "https" and not settings.is_loopback:
        checks.append(
            Check(
                "proxy",
                "warn",
                "https URL but COVERSET_TRUST_PROXY=false: behind a proxy, every client shares one address",
            )
        )
    else:
        checks.append(Check("proxy", "pass", "X-Forwarded-* ignored (direct connections)"))

    checks.extend(_data_dir_check(settings))
    if check_database:
        checks.append(_database_check(settings, database_probe))
    if check_store:
        checks.append(_store_check(settings, store_probe))
    checks.append(_cache_check(settings))
    checks.append(_state_check(settings))

    if settings.executor == "modal":
        source = _modal_token_source(env, modal_container)
        if source is None:
            checks.append(
                Check(
                    "executor",
                    "fail",
                    "modal, but no Modal credentials (modal token set, or MODAL_TOKEN_ID/SECRET)",
                )
            )
        else:
            checks.append(
                Check(
                    "executor",
                    "pass",
                    f"modal (environment {settings.modal_environment}), credentials from {source}",
                )
            )
    elif settings.local_allow_webhook_runs:
        checks.append(Check("executor", "warn", "local, running webhook-triggered runs on this host"))
    else:
        checks.append(
            Check(
                "executor", "warn", "local: webhook-triggered runs are refused (set COVERSET_EXECUTOR=modal)"
            )
        )

    stored = _stored_credentials(settings) if check_database and database_probe is None else None
    checks.extend(_github_checks(settings, env, stored))
    return checks


_MARK = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}


@click.group(name="deploy")
def deploy_group() -> None:
    """Deployment helpers (docs/deploy.md)."""


@deploy_group.command("check")
@click.option("--offline", is_flag=True, help="Skip the GET <public url>/healthz probe.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object per row.")
def deploy_check_command(offline: bool, as_json: bool) -> None:
    """Print this environment's deployment readiness (exit 1 when a check fails)."""
    from coverset.cli import load_settings

    try:
        settings = load_settings()
    except click.ClickException as exc:
        checks = [Check("configuration", "fail", exc.message)]
    else:
        checks = deployment_checks(settings, prober=None if offline else probe_healthz)
    width = max(len(check.name) for check in checks)
    for check in checks:
        if as_json:
            click.echo(json.dumps(asdict(check), sort_keys=True))
        else:
            click.echo(f"{_MARK[check.status]}  {check.name:<{width}}  {check.detail}")
    failed = sum(check.status == "fail" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    if not as_json:
        click.echo(f"\n{len(checks) - failed - warned} passed, {warned} warnings, {failed} failed")
    if failed:
        raise SystemExit(1)
