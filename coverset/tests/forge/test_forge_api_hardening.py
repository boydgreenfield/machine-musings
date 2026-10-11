"""Hardening of the ``/v1`` API and OIDC path: what anonymous callers see, stream caps, limiters,
strict numeric parameters, the admin approve route, and the guarded JWKS client."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from forge_helpers import (
    DEMO_REPO_ID,
    SHA_A,
    RecordingQueue,
    StubJwkClient,
    oidc_claims,
    seed_repo,
    seed_run,
    sign_token,
)
from quart import Quart
from quart.typing import TestClientProtocol as Client

from coverset.config import Settings
from coverset.db.models import Run
from coverset.db.session import Database
from coverset.forge.fake import FakeForge
from coverset.forge.oidc import GuardedJwkClient, OidcError, verify_actions_oidc
from coverset.web import api
from coverset.web.api import JWK_CLIENT_EXTENSION, STREAM_SLOTS_EXTENSION, StreamSlots

SECRET_LOG = "AWS_SECRET=hunter2 from the PR's own code"
HOSTILE_EVENTS = [
    ("queued", {"status": "queued", "delivery_id": "1c656f19-0000", "trusted": False}),
    ("approved", {"by": "dana-maintainer", "network": "blocked"}),
    ("log", {"shard": 0, "text": SECRET_LOG}),
    (
        "test",
        {
            "nodeid": "tests/test_a.py::test_bad",
            "outcome": "failed",
            "duration_s": 0.2,
            "failed_phase": "call",
            "longrepr": "assert token == 'hunter2'",
            "shard": 0,
            "flaky": False,
            "requested_by": "someone",
        },
    ),
    ("progress", {"done": 1, "total": 1, "passed": 0, "failed": 1, "shards": 1, "elapsed_s": 0.3}),
    ("verdict", {"conclusion": "failure", "reason": "1 failed", "by": "dana-maintainer"}),
    ("done", {"conclusion": "failure", "exit_code": 1, "counts": {"failed": 1}, "delivery_id": "x"}),
]


@pytest.fixture
def settings_overrides(admin_token: str) -> dict[str, Any]:
    """An admin token, so anonymous callers are public callers (not open mode)."""
    return {
        "admin_token": admin_token,
        "public_verdict_rpm": 5,
        "github_webhook_secret": "coverset-test-secret",
        "allowed_owners": ["coverset-demo"],
    }


@pytest.fixture
def oidc_app(app: Quart, jwk_client: StubJwkClient) -> Quart:
    app.extensions[JWK_CLIENT_EXTENSION] = jwk_client
    return app


def slots_of(app: Quart) -> StreamSlots:
    """The app's anonymous stream counter (created here when no request has made it yet)."""
    return app.extensions.setdefault(STREAM_SLOTS_EXTENSION, StreamSlots())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def parse_sse(text: str) -> list[tuple[int, str, dict[str, Any]]]:
    frames = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":"))
        if fields:
            frames.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return frames


# -- what anonymous callers see -------------------------------------------------------------------


async def test_public_events_are_filtered_to_the_addendum_shapes(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="completed", verdict="failure", events=HOSTILE_EVENTS)
    body = await (await client.get(f"/v1/runs/{run.id}/events")).get_json()
    assert [e["kind"] for e in body["events"]] == ["test", "progress", "verdict", "done"]
    assert body["done"] is True
    text = json.dumps(body)
    for leaked in ("hunter2", "delivery_id", "dana-maintainer", "requested_by", "1c656f19"):
        assert leaked not in text, leaked
    test = body["events"][0]["payload"]
    assert test["longrepr"] is None and test["nodeid"] == "tests/test_a.py::test_bad"
    assert body["events"][3]["payload"] == {"conclusion": "failure", "exit_code": 1, "counts": {"failed": 1}}

    sse = await client.get(f"/v1/runs/{run.id}/events", headers={"Accept": "text/event-stream"})
    frames = parse_sse(await sse.get_data(as_text=True))
    assert [kind for _, kind, _ in frames] == ["test", "progress", "verdict", "done"]
    assert "hunter2" not in json.dumps(frames) and frames[0][2]["longrepr"] is None

    admin = await (await client.get(f"/v1/runs/{run.id}/events", headers=bearer(admin_token))).get_json()
    assert len(admin["events"]) == len(HOSTILE_EVENTS) and "hunter2" in json.dumps(admin)


async def test_public_polling_pages_never_stall_on_hidden_events(
    oidc_app: Quart, client: Client, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filtering happens in SQL, so a page of hidden log events cannot leave a poller stuck."""
    monkeypatch.setattr(api, "EVENTS_PAGE", 3)
    repo = await seed_repo(db, private=False)
    logs = [("log", {"shard": 0, "text": f"line {i}"}) for i in range(10)]
    run = await seed_run(db, repo, status="running", events=[*logs, HOSTILE_EVENTS[3]])
    body = await (await client.get(f"/v1/runs/{run.id}/events?since=-1")).get_json()
    assert [e["seq"] for e in body["events"]] == [10]


@pytest.mark.security
async def test_public_run_detail_has_no_free_text_reason(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="completed", verdict="failure", results=[("t::a", "failed")])
    async with db.session() as session:
        stored = await session.get(Run, run.id)
        assert stored is not None
        stored.reason = "secret detail from the PR"
        await session.commit()
    public = await (await client.get(f"/v1/runs/{run.id}")).get_json()
    assert public["summary"] == "failure: 1 failed" and public["counts"]["failed"] == 1
    admin = await (await client.get(f"/v1/runs/{run.id}", headers=bearer(admin_token))).get_json()
    assert admin["summary"] == "failure: 1 failed (secret detail from the PR)"


# -- streams and limiters ---------------------------------------------------------------------------


def test_stream_slots() -> None:
    now = [0.0]
    slots = StreamSlots(per_key=2, total=3, ttl_s=100.0, clock=lambda: now[0])
    assert [slots.acquire("a"), slots.acquire("a"), slots.acquire("a")] == [True, True, False]
    assert slots.acquire("b") and not slots.acquire("c"), "total cap"
    slots.release("a")
    assert slots.acquire("c") and slots.count() == 3
    now[0] = 150.0
    assert slots.count() == 0 and slots.acquire("a"), "leaked slots expire"


@pytest.mark.security
async def test_anonymous_sse_streams_are_capped_per_client(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="completed", verdict="success", events=HOSTILE_EVENTS[3:])
    slots = slots_of(oidc_app)
    for _ in range(api.ANON_SSE_PER_KEY):
        assert slots.acquire("<local>")
    headers = {"Accept": "text/event-stream"}
    refused = await client.get(f"/v1/runs/{run.id}/events", headers=headers)
    assert refused.status_code == 429 and await refused.get_json() == {"error": "too_many_streams"}
    admin = await client.get(f"/v1/runs/{run.id}/events", headers={**headers, **bearer(admin_token)})
    assert admin.status_code == 200, "admins are not capped"
    slots.release("<local>")
    finished = await client.get(f"/v1/runs/{run.id}/events", headers=headers)
    assert finished.status_code == 200 and [k for _, k, _ in parse_sse(await finished.get_data(as_text=True))]
    assert slots.count("<local>") == api.ANON_SSE_PER_KEY - 1, "a finished stream gives its slot back"


@pytest.mark.security
async def test_anonymous_streams_end_after_their_short_lifetime(
    oidc_app: Quart, client: Client, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "ANON_SSE_MAX_STREAM_S", 0.2)
    monkeypatch.setattr(api, "POLL_INTERVAL_S", 0.05)
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="running", events=HOSTILE_EVENTS[3:4])
    response = await client.get(f"/v1/runs/{run.id}/events", headers={"Accept": "text/event-stream"})
    frames = parse_sse(await response.get_data(as_text=True))
    assert [kind for _, kind, _ in frames] == ["test"], "ended without done; client resumes by Last-Event-ID"
    assert slots_of(oidc_app).count() == 0


@pytest.mark.security
async def test_json_polls_use_their_own_budget(oidc_app: Quart, client: Client, db: Database) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="running")
    polls = [(await client.get(f"/v1/runs/{run.id}/events")).status_code for _ in range(10)]
    assert polls == [200] * 10
    verdicts = [
        (await client.get(f"/v1/verdict?repo_id={DEMO_REPO_ID}&sha={SHA_A}")).status_code for _ in range(6)
    ]
    assert verdicts == [200] * 5 + [429], "polling did not eat the verdict budget"
    assert (await client.get(f"/v1/runs/{run.id}/events")).status_code == 429


@pytest.mark.security
async def test_bearer_path_is_rate_limited_before_verification(
    oidc_app: Quart,
    client: Client,
    db: Database,
    settings: Settings,
    rsa_key: rsa.RSAPrivateKey,
    jwk_client: StubJwkClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "BEARER_RPM", 3)
    repo = await seed_repo(db, private=True)
    await seed_run(db, repo)
    token = sign_token(rsa_key, oidc_claims(settings.oidc_audience))
    path = f"/v1/verdict?repo_id={DEMO_REPO_ID}&sha={SHA_A}"
    statuses = [(await client.get(path, headers=bearer(token))).status_code for _ in range(3)]
    garbage = await client.get(path, headers=bearer("x.y.z"))
    assert statuses == [200, 200, 200] and garbage.status_code == 429
    assert jwk_client.calls == 3, "the rate-limited request never reached key lookup"


# -- strict numeric parameters ----------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "query",
    [
        "repo_id=%C2%B2&sha=" + SHA_A,
        "repo_id=" + "9" * 20 + "&sha=" + SHA_A,
        f"repo_id={2**63}&sha=" + SHA_A,
        "repo_id=-1&sha=" + SHA_A,
        "repo_id=%EF%BC%91&sha=" + SHA_A,
    ],
)
async def test_bad_repo_ids_are_400(oidc_app: Quart, client: Client, admin_token: str, query: str) -> None:
    for headers in ({}, bearer(admin_token)):
        response = await client.get(f"/v1/verdict?{query}", headers=headers)
        assert response.status_code == 400, (query, headers)


@pytest.mark.security
async def test_largest_repo_id_is_a_plain_404(oidc_app: Quart, client: Client, admin_token: str) -> None:
    response = await client.get(f"/v1/verdict?repo_id={2**63 - 1}&sha={SHA_A}", headers=bearer(admin_token))
    assert response.status_code == 404


@pytest.mark.security
async def test_bad_sequence_numbers_are_400(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="completed", verdict="success", events=HOSTILE_EVENTS)
    path = f"/v1/runs/{run.id}/events"
    for since in ("9" * 19, "9" * 30, "%C2%B2", "1.5", "--1"):
        assert (await client.get(f"{path}?since={since}", headers=bearer(admin_token))).status_code == 400
    sse = {"Accept": "text/event-stream", **bearer(admin_token)}
    overflow = await client.get(path, headers={**sse, "Last-Event-ID": "9" * 30})
    assert overflow.status_code == 400, "rejected before the stream starts"
    assert (await client.get(f"{path}?since=-1", headers=bearer(admin_token))).status_code == 200


# -- admin approval route ---------------------------------------------------------------------------


async def test_admin_can_approve_an_awaiting_run(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    queue = RecordingQueue()
    oidc_app.extensions["run_queue"] = queue
    oidc_app.extensions["forge"] = FakeForge()
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="awaiting_approval")
    assert (await client.post(f"/runs/{run.id}/approve")).status_code == 401
    async with client.session_transaction() as session:
        session["admin"] = True
        session["csrf"] = "known-token"
    assert (await client.post(f"/runs/{run.id}/approve")).status_code == 403, "cookie needs the CSRF token"
    session_approved = await client.post(
        f"/runs/{run.id}/approve", form={"csrf_token": "known-token"}, headers={"Accept": "text/html"}
    )
    assert session_approved.status_code == 303
    async with db.session() as session:
        stored = await session.get(Run, run.id)
        assert stored is not None
        assert (stored.status, stored.network, stored.trusted, stored.approved_by) == (
            "queued",
            "blocked",
            False,
            "coverset admin",
        )
    assert queue.run_ids == [run.id]
    again = await client.post(f"/runs/{run.id}/approve", headers=bearer(admin_token))
    assert again.status_code == 409 and await again.get_json() == {"error": "not_awaiting_approval"}

    other = await seed_run(db, repo, sha="c" * 40, status="awaiting_approval")
    by_bearer = await client.post(f"/runs/{other.id}/approve", headers=bearer(admin_token))
    assert by_bearer.status_code == 200 and await by_bearer.get_json() == {
        "status": "queued",
        "run_id": other.id,
    }
    assert (await client.post("/runs/run_missing/approve", headers=bearer(admin_token))).status_code == 404


# -- the guarded JWKS client over a real PyJWKClient ------------------------------------------------


class _Jwks:
    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        public.update({"kid": "good-kid", "alg": "RS256", "use": "sig"})
        self.body = json.dumps({"keys": [public]}).encode()
        self.hits = 0
        self.down = False


@pytest.fixture
def jwks_server(rsa_key: rsa.RSAPrivateKey) -> Iterator[tuple[str, _Jwks]]:
    state = _Jwks(rsa_key)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.hits += 1
            if state.down:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/jwks", state
    finally:
        server.shutdown()
        server.server_close()


def test_guarded_client_caches_unknown_kids_and_outages(
    jwks_server: tuple[str, _Jwks], rsa_key: rsa.RSAPrivateKey
) -> None:
    url, state = jwks_server
    now = [1000.0]
    inner = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=2, cooldown_duration=0)
    guarded = GuardedJwkClient(inner, negative_ttl_s=30.0, clock=lambda: now[0])
    audience = "http://127.0.0.1:8000"
    good = sign_token(rsa_key, oidc_claims(audience), kid="good-kid")
    assert verify_actions_oidc(good, audience=audience, jwk_client=guarded).repository_id == DEMO_REPO_ID
    assert state.hits == 1

    random_kid = sign_token(rsa_key, oidc_claims(audience), kid="random-1")
    for _ in range(5):
        with pytest.raises(OidcError):
            verify_actions_oidc(random_kid, audience=audience, jwk_client=guarded)
    assert state.hits == 2, "one refresh for the unknown kid, then the negative cache answers"

    state.down = True
    with pytest.raises(OidcError):
        verify_actions_oidc(
            sign_token(rsa_key, oidc_claims(audience), kid="random-2"), audience=audience, jwk_client=guarded
        )
    hits_after_outage = state.hits
    for index in range(20):
        token = sign_token(rsa_key, oidc_claims(audience), kid=f"random-{index + 3}")
        with pytest.raises(OidcError):
            verify_actions_oidc(token, audience=audience, jwk_client=guarded)
    assert state.hits == hits_after_outage, "no fetches while the outage is cached"
    assert verify_actions_oidc(good, audience=audience, jwk_client=guarded).sub, "known kids still verify"

    now[0] += 31.0
    state.down = False
    fresh = sign_token(rsa_key, oidc_claims(audience), kid="random-1")
    with pytest.raises(OidcError):
        verify_actions_oidc(fresh, audience=audience, jwk_client=guarded)
    assert state.hits == hits_after_outage + 1, "after the cooldown the endpoint is asked again"


@pytest.mark.security
def test_guarded_client_rejects_tokens_without_a_kid(rsa_key: rsa.RSAPrivateKey) -> None:
    guarded = GuardedJwkClient(StubJwkClient(rsa_key))
    no_kid = jwt.encode(oidc_claims("aud"), rsa_key, algorithm="RS256")
    with pytest.raises(jwt.PyJWKClientError):
        guarded.get_signing_key_from_jwt(no_kid)
