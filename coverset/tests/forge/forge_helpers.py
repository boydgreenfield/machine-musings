"""Helpers shared by the forge tests (fixtures live in this directory's conftest)."""

from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from quart import Quart
from sqlalchemy import func, select

from coverset.config import Settings
from coverset.db.models import Repo, Run, RunEvent, TestCase, TestResult
from coverset.db.session import Database
from coverset.forge.oidc import ISSUER

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "webhooks"
FIXTURE_NAMES = (
    "pr_opened",
    "pr_synchronize",
    "pr_opened_fork",
    "push_main",
    "check_run_rerequested",
    "check_run_requested_action",
    "installation_created",
    "ping",
)
DEMO_REPO_ID = 424242
DEMO_OWNER_ID = 777
DEMO_FULL_NAME = "coverset-demo/ledger"
SHA_A = "a" * 40
SHA_B = "b" * 40


def load_fixture(name: str) -> tuple[dict[str, str], bytes]:
    """Headers and the exact signed body of ``fixtures/webhooks/<name>.json``."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return dict(data["headers"]), data["body"].encode("utf-8")


@dataclass
class RecordingQueue:
    """A :class:`coverset.orchestrator.RunQueue` that remembers what it was given."""

    run_ids: list[str] = field(default_factory=list)

    def enqueue(self, run_id: str) -> None:
        self.run_ids.append(run_id)


async def seed_repo(
    db: Database,
    *,
    external_id: int = DEMO_REPO_ID,
    owner_id: int | None = DEMO_OWNER_ID,
    private: bool = False,
    forge: str = "github",
) -> Repo:
    """A repo as a verified webhook would have stored it."""
    async with db.session() as session:
        repo = Repo(
            forge=forge,
            external_id=str(external_id),
            full_name=DEMO_FULL_NAME,
            private=private,
            owner_id=owner_id,
            installation_id=55,
        )
        session.add(repo)
        await session.commit()
        return repo


async def seed_run(
    db: Database,
    repo: Repo,
    *,
    sha: str = SHA_A,
    status: str = "running",
    verdict: str | None = None,
    events: Sequence[tuple[str, dict[str, Any]]] = (),
    results: Sequence[tuple[str, str]] = (),
) -> Run:
    """A run with ``events`` stored at seq 0, 1, ... and ``results`` as (nodeid, outcome) pairs."""
    async with db.session() as session:
        run = Run(repo_id=repo.id, head_sha=sha, trigger="pull_request", status=status, verdict=verdict)
        session.add(run)
        await session.flush()
        for seq, (kind, payload) in enumerate(events):
            session.add(RunEvent(run_id=run.id, seq=seq, kind=kind, payload=payload))
        for index, (nodeid, outcome) in enumerate(results):
            test = TestCase(
                repo_id=repo.id, nodeid=nodeid, nodeid_hash=index.to_bytes(16, "big"), family=nodeid
            )
            session.add(test)
            await session.flush()
            session.add(TestResult(run_id=run.id, test_id=test.id, outcome=outcome))
        await session.commit()
        return run


async def add_events(db: Database, run_id: str, events: Sequence[tuple[str, dict[str, Any]]]) -> None:
    """Append events after the run's current last seq."""
    async with db.session() as session:
        last = await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
        start = -1 if last is None else last
        for offset, (kind, payload) in enumerate(events, start=1):
            session.add(RunEvent(run_id=run_id, seq=start + offset, kind=kind, payload=payload))
        await session.commit()


async def set_run_status(db: Database, run_id: str, status: str, verdict: str | None = None) -> None:
    """Change a run's status (and verdict)."""
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.status = status  # type: ignore[assignment]
        run.verdict = verdict  # type: ignore[assignment]
        await session.commit()


class StubJwkClient:
    """Serves one public key by ``kid`` (stands in for GitHub's JWKS endpoint)."""

    def __init__(self, key: rsa.RSAPrivateKey, kid: str = "test-kid") -> None:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        public.update({"kid": kid, "alg": "RS256", "use": "sig"})
        self.kid = kid
        self.jwk = PyJWK(public)
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        self.calls += 1
        if jwt.get_unverified_header(token).get("kid") != self.kid:
            raise jwt.PyJWKClientError("Unable to find a signing key that matches")
        return self.jwk


def oidc_claims(audience: str, **overrides: Any) -> dict[str, Any]:
    """Claims shaped like a GitHub Actions token for the demo repository (None drops a claim)."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": audience,
        "sub": "repo:coverset-demo/ledger:pull_request",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "repository": DEMO_FULL_NAME,
        "repository_id": str(DEMO_REPO_ID),
        "repository_owner": "coverset-demo",
        "repository_owner_id": str(DEMO_OWNER_ID),
        "ref": "refs/pull/1/merge",
        "sha": SHA_A,
        "event_name": "pull_request",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def sign_token(key: rsa.RSAPrivateKey, claims: dict[str, Any], *, kid: str = "test-kid") -> str:
    """An RS256 JWT over ``claims``."""
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def free_port() -> int:
    """A currently free loopback TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LiveServer:
    """A Hypercorn-served app in a background thread; ``requests`` logs what the app saw."""

    url: str
    app: Quart
    settings: Settings
    requests: list[str] = field(default_factory=list)
