"""``/v1/agent``: the agent tools over HTTP, against the demo's facts store.

Access: the path is not public, so a caller without credentials gets 401; an API token sees only
the repositories its owner (or its binding) may read, and a repository it cannot see answers 404
exactly like one that does not exist; an upload-only token is refused; calls are rate-limited per
principal. The tools answer the facts store's runs (a PR run's patch coverage, the PR test tax,
test history), and ``coverset mcp serve --server`` (``RemoteService``) works end to end through an
in-process MCP client.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp import Client
from quart import Quart
from quart.typing import TestClientProtocol as QuartClient
from web_helpers import Demo

from coverset.agent import backends
from coverset.agent.backends import StoreBackend
from coverset.agent.mcp_server import build_server
from coverset.agent.remote import RemoteService, check_server_url
from coverset.agent.tools import TOOLS
from coverset.db.models import ApiToken, Repo, RepoAccess, User
from coverset.web import tokens
from coverset.web.agent_api import LIMITER_EXTENSION
from coverset.web.api import SlidingWindowLimiter

pytestmark = pytest.mark.security


@pytest.fixture
def settings_overrides(demo: Demo, admin_token: str) -> dict[str, Any]:
    return {"data_dir": demo.data_dir, "database_url": demo.db_url, "admin_token": admin_token}


async def make_token(
    app: Quart,
    *,
    scopes: list[str],
    repo_id: int | None = None,
    login: str | None = None,
    access: dict[int, str] | None = None,
    revoked: bool = False,
) -> str:
    """Insert an API token (a repository token with ``repo_id``, else a personal one for ``login``)."""
    raw, prefix, digest = tokens.generate()
    async with app.extensions["db"].session() as session:
        user_id = None
        if login is not None:
            user = User(forge="github", external_id=f"agent-{login}", login=login, role="member")
            session.add(user)
            await session.flush()
            user_id = user.id
            for rid, permission in (access or {}).items():
                session.add(RepoAccess(user_id=user.id, repo_id=rid, permission=permission))  # type: ignore[arg-type]
        session.add(
            ApiToken(
                token_hash=digest,
                prefix=prefix,
                name="agent test",
                kind="repo" if repo_id is not None else "personal",
                repo_id=repo_id,
                user_id=user_id,
                scopes=scopes,
                revoked_at=app.extensions["settings"].now() if revoked else None,
            )
        )
        await session.commit()
    return raw


async def other_repo(app: Quart) -> int:
    """A second repository the demo's tokens must not reach (created once per database)."""
    from sqlalchemy import select

    async with app.extensions["db"].session() as session:
        found = await session.scalar(select(Repo).where(Repo.full_name == "someone/else"))
        if found is None:
            found = Repo(forge="fake", external_id="424242", full_name="someone/else", private=True)
            session.add(found)
            await session.commit()
        return found.id


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def post(client: QuartClient, tool: str, token: str, **args: Any) -> tuple[int, Any]:
    response = await client.post(f"/v1/agent/{tool}", json=args, headers=bearer(token))
    return response.status_code, await response.get_json()


# -- access ------------------------------------------------------------------------------------------


async def test_public_callers_get_401(client: QuartClient) -> None:
    for response in (
        await client.get("/v1/agent"),
        await client.post("/v1/agent/list_repos", json={}),
        await client.get("/v1/agent/get_run_summary"),
    ):
        assert response.status_code == 401
        assert "Bearer" in response.headers.get("WWW-Authenticate", "")


async def test_an_unknown_or_revoked_token_gets_401(app: Quart, client: QuartClient, demo: Demo) -> None:
    revoked = await make_token(app, scopes=["read"], repo_id=demo.repo_id, revoked=True)
    raw, _, _ = tokens.generate()
    for token in (revoked, raw, "not-a-token"):
        assert (await client.get("/v1/agent", headers=bearer(token))).status_code == 401


async def test_the_self_description_lists_every_tool(client: QuartClient, admin_token: str) -> None:
    response = await client.get("/v1/agent", headers=bearer(admin_token))
    assert response.status_code == 200
    body = await response.get_json()
    assert [t["name"] for t in body["tools"]] == list(TOOLS)
    assert all(t["input_schema"]["type"] == "object" for t in body["tools"])
    assert body["limits"] == {"calls_per_minute": 120, "max_rows": 200}


async def test_a_read_token_reads_its_repository(app: Quart, client: QuartClient, demo: Demo) -> None:
    token = await make_token(app, scopes=["read"], repo_id=demo.repo_id)
    status, repos = await post(client, "list_repos", token)
    assert status == 200 and [r["id"] for r in repos["repos"]] == [demo.repo_id]
    status, summary = await post(client, "get_run_summary", token, run=demo.pr1_run)
    assert status == 200, summary
    assert summary["run"] == demo.pr1_run and summary["run_info"]["pr_number"] == 1
    assert summary["tests"]["outcomes"].get("failed") == 1
    status, default = await post(client, "get_run_summary", token)
    assert status == 200 and default["run_info"]["kind"] in ("baseline", "push")


async def test_a_token_for_another_repository_gets_404(app: Quart, client: QuartClient, demo: Demo) -> None:
    elsewhere = await other_repo(app)
    token = await make_token(app, scopes=["read"], repo_id=elsewhere)
    status, repos = await post(client, "list_repos", token)
    assert status == 200 and [r["name"] for r in repos["repos"]] == ["someone/else"]
    for tool, args in (
        ("get_run_summary", {"run": demo.pr1_run}),
        ("get_function", {"run": demo.pr1_run, "function": "allocate_cents"}),
        ("list_runs", {"repo": str(demo.repo_id)}),
        ("list_runs", {"repo": "coverset-demo/ledger"}),
        ("get_test", {"run": demo.pr2_run, "nodeid": "tests/test_parse.py::test_parse_duration[90s-90]"}),
    ):
        status, body = await post(client, tool, token, **args)
        assert status == 404 and body["error"] == "not_found", (tool, body)
    unknown_status, unknown = await post(
        client, "get_run_summary", token, run="run_00000000000000000000000000"
    )
    assert (unknown_status, unknown["error"]) == (404, "not_found"), "unseen and unknown look the same"
    assert unknown["message"] == body["message"]


async def test_personal_tokens_follow_the_users_access(app: Quart, client: QuartClient, demo: Demo) -> None:
    reader = await make_token(app, scopes=["read"], login="agent-reader", access={demo.repo_id: "read"})
    stranger = await make_token(app, scopes=["read"], login="agent-stranger")
    assert (await post(client, "get_run_summary", reader, run=demo.pr2_run))[0] == 200
    status, repos = await post(client, "list_repos", stranger)
    assert status == 200 and repos["repos"] == []
    assert (await post(client, "get_run_summary", stranger, run=demo.pr2_run))[0] == 404


async def test_an_upload_only_token_is_refused(app: Quart, client: QuartClient, demo: Demo) -> None:
    token = await make_token(app, scopes=["upload"], repo_id=demo.repo_id)
    status, body = await post(client, "list_repos", token)
    assert status == 403 and body["error"] == "insufficient_scope"


async def test_rate_limit_is_per_principal(app: Quart, client: QuartClient, demo: Demo) -> None:
    app.extensions[LIMITER_EXTENSION] = SlidingWindowLimiter(2)
    first = await make_token(app, scopes=["read"], repo_id=demo.repo_id)
    second = await make_token(app, scopes=["read"], repo_id=demo.repo_id)
    try:
        assert (await post(client, "explain_shape_key", first, key="int|neg"))[0] == 200
        assert (await post(client, "explain_shape_key", first, key="int|neg"))[0] == 200
        response = await client.post(
            "/v1/agent/explain_shape_key", json={"key": "int"}, headers=bearer(first)
        )
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1
        assert (await response.get_json())["error"] == "rate_limited"
        assert (await post(client, "explain_shape_key", second, key="int|neg"))[0] == 200, "another token"
        assert (await client.get("/v1/agent", headers=bearer(first))).status_code == 200, "not charged"
    finally:
        app.extensions.pop(LIMITER_EXTENSION, None)


# -- arguments and errors ----------------------------------------------------------------------------


async def test_get_with_query_arguments_and_bad_input(
    client: QuartClient, admin_token: str, demo: Demo
) -> None:
    response = await client.get(
        f"/v1/agent/find_gaps?run={demo.baseline_run}&limit=2&include_uncalled=false",
        headers=bearer(admin_token),
    )
    assert response.status_code == 200
    assert len((await response.get_json())["gaps"]) == 2
    for path, status, slug in (
        ("/v1/agent/find_gaps?limit=lots", 400, "invalid_argument"),
        ("/v1/agent/find_gaps?limit=0", 400, "invalid_argument"),
        ("/v1/agent/find_gaps?nope=1", 400, "invalid_argument"),
        ("/v1/agent/drop_tables", 404, "unknown_tool"),
    ):
        response = await client.get(path, headers=bearer(admin_token))
        assert response.status_code == status, path
        assert (await response.get_json())["error"] == slug
    for body in (b"[1, 2]", b"{not json"):
        response = await client.post(
            "/v1/agent/list_repos",
            data=body,
            headers={**bearer(admin_token), "Content-Type": "application/json"},
        )
        assert response.status_code == 400
    response = await client.post(
        "/v1/agent/get_function",
        json={"run": demo.pr1_run, "function": "nope_nope"},
        headers=bearer(admin_token),
    )
    assert response.status_code == 404 and (await response.get_json())["similar"] == []


# -- the tools over the facts store -----------------------------------------------------------------


async def test_pr_run_patch_coverage_and_tax(client: QuartClient, admin_token: str, demo: Demo) -> None:
    status, patch = await post(client, "get_patch_coverage", admin_token, run=demo.pr1_run)
    assert status == 200 and patch["applicable"] is True
    refund = next(f for f in patch["functions"] if f["function"].endswith("::refund"))
    assert refund["change"] == "added" and refund["tests"] == 3
    assert "weights:empty" in refund["input_classes"]["missing"]
    assert patch["total"] == sum(f["total"] for f in patch["files"])
    status, baseline = await post(client, "get_patch_coverage", admin_token, run=demo.baseline_run)
    assert status == 200 and baseline["applicable"] is False
    status, tax = await post(client, "test_tax", admin_token, run=demo.pr1_run)
    assert status == 200
    if tax["available"]:
        assert tax["pr_tax"] is not None and tax["pr_tax"]["new_tests"] == 3
    status, changed = await post(client, "minimal_test_set", admin_token, run=demo.pr1_run, scope="changed")
    assert status == 200
    kept = {t["nodeid"] for t in changed["kept"]["tests"]}
    assert "tests/test_refund.py::test_refund_partial" in kept, "a failing test is always kept"


async def test_test_history_and_failure_message(client: QuartClient, admin_token: str, demo: Demo) -> None:
    status, found = await post(
        client, "get_test", admin_token, run=demo.pr1_run, nodeid="tests/test_refund.py::test_refund_partial"
    )
    assert status == 200 and found["outcome"] == "failed"
    assert found["message"] and len(found["message"]) <= 2000
    by_run = {row["run_id"]: row for row in found["history"]}
    assert by_run[demo.pr1_run]["outcome"] == "failed", "first attempts, as the bundles recorded them"
    assert by_run[demo.pr2_run]["outcome"] is None, "the test does not exist on PR #2's branch"
    shapes = {s["param"] for s in found["shapes"] if s["function"].endswith("::allocate_cents")}
    assert "total_cents" in shapes


async def test_failure_messages_are_read_once_per_run(
    client: QuartClient, admin_token: str, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    original = StoreBackend._failure_messages

    async def counting(self: StoreBackend, run_id: str) -> dict[str, tuple[str | None, str | None]]:
        reads.append(run_id)
        return await original(self, run_id)

    monkeypatch.setattr(StoreBackend, "_failure_messages", counting)
    StoreBackend._contexts.clear()
    status, passed = await post(
        client,
        "get_test",
        admin_token,
        run=demo.pr1_run,
        nodeid="tests/test_parse.py::test_parse_duration[90s-90]",
    )
    assert status == 200 and passed["outcome"] == "passed" and passed["message"] is None
    assert reads == [], "a passed test costs no event query"
    for _ in range(3):
        status, failed = await post(
            client,
            "get_test",
            admin_token,
            run=demo.pr1_run,
            nodeid="tests/test_refund.py::test_refund_partial",
        )
        assert status == 200 and failed["message"]
    assert reads == [demo.pr1_run], "the run's events are read once and cached on its context"


async def test_the_context_cache_is_bounded(
    client: QuartClient, admin_token: str, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert backends.context_cache_size() == backends.CONTEXT_CACHE == 3
    monkeypatch.setenv(backends.CONTEXT_CACHE_ENV, "0")
    assert backends.context_cache_size() == 3, "nonsense falls back to the default"
    monkeypatch.setenv(backends.CONTEXT_CACHE_ENV, "1")
    StoreBackend._contexts.clear()
    for run in (demo.baseline_run, demo.pr1_run, demo.pr2_run):
        status, _ = await post(client, "get_run_summary", admin_token, run=run)
        assert status == 200
    assert len(StoreBackend._contexts) == 1
    assert next(iter(StoreBackend._contexts))[2] == demo.pr2_run, "the most recent run stays"


async def test_property_tests_are_found_in_the_store_too(
    client: QuartClient, admin_token: str, demo: Demo
) -> None:
    status, found = await post(client, "minimal_test_set", admin_token, run=demo.baseline_run, limit=200)
    assert status == 200
    kept = {t["nodeid"] for t in found["kept"]["tests"]}
    assert "tests/test_properties.py::test_allocation_conserves_cents" in kept
    assert any("property-based" in c for c in found["caveats"])


# -- coverset mcp serve --server, end to end ---------------------------------------------------------


async def test_remote_mode_through_an_mcp_client(app: Quart, demo: Demo) -> None:
    token = await make_token(app, scopes=["read"], repo_id=demo.repo_id)
    elsewhere = await make_token(app, scopes=["read"], repo_id=await other_repo(app))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    remote = RemoteService("http://localhost", token, transport=transport)
    try:
        async with Client(build_server(remote)) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            summary = await client.call_tool("get_run_summary", {"run": demo.pr2_run})
            suggestion = await client.call_tool(
                "suggest_tests", {"run": demo.pr2_run, "function": "validate_email"}
            )
            bad = await client.call_tool("find_gaps", {"limit": 0})
    finally:
        await remote.aclose()
    assert names == list(TOOLS)
    assert not summary.is_error and summary.structured_content["run_info"]["pr_number"] == 2
    missing = {m["class"] for m in suggestion.structured_content["missing"]}
    assert {"email:multi-label-domain", "email:idn"} <= missing
    assert bad.is_error and '"invalid_argument"' in bad.content[0].text
    blocked = RemoteService("http://localhost", elsewhere, transport=httpx.ASGITransport(app=app))  # type: ignore[arg-type]
    try:
        async with Client(build_server(blocked)) as client:
            result = await client.call_tool("get_run_summary", {"run": demo.pr2_run})
    finally:
        await blocked.aclose()
    assert result.is_error and '"not_found"' in result.content[0].text


def test_server_urls_must_be_https_unless_local() -> None:
    assert check_server_url("https://coverset.example.com/") == "https://coverset.example.com"
    assert check_server_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for bad in ("http://coverset.example.com", "ftp://x", "https://user:pw@x.example", "nope"):
        with pytest.raises(ValueError):
            check_server_url(bad)
    with pytest.raises(ValueError):
        RemoteService("https://coverset.example.com", "")
