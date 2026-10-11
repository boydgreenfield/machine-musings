"""``POST /webhooks/<forge>``: signature first, fail closed, dedupe, allowlist, size, hand-off."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from forge_helpers import RecordingQueue, load_fixture
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coverset.app import MAX_CONTENT_LENGTH, create_app
from coverset.config import Settings
from coverset.db.models import Repo, Run, WebhookDelivery
from coverset.db.session import Database
from coverset.forge.fake import FakeForge
from coverset.forge.signing import TEST_WEBHOOK_SECRET, sign_body
from coverset.web import webhooks


async def post_fixture(client: Client, name: str, **header_overrides: str) -> Any:
    headers, body = load_fixture(name)
    headers.update(header_overrides)
    return await client.post("/webhooks/github", data=body, headers=headers)


async def count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def drain_background(app: Quart) -> None:
    """Wait for ``add_background_task`` work (forge follow-ups) to finish."""
    for _ in range(100):
        pending = [task for task in app.background_tasks if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending)


async def test_trusted_pr_is_accepted_and_enqueued(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    queue = RecordingQueue()
    app.extensions["run_queue"] = queue
    response = await post_fixture(client, "pr_opened")
    assert response.status_code == 202
    body = await response.get_json()
    assert body["status"] == "created" and body["run_id"].startswith("run_")
    assert queue.run_ids == [body["run_id"]]
    assert fake_forge.calls == []


async def test_without_a_run_queue_runs_wait_in_the_database(
    client: Client, db: Database, fake_forge: FakeForge
) -> None:
    response = await post_fixture(client, "push_main")
    assert response.status_code == 202
    async with db.session() as session:
        run = await session.scalar(select(Run))
        assert run is not None and run.status == "queued" and run.trigger == "baseline"


@pytest.mark.security
async def test_bad_signature_is_403_without_echo(client: Client, db: Database, fake_forge: FakeForge) -> None:
    response = await post_fixture(client, "pr_opened", **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert response.status_code == 403
    text = await response.get_data(as_text=True)
    assert json.loads(text) == {"error": "bad_signature"}
    assert "Support partial refunds" not in text
    assert await count(db, WebhookDelivery) == 0


async def test_missing_signature_is_403(client: Client, db: Database, fake_forge: FakeForge) -> None:
    headers, body = load_fixture("pr_opened")
    del headers["X-Hub-Signature-256"]
    response = await client.post("/webhooks/github", data=body, headers=headers)
    assert response.status_code == 403
    assert await count(db, WebhookDelivery) == 0


@pytest.mark.security
async def test_tampered_body_is_403(client: Client, db: Database, fake_forge: FakeForge) -> None:
    headers, body = load_fixture("pr_opened")
    response = await client.post("/webhooks/github", data=body.replace(b"MEMBER", b"OWNER"), headers=headers)
    assert response.status_code == 403


@pytest.mark.security
@pytest.mark.parametrize("overrides", [{"github_webhook_secret": None}, {"github_webhook_secret": ""}])
async def test_empty_secret_fails_closed(tmp_path: Any, overrides: dict[str, Any]) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data", **overrides)
    app = create_app(settings)
    async with app.test_app():
        response = await post_fixture(app.test_client(), "pr_opened")
    assert response.status_code == 503
    assert await response.get_json() == {"error": "webhook_secret_unset"}


@pytest.mark.security
async def test_test_secret_rejected_outside_test_env(tmp_path: Any) -> None:
    settings = Settings.for_tests(
        env="dev", data_dir=tmp_path / "data", github_webhook_secret=TEST_WEBHOOK_SECRET
    )
    app = create_app(settings)
    async with app.test_app():
        response = await post_fixture(app.test_client(), "pr_opened")
    assert response.status_code == 503
    assert await response.get_json() == {"error": "webhook_secret_rejected"}


@pytest.mark.security
async def test_duplicate_delivery_is_processed_once(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    queue = RecordingQueue()
    app.extensions["run_queue"] = queue
    first = await post_fixture(client, "pr_opened")
    second = await post_fixture(client, "pr_opened")
    assert first.status_code == 202
    assert second.status_code == 200 and await second.get_json() == {"status": "duplicate"}
    assert await count(db, Run) == 1 and await count(db, WebhookDelivery) == 1
    assert len(queue.run_ids) == 1


@pytest.mark.security
async def test_oversized_body_is_413(client: Client, fake_forge: FakeForge) -> None:
    headers, _body = load_fixture("pr_opened")
    size = MAX_CONTENT_LENGTH + 1
    response = await client.post(
        "/webhooks/github", data=b"x" * size, headers={**headers, "Content-Length": str(size)}
    )
    assert response.status_code == 413


@pytest.mark.security
async def test_unknown_event_is_recorded_and_ignored(
    client: Client, db: Database, fake_forge: FakeForge
) -> None:
    body = b'{"action":"opened","issue":{"number":1}}'
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "11111111-2222-3333-4444-555555555555",
        "X-Hub-Signature-256": sign_body(TEST_WEBHOOK_SECRET, body),
    }
    response = await client.post("/webhooks/github", data=body, headers=headers)
    assert response.status_code == 202
    assert (await response.get_json())["status"] == "ignored_event"
    async with db.session() as session:
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None and (delivery.event, delivery.status, delivery.payload) == (
            "issues",
            "ignored",
            None,
        )


@pytest.mark.security
async def test_ping_is_ignored(client: Client, db: Database, fake_forge: FakeForge) -> None:
    response = await post_fixture(client, "ping")
    assert response.status_code == 202 and (await response.get_json())["reason"] == "ping"
    assert await count(db, Run) == 0


async def test_malformed_payload_is_400(client: Client, db: Database, fake_forge: FakeForge) -> None:
    headers, body = load_fixture("pr_opened")
    payload = json.loads(body)
    payload["pull_request"]["head"]["sha"] = "not-a-sha"
    raw = json.dumps(payload).encode()
    headers["X-Hub-Signature-256"] = sign_body(TEST_WEBHOOK_SECRET, raw)
    response = await client.post("/webhooks/github", data=raw, headers=headers)
    assert response.status_code == 400 and await response.get_json() == {"error": "invalid_payload"}
    async with db.session() as session:
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None and delivery.status == "error"
    assert await count(db, Run) == 0


@pytest.mark.security
async def test_bad_delivery_id_is_400(client: Client, fake_forge: FakeForge) -> None:
    response = await post_fixture(client, "pr_opened", **{"X-GitHub-Delivery": "../../etc/passwd"})
    assert response.status_code == 400


@pytest.mark.security
async def test_unknown_forge_route_is_404(client: Client, fake_forge: FakeForge) -> None:
    headers, body = load_fixture("pr_opened")
    assert (await client.post("/webhooks/gitlab", data=body, headers=headers)).status_code == 404
    assert (await client.post("/webhooks/fake", data=body, headers=headers)).status_code == 202


async def test_fork_pr_gets_the_approval_check(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    queue = RecordingQueue()
    app.extensions["run_queue"] = queue
    response = await post_fixture(client, "pr_opened_fork")
    assert response.status_code == 202
    body = await response.get_json()
    assert body["status"] == "awaiting_approval"
    await drain_background(app)
    assert [call.method for call in fake_forge.calls] == ["create_check"]
    assert queue.run_ids == []

    approve = await post_fixture(client, "check_run_requested_action")
    assert (await approve.get_json())["status"] == "approval_pending"
    assert queue.run_ids == [], "queued only after the sender's permission is verified"
    await drain_background(app)
    assert queue.run_ids == [body["run_id"]]
    assert [call.method for call in fake_forge.calls] == [
        "create_check",
        "collaborator_permission",
        "update_check",
    ]


@pytest.mark.security
async def test_approve_run_from_a_non_writer_is_not_queued(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    queue = RecordingQueue()
    app.extensions["run_queue"] = queue
    fake_forge.permissions["dana-maintainer"] = "read"
    created = await (await post_fixture(client, "pr_opened_fork")).get_json()
    await post_fixture(client, "check_run_requested_action")
    await drain_background(app)
    assert queue.run_ids == []
    async with db.session() as session:
        run = await session.get(Run, created["run_id"])
        assert run is not None and run.status == "awaiting_approval" and run.approved_by is None


@pytest.mark.security
async def test_approval_fixture_binds_the_check_external_id(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    """The fixture's ``check_run.external_id`` names the run; it wins over a newer run on the same head."""
    queue = RecordingQueue()
    app.extensions["run_queue"] = queue
    newer = await (await post_fixture(client, "pr_opened_fork")).get_json()
    headers, body = load_fixture("check_run_requested_action")
    payload = json.loads(body)
    external_id = payload["check_run"]["external_id"]
    assert external_id.startswith("run_")
    async with db.session() as session:
        newest = await session.get(Run, newer["run_id"])
        assert newest is not None
        session.add(
            Run(
                id=external_id,
                repo_id=newest.repo_id,
                head_sha=payload["check_run"]["head_sha"],
                pr_id=newest.pr_id,
                trigger="pull_request",
                status="awaiting_approval",
                trusted=False,
                network="blocked",
                created_at=newest.created_at - timedelta(minutes=5),
            )
        )
        await session.commit()
    answer = await (await client.post("/webhooks/github", data=body, headers=headers)).get_json()
    assert answer["run_id"] == external_id
    await drain_background(app)
    assert queue.run_ids == [external_id]
    async with db.session() as session:
        untouched = await session.get(Run, newer["run_id"])
        assert untouched is not None and untouched.status == "awaiting_approval"


@pytest.mark.security
async def test_concurrent_deliveries_are_all_recorded(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    """A burst for a new repo (and two events for one PR) must not lose deliveries to unique races."""
    names = ("pr_opened", "pr_synchronize", "push_main", "pr_opened_fork")
    responses = await asyncio.gather(*(post_fixture(client, name) for name in names))
    assert [r.status_code for r in responses] == [202] * 4
    assert await count(db, WebhookDelivery) == 4 and await count(db, Run) == 4

    headers, body = load_fixture("pr_opened")
    payload = json.loads(body)
    payload["action"] = "ready_for_review"
    raw = json.dumps(payload).encode()
    ready_headers = {
        **headers,
        "X-GitHub-Delivery": "aaaaaaaa-0000-0000-0000-000000000001",
        "X-Hub-Signature-256": sign_body(TEST_WEBHOOK_SECRET, raw),
    }
    reopened = dict(payload, action="reopened")
    raw2 = json.dumps(reopened).encode()
    reopened_headers = {
        **headers,
        "X-GitHub-Delivery": "aaaaaaaa-0000-0000-0000-000000000002",
        "X-Hub-Signature-256": sign_body(TEST_WEBHOOK_SECRET, raw2),
    }
    both = await asyncio.gather(
        client.post("/webhooks/github", data=raw, headers=ready_headers),
        client.post("/webhooks/github", data=raw2, headers=reopened_headers),
    )
    assert [r.status_code for r in both] == [202, 202]
    assert await count(db, WebhookDelivery) == 6


@pytest.mark.security
async def test_integrity_error_is_retried_once_then_503(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real_intake = webhooks.intake

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("INSERT INTO repos", None, Exception("UNIQUE constraint failed"))
        return await real_intake(*args, **kwargs)

    monkeypatch.setattr(webhooks, "intake", flaky)
    response = await post_fixture(client, "pr_opened")
    assert response.status_code == 202 and (await response.get_json())["status"] == "created"
    assert len(calls) == 2 and await count(db, WebhookDelivery) == 1

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise IntegrityError("INSERT INTO repos", None, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(webhooks, "intake", broken)
    failed = await post_fixture(client, "push_main")
    assert failed.status_code == 503 and await failed.get_json() == {"error": "conflict"}
    assert await count(db, WebhookDelivery) == 1, "nothing half-stored; GitHub can redeliver"


@pytest.mark.security
async def test_replay_with_a_fresh_guid_is_a_duplicate(
    app: Quart, client: Client, db: Database, fake_forge: FakeForge
) -> None:
    """HMAC covers only the body; the body digest stops a captured delivery being replayed."""
    statuses = []
    for index in range(3):
        guid = f"bbbbbbbb-0000-0000-0000-00000000000{index}"
        response = await post_fixture(client, "push_main", **{"X-GitHub-Delivery": guid})
        statuses.append((response.status_code, (await response.get_json())["status"]))
    assert statuses == [(202, "created"), (200, "duplicate"), (200, "duplicate")]
    assert await count(db, Run) == 1 and await count(db, WebhookDelivery) == 1
    async with db.session() as session:
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None and delivery.body_sha256 is not None and len(delivery.body_sha256) == 64


@pytest.mark.security
async def test_deeply_nested_payload_is_400(client: Client, db: Database, fake_forge: FakeForge) -> None:
    raw = b"[" * 100_000 + b"]" * 100_000
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "cccccccc-0000-0000-0000-000000000000",
        "X-Hub-Signature-256": sign_body(TEST_WEBHOOK_SECRET, raw),
    }
    response = await client.post("/webhooks/github", data=raw, headers=headers)
    assert response.status_code == 400 and await response.get_json() == {"error": "invalid_payload"}


async def test_repository_privatized_is_accepted(client: Client, db: Database, fake_forge: FakeForge) -> None:
    await post_fixture(client, "pr_opened")
    headers, body = load_fixture("pr_opened")
    payload = json.loads(body)
    payload.pop("pull_request")
    payload.update(action="privatized")
    payload["repository"]["private"] = True
    raw = json.dumps(payload).encode()
    headers.update(
        {
            "X-GitHub-Event": "repository",
            "X-GitHub-Delivery": "dddddddd-0000-0000-0000-000000000000",
            "X-Hub-Signature-256": sign_body(TEST_WEBHOOK_SECRET, raw),
        }
    )
    response = await client.post("/webhooks/github", data=raw, headers=headers)
    assert response.status_code == 202
    async with db.session() as session:
        repo = await session.scalar(select(Repo))
        assert repo is not None and repo.private is True


@pytest.mark.security
async def test_other_owner_answered_202_without_rows(
    client: Client, db: Database, fake_forge: FakeForge
) -> None:
    headers, body = load_fixture("pr_opened")
    payload = json.loads(body)
    payload["repository"]["full_name"] = "intruder/ledger"
    raw = json.dumps(payload).encode()
    headers["X-Hub-Signature-256"] = sign_body(TEST_WEBHOOK_SECRET, raw)
    response = await client.post("/webhooks/github", data=raw, headers=headers)
    assert response.status_code == 202 and (await response.get_json())["status"] == "ignored_owner"
    assert await count(db, Run) == 0


async def test_default_forge_is_fake_without_credentials(client: Client, app: Quart) -> None:
    response = await post_fixture(client, "pr_opened")
    assert response.status_code == 202
    assert app.extensions["forge"].name == "fake"
