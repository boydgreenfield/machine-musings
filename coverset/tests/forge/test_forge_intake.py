"""The intake trigger matrix: trusted and untrusted PRs, approval, baselines, drafts, dedupe, owners."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from forge_helpers import DEMO_REPO_ID, load_fixture
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coverset.config import Settings
from coverset.db.models import ForgeCall, PullRequest, Repo, Run, RunEvent, WebhookDelivery
from coverset.db.session import Database
from coverset.forge.base import ForgeCapabilities, ForgeEvent
from coverset.forge.fake import FakeForge
from coverset.forge.github_events import parse_github_event
from coverset.forge.registry import effective_untrusted_pr_policy
from coverset.forge.verdict import APPROVAL_CHECK_NAME
from coverset.orchestrator import intake as intake_module
from coverset.orchestrator.intake import (
    Followup,
    IntakeResult,
    apply_forge_followups,
    approve_awaiting_run,
    intake,
)

counter = iter(range(1, 1_000_000))


def event_from(name: str, mutate: Callable[[dict[str, Any]], None] | None = None) -> ForgeEvent:
    """Parse a fixture (optionally edited) with a fresh delivery id."""
    headers, body = load_fixture(name)
    if mutate is not None:
        payload = json.loads(body)
        mutate(payload)
        body = json.dumps(payload).encode()
    headers["X-GitHub-Delivery"] = f"delivery-{next(counter)}"
    return parse_github_event(headers, body)


async def run_intake(db: Database, settings: Settings, event: ForgeEvent) -> IntakeResult:
    async with db.session() as session:
        return await intake(session, settings, "github", event, received_at=settings.now())


async def count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def get_run(db: Database, run_id: str | None) -> Run:
    assert run_id is not None
    async with db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run


async def test_trusted_pr_creates_a_queued_run(db: Database, settings: Settings) -> None:
    result = await run_intake(db, settings, event_from("pr_opened"))
    assert result.status == "created" and result.should_enqueue and not result.followups
    run = await get_run(db, result.run_id)
    assert (run.status, run.trigger, run.trusted, run.network) == ("queued", "pull_request", True, None)
    assert run.branch == "pr-1-partial-refunds" and run.base_sha and run.pr_id is not None
    assert set(run.timeline) == {"webhook_received", "queued"}
    async with db.session() as session:
        repo = await session.scalar(select(Repo))
        assert repo is not None
        assert (repo.forge, repo.external_id, repo.owner_id, repo.installation_id) == (
            "github",
            str(DEMO_REPO_ID),
            777,
            55,
        )
        assert (repo.private, repo.default_branch, repo.language) == (False, "main", "python")
        pr = await session.scalar(select(PullRequest))
        assert pr is not None and (pr.number, pr.title, pr.author_association) == (
            1,
            "Support partial refunds",
            "MEMBER",
        )
        events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))
        assert [(e.seq, e.kind) for e in events] == [(0, "queued")]
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None and delivery.status == "processed" and delivery.run_id == run.id
        assert delivery.payload is not None and delivery.payload["number"] == 1


async def test_same_head_is_deduplicated(db: Database, settings: Settings) -> None:
    first = await run_intake(db, settings, event_from("pr_opened"))
    again = await run_intake(db, settings, event_from("pr_opened", lambda p: p.update(action="reopened")))
    assert again.status == "duplicate" and again.run_id == first.run_id
    assert await count(db, Run) == 1


async def test_cancelled_run_does_not_block_a_new_one(db: Database, settings: Settings) -> None:
    first = await run_intake(db, settings, event_from("pr_opened"))
    async with db.session() as session:
        run = await session.get(Run, first.run_id)
        assert run is not None
        run.status = "cancelled"
        await session.commit()
    again = await run_intake(db, settings, event_from("pr_opened", lambda p: p.update(action="reopened")))
    assert again.status == "created" and again.run_id != first.run_id


@pytest.mark.security
async def test_duplicate_delivery_is_a_no_op(db: Database, settings: Settings) -> None:
    event = event_from("pr_opened")
    assert (await run_intake(db, settings, event)).status == "created"
    second = await run_intake(db, settings, replace(event))
    assert second.status == "duplicate" and second.reason == "duplicate delivery"
    assert await count(db, Run) == 1 and await count(db, WebhookDelivery) == 1


@pytest.mark.parametrize("run_drafts", [False, True])
async def test_drafts(db: Database, settings: Settings, run_drafts: bool) -> None:
    settings = settings.model_copy(update={"run_drafts": run_drafts})
    draft = event_from("pr_opened", lambda p: p["pull_request"].update(draft=True))
    result = await run_intake(db, settings, draft)
    assert result.status == ("created" if run_drafts else "ignored_event")
    assert await count(db, Run) == (1 if run_drafts else 0)
    assert await count(db, PullRequest) == 1
    if not run_drafts:
        ready = await run_intake(
            db,
            settings,
            event_from("pr_opened", lambda p: p.update(action="ready_for_review")),
        )
        assert ready.status == "created"


@pytest.mark.security
async def test_fork_pr_awaits_approval_with_a_neutral_check(
    db: Database, settings: Settings, fake_forge: FakeForge
) -> None:
    result = await run_intake(db, settings, event_from("pr_opened_fork"))
    assert result.status == "awaiting_approval" and not result.should_enqueue
    run = await get_run(db, result.run_id)
    assert (run.status, run.trusted, run.network) == ("awaiting_approval", False, "blocked")
    assert "awaiting_approval" in run.timeline and "queued" not in run.timeline

    await apply_forge_followups(db.sessionmaker, fake_forge, settings, result)
    (call,) = fake_forge.calls_named("create_check")
    request = call.request
    assert request["name"] == APPROVAL_CHECK_NAME
    assert (request["status"], request["conclusion"]) == ("completed", "neutral")
    assert request["external_id"] == run.id
    assert request["actions"] == [
        {
            "label": "Approve run",
            "description": "Run this PR's tests (network off)",
            "identifier": "approve-run",
        }
    ]
    assert request["details_url"] == f"{settings.public_url}/runs/{run.id}"
    run = await get_run(db, run.id)
    assert run.approval_check_id == next(iter(fake_forge.checks))
    async with db.session() as session:
        stored = await session.scalar(select(ForgeCall).where(ForgeCall.method == "create_check"))
        assert stored is not None and stored.run_id == run.id and stored.repo_id == run.repo_id


async def approve(
    db: Database, settings: Settings, forge: FakeForge, event: ForgeEvent, queued: list[str] | None = None
) -> IntakeResult:
    """Intake an approve-run press, then run its follow-ups (the permission check)."""
    result = await run_intake(db, settings, event)

    async def on_queued(run_id: str) -> None:
        if queued is not None:
            queued.append(run_id)

    await apply_forge_followups(db.sessionmaker, forge, settings, result, on_queued=on_queued)
    return result


async def event_kinds(db: Database, run_id: str) -> list[str]:
    async with db.session() as session:
        return list(
            await session.scalars(
                select(RunEvent.kind).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            )
        )


@pytest.mark.security
async def test_approve_run_queues_with_network_blocked(
    db: Database, settings: Settings, fake_forge: FakeForge
) -> None:
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    await apply_forge_followups(db.sessionmaker, fake_forge, settings, awaiting)
    queued: list[str] = []
    approved = await approve(db, settings, fake_forge, event_from("check_run_requested_action"), queued)
    assert approved.status == "approval_pending" and approved.run_id == awaiting.run_id
    assert not approved.should_enqueue, "the button press alone never queues"
    assert queued == [awaiting.run_id]
    run = await get_run(db, approved.run_id)
    assert (run.status, run.trusted, run.network, run.approved_by, run.approval_requested_by) == (
        "queued",
        False,
        "blocked",
        "dana-maintainer",
        "dana-maintainer",
    )
    assert {"approval_requested", "approved", "queued"} <= set(run.timeline)
    assert await event_kinds(db, run.id) == ["queued", "approved"]
    (permission,) = fake_forge.calls_named("collaborator_permission")
    assert permission.request == {"login": "dana-maintainer"}
    (update,) = fake_forge.calls_named("update_check")
    assert update.request["check_id"] == run.approval_check_id and update.request["actions"] == []
    check = fake_forge.checks[run.approval_check_id or 0]
    assert check.actions == [] and check.output is not None and check.output["title"] == "coverset: approved"

    again = await run_intake(db, settings, event_from("check_run_requested_action"))
    assert again.status == "ignored_event"


@pytest.mark.security
@pytest.mark.parametrize("role", ["read", "triage", "none"])
async def test_approve_run_by_a_non_writer_is_refused(
    db: Database, settings: Settings, fake_forge: FakeForge, role: str
) -> None:
    fake_forge.permissions["dana-maintainer"] = role
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    queued: list[str] = []
    await approve(db, settings, fake_forge, event_from("check_run_requested_action"), queued)
    run = await get_run(db, awaiting.run_id)
    assert (run.status, run.approved_by, run.approval_requested_by) == (
        "awaiting_approval",
        None,
        "dana-maintainer",
    )
    assert queued == [] and fake_forge.calls_named("update_check") == []
    assert await event_kinds(db, run.id) == ["queued", "approval_rejected"]


@pytest.mark.security
async def test_untrusted_author_cannot_approve_their_own_pr(
    db: Database, settings: Settings, fake_forge: FakeForge
) -> None:
    """Even with write access (e.g. a collaborator pushing from a fork), the PR author is refused."""
    fake_forge.permissions["outside-contrib"] = "write"
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    press = event_from("check_run_requested_action", lambda p: p["sender"].update(login="Outside-Contrib"))
    queued: list[str] = []
    await approve(db, settings, fake_forge, press, queued)
    run = await get_run(db, awaiting.run_id)
    assert run.status == "awaiting_approval" and queued == []
    assert fake_forge.calls_named("collaborator_permission") == [], "refused before asking GitHub"
    async with db.session() as session:
        pr = await session.get(PullRequest, run.pr_id)
        assert pr is not None and pr.author == "outside-contrib"
        payload = await session.scalar(
            select(RunEvent.payload).where(RunEvent.run_id == run.id, RunEvent.kind == "approval_rejected")
        )
    assert payload is not None and "cannot approve" in payload["reason"]


@pytest.mark.security
async def test_permission_lookup_failure_keeps_the_run_waiting(
    db: Database, settings: Settings, fake_forge: FakeForge
) -> None:
    fake_forge.fail_on.add("collaborator_permission")
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    queued: list[str] = []
    await approve(db, settings, fake_forge, event_from("check_run_requested_action"), queued)
    assert (await get_run(db, awaiting.run_id)).status == "awaiting_approval" and queued == []
    fake_forge.fail_on.clear()
    await approve(db, settings, fake_forge, event_from("check_run_requested_action"), queued)
    assert (await get_run(db, awaiting.run_id)).status == "queued" and queued == [awaiting.run_id]


@pytest.mark.security
async def test_approve_run_by_external_id(db: Database, settings: Settings, fake_forge: FakeForge) -> None:
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    button = event_from(
        "check_run_requested_action",
        lambda p: p["check_run"].update(external_id=awaiting.run_id, head_sha="d" * 40),
    )
    assert (await approve(db, settings, fake_forge, button)).run_id == awaiting.run_id
    assert (await get_run(db, awaiting.run_id)).status == "queued"


@pytest.mark.security
async def test_concurrent_approvals_queue_once(
    db: Database, settings: Settings, fake_forge: FakeForge
) -> None:
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    first = await run_intake(db, settings, event_from("check_run_requested_action"))
    second = await run_intake(db, settings, event_from("check_run_requested_action"))
    queued: list[str] = []

    async def on_queued(run_id: str) -> None:
        queued.append(run_id)

    await asyncio.gather(
        apply_forge_followups(db.sessionmaker, fake_forge, settings, first, on_queued=on_queued),
        apply_forge_followups(db.sessionmaker, fake_forge, settings, second, on_queued=on_queued),
    )
    assert queued == [awaiting.run_id]
    assert (await event_kinds(db, awaiting.run_id or "")).count("approved") == 1


async def test_admin_approval_helper(db: Database, settings: Settings) -> None:
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    assert awaiting.run_id is not None
    async with db.session() as session:
        assert (
            await approve_awaiting_run(session, settings, awaiting.run_id, "coverset admin", via="admin")
            == "queued"
        )
    async with db.session() as session:
        assert await approve_awaiting_run(session, settings, awaiting.run_id, "again", via="admin") is None
    run = await get_run(db, awaiting.run_id)
    assert (run.status, run.network, run.trusted, run.approved_by) == (
        "queued",
        "blocked",
        False,
        "coverset admin",
    )


@pytest.mark.security
async def test_token_mode_defaults_untrusted_prs_to_run_blocked_network(
    db: Database, settings: Settings
) -> None:
    token = settings.model_copy(update={"github_token": SecretStr("ghp_" + "x" * 36), "forge": "github"})
    assert "untrusted_pr_policy" not in token.model_fields_set
    assert effective_untrusted_pr_policy(token) == "run_blocked_network"
    result = await run_intake(db, token, event_from("pr_opened_fork"))
    assert result.status == "created" and not result.followups
    run = await get_run(db, result.run_id)
    assert (run.status, run.trusted, run.network) == ("queued", False, "blocked")

    explicit = Settings.for_tests(
        data_dir=settings.data_dir,
        github_token="ghp_" + "y" * 36,
        forge="github",
        untrusted_pr_policy="approve",
    )
    assert effective_untrusted_pr_policy(explicit) == "approve", "explicit approve opts into the admin route"
    assert effective_untrusted_pr_policy(settings) == "approve", "the fake forge keeps the default"


@pytest.mark.security
async def test_token_mode_approval_status_and_admin_approval(db: Database, settings: Settings) -> None:
    class TokenModeForge(FakeForge):
        def capabilities(self) -> ForgeCapabilities:
            return replace(super().capabilities(), check_runs=False, check_actions=False)

    forge = TokenModeForge()
    awaiting = await run_intake(db, settings, event_from("pr_opened_fork"))
    await apply_forge_followups(db.sessionmaker, forge, settings, awaiting)
    (pending,) = forge.statuses
    assert pending["state"] == "pending" and "/runs/<id>/approve" in pending["description"]
    assert len(pending["description"]) <= 140
    assert awaiting.run_id is not None
    async with db.session() as session:
        await approve_awaiting_run(session, settings, awaiting.run_id, "coverset admin", via="admin")
    done = IntakeResult("created", run_id=awaiting.run_id, followups=(Followup("approved", awaiting.run_id),))
    await apply_forge_followups(db.sessionmaker, forge, settings, done)
    assert [(s["context"], s["state"]) for s in forge.statuses] == [
        (APPROVAL_CHECK_NAME, "pending"),
        (APPROVAL_CHECK_NAME, "success"),
    ]


async def test_repository_visibility_events_update_privacy(db: Database, settings: Settings) -> None:
    """A repo made private stops answering anonymously at once, not at its next push."""
    await run_intake(db, settings, event_from("pr_opened"))
    headers, body = load_fixture("pr_opened")
    payload = json.loads(body)
    for key in ("pull_request", "number"):
        payload.pop(key)
    headers["X-GitHub-Event"] = "repository"

    def visibility_event(action: str, private: bool, delivery: str) -> ForgeEvent:
        payload["action"] = action
        payload["repository"]["private"] = private
        headers["X-GitHub-Delivery"] = delivery
        return parse_github_event(headers, json.dumps(payload).encode())

    async def stored_private() -> bool:
        async with db.session() as session:
            repo = await session.scalar(select(Repo))
            assert repo is not None
            return repo.private

    assert await stored_private() is False
    privatized = visibility_event("privatized", True, "visibility-1")
    assert privatized.kind == "repo_visibility" and privatized.repo is not None and privatized.repo.private
    result = await run_intake(db, settings, privatized)
    assert result.delivery_status == "processed" and "private" in (result.reason or "")
    assert await stored_private() is True

    await run_intake(db, settings, visibility_event("publicized", False, "visibility-2"))
    assert await stored_private() is False
    assert visibility_event("renamed", False, "visibility-3").kind == "ignore"
    assert await count(db, Repo) == 1


async def test_intake_integrity_errors_are_duplicates_only_when_the_delivery_exists(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = event_from("pr_opened")

    async def collide(self: Any) -> IntakeResult:
        raise IntegrityError("INSERT INTO repos", None, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(intake_module._Intake, "dispatch", collide)
    with pytest.raises(IntegrityError):
        await run_intake(db, settings, event)

    async def raced(self: Any) -> IntakeResult:
        async with db.session() as other:
            other.add(
                WebhookDelivery(
                    delivery_id=event.delivery_id,
                    forge="github",
                    event="pull_request",
                    status="processed",
                    received_at=settings.now(),
                )
            )
            await other.commit()
        raise IntegrityError("INSERT INTO repos", None, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(intake_module._Intake, "dispatch", raced)
    result = await run_intake(db, settings, event)
    assert (result.status, result.reason) == ("duplicate", "duplicate delivery")


async def test_body_digest_dedupes_a_replay_under_a_new_guid(db: Database, settings: Settings) -> None:
    first = event_from("check_run_rerequested")
    second = event_from("check_run_rerequested")
    assert first.delivery_id != second.delivery_id
    async with db.session() as session:
        await intake(session, settings, "github", first, received_at=settings.now(), body_digest="ab" * 32)
    async with db.session() as session:
        again = await intake(
            session, settings, "github", second, received_at=settings.now(), body_digest="ab" * 32
        )
    assert again.status == "duplicate" and await count(db, WebhookDelivery) == 1


@pytest.mark.security
async def test_untrusted_policies(db: Database, settings: Settings) -> None:
    skip = settings.model_copy(update={"untrusted_pr_policy": "skip"})
    result = await run_intake(db, skip, event_from("pr_opened_fork"))
    assert result.status == "ignored_event" and "skip" in (result.reason or "")
    assert await count(db, Run) == 0

    blocked = settings.model_copy(update={"untrusted_pr_policy": "run_blocked_network"})
    result = await run_intake(db, blocked, event_from("pr_opened_fork"))
    assert result.status == "created" and not result.followups
    run = await get_run(db, result.run_id)
    assert (run.status, run.trusted, run.network) == ("queued", False, "blocked")


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
async def test_non_member_authors_are_untrusted_even_without_a_fork(
    db: Database, settings: Settings, association: str
) -> None:
    event = event_from("pr_opened", lambda p: p["pull_request"].update(author_association=association))
    assert (await run_intake(db, settings, event)).status == "awaiting_approval"


@pytest.mark.security
async def test_new_head_supersedes_an_awaiting_run(db: Database, settings: Settings) -> None:
    first = await run_intake(db, settings, event_from("pr_opened_fork"))
    second = await run_intake(
        db,
        settings,
        event_from(
            "pr_opened_fork",
            lambda p: (p.update(action="synchronize"), p["pull_request"]["head"].update(sha="e" * 40)),
        ),
    )
    assert second.status == "awaiting_approval"
    old = await get_run(db, first.run_id)
    assert old.status == "cancelled" and "superseded" in (old.reason or "")


async def test_push_to_default_branch_is_a_baseline(db: Database, settings: Settings) -> None:
    event = event_from("push_main")
    result = await run_intake(db, settings, event)
    run = await get_run(db, result.run_id)
    assert (run.trigger, run.status, run.trusted, run.branch, run.pr_id) == (
        "baseline",
        "queued",
        True,
        "main",
        None,
    )
    assert (run.head_sha, run.base_sha) == (event.head_sha, event.base_sha)
    assert (await run_intake(db, settings, event_from("push_main"))).status == "duplicate"


async def test_push_elsewhere_is_ignored(db: Database, settings: Settings) -> None:
    result = await run_intake(
        db, settings, event_from("push_main", lambda p: p.update(ref="refs/heads/topic"))
    )
    assert result.status == "ignored_event" and await count(db, Run) == 0
    async with db.session() as session:
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None and delivery.status == "ignored"


async def test_other_owners_create_nothing(db: Database, settings: Settings) -> None:
    def foreign(payload: dict[str, Any]) -> None:
        payload["repository"]["full_name"] = "someone-else/ledger"

    result = await run_intake(db, settings, event_from("pr_opened", foreign))
    assert result.status == "ignored_owner"
    assert await count(db, Repo) == 0 and await count(db, Run) == 0 and await count(db, PullRequest) == 0
    async with db.session() as session:
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None and delivery.status == "ignored_owner" and delivery.payload is None


@pytest.mark.security
async def test_allowed_repos_narrow_allowed_owners(db: Database, settings: Settings) -> None:
    narrowed = settings.model_copy(update={"allowed_repos": ["coverset-demo/other"]})
    assert (await run_intake(db, narrowed, event_from("pr_opened"))).status == "ignored_owner"
    widened = settings.model_copy(update={"allowed_repos": ["Coverset-Demo/Ledger"]})
    assert (await run_intake(db, widened, event_from("pr_opened"))).status == "created"


async def test_paused_stores_runs_as_paused(db: Database, settings: Settings) -> None:
    paused = settings.model_copy(update={"paused": True})
    result = await run_intake(db, paused, event_from("pr_opened"))
    assert result.status == "paused" and not result.should_enqueue
    assert (await get_run(db, result.run_id)).status == "paused"


async def test_rerun_copies_the_last_run(db: Database, settings: Settings) -> None:
    assert (await run_intake(db, settings, event_from("check_run_rerequested"))).status == "ignored_event"
    first = await run_intake(db, settings, event_from("pr_opened"))
    busy = await run_intake(db, settings, event_from("check_run_rerequested"))
    assert busy.status == "duplicate"
    async with db.session() as session:
        run = await session.get(Run, first.run_id)
        assert run is not None
        run.status, run.verdict = "completed", "failure"
        await session.commit()
    rerun = await run_intake(db, settings, event_from("check_run_rerequested"))
    assert rerun.status == "created" and rerun.run_id != first.run_id
    new = await get_run(db, rerun.run_id)
    old = await get_run(db, first.run_id)
    assert (new.trigger, new.pr_id, new.head_sha, new.base_sha) == (
        "rerun",
        old.pr_id,
        old.head_sha,
        old.base_sha,
    )
    async with db.session() as session:
        payload = await session.scalar(select(RunEvent.payload).where(RunEvent.run_id == new.id))
    assert payload is not None and payload["rerun_of"] == old.id and payload["rerun_scope"] == "all"


async def test_rerun_failed_button(db: Database, settings: Settings) -> None:
    first = await run_intake(db, settings, event_from("pr_opened"))
    async with db.session() as session:
        run = await session.get(Run, first.run_id)
        assert run is not None
        run.status = "completed"
        await session.commit()
    button = event_from(
        "check_run_requested_action",
        lambda p: (
            p["requested_action"].update(identifier="rerun-failed"),
            p["check_run"].update(head_sha=run.head_sha),
        ),
    )
    result = await run_intake(db, settings, button)
    assert result.status == "created"
    async with db.session() as session:
        payload = await session.scalar(select(RunEvent.payload).where(RunEvent.run_id == result.run_id))
    assert payload is not None and payload["rerun_scope"] == "failed"


@pytest.mark.security
async def test_rerun_of_unapproved_untrusted_run_needs_approval(db: Database, settings: Settings) -> None:
    first = await run_intake(db, settings, event_from("pr_opened_fork"))
    async with db.session() as session:
        run = await session.get(Run, first.run_id)
        assert run is not None
        run.status = "cancelled"
        await session.commit()
    rerun = await run_intake(
        db,
        settings,
        event_from("check_run_rerequested", lambda p: p["check_run"].update(head_sha=run.head_sha)),
    )
    assert rerun.status == "awaiting_approval" and rerun.followups


async def test_installation_events(db: Database, settings: Settings) -> None:
    result = await run_intake(db, settings, event_from("installation_created"))
    assert result.status == "ignored_event" and result.delivery_status == "processed"
    async with db.session() as session:
        repo = await session.scalar(select(Repo))
        assert repo is not None and repo.installation_id == 55 and repo.full_name == "coverset-demo/ledger"
    removed = await run_intake(
        db, settings, event_from("installation_created", lambda p: p.update(action="deleted"))
    )
    assert removed.delivery_status == "processed"
    async with db.session() as session:
        repo = await session.scalar(select(Repo))
        assert repo is not None and repo.installation_id is None


async def test_ping_and_unknown_events_are_stored_and_ignored(db: Database, settings: Settings) -> None:
    result = await run_intake(db, settings, event_from("ping"))
    assert result.status == "ignored_event" and result.reason == "ping"
    assert await count(db, WebhookDelivery) == 1 and await count(db, Run) == 0
