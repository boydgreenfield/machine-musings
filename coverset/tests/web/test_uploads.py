"""``POST /v1/bundles`` and ``GET /v1/bundles/<run_id>``: upload mode end to end on the test app.

A real bundle comes from ``pytest --coverset`` on a copy of ``examples/demo-app`` (run once per
module, about 3 s). The archive is built the way ``coverset-ci upload`` builds it, so these tests
pin the server's half of the contract: the auth matrix (no credentials, a token for another
repository, a token without the upload scope, OIDC for another repository, unknown owners), the
size cap, the switch, the happy path (run, facts, results, verdict, the sticky comment in the fake
forge's outbox), replay idempotence, shard merging and the uploaded-source store (only files the
bundle lists; traversal refused).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import func, select

from coverset.config import Settings
from coverset.db.models import AuditEvent, PullRequest, Repo, Run, RunEvent, Shard, TestResult
from coverset.facts import sources as source_store
from coverset.facts.store import facts_store
from coverset.forge.fake import FakeForge
from coverset.forge.oidc import ISSUER
from coverset.orchestrator.bundles import IngestPool
from coverset.orchestrator.runner import OrchestratorContext
from coverset.orchestrator.upload import NO_DIFF_REASON
from coverset.storage import from_settings
from coverset.storage.uploads import upload_key
from coverset.web import tokens
from coverset.web.api import JWK_CLIENT_EXTENSION
from coverset.web.uploads import upload_worker

ROOT = Path(__file__).resolve().parents[2]
DEMO_APP = ROOT / "examples" / "demo-app"
ADMIN = "upload-admin-token-0123456789abcdefghij"
REPO = "acme/widgets"
REPO_GH_ID = 4001
OWNER_ID = 77
SHA = "1" * 40
BASE = "2" * 40


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"admin_token": ADMIN, "allowed_owners": ["acme"], "forge": "fake", "ai": "off"}


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``.coverset/`` from ``pytest --coverset`` on a copy of the demo app."""
    work = tmp_path_factory.mktemp("upload-demo") / "demo"
    shutil.copytree(DEMO_APP, work, ignore=shutil.ignore_patterns(".coverset", "__pycache__", ".venv"))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--coverset", "-q", "-p", "no:cacheprovider"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return work / ".coverset"


def make_archive(
    bundle: Path,
    *,
    sources: dict[str, bytes] | None = None,
    python: bool = True,
    neutral: bool = True,
    raw_members: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """An upload archive shaped like ``coverset-ci upload``'s."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        if python:
            add("bundle.tar.gz", (bundle / "bundle.tar.gz").read_bytes())
        if neutral:
            add("neutral.tar.gz", (bundle / "neutral.tar.gz").read_bytes())
        add("meta.json", (bundle / "meta.json").read_bytes())
        for path, data in (sources or {}).items():
            add(f"sources/{path}", data)
        for name, data in raw_members or []:
            add(name, data)
    return buffer.getvalue()


def headers(token: str | None, **extra: str) -> dict[str, str]:
    values = {
        "Content-Type": "application/gzip",
        "X-Coverset-Repo": REPO,
        "X-Coverset-Sha": SHA,
        "X-Coverset-Label": "py313",
    }
    if token is not None:
        values["Authorization"] = f"Bearer {token}"
    values.update(extra)
    return values


@pytest.fixture
def forge(app: Quart) -> FakeForge:
    fake = FakeForge(session_factory=app.extensions["db"].sessionmaker)
    app.extensions["forge"] = fake
    ctx = OrchestratorContext.from_settings(
        app.extensions["settings"], db=app.extensions["db"], ingest=IngestPool("thread", max_workers=2)
    )
    ctx.forge = fake
    app.extensions["orchestrator"] = ctx
    return fake


@pytest.fixture
async def repo(app: Quart) -> Repo:
    async with app.extensions["db"].session() as session:
        row = Repo(forge="github", external_id=str(REPO_GH_ID), full_name=REPO, owner_id=OWNER_ID)
        other = Repo(forge="github", external_id="4002", full_name="acme/other", owner_id=OWNER_ID)
        session.add_all([row, other])
        await session.commit()
        return row


async def make_token(app: Quart, repo_id: int | None, scopes: list[str]) -> str:
    async with app.extensions["db"].session() as session:
        raw, _row = await tokens.create(
            session, name="ci", kind="repo", scopes=scopes, now=datetime.now(UTC), repo_id=repo_id
        )
        await session.commit()
        return raw


async def other_repo_id(app: Quart) -> int:
    async with app.extensions["db"].session() as session:
        found = await session.scalar(select(Repo.id).where(Repo.full_name == "acme/other"))
        assert found is not None
        return found


async def settle(app: Quart) -> None:
    await upload_worker(app).idle()


# -- OIDC --------------------------------------------------------------------------------------


class StubJwk:
    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        public.update({"kid": "k1", "alg": "RS256", "use": "sig"})
        self.jwk = PyJWK(public)

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        return self.jwk


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def oidc_token(key: rsa.RSAPrivateKey, settings: Settings, **overrides: Any) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": settings.oidc_audience,
        "sub": f"repo:{REPO}:pull_request",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "repository": REPO,
        "repository_id": str(REPO_GH_ID),
        "repository_owner": "acme",
        "repository_owner_id": str(OWNER_ID),
        "repository_visibility": "private",
        "event_name": "pull_request",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def oidc(app: Quart, rsa_key: rsa.RSAPrivateKey) -> rsa.RSAPrivateKey:
    app.extensions[JWK_CLIENT_EXTENSION] = StubJwk(rsa_key)
    return rsa_key


# -- the auth matrix -----------------------------------------------------------------------------


async def test_no_credentials_is_401(client: Client, repo: Repo, bundle_dir: Path) -> None:
    response = await client.post("/v1/bundles", headers=headers(None), data=make_archive(bundle_dir))
    assert response.status_code == 401
    assert (await response.get_json())["error"] == "unauthorized"


async def test_an_unknown_cst_token_is_401(client: Client, repo: Repo, bundle_dir: Path) -> None:
    fake = "cst_abcdefgh_" + "x" * 40
    response = await client.post("/v1/bundles", headers=headers(fake), data=make_archive(bundle_dir))
    assert response.status_code == 401


async def test_a_token_for_another_repository_is_403(
    app: Quart, client: Client, repo: Repo, bundle_dir: Path
) -> None:
    token = await make_token(app, await other_repo_id(app), ["upload"])
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 403
    assert (await response.get_json())["error"] == "wrong_repository"


async def test_a_token_without_the_upload_scope_is_403(
    app: Quart, client: Client, repo: Repo, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["read"])
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 403
    assert (await response.get_json())["error"] == "insufficient_scope"


async def test_oidc_bound_to_another_repository_is_403(
    app: Quart, client: Client, repo: Repo, oidc: rsa.RSAPrivateKey, bundle_dir: Path
) -> None:
    token = oidc_token(oidc, app.extensions["settings"], repository="acme/other", repository_id="4002")
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 403


async def test_oidc_with_a_mismatched_owner_is_403(
    app: Quart, client: Client, repo: Repo, oidc: rsa.RSAPrivateKey, bundle_dir: Path
) -> None:
    token = oidc_token(oidc, app.extensions["settings"], repository_owner_id="999")
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 403


async def test_oidc_for_an_unknown_repo_of_an_allowed_owner_creates_it(
    app: Quart, client: Client, forge: FakeForge, oidc: rsa.RSAPrivateKey, bundle_dir: Path
) -> None:
    token = oidc_token(oidc, app.extensions["settings"])
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 202, await response.get_data()
    await settle(app)
    async with app.extensions["db"].session() as session:
        created = await session.scalar(select(Repo).where(Repo.full_name == REPO))
        assert created is not None
        assert (created.forge, created.external_id, created.owner_id, created.private) == (
            "github",
            str(REPO_GH_ID),
            OWNER_ID,
            True,
        )


async def test_oidc_for_an_owner_not_allowed_is_404(
    app: Quart, client: Client, oidc: rsa.RSAPrivateKey, bundle_dir: Path
) -> None:
    token = oidc_token(
        oidc,
        app.extensions["settings"],
        repository="mallory/widgets",
        repository_owner="mallory",
        repository_id="5005",
    )
    response = await client.post(
        "/v1/bundles", headers=headers(token, **{"X-Coverset-Repo": "mallory/widgets"}), data=b"x"
    )
    assert response.status_code == 404
    assert (await response.get_json())["error"] == "unknown_repository"


async def test_a_pull_request_target_token_records_an_untrusted_run(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, oidc: rsa.RSAPrivateKey, bundle_dir: Path
) -> None:
    token = oidc_token(oidc, app.extensions["settings"], event_name="pull_request_target")
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 202
    await settle(app)
    async with app.extensions["db"].session() as session:
        run = await session.get(Run, (await response.get_json())["run_id"])
        assert run is not None and run.trusted is False


async def test_the_size_cap_is_413(
    app: Quart, client: Client, repo: Repo, bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    monkeypatch.setattr(app.extensions["settings"], "max_upload_bytes", 1000)
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 413


async def test_uploads_switched_off_are_404(
    app: Quart, client: Client, repo: Repo, bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    monkeypatch.setattr(app.extensions["settings"], "uploads_enabled", False)
    response = await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
    assert response.status_code == 404


async def test_bad_headers_are_400(app: Quart, client: Client, repo: Repo) -> None:
    token = await make_token(app, repo.id, ["upload"])
    for bad in ({"X-Coverset-Sha": "HEAD"}, {"X-Coverset-Label": "../x"}, {"X-Coverset-Pr": "-1"}):
        response = await client.post("/v1/bundles", headers=headers(token, **bad), data=b"x")
        assert response.status_code == 400, bad


async def test_a_digest_mismatch_and_a_garbage_body_are_400(
    app: Quart, client: Client, repo: Repo, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    response = await client.post(
        "/v1/bundles",
        headers=headers(token, **{"X-Coverset-Archive-Sha256": "0" * 64}),
        data=make_archive(bundle_dir),
    )
    assert (response.status_code, (await response.get_json())["error"]) == (400, "digest_mismatch")
    response = await client.post("/v1/bundles", headers=headers(token), data=b"not a tarball")
    assert (response.status_code, (await response.get_json())["error"]) == (400, "bad_archive")


# -- the happy path ------------------------------------------------------------------------------


async def test_an_upload_becomes_a_run_with_facts_a_verdict_and_a_sticky_comment(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    response = await client.post(
        "/v1/bundles",
        headers=headers(
            token,
            **{"X-Coverset-Pr": "7", "X-Coverset-Base": BASE, "X-Coverset-Branch": "feature%2Fx"},
        ),
        data=make_archive(bundle_dir),
    )
    assert response.status_code == 202, await response.get_data()
    body = await response.get_json()
    assert body["state"] == "running" and body["shard"] == 0 and body["duplicate"] is False
    assert body["url"].endswith(f"/runs/{body['run_id']}")
    assert body["pr_url"].endswith(f"/repos/{repo.id}/prs/7")
    await settle(app)
    db = app.extensions["db"]
    async with db.session() as session:
        run = await session.get(Run, body["run_id"])
        assert run is not None
        assert (run.trigger, run.status, run.verdict, run.branch) == (
            "upload",
            "completed",
            "success",
            "feature/x",
        )
        assert run.reason is not None and NO_DIFF_REASON in run.reason
        assert run.coverage_complete is True
        results = await session.scalar(select(func.count()).where(TestResult.run_id == run.id))
        assert results == 97
        pr = await session.get(PullRequest, run.pr_id)
        assert pr is not None and pr.number == 7 and pr.head_sha == SHA
        kinds = list(await session.scalars(select(RunEvent.kind).where(RunEvent.run_id == run.id)))
        assert kinds[-2:] == ["verdict", "done"]
        audited = await session.scalar(select(func.count()).where(AuditEvent.action == "bundle_uploaded"))
        assert audited == 1
    store = facts_store(app.extensions["settings"])
    assert store.run_dir(repo.id, body["run_id"]).is_dir()
    comments = forge.calls_named("upsert_pr_comment")
    assert len(comments) == 1
    assert comments[0].request["pr_number"] == 7
    body_md = comments[0].request["body"]
    assert "<!-- coverset:sticky -->" in body_md and "no diff available" in body_md
    assert "largest input\\-class gaps" in body_md
    assert any(call.method in ("create_check", "update_check") for call in forge.calls)
    # The outbox route renders what the fake forge recorded.
    outbox = await client.get("/forge/outbox", headers={"Authorization": f"Bearer {ADMIN}"})
    assert outbox.status_code == 200
    assert "coverset" in (await outbox.get_data(as_text=True))

    admin = {"Authorization": f"Bearer {ADMIN}"}
    for page in (f"/runs/{body['run_id']}", f"/repos/{repo.id}/prs/7", f"/repos/{repo.id}"):
        rendered = await client.get(page, headers=admin)
        assert rendered.status_code == 200, page
    status = await client.get(f"/v1/bundles/{body['run_id']}", headers={"Authorization": f"Bearer {token}"})
    assert status.status_code == 200
    state = await status.get_json()
    assert (state["state"], state["conclusion"], state["exit_code"]) == ("completed", "success", 0)
    assert state["counts"]["passed"] == 97
    assert state["shards"] == [{"shard": 0, "label": "py313", "status": "completed"}]


async def test_a_replayed_upload_is_idempotent_and_a_second_label_merges(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    archive = make_archive(bundle_dir)
    first = await (await client.post("/v1/bundles", headers=headers(token), data=archive)).get_json()
    await settle(app)
    again = await client.post("/v1/bundles", headers=headers(token), data=archive)
    replay = await again.get_json()
    assert again.status_code == 202
    assert (replay["run_id"], replay["shard"], replay["duplicate"]) == (first["run_id"], 0, True)
    await settle(app)
    second = await (
        await client.post(
            "/v1/bundles", headers=headers(token, **{"X-Coverset-Label": "py312"}), data=archive
        )
    ).get_json()
    assert (second["run_id"], second["shard"], second["duplicate"]) == (first["run_id"], 1, False)
    await settle(app)
    db = app.extensions["db"]
    async with db.session() as session:
        shards = list(await session.scalars(select(Shard).where(Shard.run_id == first["run_id"])))
        assert sorted((s.shard_id, s.status) for s in shards) == [(0, "completed"), (1, "completed")]
        run = await session.get(Run, first["run_id"])
        assert run is not None and run.status == "completed" and run.coverage_complete
        runs = await session.scalar(select(func.count()).where(Run.repo_id == repo.id))
        assert runs == 1
        dones = await session.scalar(
            select(func.count()).where(RunEvent.run_id == run.id, RunEvent.kind == "done")
        )
        assert dones == 2  # one per pass that concluded; the replay changed nothing


async def test_expected_shards_hold_the_verdict_until_every_label_is_in(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    archive = make_archive(bundle_dir)
    extra = {"X-Coverset-Shards": "2", "X-Coverset-Pr": "9"}
    first = await (await client.post("/v1/bundles", headers=headers(token, **extra), data=archive)).get_json()
    await settle(app)
    async with app.extensions["db"].session() as session:
        run = await session.get(Run, first["run_id"])
        assert run is not None and run.status == "running" and run.verdict is None
    assert forge.calls_named("upsert_pr_comment") == []
    extra["X-Coverset-Label"] = "py312"
    await client.post("/v1/bundles", headers=headers(token, **extra), data=archive)
    await settle(app)
    async with app.extensions["db"].session() as session:
        run = await session.get(Run, first["run_id"])
        assert run is not None and (run.status, run.verdict) == ("completed", "success")
    assert len(forge.calls_named("upsert_pr_comment")) == 1


async def test_the_status_route_hides_runs_from_other_credentials(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    run_id = (
        await (
            await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
        ).get_json()
    )["run_id"]
    await settle(app)
    stranger = await make_token(app, await other_repo_id(app), ["upload"])
    response = await client.get(f"/v1/bundles/{run_id}", headers={"Authorization": f"Bearer {stranger}"})
    assert response.status_code == 404
    assert (await client.get(f"/v1/bundles/{run_id}")).status_code == 401


# -- uploaded sources ----------------------------------------------------------------------------


async def test_sources_are_stored_only_for_files_the_bundle_lists(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    parse = (DEMO_APP / "src" / "ledger" / "durations" / "parse.py").read_bytes()
    archive = make_archive(
        bundle_dir,
        sources={
            "src/ledger/durations/parse.py": parse,
            "pyproject.toml": (DEMO_APP / "pyproject.toml").read_bytes(),
            ".env": b"SECRET=1\n",
            "src/ledger/not_measured.txt": b"nope",
        },
    )
    response = await client.post("/v1/bundles", headers=headers(token), data=archive)
    assert response.status_code == 202
    await settle(app)
    sources = source_store.source_store(app.extensions["settings"])  # local files, or the bucket
    stored = sources.list(repo.id, SHA)
    assert "src/ledger/durations/parse.py" in stored and "pyproject.toml" in stored
    assert ".env" not in stored and "src/ledger/not_measured.txt" not in stored
    root = sources.checkout(repo.id, SHA)  # the data dir's own copy, or the cache's
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert oct((root / "src/ledger/durations/parse.py").stat().st_mode & 0o777) == "0o600"
    # The source view falls back to the store when there is no mirror.
    run_id = (await response.get_json())["run_id"]
    page = await client.get(
        f"/repos/{repo.id}/files/src/ledger/durations/parse.py?run={run_id}",
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert page.status_code == 200
    assert "parse_duration" in await page.get_data(as_text=True)


async def test_a_traversal_member_is_refused(
    app: Quart, client: Client, repo: Repo, bundle_dir: Path
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    archive = make_archive(bundle_dir, raw_members=[("sources/../../evil.py", b"x")])
    response = await client.post("/v1/bundles", headers=headers(token), data=archive)
    assert response.status_code == 400
    assert (await response.get_json())["error"] == "bad_archive"
    assert not (app.extensions["settings"].data_dir / "evil.py").exists()


def test_the_store_refuses_unsafe_paths_and_never_overwrites(tmp_path: Path) -> None:
    uploaded = tmp_path / "up"
    (uploaded / "pkg").mkdir(parents=True)
    (uploaded / "pkg" / "a.py").write_text("one")
    (uploaded / "pkg" / "b.py").write_text("two")
    (uploaded / "pkg" / "link.py").symlink_to(uploaded / "pkg" / "a.py")
    data = tmp_path / "data"
    result = source_store.store_sources(data, 3, SHA, uploaded, allowed={"pkg/a.py", "pkg/link.py"})
    assert result.stored == ["pkg/a.py"]
    assert result.refused == {"not_in_bundle": 1, "not_regular": 1}
    (uploaded / "pkg" / "a.py").write_text("changed")
    again = source_store.store_sources(data, 3, SHA, uploaded, allowed={"pkg/a.py"})
    assert again.kept == ["pkg/a.py"]
    assert source_store.read_source(data, 3, SHA, "pkg/a.py") == b"one"
    assert source_store.read_source(data, 3, SHA, "../x") is None
    assert source_store.read_source(data, 3, "HEAD", "pkg/a.py") is None


@pytest.fixture(autouse=True)
def _no_network_git(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test here may reach a real forge: a mirror fetch is recorded and fails the test.

    The stub records instead of raising because the processing pass swallows any exception from
    the fetch (a failed refresh falls back to what exists), so a raise would only be logged.
    """
    import coverset.orchestrator.upload as upload

    fetches: list[tuple[Any, ...]] = []

    async def record(*args: Any, **kwargs: Any) -> Path:
        fetches.append(args)
        raise RuntimeError("upload tests must not fetch a mirror")

    monkeypatch.setattr(upload, "ensure_mirror_async", record)
    yield
    assert fetches == [], "a test tried to refresh a mirror with forge credentials"


# -- the Actions integration ------------------------------------------------------------------------


def test_the_upload_action_keeps_tokens_away_from_the_code_under_test() -> None:
    import yaml

    action = yaml.safe_load((ROOT / "src/coverset/forge/gha/upload/action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["server"]["required"] and action["inputs"]["package"]["required"]
    assert "default" not in action["inputs"]["package"]
    install, run = action["runs"]["steps"]
    assert "env -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_ID_TOKEN_REQUEST_TOKEN" in install["run"]
    # `shards` without a distinct `label` per leg can never conclude: the step refuses before pytest.
    assert run["run"].index("set 'label' per matrix job") < run["run"].index("python -m pytest")
    assert (
        "env -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_ID_TOKEN_REQUEST_TOKEN -u COVERSET_TOKEN \\\n"
        "  python -m pytest --coverset" in run["run"]
    )
    assert "coverset-ci" in run["run"] and "${{" not in run["run"]  # inputs only through env


def test_the_example_workflow_uses_the_action_with_oidc_and_the_pr_head() -> None:
    import yaml

    workflow = yaml.safe_load((ROOT / "docs/examples/coverset-upload.yml").read_text())
    assert workflow["permissions"]["id-token"] == "write"
    steps = workflow["jobs"]["test"]["steps"]
    assert "pull_request.head.sha" in steps[0]["with"]["ref"]
    uses = steps[-1]["uses"]
    assert uses.split("@")[0].endswith("coverset/src/coverset/forge/gha/upload")
    assert (ROOT / "src/coverset/forge/gha/upload/action.yml").is_file()


# -- concurrent passes ---------------------------------------------------------------------------


async def _register(
    app: Quart, repo: Repo, archive: bytes, label: str, tmp_path: Path, *, shards: int | None = None
) -> str:
    from coverset.orchestrator.upload import UploadMeta, register_upload, unpack_upload

    settings = app.extensions["settings"]
    path = tmp_path / f"{label}.tar.gz"
    path.write_bytes(archive)
    unpacked = unpack_upload(path, tmp_path / f"{label}.d", settings)
    meta = UploadMeta(
        repo_id=repo.id,
        head_sha=SHA,
        base_sha=None,
        branch=None,
        pr_number=None,
        label=label,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        archive_bytes=len(archive),
        expected_shards=shards,
    )
    registration = await register_upload(
        app.extensions["db"], settings, meta, unpacked, claimant=app.extensions["orchestrator"].claimant
    )
    return registration.run_id


async def test_a_pass_that_missed_a_new_shard_writes_nothing_final(
    app: Quart, repo: Repo, forge: FakeForge, bundle_dir: Path, tmp_path: Path
) -> None:
    from coverset.orchestrator.upload import _Pass, process_run, shard_dir

    db = app.extensions["db"]
    ctx = app.extensions["orchestrator"]
    run_id = await _register(app, repo, make_archive(bundle_dir), "a", tmp_path)
    async with db.session() as session:
        rows = (await session.execute(select(Shard.shard_id, Shard.worker_ref))).all()
        stale = _Pass(seen={shard_id: ref for shard_id, ref in rows}, arriving=set())
        assert await stale.current(session, run_id)
    await _register(app, repo, make_archive(bundle_dir, python=False), "b", tmp_path)
    async with db.session() as session:
        assert not await stale.current(session, run_id)
    # A shard registered but not on disk yet holds the verdict back without failing the run. The
    # pass would restore it from its archive in the blob store, so that is parked too: the moment
    # between registering a shard and archiving it.
    moved = shard_dir(app.extensions["settings"], run_id, 1)
    parked = tmp_path / "parked"
    moved.rename(parked)
    blobs = from_settings(app.extensions["settings"])
    archive = upload_key(repo.id, SHA, "b")
    parked_archive = blobs.get_bytes(archive)[0]
    blobs.delete([archive])
    await process_run(ctx, run_id)
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and (run.status, run.verdict) == ("running", None)
    assert forge.calls_named("upsert_pr_comment") == [] and forge.calls_named("set_commit_status") == []
    parked.rename(moved)
    blobs.put_bytes(archive, parked_archive)
    await process_run(ctx, run_id)
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and (run.status, run.verdict) == ("completed", "success")
        statuses = list(await session.scalars(select(Shard.status).where(Shard.run_id == run_id)))
        assert statuses == ["completed", "completed"]


async def test_a_pass_reads_the_neutral_manifests_once_off_the_event_loop(
    app: Quart,
    repo: Repo,
    forge: FakeForge,
    bundle_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding a neutral bundle's language may decompress the whole archive: a pass does it once per
    shard, in a worker thread, and reuses the answer for the diff and the ingest jobs."""
    import threading

    from coverset.orchestrator import upload as upload_module
    from coverset.orchestrator.upload import process_run

    loop_thread = threading.get_ident()
    calls: list[bool] = []
    real = upload_module._neutral_language

    def recording(tarball: Path) -> str:
        calls.append(threading.get_ident() == loop_thread)
        return real(tarball)

    monkeypatch.setattr(upload_module, "_neutral_language", recording)
    run_id = await _register(app, repo, make_archive(bundle_dir, python=False), "neutral", tmp_path)
    await process_run(app.extensions["orchestrator"], run_id)
    async with app.extensions["db"].session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.status == "completed"
    assert calls == [False], "one read per shard, never on the event loop"


# -- a control-plane restart ---------------------------------------------------------------------


async def _restart(app: Quart, monkeypatch: pytest.MonkeyPatch, *, requeue: bool) -> list[str]:
    """What a new ``coverset serve`` does at boot: the dispatcher's takeover of leftover runs.

    A new ``serve`` claimant on the same host is what makes the previous one's runs orphans."""
    from coverset.orchestrator.queue import RunDispatcher
    from coverset.orchestrator.runner import process_claimant

    ctx = app.extensions["orchestrator"]
    ctx.claimant = process_claimant("serve")
    monkeypatch.setattr(app.extensions["settings"], "requeue_on_boot", requeue)
    return await RunDispatcher(ctx, max_concurrent=1).on_boot()


@pytest.mark.parametrize("requeue", [False, True])
async def test_a_restart_between_two_matrix_legs_does_not_strand_the_run(
    app: Quart,
    repo: Repo,
    forge: FakeForge,
    bundle_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requeue: bool,
) -> None:
    """Leg A uploads with ``--shards 2``, the server restarts, leg B uploads: one run concludes with
    both shards. The boot takeover skips upload runs (they are never executed, and one waiting for
    its legs is ``running`` by design), so the run is neither abandoned nor copied, with or without
    ``COVERSET_REQUEUE_ON_BOOT``; it was formerly abandoned and re-opened by leg B."""
    from coverset.orchestrator.runner import process_claimant
    from coverset.orchestrator.upload import process_run

    db = app.extensions["db"]
    ctx = app.extensions["orchestrator"]
    ctx.claimant = process_claimant("serve")
    run_id = await _register(app, repo, make_archive(bundle_dir), "py313", tmp_path, shards=2)
    await process_run(ctx, run_id)
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and (run.status, run.verdict) == ("running", None)
    touched = await _restart(app, monkeypatch, requeue=requeue)
    assert run_id not in touched
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and (run.status, run.verdict) == ("running", None)
        copies = list(await session.scalars(select(Run).where(Run.id != run_id, Run.head_sha == SHA)))
        assert copies == []

    joined = await _register(app, repo, make_archive(bundle_dir, python=False), "py312", tmp_path, shards=2)
    assert joined == run_id
    await process_run(ctx, run_id)
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and (run.status, run.verdict) == ("completed", "success")
        assert run.shard_count == 2 and "finished" in (run.timeline or {})
        statuses = list(await session.scalars(select(Shard.status).where(Shard.run_id == run_id)))
        assert statuses == ["completed", "completed"]
        kinds = list(
            await session.scalars(
                select(RunEvent.kind).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            )
        )
        assert kinds[-2:] == ["verdict", "done"] and kinds.count("done") == 1
        copies = list(await session.scalars(select(Run).where(Run.id != run_id, Run.head_sha == SHA)))
        assert copies == []


async def test_a_pass_cut_off_by_a_restart_concludes_when_serve_resumes(
    app: Quart,
    repo: Repo,
    forge: FakeForge,
    bundle_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose every shard is on disk but whose pass never finished (the process died) is
    ``running`` with nobody to schedule it: the boot takeover skips it, and the start-up resume
    (:func:`~coverset.orchestrator.queue.resume_upload_runs`) concludes it. A ``--shards 2`` run with
    one leg in gets the same pass and keeps waiting, with nothing posted."""
    from coverset.orchestrator.queue import resume_upload_runs

    db = app.extensions["db"]
    done = await _register(app, repo, make_archive(bundle_dir), "py313", tmp_path)
    async with db.session() as session:
        other_repo = await session.get(Repo, await other_repo_id(app))
        assert other_repo is not None
    (tmp_path / "w").mkdir()
    waiting = await _register(app, other_repo, make_archive(bundle_dir), "py313", tmp_path / "w", shards=2)
    touched = await _restart(app, monkeypatch, requeue=False)
    assert done not in touched and waiting not in touched
    assert set(await resume_upload_runs(app)) == {done, waiting}
    await settle(app)
    async with db.session() as session:
        run = await session.get(Run, done)
        assert run is not None and (run.status, run.verdict) == ("completed", "success")
        kinds = list(await session.scalars(select(RunEvent.kind).where(RunEvent.run_id == done)))
        assert kinds.count("done") == 1
        held = await session.get(Run, waiting)
        assert held is not None and (held.status, held.verdict) == ("running", None)
        held_kinds = list(await session.scalars(select(RunEvent.kind).where(RunEvent.run_id == waiting)))
        assert "done" not in held_kinds
    assert await resume_upload_runs(app) == [waiting], "a concluded run is not resumed again"
    await settle(app)


async def test_an_abandoned_upload_run_is_left_alone_and_a_new_upload_starts_a_fresh_run(
    app: Quart, repo: Repo, forge: FakeForge, bundle_dir: Path, tmp_path: Path
) -> None:
    """An upload run an older version's boot takeover abandoned is terminal like an errored one: the
    next upload of the commit starts a fresh run instead of re-opening it."""
    db = app.extensions["db"]
    run_id = await _register(app, repo, make_archive(bundle_dir), "py313", tmp_path, shards=2)
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.status, run.verdict = "abandoned", "neutral"
        await session.commit()
    fresh = await _register(app, repo, make_archive(bundle_dir, python=False), "py312", tmp_path, shards=2)
    assert fresh != run_id
    async with db.session() as session:
        old = await session.get(Run, run_id)
        assert old is not None and old.status == "abandoned"


async def test_the_status_route_answers_alike_for_unknown_and_foreign_runs(
    app: Quart, client: Client, repo: Repo, forge: FakeForge, bundle_dir: Path
) -> None:
    """A bad bearer is 401 and a valid foreign one 404 whether or not the run exists: no oracle."""
    token = await make_token(app, repo.id, ["upload"])
    run_id = (
        await (
            await client.post("/v1/bundles", headers=headers(token), data=make_archive(bundle_dir))
        ).get_json()
    )["run_id"]
    await settle(app)
    stranger = await make_token(app, await other_repo_id(app), ["upload"])
    garbage = {"Authorization": "Bearer cst_garbage"}
    for rid in (run_id, "run_doesnotexist"):
        assert (await client.get(f"/v1/bundles/{rid}", headers=garbage)).status_code == 401
        assert (await client.get(f"/v1/bundles/{rid}")).status_code == 401
        foreign = await client.get(f"/v1/bundles/{rid}", headers={"Authorization": f"Bearer {stranger}"})
        assert foreign.status_code == 404
    assert (
        await client.get(f"/v1/bundles/{run_id}", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200


async def test_an_upload_finalizes_with_the_scoped_policy_and_warms_the_caches(
    app: Quart,
    repo: Repo,
    forge: FakeForge,
    bundle_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload path hands ``finalize_run`` the same economy inputs as a managed run (the
    repository's ``policy.test_tax_threshold``), and, in ``serve``, schedules the cache warm-up."""
    from coverset import settings_scopes
    from coverset.orchestrator import upload as upload_module
    from coverset.orchestrator.upload import process_run
    from coverset.web import loaders

    async with app.extensions["db"].session() as session:
        await settings_scopes.set_value(
            session, "policy.test_tax_threshold", 3.0, scope="repo", scope_id=repo.id
        )
    seen: list[Any] = []
    real = upload_module.finalize_run

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("economy"))
        return real(*args, **kwargs)

    warmed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(upload_module, "finalize_run", recording)
    monkeypatch.setattr(loaders, "warm_run", lambda *args: warmed.append(args))
    ctx = app.extensions["orchestrator"]
    ctx.warm_caches = True
    run_id = await _register(app, repo, make_archive(bundle_dir), "py313", tmp_path)
    await process_run(ctx, run_id)
    await ctx.finish_background()
    assert len(seen) == 1 and seen[0] is not None and seen[0].policy.test_tax_threshold == 3.0
    assert [args[2] for args in warmed] == [run_id]
    async with app.extensions["db"].session() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.status == "completed"
