"""``coverset deploy check``: is this environment ready to serve coverset on a public URL?

Run it where ``coverset serve`` will run (a container, a VM, ``modal container exec`` into the web
container) with the same environment. Every row is ``PASS``, ``WARN`` or ``FAIL`` with the reason
and, for anything not passing, what to change; the exit status is 1 when any row fails. The checks
read configuration and local state only, plus one ``GET <public url>/healthz`` (skip it with
``--offline``), and never print a secret: tokens are reported by length and source, not value.

The checks are a pure function of a :class:`~coverset.config.Settings`, an environment mapping and
a URL prober (:func:`deployment_checks`), so tests drive them with hermetic settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

import click

from coverset.config import MIN_ADMIN_TOKEN_CHARS, Settings, is_private_host

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


def _database_check(settings: Settings) -> Check:
    """Compare the database's Alembic revision with the packaged head, without creating a file."""
    from coverset.db.session import sqlite_file_path

    db_file = sqlite_file_path(settings.database_url)
    if db_file is not None and not db_file.exists():
        return Check("database", "warn", f"{db_file} does not exist yet (serve creates and migrates it)")
    try:
        current, heads = asyncio.run(_revisions(settings.database_url))
    except Exception as exc:  # noqa: BLE001 - any driver error is a report row, not a crash
        return Check("database", "fail", f"cannot read the schema revision: {type(exc).__name__}: {exc}")
    if not current:
        return Check(
            "database", "warn", "not migrated yet (serve migrates at startup; or `coverset init-db`)"
        )
    if set(current) == heads:
        return Check("database", "pass", f"migrated to head {', '.join(sorted(heads))}")
    return Check(
        "database",
        "warn",
        f"at {', '.join(sorted(current))}, head is {', '.join(sorted(heads))} (serve upgrades at startup)",
    )


async def _revisions(url: str) -> tuple[tuple[str, ...], set[str]]:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from coverset.db.session import alembic_config, make_engine

    heads = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    engine = make_engine(url)
    try:
        async with engine.connect() as connection:
            current = await connection.run_sync(
                lambda sync: tuple(MigrationContext.configure(sync).get_current_heads())
            )
    finally:
        await engine.dispose()
    return current, heads


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


def _github_checks(settings: Settings, environ: Mapping[str, str]) -> list[Check]:
    from coverset.forge.registry import forge_mode
    from coverset.forge.signing import webhook_secret

    try:
        mode = forge_mode(settings, environ)
    except ValueError as exc:
        return [Check("github", "fail", str(exc))]
    checks: list[Check] = []
    if mode == "fake":
        checks.append(
            Check("github", "warn", "no App or token: fake forge only (create the App at <public url>/setup)")
        )
    elif mode == "github-app":
        key = settings.github_app_private_key_path
        if not key.is_file():
            checks.append(
                Check("github", "fail", f"App {settings.github_app_id}: private key {key} is missing")
            )
        elif not os.access(key, os.R_OK):
            checks.append(Check("github", "fail", f"App {settings.github_app_id}: cannot read {key}"))
        else:
            owners = ", ".join(settings.allowed_owners) or "-"
            checks.append(Check("github", "pass", f"App {settings.github_app_id}, allowed owners: {owners}"))
        if not settings.github_app_client_id or settings.github_app_client_secret is None:
            checks.append(
                Check("github login", "warn", "no App client id/secret: GitHub sign-in is unavailable")
            )
    else:
        checks.append(Check("github", "pass", "token mode (commit statuses and comments)"))
    _secret, problem = webhook_secret(settings)
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
) -> list[Check]:
    """Every readiness row for ``settings`` (``prober=None`` skips the network probe)."""
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
        checks.append(Check("secret key", "warn", f"random key file {settings.data_dir / 'secret_key'}"))

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
        checks.append(_database_check(settings))

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

    checks.extend(_github_checks(settings, env))
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
