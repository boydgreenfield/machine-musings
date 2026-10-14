"""GitHub sign-in for coverset: the OAuth web flow of the GitHub App, and the calls that ask GitHub
which repositories a signed-in user may see.

Why the App's own client id and secret: a GitHub App is also an OAuth client, and a user token it
issues (``ghu_...``) carries the *intersection* of the App's permissions and the user's own, so
coverset never needs to request OAuth scopes (``scope`` is left empty on purpose) and a user can
never do more through coverset than they could on GitHub. New Apps issue expiring user tokens
(8 h) with a refresh token (``ghr_...``, 6 months); :meth:`GitHubOAuth.refresh` renews them.

Everything here is pure I/O against two hosts: ``https://github.com`` for the authorization and
token endpoints and ``https://api.github.com`` for the REST calls. Both are overridable, and an
``httpx`` transport can be injected, so the tests stub GitHub with :class:`httpx.MockTransport`
and never touch the network. Tokens are never logged: the redactor masks every ``gh?_`` string.

Rate-limit cost: a user token has its own 5,000 requests/hour budget. A sign-in costs one call
(``/user``); a repository-access refresh costs one call per installation the user belongs to
(``/user/installations/{id}/repositories``, 100 repositories a page) plus one ``/repos/{owner}/{name}``
call per repository that is not covered by an installation listing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from githubkit import GitHub, TokenAuthStrategy
from githubkit.exception import GitHubException, RequestFailed

from coverset.forge.base import ForgeError
from coverset.forge.github import pin_api_version, retry_decision

log = logging.getLogger(__name__)

GITHUB_WEB: Final = "https://github.com"
CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
"""An authorization code as GitHub mints them (20 hex chars today); anything else is not sent on."""
FULL_NAME_RE: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
INSTALLATION_PAGE: Final = 100
INSTALLATION_MAX_PAGES: Final = 50
"""At most 5,000 repositories per installation are listed; beyond that the per-repo fallback runs."""
TIMEOUT_S: Final = 20.0
REVOKED_SLUGS: Final = frozenset({"bad_refresh_token"})
"""Token-endpoint error slugs that mean the user's authorization is gone, not a transient fault."""

Permission = str
"""``read``, ``write`` or ``admin`` (:data:`coverset.db.models.RepoPermission`)."""


class OAuthError(ForgeError):
    """GitHub refused or garbled an OAuth step (the message never contains a token)."""


class OAuthRevokedError(OAuthError):
    """GitHub no longer accepts the user's credentials: a REST call answered 401, or the refresh
    token was refused (``bad_refresh_token``). Transient failures are plain :class:`OAuthError`;
    this one tells the caller to forget what it knew about the user's access."""


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """What the token endpoint answers (secrets hidden from ``repr``)."""

    access_token: str = field(repr=False)
    expires_in: int | None
    """Seconds until ``access_token`` expires (None for Apps with non-expiring user tokens)."""
    refresh_token: str | None = field(repr=False)
    refresh_token_expires_in: int | None


@dataclass(frozen=True, slots=True)
class GitHubUser:
    """The ``/user`` answer: the numeric id is the stable identity, the login is for display."""

    id: int
    login: str
    name: str | None
    avatar_url: str | None


def permission_from_github(permissions: Mapping[str, Any] | None) -> Permission | None:
    """Collapse GitHub's ``permissions`` object to what coverset distinguishes.

    ``admin`` stays ``admin``; ``maintain`` and ``push`` become ``write``; ``triage`` and ``pull``
    become ``read``; an object with no true flag (or no object at all) means no access.
    """
    if not permissions:
        return None
    if permissions.get("admin"):
        return "admin"
    if permissions.get("maintain") or permissions.get("push"):
        return "write"
    if permissions.get("triage") or permissions.get("pull"):
        return "read"
    return None


class GitHubOAuth:
    """The OAuth client of one GitHub App (see the module docstring)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        web_base_url: str = GITHUB_WEB,
        api_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("GitHub sign-in needs the App's client id and client secret")
        self.client_id = client_id
        self._client_secret = client_secret
        self._web = web_base_url.rstrip("/")
        self._api = api_base_url
        self._transport = transport

    # -- the web flow ------------------------------------------------------------------------------

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser; no ``scope`` (the App's permissions are the ceiling)."""
        query = urlencode({"client_id": self.client_id, "redirect_uri": redirect_uri, "state": state})
        return f"{self._web}/login/oauth/authorize?{query}"

    async def exchange_code(self, code: str, *, redirect_uri: str) -> OAuthTokens:
        """``POST /login/oauth/access_token`` for the one-time ``code``."""
        if not CODE_RE.fullmatch(code):
            raise OAuthError("malformed authorization code")
        return await self._token_request({"code": code, "redirect_uri": redirect_uri})

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        """Renew an expired user token (GitHub rotates the refresh token too)."""
        if not refresh_token:
            raise OAuthError("no refresh token")
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _token_request(self, params: dict[str, str]) -> OAuthTokens:
        data = {"client_id": self.client_id, "client_secret": self._client_secret, **params}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT_S) as http:
                response = await http.post(
                    f"{self._web}/login/oauth/access_token",
                    data=data,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise OAuthError(f"token request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise OAuthError(f"token request failed: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise OAuthError("token request answered with something other than JSON") from None
        if not isinstance(body, dict):
            raise OAuthError("token request answered with something other than an object")
        if body.get("error"):
            # GitHub answers 200 with {"error": "bad_verification_code", ...}; the slug is public.
            slug = re.sub(r"[^a-z0-9_]", "", str(body["error"]).lower())[:64] or "error"
            if slug in REVOKED_SLUGS:
                raise OAuthRevokedError(f"GitHub refused the token request: {slug}")
            raise OAuthError(f"GitHub refused the token request: {slug}")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise OAuthError("token request answered without an access token")
        refresh = body.get("refresh_token")
        return OAuthTokens(
            access_token=token,
            expires_in=_optional_int(body.get("expires_in")),
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            refresh_token_expires_in=_optional_int(body.get("refresh_token_expires_in")),
        )

    # -- the REST calls ----------------------------------------------------------------------------

    def _github(self, token: str) -> GitHub[Any]:
        return GitHub(
            TokenAuthStrategy(token),
            base_url=self._api,
            async_transport=self._transport,
            http_cache=False,
            auto_retry=retry_decision,
            async_event_hooks={"request": [pin_api_version], "response": []},
            timeout=TIMEOUT_S,
        )

    async def _get(self, token: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            response = await self._github(token).arequest("GET", path, params=params)
        except RequestFailed as exc:
            if exc.response.status_code == 401:
                raise OAuthRevokedError(f"GET {path}: HTTP 401 (the authorization was revoked)") from None
            raise OAuthHTTPError(path, exc.response.status_code) from None
        except GitHubException as exc:
            raise OAuthError(f"GET {path}: {type(exc).__name__}") from None
        return response.json() if response.content else None

    async def fetch_user(self, token: str) -> GitHubUser:
        """``GET /user``: who the token belongs to."""
        body = await self._get(token, "/user")
        try:
            return GitHubUser(
                id=int(body["id"]),
                login=str(body["login"]),
                name=_optional_str(body.get("name")),
                avatar_url=_optional_str(body.get("avatar_url")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OAuthError(f"/user answered without {exc}") from None

    async def installation_repositories(
        self, token: str, installation_id: int
    ) -> dict[int, Permission] | None:
        """``GET /user/installations/{id}/repositories``: repository id → permission, for every
        repository of that installation the user can see. None when the user is not part of the
        installation (403/404), so the caller falls back to per-repository checks."""
        found: dict[int, Permission] = {}
        for page in range(1, INSTALLATION_MAX_PAGES + 1):
            try:
                body = await self._get(
                    token,
                    f"/user/installations/{int(installation_id)}/repositories",
                    {"per_page": INSTALLATION_PAGE, "page": page},
                )
            except OAuthHTTPError as exc:
                if exc.status in (403, 404):
                    return None
                raise
            repos = body.get("repositories") if isinstance(body, dict) else None
            if not isinstance(repos, list):
                raise OAuthError("installation repositories answered without a list")
            for item in repos:
                if not isinstance(item, dict):
                    continue
                try:
                    repo_id = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                permission = permission_from_github(item.get("permissions"))
                if permission is not None:
                    found[repo_id] = permission
            if len(repos) < INSTALLATION_PAGE:
                break
        return found

    async def repo_permission(self, token: str, full_name: str) -> Permission | None:
        """``GET /repos/{owner}/{name}``: the token's permission on one repository (None = no access,
        which GitHub reports as 404 so that private repositories stay invisible)."""
        if not FULL_NAME_RE.fullmatch(full_name):
            raise OAuthError(f"implausible repository name {full_name!r}")
        try:
            body = await self._get(token, f"/repos/{full_name}")
        except OAuthHTTPError as exc:
            if exc.status in (403, 404):
                return None
            raise
        if not isinstance(body, dict):
            raise OAuthError("repository answered without an object")
        return permission_from_github(body.get("permissions"))


class OAuthHTTPError(OAuthError):
    """A REST call answered with an error status."""

    def __init__(self, path: str, status: int) -> None:
        super().__init__(f"GET {path}: HTTP {status}")
        self.status = status


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
