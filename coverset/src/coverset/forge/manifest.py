"""The GitHub App manifest flow: build the manifest, exchange the code, store the credentials.

``/setup`` posts :func:`build_manifest` to ``https://github.com/settings/apps/new?state=...``;
GitHub creates a private App and redirects to ``/setup/github/callback?code=...&state=...``, where
:func:`exchange_code` calls ``POST /app-manifests/{code}/conversions`` and
:func:`write_credentials` writes the PEM and the ``.env`` keys atomically with mode 0600.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from githubkit import GitHub, UnauthAuthStrategy
from githubkit.exception import GitHubException, RequestFailed

from coverset.config import is_loopback_host
from coverset.forge.base import ForgeError
from coverset.forge.github import pin_api_version, retry_decision
from coverset.logging import register_secret

GITHUB_WEB = "https://github.com"
APP_PERMISSIONS: dict[str, str] = {
    "checks": "write",
    "contents": "read",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
}
APP_EVENTS: tuple[str, ...] = ("pull_request", "push", "check_run", "check_suite", "repository")
"""Subscribed events (``installation`` events reach every App without subscribing). ``repository``
keeps the stored privacy current (``privatized``/``publicized``); it needs only ``metadata: read``."""

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
ENV_KEYS = (
    "COVERSET_GITHUB_APP_ID",
    "COVERSET_GITHUB_APP_CLIENT_ID",
    "COVERSET_GITHUB_APP_CLIENT_SECRET",
    "COVERSET_GITHUB_WEBHOOK_SECRET",
    "COVERSET_GITHUB_APP_PRIVATE_KEY_PATH",
)
"""``.env`` keys the callback owns (an existing value means "already set up")."""


class CredentialsExist(Exception):
    """Credentials are already stored; pass ``overwrite=True`` to replace them."""


def build_manifest(public_url: str, *, name: str | None = None) -> dict[str, Any]:
    """The App manifest for a coverset served at ``public_url`` (private, minimal permissions)."""
    base = public_url.rstrip("/")
    host = urlsplit(base).hostname or "local"
    return {
        "name": (name or f"coverset ({host})")[:34],
        "url": base,
        "description": "Anticipatory CI and semantic coverage for agent-written Python.",
        "public": False,
        "hook_attributes": {"url": f"{base}/webhooks/github", "active": True},
        "redirect_url": f"{base}/setup/github/callback",
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": list(APP_EVENTS),
    }


def manifest_form_action(state: str, organization: str | None = None) -> str:
    """Where the manifest form posts: the user's or an organization's "new App" page."""
    if organization:
        if not OWNER_RE.fullmatch(organization):
            raise ValueError(f"not a GitHub organization name: {organization!r}")
        return f"{GITHUB_WEB}/organizations/{organization}/settings/apps/new?state={quote(state)}"
    return f"{GITHUB_WEB}/settings/apps/new?state={quote(state)}"


def public_url_reachable(public_url: str) -> bool:
    """False for loopback URLs, which GitHub cannot deliver webhooks to."""
    return not is_loopback_host(urlsplit(public_url).hostname or "")


@dataclass(frozen=True, slots=True)
class AppCredentials:
    """What ``POST /app-manifests/{code}/conversions`` returns (secrets hidden from ``repr``)."""

    app_id: int
    slug: str
    owner: str | None
    html_url: str | None
    client_id: str | None
    client_secret: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    pem: str = field(repr=False)

    @property
    def install_url(self) -> str:
        """Where to install the new App on repositories."""
        return f"{GITHUB_WEB}/apps/{quote(self.slug)}/installations/new"


async def exchange_code(
    code: str,
    *,
    base_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AppCredentials:
    """Exchange a manifest ``code`` (single use, valid one hour) for the new App's credentials."""
    if not CODE_RE.fullmatch(code):
        raise ForgeError("malformed manifest code")
    github: GitHub[Any] = GitHub(
        UnauthAuthStrategy(),
        base_url=base_url,
        async_transport=transport,
        http_cache=False,
        auto_retry=retry_decision,
        async_event_hooks={"request": [pin_api_version], "response": []},
        timeout=30.0,
    )
    try:
        response = await github.arequest("POST", f"/app-manifests/{code}/conversions")
    except RequestFailed as exc:
        raise ForgeError(f"manifest conversion failed: HTTP {exc.response.status_code}") from None
    except GitHubException as exc:
        raise ForgeError(f"manifest conversion failed: {type(exc).__name__}") from None
    data = response.json()
    try:
        credentials = AppCredentials(
            app_id=int(data["id"]),
            slug=str(data["slug"]),
            owner=(data.get("owner") or {}).get("login"),
            html_url=data.get("html_url"),
            client_id=data.get("client_id"),
            client_secret=str(data["client_secret"]),
            webhook_secret=str(data["webhook_secret"]),
            pem=str(data["pem"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ForgeError(f"manifest conversion answered without {exc}") from None
    for secret in (credentials.client_secret, credentials.webhook_secret, credentials.pem):
        register_secret(secret)
    return credentials


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a 0600 temp file in the same directory and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@,+-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _env_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        stripped = line.strip().removeprefix("export ").strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            if value.strip().strip("'\""):
                keys.add(key.strip())
    return keys


def existing_credentials(env_path: Path, key_path: Path) -> list[str]:
    """What a callback would overwrite: the PEM file and/or owned keys already set in ``.env``."""
    found = []
    if key_path.exists():
        found.append(str(key_path))
    if env_path.exists():
        found.extend(sorted(_env_keys(env_path.read_text(encoding="utf-8")) & set(ENV_KEYS)))
    return found


def merge_env(text: str, values: Mapping[str, str]) -> str:
    """``text`` with each key in ``values`` replaced in place (or appended); other lines kept."""
    lines = text.splitlines()
    pending = dict(values)
    out = []
    for line in lines:
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key in pending and not line.lstrip().startswith("#"):
            out.append(f"{key}={_env_value(pending.pop(key))}")
        else:
            out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# Written by coverset /setup (GitHub App manifest flow)")
        out.extend(f"{key}={_env_value(value)}" for key, value in pending.items())
    return "\n".join(out) + "\n"


def write_credentials(
    credentials: AppCredentials,
    *,
    env_path: Path,
    key_path: Path,
    allowed_owners: list[str],
    overwrite: bool = False,
) -> None:
    """Store the PEM at ``key_path`` and the App's keys in ``env_path`` (both mode 0600).

    Raises :class:`CredentialsExist` when either already holds credentials and not ``overwrite``.
    ``COVERSET_ALLOWED_OWNERS`` is added (as the App's owner) only when none is configured.
    """
    if not overwrite and existing_credentials(env_path, key_path):
        raise CredentialsExist(", ".join(existing_credentials(env_path, key_path)))
    current = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    values = {
        "COVERSET_GITHUB_APP_ID": str(credentials.app_id),
        "COVERSET_GITHUB_APP_CLIENT_ID": credentials.client_id or "",
        "COVERSET_GITHUB_APP_CLIENT_SECRET": credentials.client_secret,
        "COVERSET_GITHUB_WEBHOOK_SECRET": credentials.webhook_secret,
        "COVERSET_GITHUB_APP_PRIVATE_KEY_PATH": str(key_path),
    }
    if not allowed_owners and "COVERSET_ALLOWED_OWNERS" not in _env_keys(current) and credentials.owner:
        values["COVERSET_ALLOWED_OWNERS"] = credentials.owner
    _atomic_write(key_path, credentials.pem if credentials.pem.endswith("\n") else credentials.pem + "\n")
    _atomic_write(env_path, merge_env(current, values))
