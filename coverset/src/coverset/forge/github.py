"""The GitHub adapter: App mode (check runs, statuses, comments) and token mode (statuses, comments).

Every request goes through githubkit with :func:`retry_decision` (server errors are retried only
for idempotent ``GET``/``PATCH``, so a 502 on a committed ``POST`` never duplicates a comment or
check run; rate-limit waits are capped at :data:`MAX_RATE_LIMIT_WAIT_S`) and carries
``X-GitHub-Api-Version: 2022-11-28`` (set by a request hook, so the installation-token exchange
inside githubkit's auth flow carries it too). App mode authenticates per repository with
``AppAuthStrategy(...).as_installation(installation_id, repository_ids=[id], permissions=...)``;
token mode uses ``COVERSET_GITHUB_TOKEN``. Writes are spaced by a :class:`TokenBucket` for the
secondary rate limit, and check-run annotations go out at most 50 per PATCH.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from githubkit import AppAuthStrategy, GitHub, TokenAuthStrategy
from githubkit.cache import MemCacheStrategy
from githubkit.exception import GitHubException, RateLimitExceeded, RequestFailed
from githubkit.retry import RetryOption

from coverset.config import Settings
from coverset.forge.base import (
    Annotation,
    CheckAction,
    CheckOutput,
    CheckStatus,
    CommitState,
    Conclusion,
    ForgeCapabilities,
    ForgeError,
    ForgeEvent,
    ForgeEventError,
    ForgeUnsupported,
    MirrorCredentials,
    RepoRef,
    check_full_name,
    check_sha,
)
from coverset.forge.github_events import parse_github_event
from coverset.forge.signing import verify_signature, webhook_secret
from coverset.forge.throttle import TokenBucket
from coverset.logging import register_secret

API_VERSION = "2022-11-28"
"""Pinned REST API version (2026-03-10 drops ``merge_commit_sha`` and other fields)."""

INSTALLATION_PERMISSIONS: dict[str, str] = {
    "checks": "write",
    "contents": "read",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
}
"""Permissions requested for per-repository installation tokens (the App's full set)."""

MIRROR_PERMISSIONS: dict[str, str] = {"contents": "read", "metadata": "read"}
"""Permissions of the token handed to ``git fetch`` for a mirror."""

INSTALLATION_TOKEN_RE = re.compile(r"ghs_[A-Za-z0-9._-]{36,}")
"""Stateless installation tokens (~520 chars; store in columns of at least 1024)."""

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
"""A GitHub login (bots end in ``[bot]``); checked before it is interpolated into an API path."""

ANNOTATIONS_PER_PATCH = 50
SUMMARY_LIMIT = 65_000
STATUS_DESCRIPTION_LIMIT = 140
COMMENT_PAGE_SIZE = 100
COMMENT_SCAN_PAGES = 10
_MINIMIZE = (
    "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED})"
    " { minimizedComment { isMinimized } } }"
)

Mode = Literal["app", "token"]

RETRY_SERVER_ERROR_METHODS = frozenset({"GET", "HEAD", "PATCH"})
"""Methods retried after a 5xx (idempotent: repeating them cannot duplicate a comment or check)."""
MAX_SERVER_ERROR_RETRIES = 3
MAX_RATE_LIMIT_WAIT_S = 60.0
"""Longest rate-limit wait honoured inline; a longer reset fails the call (posting is best effort)."""


def retry_decision(exc: GitHubException, retry_count: int) -> RetryOption:
    """githubkit ``auto_retry`` policy: one capped rate-limit retry; 5xx retries only when idempotent.

    A rate-limited request was not processed, so it is safe to resend whatever its method.
    """
    if isinstance(exc, RateLimitExceeded):
        wait = exc.retry_after.total_seconds()
        if retry_count < 1 and wait <= MAX_RATE_LIMIT_WAIT_S:
            return RetryOption(True, timedelta(seconds=max(wait, 0.0)))
        return RetryOption(False)
    if isinstance(exc, RequestFailed) and exc.response.status_code >= 500:
        method = str(exc.request.method).upper()
        if method in RETRY_SERVER_ERROR_METHODS and retry_count < MAX_SERVER_ERROR_RETRIES:
            return RetryOption(True, timedelta(seconds=(retry_count + 1) ** 2))
    return RetryOption(False)


class ForgeHTTPError(ForgeError):
    """A GitHub API call answered with an error status."""

    def __init__(self, method: str, path: str, status: int) -> None:
        super().__init__(f"{method} {path}: HTTP {status}")
        self.status = status


async def pin_api_version(request: httpx.Request) -> None:
    """httpx request hook: pin the REST API version on every request, auth-flow requests included."""
    request.headers["X-GitHub-Api-Version"] = API_VERSION
    request.headers["Accept"] = "application/vnd.github+json"


class GitHubForge:
    """The GitHub adapter (see the module docstring). Construct with :meth:`from_settings`."""

    name = "github"

    def __init__(
        self,
        *,
        mode: Mode,
        webhook_secret: str | None,
        app_id: int | None = None,
        private_key: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        bucket: TokenBucket | None = None,
    ) -> None:
        if mode == "app" and (app_id is None or not private_key):
            raise ValueError("App mode needs an app id and a private key")
        if mode == "token" and not token:
            raise ValueError("token mode needs a token")
        self.mode: Mode = mode
        self.app_id = app_id
        self._webhook_secret = webhook_secret
        self._private_key = private_key
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = token
        self._base_url = base_url
        self._transport = transport
        self._bucket = bucket or TokenBucket()
        self._cache = MemCacheStrategy()
        self._clients: dict[tuple[int, int], GitHub[Any]] = {}
        self._token_login: str | None = None
        for secret in (private_key, client_secret, token):
            register_secret(secret)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        bucket: TokenBucket | None = None,
    ) -> GitHubForge:
        """App mode when ``COVERSET_GITHUB_APP_ID`` is set, else token mode with ``COVERSET_GITHUB_TOKEN``."""
        secret, _problem = webhook_secret(settings)
        common: dict[str, Any] = {"webhook_secret": secret, "transport": transport, "bucket": bucket}
        if settings.github_app_id is not None:
            key = _read_private_key(settings.github_app_private_key_path)
            client_secret = settings.github_app_client_secret
            return cls(
                mode="app",
                app_id=settings.github_app_id,
                private_key=key,
                client_id=settings.github_app_client_id,
                client_secret=client_secret.get_secret_value() if client_secret else None,
                **common,
            )
        if settings.github_token is not None and settings.github_token.get_secret_value():
            return cls(mode="token", token=settings.github_token.get_secret_value(), **common)
        raise ValueError("GitHub forge needs COVERSET_GITHUB_APP_ID or COVERSET_GITHUB_TOKEN")

    # -- clients ---------------------------------------------------------------------------------

    def _github(self, auth: Any) -> GitHub[Any]:
        return GitHub(
            auth,
            base_url=self._base_url,
            async_transport=self._transport,
            http_cache=False,
            auto_retry=retry_decision,
            cache_strategy=self._cache,
            async_event_hooks={"request": [pin_api_version], "response": []},
            timeout=30.0,
        )

    def _app_strategy(self) -> AppAuthStrategy:
        assert self._private_key is not None
        return AppAuthStrategy(self.app_id, self._private_key, self._client_id, self._client_secret)

    def _app_client(self) -> GitHub[Any]:
        if self.mode != "app":
            raise ForgeUnsupported("App authentication needs App mode")
        return self._github(self._app_strategy())

    def _repo_client(self, repo: RepoRef) -> GitHub[Any]:
        if self.mode == "token":
            assert self._token is not None
            return self._github(TokenAuthStrategy(self._token))
        if repo.installation_id is None:
            raise ForgeError(f"no installation id known for {repo.full_name}")
        key = (repo.installation_id, repo.external_id)
        client = self._clients.get(key)
        if client is None:
            strategy = self._app_strategy().as_installation(
                repo.installation_id,
                repository_ids=[repo.external_id],
                permissions=INSTALLATION_PERMISSIONS,  # type: ignore[arg-type]
            )
            client = self._clients[key] = self._github(strategy)
        return client

    async def _call(
        self,
        client: GitHub[Any],
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        write: bool = False,
    ) -> Any:
        if write:
            await self._bucket.acquire()
        try:
            response = await client.arequest(method, path, json=json, params=params)
        except RequestFailed as exc:
            raise ForgeHTTPError(method, path, exc.response.status_code) from None
        except GitHubException as exc:
            raise ForgeError(f"{method} {path}: {type(exc).__name__}") from None
        return response.json() if response.content else None

    # -- webhooks --------------------------------------------------------------------------------

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        """Verify ``X-Hub-Signature-256`` over the raw body; False when no secret is configured."""
        return verify_signature(self._webhook_secret, headers, raw_body)

    def parse_event(self, headers: Mapping[str, str], raw_body: bytes) -> ForgeEvent:
        """Parse a verified delivery (see :mod:`coverset.forge.github_events`)."""
        return parse_github_event(headers, raw_body)

    # -- mirror credentials ----------------------------------------------------------------------

    async def fetch_mirror_credentials(self, installation_id: int | None, repo: RepoRef) -> MirrorCredentials:
        """App mode: a fresh installation token scoped to ``repo`` with ``contents: read``.

        Token mode returns the configured token. Either way the token is registered for log
        redaction and must only be used through :meth:`MirrorCredentials.git_config_args`.
        """
        if self.mode == "token":
            assert self._token is not None
            return MirrorCredentials(self._token)
        installation = installation_id if installation_id is not None else repo.installation_id
        if installation is None:
            raise ForgeError(f"no installation id known for {repo.full_name}")
        body = await self._call(
            self._app_client(),
            "POST",
            f"/app/installations/{int(installation)}/access_tokens",
            json={"repository_ids": [repo.external_id], "permissions": MIRROR_PERMISSIONS},
        )
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not INSTALLATION_TOKEN_RE.fullmatch(token):
            raise ForgeError("GitHub returned an unexpected installation token")
        register_secret(token)
        return MirrorCredentials(token, _parse_time(body.get("expires_at")))

    # -- check runs ------------------------------------------------------------------------------

    def _require_checks(self) -> None:
        if self.mode != "app":
            raise ForgeUnsupported("check runs need a GitHub App (token mode posts commit statuses)")

    async def create_check(
        self,
        repo: RepoRef,
        head_sha: str,
        name: str,
        *,
        external_id: str | None,
        details_url: str | None,
        status: CheckStatus = "queued",
        conclusion: Conclusion | None = None,
        output: CheckOutput | None = None,
        actions: Sequence[CheckAction] = (),
    ) -> int:
        """``POST /repos/{repo}/check-runs``; annotations past the first 50 follow as PATCHes."""
        self._require_checks()
        body: dict[str, Any] = {"name": name[:255], "head_sha": check_sha(head_sha, "head_sha")}
        if conclusion is not None:
            body["conclusion"] = conclusion
            body["status"] = "completed"
        else:
            body["status"] = status
        if external_id:
            body["external_id"] = external_id[:255]
        if details_url:
            body["details_url"] = details_url
        if actions:
            body["actions"] = [action.to_github() for action in actions[:3]]
        rest: tuple[Annotation, ...] = ()
        if output is not None:
            body["output"] = _output_body(output, output.annotations[:ANNOTATIONS_PER_PATCH])
            rest = output.annotations[ANNOTATIONS_PER_PATCH:]
        client = self._repo_client(repo)
        created = await self._call(client, "POST", f"{_repo_path(repo)}/check-runs", json=body, write=True)
        check_id = int(created["id"])
        if rest:
            assert output is not None
            await self._patch_annotations(client, repo, check_id, output, rest)
        return check_id

    async def update_check(
        self,
        repo: RepoRef,
        check_id: int,
        *,
        status: CheckStatus | None = None,
        conclusion: Conclusion | None = None,
        output: CheckOutput | None = None,
        actions: Sequence[CheckAction] | None = None,
        details_url: str | None = None,
    ) -> None:
        """PATCH the check run; with more than 50 annotations every PATCH repeats title and summary,
        and the status, conclusion and actions ride on the last one."""
        self._require_checks()
        client = self._repo_client(repo)
        final: dict[str, Any] = {}
        if status is not None:
            final["status"] = status
        if conclusion is not None:
            final["conclusion"] = conclusion
        if actions is not None:
            final["actions"] = [action.to_github() for action in actions[:3]]
        if details_url is not None:
            final["details_url"] = details_url
        annotations = output.annotations if output is not None else ()
        chunks = [
            annotations[i : i + ANNOTATIONS_PER_PATCH]
            for i in range(0, len(annotations), ANNOTATIONS_PER_PATCH)
        ] or [()]
        path = f"{_repo_path(repo)}/check-runs/{int(check_id)}"
        for index, chunk in enumerate(chunks):
            body: dict[str, Any] = {}
            if output is not None:
                body["output"] = _output_body(output, chunk)
            if index == len(chunks) - 1:
                body.update(final)
            await self._call(client, "PATCH", path, json=body, write=True)

    async def _patch_annotations(
        self,
        client: GitHub[Any],
        repo: RepoRef,
        check_id: int,
        output: CheckOutput,
        annotations: Sequence[Annotation],
    ) -> None:
        path = f"{_repo_path(repo)}/check-runs/{int(check_id)}"
        for start in range(0, len(annotations), ANNOTATIONS_PER_PATCH):
            chunk = tuple(annotations[start : start + ANNOTATIONS_PER_PATCH])
            await self._call(client, "PATCH", path, json={"output": _output_body(output, chunk)}, write=True)

    # -- statuses and comments -------------------------------------------------------------------

    async def set_commit_status(
        self,
        repo: RepoRef,
        sha: str,
        state: CommitState,
        *,
        context: str,
        description: str,
        target_url: str | None,
    ) -> None:
        """``POST /repos/{repo}/statuses/{sha}``."""
        body: dict[str, Any] = {
            "state": state,
            "context": context[:255],
            "description": description[:STATUS_DESCRIPTION_LIMIT],
        }
        if target_url:
            body["target_url"] = target_url
        path = f"{_repo_path(repo)}/statuses/{check_sha(sha, 'sha')}"
        await self._call(self._repo_client(repo), "POST", path, json=body, write=True)

    async def upsert_pr_comment(
        self, repo: RepoRef, pr_number: int, marker: str, body: str, prior_id: int | None
    ) -> int:
        """Edit the stored comment, else the newest own comment carrying ``marker``, else create one.

        "Own" means authored through this App (``performed_via_github_app.id == app_id``) or, in
        token mode, by the token's user; a marker in anyone else's comment is ignored.
        """
        if marker not in body:
            body = f"{body}\n{marker}\n"
        client = self._repo_client(repo)
        if prior_id is not None:
            try:
                await self._edit_comment(client, repo, prior_id, body)
                return prior_id
            except ForgeHTTPError as exc:
                if exc.status not in (403, 404, 410):
                    raise
        found = await self._find_own_comment(client, repo, pr_number, marker)
        if found is not None:
            await self._edit_comment(client, repo, found, body)
            return found
        created = await self._call(
            client,
            "POST",
            f"{_repo_path(repo)}/issues/{int(pr_number)}/comments",
            json={"body": body},
            write=True,
        )
        return int(created["id"])

    async def _edit_comment(self, client: GitHub[Any], repo: RepoRef, comment_id: int, body: str) -> None:
        path = f"{_repo_path(repo)}/issues/comments/{int(comment_id)}"
        await self._call(client, "PATCH", path, json={"body": body}, write=True)

    async def _find_own_comment(
        self, client: GitHub[Any], repo: RepoRef, pr_number: int, marker: str
    ) -> int | None:
        match: int | None = None
        path = f"{_repo_path(repo)}/issues/{int(pr_number)}/comments"
        for page in range(1, COMMENT_SCAN_PAGES + 1):
            params = {"per_page": COMMENT_PAGE_SIZE, "page": page}
            comments = await self._call(client, "GET", path, params=params)
            if not isinstance(comments, list):
                break
            for comment in comments:
                carries_marker = isinstance(comment, dict) and marker in str(comment.get("body") or "")
                if carries_marker and await self._is_own(client, comment):
                    match = int(comment["id"])
            if len(comments) < COMMENT_PAGE_SIZE:
                break
        return match

    async def _is_own(self, client: GitHub[Any], comment: Mapping[str, Any]) -> bool:
        if self.mode == "app":
            via = comment.get("performed_via_github_app")
            return isinstance(via, Mapping) and via.get("id") == self.app_id
        user = comment.get("user")
        login = user.get("login") if isinstance(user, Mapping) else None
        return login is not None and login == await self._token_user(client)

    async def _token_user(self, client: GitHub[Any]) -> str | None:
        if self._token_login is None:
            me = await self._call(client, "GET", "/user")
            self._token_login = me.get("login") if isinstance(me, dict) else None
        return self._token_login

    async def hide_comment(self, repo: RepoRef, comment_id: int) -> None:
        """Minimise a comment as OUTDATED through GraphQL ``minimizeComment``."""
        client = self._repo_client(repo)
        comment = await self._call(client, "GET", f"{_repo_path(repo)}/issues/comments/{int(comment_id)}")
        node_id = comment.get("node_id") if isinstance(comment, dict) else None
        if not isinstance(node_id, str):
            raise ForgeError("comment has no node id")
        answer = await self._call(
            client, "POST", "/graphql", json={"query": _MINIMIZE, "variables": {"id": node_id}}, write=True
        )
        if isinstance(answer, dict) and answer.get("errors"):
            raise ForgeError("minimizeComment failed")

    async def collaborator_permission(self, repo: RepoRef, login: str) -> str:
        """``GET /repos/{repo}/collaborators/{login}/permission``: the ``role_name`` (``maintain`` and
        ``triage`` included), else the coarser ``permission``; ``none`` for a non-collaborator (404)."""
        if not LOGIN_RE.fullmatch(login):
            raise ForgeError(f"implausible login {login!r}")
        path = f"{_repo_path(repo)}/collaborators/{login}/permission"
        try:
            body = await self._call(self._repo_client(repo), "GET", path)
        except ForgeHTTPError as exc:
            if exc.status == 404:
                return "none"
            raise
        if not isinstance(body, dict):
            raise ForgeError("GitHub returned no permission")
        role = body.get("role_name") or body.get("permission")
        if not isinstance(role, str):
            raise ForgeError("GitHub returned no permission")
        return role.lower()

    def capabilities(self) -> ForgeCapabilities:
        """App mode can do everything; token mode has no check runs (so no buttons)."""
        app = self.mode == "app"
        return ForgeCapabilities(
            name=f"github-{self.mode}",
            check_runs=app,
            commit_statuses=True,
            pr_comments=True,
            hide_comments=True,
            mirror_credentials=True,
            check_actions=app,
        )


def _repo_path(repo: RepoRef) -> str:
    try:
        return f"/repos/{check_full_name(repo.full_name)}"
    except ForgeEventError as exc:
        raise ForgeError(str(exc)) from None


def _output_body(output: CheckOutput, annotations: Sequence[Annotation]) -> dict[str, Any]:
    body: dict[str, Any] = {"title": output.title[:255], "summary": output.summary[:SUMMARY_LIMIT]}
    if output.text:
        body["text"] = output.text[:SUMMARY_LIMIT]
    if annotations:
        body["annotations"] = [annotation.to_github() for annotation in annotations]
    return body


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _read_private_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read the GitHub App private key at {path}: {exc.strerror}") from None
    register_secret(key)
    return key
