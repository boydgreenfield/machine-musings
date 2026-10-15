"""The GitHub App manifest flow: build the manifest and exchange the code for the App's credentials.

``/setup`` posts :func:`build_manifest` to ``https://github.com/settings/apps/new?state=...``;
GitHub creates a private App and redirects to ``/setup/github/callback?code=...&state=...``, where
:func:`exchange_code` calls ``POST /app-manifests/{code}/conversions``. ``/setup`` then seals the
result into the database (:func:`coverset.forge.credentials.store_app`).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from githubkit import GitHub, UnauthAuthStrategy
from githubkit.exception import GitHubException, RequestFailed

from coverset.config import is_loopback_host
from coverset.forge.base import ForgeError
from coverset.forge.credentials import GITHUB_WEB, AppCredentials
from coverset.forge.github import pin_api_version, retry_decision

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
        "callback_urls": [f"{base}/auth/github/callback"],
        "request_oauth_on_install": False,
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
            pem=str(data["pem"]),
            client_id=data.get("client_id"),
            client_secret=str(data["client_secret"]),
            webhook_secret=str(data["webhook_secret"]),
            slug=str(data["slug"]),
            owner=(data.get("owner") or {}).get("login"),
            html_url=data.get("html_url"),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ForgeError(f"manifest conversion answered without {exc}") from None
    credentials.register_secrets()
    return credentials
