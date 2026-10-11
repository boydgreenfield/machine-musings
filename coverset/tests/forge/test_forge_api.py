"""OIDC verification and the ``/v1`` API: verdict, run, events (JSON and SSE), authorization."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from forge_helpers import (
    DEMO_OWNER_ID,
    DEMO_REPO_ID,
    SHA_A,
    SHA_B,
    StubJwkClient,
    oidc_claims,
    seed_repo,
    seed_run,
    sign_token,
)
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import Run
from coverset.db.session import Database
from coverset.forge.oidc import ISSUER, OidcError, verify_actions_oidc
from coverset.web.api import JWK_CLIENT_EXTENSION, SlidingWindowLimiter

AUDIENCE = "http://127.0.0.1:8000"


# -- verify_actions_oidc ----------------------------------------------------------------------------


@pytest.mark.security
def test_valid_token(rsa_key: rsa.RSAPrivateKey, jwk_client: StubJwkClient) -> None:
    claims = verify_actions_oidc(
        sign_token(rsa_key, oidc_claims(AUDIENCE)), audience=AUDIENCE, jwk_client=jwk_client
    )
    assert (claims.repository_id, claims.repository_owner_id) == (DEMO_REPO_ID, DEMO_OWNER_ID)
    assert claims.repository == "coverset-demo/ledger" and claims.sha == SHA_A


@pytest.mark.security
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://github.com/coverset-demo"},
        {"iss": "https://token.actions.githubusercontent.com.evil.example"},
        {"exp": datetime.now(UTC) - timedelta(minutes=5)},
        {"nbf": datetime.now(UTC) + timedelta(minutes=5)},
        {"repository_id": None},
        {"repository_owner_id": None},
        {"sub": None},
        {"repository_id": "not-a-number"},
    ],
    ids=[
        "wrong-aud",
        "wrong-iss",
        "expired",
        "not-yet-valid",
        "no-repo-id",
        "no-owner-id",
        "no-sub",
        "bad-repo-id",
    ],
)
def test_rejected_claims(
    rsa_key: rsa.RSAPrivateKey, jwk_client: StubJwkClient, overrides: dict[str, Any]
) -> None:
    token = sign_token(rsa_key, oidc_claims(AUDIENCE, **overrides))
    with pytest.raises(OidcError):
        verify_actions_oidc(token, audience=AUDIENCE, jwk_client=jwk_client)


@pytest.mark.security
def test_leeway_accepts_a_just_expired_token(rsa_key: rsa.RSAPrivateKey, jwk_client: StubJwkClient) -> None:
    token = sign_token(rsa_key, oidc_claims(AUDIENCE, exp=datetime.now(UTC) - timedelta(seconds=30)))
    verify_actions_oidc(token, audience=AUDIENCE, jwk_client=jwk_client)


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, default=str).encode()).rstrip(b"=").decode()


def test_alg_none_rejected_before_key_lookup(jwk_client: StubJwkClient) -> None:
    claims = {
        k: (int(v.timestamp()) if isinstance(v, datetime) else v) for k, v in oidc_claims(AUDIENCE).items()
    }
    token = f"{_b64({'alg': 'none', 'typ': 'JWT', 'kid': 'test-kid'})}.{_b64(claims)}."
    with pytest.raises(OidcError, match="algorithm"):
        verify_actions_oidc(token, audience=AUDIENCE, jwk_client=jwk_client)
    assert jwk_client.calls == 0


@pytest.mark.security
def test_hs256_key_confusion_rejected(rsa_key: rsa.RSAPrivateKey, jwk_client: StubJwkClient) -> None:
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = {"alg": "HS256", "typ": "JWT", "kid": "test-kid"}
    claims = {
        k: (int(v.timestamp()) if isinstance(v, datetime) else v) for k, v in oidc_claims(AUDIENCE).items()
    }
    signing_input = f"{_b64(header)}.{_b64(claims)}".encode()
    import hashlib
    import hmac

    signature = base64.urlsafe_b64encode(hmac.new(public_pem, signing_input, hashlib.sha256).digest()).rstrip(
        b"="
    )
    token = f"{signing_input.decode()}.{signature.decode()}"
    with pytest.raises(OidcError, match="algorithm"):
        verify_actions_oidc(token, audience=AUDIENCE, jwk_client=jwk_client)


@pytest.mark.security
def test_wrong_key_and_garbage_rejected(jwk_client: StubJwkClient) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(OidcError):
        verify_actions_oidc(
            sign_token(other, oidc_claims(AUDIENCE)), audience=AUDIENCE, jwk_client=jwk_client
        )
    with pytest.raises(OidcError):
        verify_actions_oidc(
            sign_token(other, oidc_claims(AUDIENCE), kid="unknown"), audience=AUDIENCE, jwk_client=jwk_client
        )
    for garbage in ("", "abc", "a.b.c", "x" * 20_000):
        with pytest.raises(OidcError):
            verify_actions_oidc(garbage, audience=AUDIENCE, jwk_client=jwk_client)
    assert ISSUER == "https://token.actions.githubusercontent.com"
    assert jwt.__version__ == "2.15.1"


# -- /v1 API ----------------------------------------------------------------------------------------


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


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def verdict_path(repo_id: int = DEMO_REPO_ID, sha: str = SHA_A) -> str:
    return f"/v1/verdict?repo_id={repo_id}&sha={sha}"


EVENTS = [
    ("queued", {"status": "queued"}),
    (
        "test",
        {
            "nodeid": "tests/test_a.py::test_ok",
            "outcome": "passed",
            "duration_s": 0.1,
            "failed_phase": None,
            "longrepr": None,
            "shard": 0,
            "flaky": False,
        },
    ),
    ("verdict", {"conclusion": "success", "reason": "all passed"}),
    ("done", {"conclusion": "success", "exit_code": 0, "counts": {"passed": 1}}),
]


@pytest.mark.security
async def test_verdict_with_a_valid_oidc_token(
    oidc_app: Quart, client: Client, db: Database, settings: Settings, rsa_key: rsa.RSAPrivateKey
) -> None:
    repo = await seed_repo(db, private=True)
    run = await seed_run(
        db, repo, status="completed", verdict="success", results=[("t::a", "passed"), ("t::b", "skipped")]
    )
    token = sign_token(rsa_key, oidc_claims(settings.oidc_audience))
    response = await client.get(verdict_path(), headers=bearer(token))
    assert response.status_code == 200
    body = await response.get_json()
    assert body == {
        "state": "completed",
        "conclusion": "success",
        "run_id": run.id,
        "summary": "success: 1 passed, 1 skipped",
        "summary_url": f"{settings.public_url}/runs/{run.id}",
    }
    async with db.session() as session:
        stored = await session.get(Run, run.id)
        assert stored is not None and stored.ci_first_contact_at is not None
        first_contact = stored.ci_first_contact_at
    await client.get(verdict_path(), headers=bearer(token))
    async with db.session() as session:
        stored = await session.get(Run, run.id)
        assert stored is not None and stored.ci_first_contact_at == first_contact


@pytest.mark.security
async def test_oidc_token_for_another_repo_is_404(
    oidc_app: Quart, client: Client, db: Database, settings: Settings, rsa_key: rsa.RSAPrivateKey
) -> None:
    repo = await seed_repo(db, private=True)
    await seed_run(db, repo)
    for overrides in ({"repository_id": "1"}, {"repository_owner_id": "1"}):
        token = sign_token(rsa_key, oidc_claims(settings.oidc_audience, **overrides))
        response = await client.get(verdict_path(), headers=bearer(token))
        assert response.status_code == 404 and await response.get_json() == {"error": "not_found"}


@pytest.mark.security
async def test_invalid_oidc_token_is_401(
    oidc_app: Quart, client: Client, db: Database, rsa_key: rsa.RSAPrivateKey
) -> None:
    repo = await seed_repo(db, private=False)
    await seed_run(db, repo)
    token = sign_token(rsa_key, oidc_claims("https://wrong-audience.example"))
    response = await client.get(verdict_path(), headers=bearer(token))
    assert response.status_code == 401 and await response.get_json() == {"error": "invalid_token"}
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.security
async def test_public_repo_without_token_gets_a_reduced_answer(
    oidc_app: Quart, client: Client, db: Database
) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="queued")
    response = await client.get(verdict_path())
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body) == {"state", "conclusion", "run_id", "summary_url"}
    assert (body["state"], body["conclusion"], body["run_id"]) == ("queued", None, run.id)


@pytest.mark.security
async def test_private_unknown_and_local_repos_are_404_without_token(
    oidc_app: Quart, client: Client, db: Database
) -> None:
    private = await seed_repo(db, private=True)
    await seed_run(db, private)
    local = await seed_repo(db, external_id=5, private=False, forge="local")
    await seed_run(db, local)
    for path in (verdict_path(), verdict_path(repo_id=999), verdict_path(repo_id=5)):
        response = await client.get(path)
        assert response.status_code == 404, path


@pytest.mark.security
async def test_public_callers_are_rate_limited(oidc_app: Quart, client: Client, db: Database) -> None:
    repo = await seed_repo(db, private=False)
    await seed_run(db, repo)
    statuses = [(await client.get(verdict_path())).status_code for _ in range(6)]
    assert statuses == [200] * 5 + [429]
    limited = await client.get(verdict_path())
    assert int(limited.headers["Retry-After"]) >= 1 and await limited.get_json() == {"error": "rate_limited"}


async def test_admin_sees_everything_and_bad_params_are_400(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db, private=True)
    run = await seed_run(db, repo, status="awaiting_approval")
    response = await client.get(verdict_path(), headers=bearer(admin_token))
    body = await response.get_json()
    assert (body["state"], body["summary"]) == ("queued", "awaiting approval: untrusted pull request")
    by_internal_id = await client.get(verdict_path(repo_id=repo.id), headers=bearer(admin_token))
    assert (await by_internal_id.get_json())["run_id"] == run.id
    assert (await client.get(verdict_path(sha=SHA_B), headers=bearer(admin_token))).status_code == 404
    for path in ("/v1/verdict?repo_id=x&sha=" + SHA_A, "/v1/verdict?repo_id=1&sha=abc", "/v1/verdict"):
        assert (await client.get(path, headers=bearer(admin_token))).status_code == 400


def test_sliding_window_limiter() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(2, clock=lambda: now[0])
    assert [limiter.hit("a"), limiter.hit("a")] == [None, None]
    assert limiter.hit("a") == pytest.approx(60.0)
    assert limiter.hit("b") is None
    now[0] = 61.0
    assert limiter.hit("a") is None
    assert SlidingWindowLimiter(0).hit("a") == 60.0


async def test_run_detail_counts(oidc_app: Quart, client: Client, db: Database, admin_token: str) -> None:
    repo = await seed_repo(db, private=True)
    run = await seed_run(
        db,
        repo,
        status="completed",
        verdict="failure",
        results=[("t::a", "failed"), ("t::b", "passed"), ("t::c", "passed")],
    )
    response = await client.get(f"/v1/runs/{run.id}", headers=bearer(admin_token))
    body = await response.get_json()
    assert body["counts"] == {
        "passed": 2,
        "failed": 1,
        "error": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "not_run": 0,
    }
    assert (body["state"], body["conclusion"], body["summary"]) == (
        "completed",
        "failure",
        "failure: 2 passed, 1 failed",
    )
    assert (await client.get("/v1/runs/run_missing", headers=bearer(admin_token))).status_code == 404
    assert (await client.get(f"/v1/runs/{run.id}")).status_code == 404, "private repo, no token"


async def test_events_json_polling(oidc_app: Quart, client: Client, db: Database, admin_token: str) -> None:
    repo = await seed_repo(db, private=False)
    run = await seed_run(db, repo, status="completed", verdict="success", events=EVENTS)
    body = await (await client.get(f"/v1/runs/{run.id}/events", headers=bearer(admin_token))).get_json()
    assert [e["seq"] for e in body["events"]] == [0, 1, 2, 3] and body["done"] is True
    later = await (
        await client.get(f"/v1/runs/{run.id}/events?since=1", headers=bearer(admin_token))
    ).get_json()
    assert [e["kind"] for e in later["events"]] == ["verdict", "done"]
    public = await client.get(f"/v1/runs/{run.id}/events?since=3")
    assert public.status_code == 200 and await public.get_json() == {"events": [], "done": True}


async def test_events_json_not_done_while_running(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db)
    run = await seed_run(db, repo, status="running", events=EVENTS[:2])
    body = await (
        await client.get(f"/v1/runs/{run.id}/events?since=0", headers=bearer(admin_token))
    ).get_json()
    assert [e["seq"] for e in body["events"]] == [1] and body["done"] is False


async def test_events_synthesise_done_for_runs_that_ended_without_one(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db)
    run = await seed_run(db, repo, status="cancelled", events=EVENTS[:1])
    body = await (
        await client.get(f"/v1/runs/{run.id}/events?since=0", headers=bearer(admin_token))
    ).get_json()
    assert body["done"] is True
    assert body["events"] == [
        {
            "seq": 1,
            "kind": "done",
            "payload": {
                "conclusion": "cancelled",
                "exit_code": 2,
                "counts": body["events"][0]["payload"]["counts"],
            },
        }
    ]


def parse_sse(text: str) -> list[dict[str, Any]]:
    frames = []
    for block in text.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":") or not line:
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if fields:
            frames.append(
                {"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])}
            )
    return frames


async def test_events_sse_stream_honours_last_event_id(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db)
    run = await seed_run(db, repo, status="completed", verdict="success", events=EVENTS)
    headers = {**bearer(admin_token), "Accept": "text/event-stream"}
    response = await client.get(f"/v1/runs/{run.id}/events", headers=headers)
    assert response.status_code == 200 and response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache" and response.headers["X-Accel-Buffering"] == "no"
    text = await response.get_data(as_text=True)
    assert text.startswith(": ")
    frames = parse_sse(text)
    assert [(f["id"], f["event"]) for f in frames] == [
        (0, "queued"),
        (1, "test"),
        (2, "verdict"),
        (3, "done"),
    ]
    assert frames[1]["data"]["nodeid"] == "tests/test_a.py::test_ok"
    resumed = await client.get(f"/v1/runs/{run.id}/events", headers={**headers, "Last-Event-ID": "2"})
    assert [f["event"] for f in parse_sse(await resumed.get_data(as_text=True))] == ["done"]


@pytest.mark.security
async def test_events_authorization_follows_the_runs_repo(
    oidc_app: Quart, client: Client, db: Database, settings: Settings, rsa_key: rsa.RSAPrivateKey
) -> None:
    repo = await seed_repo(db, private=True)
    run = await seed_run(db, repo, status="completed", verdict="success", events=EVENTS)
    assert (await client.get(f"/v1/runs/{run.id}/events")).status_code == 404
    token = sign_token(rsa_key, oidc_claims(settings.oidc_audience))
    response = await client.get(f"/v1/runs/{run.id}/events", headers=bearer(token))
    assert response.status_code == 200 and (await response.get_json())["done"] is True
    wrong = sign_token(rsa_key, oidc_claims(settings.oidc_audience, repository_id="5"))
    assert (await client.get(f"/v1/runs/{run.id}/events", headers=bearer(wrong))).status_code == 404


async def test_verdict_prefers_the_newest_run(
    oidc_app: Quart, client: Client, db: Database, admin_token: str
) -> None:
    repo = await seed_repo(db)
    await seed_run(db, repo, status="completed", verdict="failure")
    newest = await seed_run(db, repo, status="queued")
    async with db.session() as session:
        runs = list(await session.scalars(select(Run).order_by(Run.created_at)))
        runs[-1].created_at = runs[0].created_at + timedelta(seconds=5)
        await session.commit()
    body = await (await client.get(verdict_path(), headers=bearer(admin_token))).get_json()
    assert body["run_id"] == newest.id
