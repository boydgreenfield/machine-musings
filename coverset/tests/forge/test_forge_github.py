"""The GitHub adapter against a mocked githubkit transport (``httpx.MockTransport``): API version
header on every request, installation tokens, annotation chunking, write spacing, comment upserts,
token mode and the registry."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from githubkit.exception import RateLimitExceeded, RequestFailed
from githubkit.response import Response as GitHubResponse
from pydantic import SecretStr

from coverset.config import Settings
from coverset.forge.base import Annotation, CheckOutput, ForgeError, ForgeUnsupported, RepoRef
from coverset.forge.fake import FakeForge
from coverset.forge.github import API_VERSION, ForgeHTTPError, GitHubForge, retry_decision
from coverset.forge.registry import forge_mode, get_forge
from coverset.forge.throttle import TokenBucket
from coverset.forge.verdict import STICKY_MARKER
from coverset.logging import redact

APP_ID = 1001
INSTALLATION_TOKEN = "ghs_" + "Ab1._-" * 86
REPO = RepoRef(external_id=424242, full_name="coverset-demo/ledger", owner_id=777, installation_id=55)
SHA = "c" * 40

Handler = Callable[[httpx.Request], httpx.Response | None]


class MockGitHub:
    """Routes requests to handlers by ``(method, path)``; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.route(
            "POST",
            "/app/installations/55/access_tokens",
            201,
            {"token": INSTALLATION_TOKEN, "expires_at": "2099-01-01T00:00:00Z"},
        )

    def route(self, method: str, path: str, status: int = 200, body: Any = None) -> None:
        self.routes[(method, path)] = lambda _request: httpx.Response(
            status, json=body if body is not None else {}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return handler(request)

    def calls(self, method: str | None = None, prefix: str = "") -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if (method is None or r.method == method) and r.url.path.startswith(prefix)
        ]


class FakeClock:
    """A monotonic clock advanced only by its own ``sleep``."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content or b"{}")


@pytest.fixture
def github() -> MockGitHub:
    mock = MockGitHub()
    mock.route("POST", "/repos/coverset-demo/ledger/check-runs", 201, {"id": 7})
    mock.route("PATCH", "/repos/coverset-demo/ledger/check-runs/7", 200, {"id": 7})
    return mock


def app_forge(github: MockGitHub, pem: str, bucket: TokenBucket | None = None) -> GitHubForge:
    return GitHubForge(
        mode="app",
        webhook_secret="s" * 20,
        app_id=APP_ID,
        private_key=pem,
        transport=httpx.MockTransport(github),
        bucket=bucket or TokenBucket(per_minute=1e9, burst=1000),
    )


def token_forge(github: MockGitHub, bucket: TokenBucket | None = None) -> GitHubForge:
    return GitHubForge(
        mode="token",
        webhook_secret="s" * 20,
        token="github_pat_" + "z" * 40,
        transport=httpx.MockTransport(github),
        bucket=bucket or TokenBucket(per_minute=1e9, burst=1000),
    )


def annotations(n: int) -> tuple[Annotation, ...]:
    return tuple(Annotation(f"tests/test_{i}.py", i + 1, i + 1, "failure", f"boom {i}") for i in range(n))


@pytest.mark.security
async def test_app_mode_pins_the_api_version_on_every_request(github: MockGitHub, rsa_pem: str) -> None:
    forge = app_forge(github, rsa_pem)
    check_id = await forge.create_check(
        REPO, SHA, "coverset", external_id="run_1", details_url="https://cs.example/runs/run_1"
    )
    assert check_id == 7
    exchange, create = github.requests
    assert all(r.headers["X-GitHub-Api-Version"] == API_VERSION for r in github.requests)
    assert exchange.url.path == "/app/installations/55/access_tokens"
    assert exchange.headers["Authorization"].startswith("Bearer ")
    assert body_of(exchange)["repository_ids"] == [424242]
    assert body_of(exchange)["permissions"] == {
        "checks": "write",
        "contents": "read",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
    }
    assert create.headers["Authorization"] == f"token {INSTALLATION_TOKEN}"
    sent = body_of(create)
    assert sent == {
        "name": "coverset",
        "head_sha": SHA,
        "status": "queued",
        "external_id": "run_1",
        "details_url": "https://cs.example/runs/run_1",
    }
    await forge.update_check(REPO, 7, status="in_progress")
    assert len(github.calls("POST", "/app/")) == 1, "the installation token is cached"


async def test_update_check_chunks_annotations_by_50(github: MockGitHub, rsa_pem: str) -> None:
    forge = app_forge(github, rsa_pem)
    output = CheckOutput(title="FAIL", summary="2 failed", annotations=annotations(120))
    await forge.update_check(REPO, 7, conclusion="failure", output=output, actions=())
    patches = [body_of(r) for r in github.calls("PATCH")]
    assert [len(p["output"].get("annotations", [])) for p in patches] == [50, 50, 20]
    assert all(p["output"]["title"] == "FAIL" and p["output"]["summary"] == "2 failed" for p in patches)
    assert ["conclusion" in p for p in patches] == [False, False, True]
    assert patches[-1]["conclusion"] == "failure" and patches[-1]["actions"] == []
    sent = [a["message"] for p in patches for a in p["output"]["annotations"]]
    assert sent == [f"boom {i}" for i in range(120)]


async def test_create_check_sends_annotations_past_50_as_patches(github: MockGitHub, rsa_pem: str) -> None:
    forge = app_forge(github, rsa_pem)
    output = CheckOutput(title="FAIL", summary="s", annotations=annotations(75))
    await forge.create_check(
        REPO, SHA, "coverset", external_id=None, details_url=None, conclusion="failure", output=output
    )
    (create,) = github.calls("POST", "/repos/")
    assert len(body_of(create)["output"]["annotations"]) == 50
    assert body_of(create)["status"] == "completed" and body_of(create)["conclusion"] == "failure"
    (patch,) = github.calls("PATCH")
    assert len(body_of(patch)["output"]["annotations"]) == 25


@pytest.mark.security
async def test_writes_are_spaced_by_the_token_bucket(github: MockGitHub, rsa_pem: str) -> None:
    clock = FakeClock()
    bucket = TokenBucket(per_minute=100, burst=1, clock=clock.clock, sleep=clock.sleep)
    forge = app_forge(github, rsa_pem, bucket)
    output = CheckOutput(title="FAIL", summary="s", annotations=annotations(200))
    await forge.update_check(REPO, 7, conclusion="failure", output=output)
    assert len(github.calls("PATCH")) == 4
    assert clock.sleeps == pytest.approx([0.6, 0.6, 0.6])


async def test_token_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(per_minute=60, burst=2, clock=clock.clock, sleep=clock.sleep)
    assert [await bucket.acquire() for _ in range(3)] == [0.0, 0.0, pytest.approx(1.0)]
    clock.now += 10
    assert await bucket.acquire() == 0.0 and await bucket.acquire() == 0.0
    with pytest.raises(ValueError):
        TokenBucket(per_minute=0)


@pytest.mark.security
async def test_upsert_falls_back_to_a_marker_scan_of_own_comments(github: MockGitHub, rsa_pem: str) -> None:
    comments = [
        {
            "id": 50,
            "body": f"forged {STICKY_MARKER}",
            "user": {"login": "mallory"},
            "performed_via_github_app": None,
        },
        {"id": 51, "body": f"other app {STICKY_MARKER}", "performed_via_github_app": {"id": 42}},
        {"id": 77, "body": f"old verdict\n{STICKY_MARKER}", "performed_via_github_app": {"id": APP_ID}},
        {"id": 78, "body": "unrelated", "performed_via_github_app": {"id": APP_ID}},
    ]
    github.route("GET", "/repos/coverset-demo/ledger/issues/1/comments", 200, comments)
    github.route("PATCH", "/repos/coverset-demo/ledger/issues/comments/77", 200, {"id": 77})
    forge = app_forge(github, rsa_pem)
    comment_id = await forge.upsert_pr_comment(REPO, 1, STICKY_MARKER, "new verdict", prior_id=999)
    assert comment_id == 77
    paths = [(r.method, r.url.path) for r in github.requests if r.url.path.startswith("/repos/")]
    assert paths == [
        ("PATCH", "/repos/coverset-demo/ledger/issues/comments/999"),
        ("GET", "/repos/coverset-demo/ledger/issues/1/comments"),
        ("PATCH", "/repos/coverset-demo/ledger/issues/comments/77"),
    ]
    assert body_of(github.requests[-1])["body"] == f"new verdict\n{STICKY_MARKER}\n"
    assert github.calls("GET", "/repos/")[0].url.params["per_page"] == "100"


async def test_upsert_creates_when_nothing_matches(github: MockGitHub, rsa_pem: str) -> None:
    github.route("GET", "/repos/coverset-demo/ledger/issues/1/comments", 200, [])
    github.route("POST", "/repos/coverset-demo/ledger/issues/1/comments", 201, {"id": 88})
    forge = app_forge(github, rsa_pem)
    assert await forge.upsert_pr_comment(REPO, 1, STICKY_MARKER, f"x\n{STICKY_MARKER}", prior_id=None) == 88


async def test_upsert_edits_the_stored_comment_directly(github: MockGitHub, rsa_pem: str) -> None:
    github.route("PATCH", "/repos/coverset-demo/ledger/issues/comments/5", 200, {"id": 5})
    forge = app_forge(github, rsa_pem)
    assert await forge.upsert_pr_comment(REPO, 1, STICKY_MARKER, "x", prior_id=5) == 5
    assert not github.calls("GET", "/repos/")


async def test_token_mode_statuses_comments_and_no_checks(github: MockGitHub) -> None:
    github.route("GET", "/user", 200, {"login": "coverset-bot"})
    github.route(
        "GET",
        "/repos/coverset-demo/ledger/issues/2/comments",
        200,
        [
            {"id": 60, "body": STICKY_MARKER, "user": {"login": "mallory"}},
            {"id": 61, "body": STICKY_MARKER, "user": {"login": "coverset-bot"}},
        ],
    )
    github.route("PATCH", "/repos/coverset-demo/ledger/issues/comments/61", 200, {"id": 61})
    github.route("POST", f"/repos/coverset-demo/ledger/statuses/{SHA}", 201, {})
    forge = token_forge(github)
    assert forge.capabilities().check_runs is False and forge.capabilities().commit_statuses is True
    with pytest.raises(ForgeUnsupported):
        await forge.create_check(REPO, SHA, "coverset", external_id=None, details_url=None)
    assert await forge.upsert_pr_comment(REPO, 2, STICKY_MARKER, "v", prior_id=None) == 61
    await forge.set_commit_status(
        REPO, SHA, "failure", context="coverset", description="x" * 300, target_url=None
    )
    status = body_of(github.calls("POST", "/repos/")[-1])
    assert status == {"state": "failure", "context": "coverset", "description": "x" * 140}
    assert all(r.headers["Authorization"].startswith("token github_pat_") for r in github.requests)
    assert all(r.headers["X-GitHub-Api-Version"] == API_VERSION for r in github.requests)
    credentials = await forge.fetch_mirror_credentials(None, REPO)
    assert credentials.token.startswith("github_pat_")


@pytest.mark.security
async def test_mirror_credentials_are_scoped_and_redacted(github: MockGitHub, rsa_pem: str) -> None:
    forge = app_forge(github, rsa_pem)
    credentials = await forge.fetch_mirror_credentials(55, REPO)
    assert credentials.token == INSTALLATION_TOKEN and credentials.expires_at is not None
    (exchange,) = github.requests
    assert body_of(exchange) == {
        "repository_ids": [424242],
        "permissions": {"contents": "read", "metadata": "read"},
    }
    masked = redact(f"git said: token {INSTALLATION_TOKEN} rejected")
    assert INSTALLATION_TOKEN[4:24] not in masked and masked.startswith("git said: token ")
    github.route(
        "POST", "/app/installations/56/access_tokens", 201, {"token": "not-a-token", "expires_at": None}
    )
    with pytest.raises(ForgeError):
        await forge.fetch_mirror_credentials(56, REPO)


async def test_hide_comment_uses_graphql(github: MockGitHub, rsa_pem: str) -> None:
    github.route("GET", "/repos/coverset-demo/ledger/issues/comments/9", 200, {"id": 9, "node_id": "IC_9"})
    github.route(
        "POST", "/graphql", 200, {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": True}}}}
    )
    await app_forge(github, rsa_pem).hide_comment(REPO, 9)
    graphql = body_of(github.calls("POST", "/graphql")[0])
    assert graphql["variables"] == {"id": "IC_9"} and "minimizeComment" in graphql["query"]


async def test_http_errors_become_forge_errors(github: MockGitHub, rsa_pem: str) -> None:
    github.route("POST", "/repos/coverset-demo/ledger/check-runs", 422, {"message": "Validation Failed"})
    with pytest.raises(ForgeHTTPError) as caught:
        await app_forge(github, rsa_pem).create_check(
            REPO, SHA, "coverset", external_id=None, details_url=None
        )
    assert caught.value.status == 422


@pytest.mark.security
async def test_repo_names_are_checked_before_use(github: MockGitHub, rsa_pem: str) -> None:
    bad = RepoRef(external_id=1, full_name="../../user", installation_id=55)
    with pytest.raises(ForgeError):
        await app_forge(github, rsa_pem).set_commit_status(
            bad, SHA, "success", context="c", description="d", target_url=None
        )


def test_from_settings_modes(rsa_pem: str) -> None:
    app_settings = Settings.for_tests(
        github_app_id=APP_ID, github_app_private_key=SecretStr(rsa_pem), allowed_owners=["coverset-demo"]
    )
    assert GitHubForge.from_settings(app_settings).mode == "app"
    missing = app_settings.model_copy(update={"github_app_private_key": None})
    with pytest.raises(ValueError, match="COVERSET_GITHUB_APP_PRIVATE_KEY"):
        GitHubForge.from_settings(missing)
    token_settings = Settings.for_tests(github_token=SecretStr("github_pat_" + "q" * 30))
    assert GitHubForge.from_settings(token_settings).mode == "token"
    with pytest.raises(ValueError):
        GitHubForge.from_settings(Settings.for_tests())


def test_the_private_key_is_pem_text_with_escaped_newlines_accepted(rsa_pem: str) -> None:
    """Secret stores that flatten line breaks to a literal backslash-n still give a usable key."""
    flattened = rsa_pem.strip().replace("\n", "\\n")
    assert "\n" not in flattened
    settings = Settings.for_tests(github_app_private_key=flattened)
    assert settings.github_app_private_key is not None
    assert settings.github_app_private_key.get_secret_value() == rsa_pem.strip()
    assert "BEGIN" not in repr(settings)


def test_registry_chooses_the_adapter(rsa_pem: str) -> None:
    from coverset.forge.credentials import AppCredentials, GitHubCredentials

    bare = Settings.for_tests()
    app_settings = Settings.for_tests(
        github_app_id=APP_ID, github_app_private_key=SecretStr(rsa_pem), allowed_owners=["coverset-demo"]
    )
    token_settings = Settings.for_tests(github_token=SecretStr("github_pat_" + "q" * 30))
    assert forge_mode(bare, {}) == "fake"
    assert forge_mode(app_settings, {}) == "github-app"
    assert forge_mode(token_settings, {}) == "github-token"
    assert forge_mode(app_settings, {"COVERSET_FORGE": "fake"}) == "fake"
    with pytest.raises(ValueError):
        forge_mode(bare, {"COVERSET_FORGE": "github"})
    with pytest.raises(ValueError):
        forge_mode(bare, {"COVERSET_FORGE": "gitlab"})
    assert isinstance(get_forge(bare, environ={}), FakeForge)
    assert isinstance(get_forge(app_settings, environ={}), GitHubForge)
    stored = GitHubCredentials(
        "database", app=AppCredentials(app_id=APP_ID, pem=rsa_pem), webhook_secret="w" * 20
    )
    assert forge_mode(bare, {}, stored) == "github-app", "an App stored by /setup"
    with pytest.raises(ValueError, match="COVERSET_ALLOWED_OWNERS"):
        get_forge(bare, credentials=stored, environ={})  # no owner and no list: refuse, never accept all
    owned = GitHubCredentials(
        "database", app=AppCredentials(app_id=APP_ID, pem=rsa_pem, owner="coverset-demo")
    )
    forge = get_forge(bare, credentials=owned, environ={})
    assert isinstance(forge, GitHubForge) and forge.app_id == APP_ID


def _failed(method: str, status: int, headers: dict[str, str] | None = None) -> RequestFailed:
    request = httpx.Request(method, "https://api.github.com/repos/o/r/thing")
    raw = httpx.Response(status, headers=headers or {}, request=request)
    return RequestFailed(GitHubResponse(raw, Any))


@pytest.mark.security
def test_retry_decision_never_retries_non_idempotent_server_errors() -> None:
    """A 502 on a POST that GitHub committed must not create a second comment or check run."""
    for method in ("POST", "PUT", "DELETE"):
        assert retry_decision(_failed(method, 502), 0).do_retry is False
    for method in ("GET", "PATCH"):
        decision = retry_decision(_failed(method, 502), 0)
        assert decision.do_retry is True and decision.retry_after == timedelta(seconds=1)
        assert retry_decision(_failed(method, 503), 3).do_retry is False, "at most three retries"
    assert retry_decision(_failed("GET", 404), 0).do_retry is False


def test_retry_decision_caps_rate_limit_waits() -> None:
    def limited(seconds: float) -> RateLimitExceeded:
        request = httpx.Request("POST", "https://api.github.com/x")
        raw = httpx.Response(429, request=request)
        return RateLimitExceeded(GitHubResponse(raw, Any), timedelta(seconds=seconds))

    short = retry_decision(limited(5), 0)
    assert short.do_retry is True and short.retry_after == timedelta(seconds=5)
    assert retry_decision(limited(5), 1).do_retry is False, "one rate-limit retry"
    assert retry_decision(limited(3600), 0).do_retry is False, "no hour-long sleep inside verdict posting"


@pytest.mark.security
async def test_post_server_error_is_sent_once(github: MockGitHub, rsa_pem: str) -> None:
    github.route("POST", "/repos/coverset-demo/ledger/issues/1/comments", 502, {"message": "Bad Gateway"})
    github.route("GET", "/repos/coverset-demo/ledger/issues/1/comments", 200, [])
    forge = app_forge(github, rsa_pem)
    with pytest.raises(ForgeHTTPError):
        await forge.upsert_pr_comment(REPO, 1, STICKY_MARKER, "body", None)
    assert len(github.calls("POST", "/repos/coverset-demo/ledger/issues")) == 1


async def test_collaborator_permission(github: MockGitHub, rsa_pem: str) -> None:
    path = "/repos/coverset-demo/ledger/collaborators/{}/permission"
    github.route("GET", path.format("dana"), 200, {"permission": "write", "role_name": "maintain"})
    github.route("GET", path.format("rita"), 200, {"permission": "read"})
    forge = app_forge(github, rsa_pem)
    assert await forge.collaborator_permission(REPO, "dana") == "maintain"
    assert await forge.collaborator_permission(REPO, "rita") == "read"
    assert await forge.collaborator_permission(REPO, "stranger") == "none", "404 means no access"
    with pytest.raises(ForgeError):
        await forge.collaborator_permission(REPO, "../../admin")
    github.route("GET", path.format("ci-bot[bot]"), 403, {"message": "Resource not accessible"})
    with pytest.raises(ForgeError):
        await forge.collaborator_permission(REPO, "ci-bot[bot]")
