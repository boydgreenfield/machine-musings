"""Turn a verified :class:`~coverset.forge.base.ForgeEvent` into database rows (the trigger matrix).

=========================================  ==================================================
event                                      effect
=========================================  ==================================================
``pull_request`` opened/synchronize/        PR upserted; a ``pull_request`` run (drafts skipped
reopened/ready_for_review                   unless ``COVERSET_RUN_DRAFTS``); untrusted PRs follow
                                            ``COVERSET_UNTRUSTED_PR_POLICY``
``push`` to the default branch              a trusted ``baseline`` run
``check_run``/``check_suite`` rerequested   a new ``rerun`` run copying the commit's last run
``check_run`` requested_action              ``approve-run`` records who asked; the run stays
                                            awaiting until :func:`apply_forge_followups` has
                                            verified their write access (network blocked);
                                            ``rerun-failed``/``rerun-all`` rerun
``installation``/``…_repositories``          repos upserted or their installation id cleared
``repository`` privatized/publicized        the stored repo's ``private`` flag updated
anything else                               delivery stored, ``ignored_event``
=========================================  ==================================================

Events for owners/repos outside ``COVERSET_ALLOWED_OWNERS``/``COVERSET_ALLOWED_REPOS`` are stored
as ``ignored_owner`` without their payload and never create a repo or run. Runs are deduplicated on
``(repo, head_sha, trigger)``; with ``COVERSET_PAUSED`` new runs are stored as ``paused``. Every
call stores one ``webhook_deliveries`` row (with the SHA-256 of the signed body, so a captured
delivery resent under a fresh GUID is a duplicate) and commits once. Forge side effects (the
"awaiting approval" check, the approver's permission check) are returned as :class:`Followup`
values for :func:`apply_forge_followups`, so the webhook can answer before any forge API call.

Approval is never granted by the button press alone: GitHub shows check-run buttons to writers,
but coverset checks ``GET /repos/{o}/{r}/collaborators/{sender}/permission`` itself (``admin``,
``maintain`` or ``write``) and refuses the untrusted PR's own author. In token mode an admin
approves with ``POST /runs/<id>/approve`` (:func:`approve_awaiting_run`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import (
    DeliveryStatus,
    ForgeKind,
    PullRequest,
    Repo,
    Run,
    RunEvent,
    RunStatus,
    RunTrigger,
    WebhookDelivery,
)
from coverset.forge.base import (
    APPROVE_RUN,
    RERUN_ALL,
    RERUN_FAILED,
    WRITE_ROLES,
    CheckOutput,
    Forge,
    ForgeError,
    ForgeEvent,
    PullRef,
    RepoRef,
    repo_ref_from_model,
)
from coverset.forge.markdown import md_text
from coverset.forge.registry import effective_untrusted_pr_policy

log = logging.getLogger(__name__)

IntakeStatus = Literal[
    "created",
    "duplicate",
    "ignored_owner",
    "ignored_event",
    "awaiting_approval",
    "approval_pending",
    "paused",
]
FollowupKind = Literal["approval_check", "approve_request", "approved"]
OnQueued = Callable[[str], Awaitable[object]]
"""Called with a run id that :func:`apply_forge_followups` moved to ``queued`` (hand it to the queue)."""

ACTIVE_STATUSES: tuple[RunStatus, ...] = ("queued", "awaiting_approval", "paused", "running")
"""Statuses of a run that has not finished."""

RETRYABLE_STATUSES: tuple[RunStatus, ...] = ("cancelled", "abandoned", "error", "skipped")
"""Finished statuses that do not block a new run for the same commit and trigger."""

APPROVAL_SUMMARY = (
    "This pull request comes from a fork, or its author is not an owner, member or collaborator of "
    "the repository, so coverset has not run its tests. Someone with write access (not the pull "
    "request's author) can press **Approve run** above to run them in a sandbox with the network "
    "blocked; coverset checks their permission before it starts."
)
TOKEN_APPROVAL_DESCRIPTION = "Untrusted PR: an admin must approve it in coverset (POST /runs/<id>/approve)"


@dataclass(frozen=True, slots=True)
class Followup:
    """A forge call to make after the intake transaction commits."""

    kind: FollowupKind
    run_id: str
    actor: str | None = None
    """``approve_request``: the login that pressed Approve run."""


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """What intake did; ``status == "created"`` means ``run_id`` is queued and should be enqueued."""

    status: IntakeStatus
    run_id: str | None = None
    reason: str | None = None
    repo_id: int | None = None
    followups: tuple[Followup, ...] = ()
    delivery_status: DeliveryStatus = "processed"

    @property
    def should_enqueue(self) -> bool:
        """True when a run became ``queued`` and the run queue should pick it up."""
        return self.status == "created" and self.run_id is not None


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def owner_allowed(settings: Settings, full_name: str) -> bool:
    """Whether ``owner/name`` passes ``COVERSET_ALLOWED_OWNERS`` and ``COVERSET_ALLOWED_REPOS``.

    Empty lists allow everything (App mode refuses to start without allowed owners).
    """
    owner = full_name.partition("/")[0].lower()
    owners = {item.lower() for item in settings.allowed_owners}
    repos = {item.lower() for item in settings.allowed_repos}
    return (not owners or owner in owners) and (not repos or full_name.lower() in repos)


async def delivery_seen(session: AsyncSession, delivery_id: str, body_digest: str | None = None) -> bool:
    """True when a delivery with this GUID, or with this body digest, is already stored."""
    condition = WebhookDelivery.delivery_id == delivery_id
    if body_digest is not None:
        condition = or_(condition, WebhookDelivery.body_sha256 == body_digest)
    found = await session.scalar(select(WebhookDelivery.id).where(condition).limit(1))
    return found is not None


DUPLICATE_DELIVERY = IntakeResult("duplicate", reason="duplicate delivery", delivery_status="ignored")


async def intake(
    session: AsyncSession,
    settings: Settings,
    forge_name: ForgeKind,
    event: ForgeEvent,
    *,
    received_at: datetime,
    body_digest: str | None = None,
) -> IntakeResult:
    """Apply ``event`` (see the module docstring), store its delivery and commit.

    ``body_digest`` is the hex SHA-256 of the signed raw body. An :class:`IntegrityError` (a
    concurrent writer inserted the same repo, PR or delivery first) is rolled back and answered as
    a duplicate only when the delivery row now exists; otherwise it propagates so the caller can
    retry the whole intake, which then reads the committed rows.
    """
    if await delivery_seen(session, event.delivery_id, body_digest):
        return DUPLICATE_DELIVERY
    worker = _Intake(session, settings, forge_name, event, received_at)
    try:
        result = await worker.dispatch()
        session.add(
            WebhookDelivery(
                delivery_id=event.delivery_id,
                body_sha256=body_digest,
                forge=forge_name,
                event=event.event_name[:64] or "(none)",
                action=(event.action or None) and event.action[:64],
                repo_external_id=str(event.repo.external_id) if event.repo else None,
                status=result.delivery_status,
                run_id=result.run_id,
                payload=None if result.status == "ignored_owner" else _payload(event),
                received_at=received_at,
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if await delivery_seen(session, event.delivery_id, body_digest):
            return DUPLICATE_DELIVERY
        raise
    return result


def _payload(event: ForgeEvent) -> dict[str, Any] | None:
    return dict(event.payload) if event.payload is not None else None


class _Intake:
    """One intake call's state; ``dispatch`` picks the handler for the event kind."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        forge_name: ForgeKind,
        event: ForgeEvent,
        received_at: datetime,
    ) -> None:
        self.session = session
        self.settings = settings
        self.forge_name = forge_name
        self.event = event
        self.received_at = received_at

    async def dispatch(self) -> IntakeResult:
        event = self.event
        if event.repo is not None and not owner_allowed(self.settings, event.repo.full_name):
            return IntakeResult("ignored_owner", reason="owner not allowed", delivery_status="ignored_owner")
        handlers: dict[str, Callable[[], Any]] = {
            "pr_opened": self._pull_request,
            "pr_sync": self._pull_request,
            "pr_reopened": self._pull_request,
            "pr_ready": self._pull_request,
            "push": self._push,
            "rerun": self._rerun,
            "action_button": self._action_button,
            "install": self._install,
            "uninstall": self._uninstall,
            "repo_visibility": self._repo_visibility,
        }
        handler = handlers.get(event.kind)
        if handler is None or (event.repo is None and event.kind not in ("install", "uninstall")):
            return _ignored(event.reason or f"{event.event_name} not handled")
        return await handler()

    # -- shared helpers --------------------------------------------------------------------------

    def _initial_status(self) -> RunStatus:
        return _initial_status(self.settings)

    @property
    def _policy(self) -> str:
        return effective_untrusted_pr_policy(self.settings)

    async def _upsert_repo(self, ref: RepoRef, *, authoritative: bool = True) -> Repo:
        """Insert or refresh the repo identified by ``(forge, repository.id)``.

        ``authoritative`` refs come from a ``repository`` object (default branch, privacy and
        owner id included); installation lists carry less, so they never overwrite those.
        """
        repo = await self.session.scalar(
            select(Repo).where(Repo.forge == self.forge_name, Repo.external_id == str(ref.external_id))
        )
        if repo is None:
            repo = Repo(
                forge=self.forge_name,
                external_id=str(ref.external_id),
                full_name=ref.full_name,
                default_branch=ref.default_branch,
                private=ref.private,
                language="python",
                owner_id=ref.owner_id,
            )
            self.session.add(repo)
        repo.full_name = ref.full_name
        if authoritative:
            repo.default_branch = ref.default_branch
            repo.private = ref.private
        if ref.owner_id is not None:
            repo.owner_id = ref.owner_id
        installation_id = ref.installation_id or self.event.installation_id
        if installation_id is not None:
            repo.installation_id = installation_id
        await self.session.flush()
        return repo

    async def _duplicate(
        self, repo_id: int, head_sha: str, trigger: RunTrigger, *, active_only: bool = False
    ) -> Run | None:
        statuses = ACTIVE_STATUSES if active_only else None
        query = select(Run).where(Run.repo_id == repo_id, Run.head_sha == head_sha, Run.trigger == trigger)
        if statuses is not None:
            query = query.where(Run.status.in_(statuses))
        else:
            query = query.where(Run.status.not_in(RETRYABLE_STATUSES))
        return await self.session.scalar(query.order_by(Run.created_at.desc()).limit(1))

    async def _new_run(
        self,
        repo: Repo,
        *,
        head_sha: str,
        base_sha: str | None,
        branch: str | None,
        pr_id: int | None,
        trigger: RunTrigger,
        status: RunStatus,
        trusted: bool,
        network: Literal["blocked"] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Run:
        now = self.settings.now()
        timeline = {"webhook_received": _iso(self.received_at)}
        timeline["awaiting_approval" if status == "awaiting_approval" else "queued"] = _iso(now)
        run = Run(
            repo_id=repo.id,
            head_sha=head_sha,
            base_sha=base_sha,
            branch=branch,
            pr_id=pr_id,
            trigger=trigger,
            status=status,
            trusted=trusted,
            network=network,
            executor=self.settings.executor,
            timeline=timeline,
            created_at=now,
        )
        self.session.add(run)
        await self.session.flush()
        payload: dict[str, Any] = {
            "status": status,
            "trigger": trigger,
            "delivery_id": self.event.delivery_id,
            "trusted": trusted,
            **(extra or {}),
        }
        self.session.add(RunEvent(run_id=run.id, seq=0, kind="queued", payload=payload, at=now))
        return run

    def _created(self, run: Run, repo: Repo, *, followups: Iterable[Followup] = ()) -> IntakeResult:
        status: IntakeStatus = {"queued": "created", "paused": "paused"}.get(run.status, "awaiting_approval")  # type: ignore[assignment]
        return IntakeResult(status, run_id=run.id, repo_id=repo.id, followups=tuple(followups))

    # -- handlers --------------------------------------------------------------------------------

    async def _pull_request(self) -> IntakeResult:
        event = self.event
        assert event.repo is not None and event.pull is not None
        pull = event.pull
        repo = await self._upsert_repo(event.repo)
        pr = await self._upsert_pull(repo, pull)
        if pull.draft and not self.settings.run_drafts:
            return _ignored("draft pull request (COVERSET_RUN_DRAFTS=false)", repo.id)
        policy = self._policy
        if not pull.trusted and policy == "skip":
            return _ignored("untrusted pull request skipped (COVERSET_UNTRUSTED_PR_POLICY=skip)", repo.id)
        duplicate = await self._duplicate(repo.id, pull.head_sha, "pull_request")
        if duplicate is not None:
            return IntakeResult(
                "duplicate", run_id=duplicate.id, repo_id=repo.id, reason="run exists for this head"
            )
        await self._supersede_awaiting(pr.id, pull.head_sha)
        common: dict[str, Any] = {
            "head_sha": pull.head_sha,
            "base_sha": pull.base_sha,
            "branch": pull.head_ref,
            "pr_id": pr.id,
            "trigger": "pull_request",
        }
        if pull.trusted:
            run = await self._new_run(repo, status=self._initial_status(), trusted=True, **common)
            return self._created(run, repo)
        if policy == "approve":
            run = await self._new_run(
                repo, status="awaiting_approval", trusted=False, network="blocked", **common
            )
            return self._created(run, repo, followups=[Followup("approval_check", run.id)])
        run = await self._new_run(
            repo, status=self._initial_status(), trusted=False, network="blocked", **common
        )
        return self._created(run, repo)

    async def _upsert_pull(self, repo: Repo, pull: PullRef) -> PullRequest:
        pr = await self.session.scalar(
            select(PullRequest).where(PullRequest.repo_id == repo.id, PullRequest.number == pull.number)
        )
        if pr is None:
            pr = PullRequest(repo_id=repo.id, number=pull.number)
            self.session.add(pr)
        pr.head_sha = pull.head_sha
        pr.base_sha = pull.base_sha
        pr.head_ref = pull.head_ref
        pr.base_ref = pull.base_ref
        pr.title = pull.title
        pr.author = (pull.author or None) and pull.author[:255]
        pr.author_association = pull.author_association[:32]
        pr.from_fork = pull.from_fork
        pr.draft = pull.draft
        pr.state = "open"
        await self.session.flush()
        return pr

    async def _supersede_awaiting(self, pr_id: int, head_sha: str) -> None:
        """A new head makes older runs of the PR that still wait for approval moot."""
        await self.session.execute(
            update(Run)
            .where(Run.pr_id == pr_id, Run.status == "awaiting_approval", Run.head_sha != head_sha)
            .values(status="cancelled", reason=f"superseded by {head_sha[:12]}")
        )

    async def _push(self) -> IntakeResult:
        event = self.event
        assert event.repo is not None and event.head_sha is not None
        repo = await self._upsert_repo(event.repo)
        duplicate = await self._duplicate(repo.id, event.head_sha, "baseline")
        if duplicate is not None:
            return IntakeResult("duplicate", run_id=duplicate.id, repo_id=repo.id, reason="baseline exists")
        run = await self._new_run(
            repo,
            head_sha=event.head_sha,
            base_sha=event.base_sha,
            branch=repo.default_branch,
            pr_id=None,
            trigger="baseline",
            status=self._initial_status(),
            trusted=True,
        )
        return self._created(run, repo)

    async def _latest_run(self, repo_id: int, head_sha: str) -> Run | None:
        return await self.session.scalar(
            select(Run)
            .where(Run.repo_id == repo_id, Run.head_sha == head_sha)
            .order_by(Run.created_at.desc())
            .limit(1)
        )

    async def _rerun(self, scope: str = "all") -> IntakeResult:
        event = self.event
        assert event.repo is not None and event.head_sha is not None
        repo = await self._upsert_repo(event.repo)
        prior = await self._latest_run(repo.id, event.head_sha)
        if prior is None:
            return _ignored("no earlier coverset run for this commit", repo.id)
        if prior.status in ACTIVE_STATUSES:
            return IntakeResult(
                "duplicate", run_id=prior.id, repo_id=repo.id, reason=f"run is {prior.status}"
            )
        needs_approval = not prior.trusted and prior.approved_by is None and self._policy == "approve"
        status: RunStatus = "awaiting_approval" if needs_approval else self._initial_status()
        run = await self._new_run(
            repo,
            head_sha=prior.head_sha,
            base_sha=prior.base_sha,
            branch=prior.branch,
            pr_id=prior.pr_id,
            trigger="rerun",
            status=status,
            trusted=prior.trusted,
            network="blocked" if not prior.trusted else prior.network,
            extra={"rerun_of": prior.id, "rerun_scope": scope, "requested_by": event.sender},
        )
        if not prior.trusted and not needs_approval:
            run.approved_by = prior.approved_by
        followups = [Followup("approval_check", run.id)] if needs_approval else []
        return self._created(run, repo, followups=followups)

    async def _action_button(self) -> IntakeResult:
        identifier = self.event.action_identifier
        if identifier == APPROVE_RUN.identifier:
            return await self._approve()
        if identifier == RERUN_FAILED.identifier:
            return await self._rerun("failed")
        if identifier == RERUN_ALL.identifier:
            return await self._rerun("all")
        return _ignored(f"unknown check action {identifier!r}")

    async def _approve(self) -> IntakeResult:
        """Record the request; :func:`apply_forge_followups` verifies the sender, then queues."""
        event = self.event
        assert event.repo is not None and event.head_sha is not None
        repo = await self._upsert_repo(event.repo)
        run = None
        if event.check_external_id:
            run = await self.session.scalar(
                select(Run).where(Run.id == event.check_external_id, Run.repo_id == repo.id)
            )
        if run is None:
            run = await self.session.scalar(
                select(Run)
                .where(
                    Run.repo_id == repo.id, Run.head_sha == event.head_sha, Run.status == "awaiting_approval"
                )
                .order_by(Run.created_at.desc())
                .limit(1)
            )
        if run is None or run.status != "awaiting_approval":
            return _ignored("no run is awaiting approval for this check", repo.id)
        if not event.sender:
            return _ignored("approve-run without a sender", repo.id)
        sender = event.sender[:255]
        run.approval_requested_by = sender
        run.timeline = {**(run.timeline or {}), "approval_requested": _iso(self.settings.now())}
        await self.session.flush()
        return IntakeResult(
            "approval_pending",
            run_id=run.id,
            repo_id=repo.id,
            reason="approval requested; checking the sender's permission",
            followups=(Followup("approve_request", run.id, actor=sender),),
        )

    async def _install(self) -> IntakeResult:
        allowed = [ref for ref in self.event.repos_added if owner_allowed(self.settings, ref.full_name)]
        if self.event.repos_added and not allowed:
            return IntakeResult("ignored_owner", reason="owner not allowed", delivery_status="ignored_owner")
        for ref in allowed:
            await self._upsert_repo(ref, authoritative=False)
        return _ignored(f"installation: {len(allowed)} repositories recorded", delivery_status="processed")

    async def _repo_visibility(self) -> IntakeResult:
        """``repository.privatized``/``publicized``: update a known repo's privacy (never create one)."""
        ref = self.event.repo
        assert ref is not None
        result = await self.session.execute(
            update(Repo)
            .where(Repo.forge == self.forge_name, Repo.external_id == str(ref.external_id))
            .values(private=ref.private)
        )
        changed = result.rowcount or 0  # type: ignore[attr-defined]
        word = "private" if ref.private else "public"
        return _ignored(f"repository is now {word} ({changed} stored)", delivery_status="processed")

    async def _uninstall(self) -> IntakeResult:
        event = self.event
        cleared = 0
        external_ids = [str(ref.external_id) for ref in event.repos_removed]
        if external_ids:
            result = await self.session.execute(
                update(Repo)
                .where(Repo.forge == self.forge_name, Repo.external_id.in_(external_ids))
                .values(installation_id=None)
            )
            cleared += result.rowcount or 0  # type: ignore[attr-defined]
        if event.event_name == "installation" and event.installation_id is not None:
            result = await self.session.execute(
                update(Repo)
                .where(Repo.forge == self.forge_name, Repo.installation_id == event.installation_id)
                .values(installation_id=None)
            )
            cleared += result.rowcount or 0  # type: ignore[attr-defined]
        return _ignored(
            f"uninstall: installation cleared on {cleared} repositories", delivery_status="processed"
        )


def _ignored(
    reason: str, repo_id: int | None = None, *, delivery_status: DeliveryStatus = "ignored"
) -> IntakeResult:
    return IntakeResult("ignored_event", reason=reason, repo_id=repo_id, delivery_status=delivery_status)


def _initial_status(settings: Settings) -> RunStatus:
    return "paused" if settings.paused else "queued"


async def approve_awaiting_run(
    session: AsyncSession, settings: Settings, run_id: str, approver: str, *, via: str
) -> RunStatus | None:
    """Move an ``awaiting_approval`` run to ``queued`` (``paused`` while paused) with the network
    blocked, record ``approved_by`` and an ``approved`` event, and commit.

    The status flip is a conditional UPDATE, so of two concurrent approvals exactly one wins.
    Returns the new status, or None when the run is missing or no longer awaiting approval.
    The caller has authorized ``approver`` (permission check, or an admin session for ``via="admin"``).
    """
    run = await session.get(Run, run_id)
    if run is None or run.status != "awaiting_approval":
        return None
    now = _iso(settings.now())
    status = _initial_status(settings)
    timeline = {**(run.timeline or {}), "approved": now, "queued": now}
    flipped = await session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == "awaiting_approval")
        .values(
            status=status,
            network="blocked",
            trusted=False,
            approved_by=approver[:255],
            timeline=timeline,
        )
        .execution_options(synchronize_session=False)
    )
    if (flipped.rowcount or 0) != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None
    await _add_event(session, run_id, "approved", {"by": approver[:255], "via": via, "network": "blocked"})
    await session.commit()
    return status


async def _add_event(session: AsyncSession, run_id: str, kind: str, payload: dict[str, Any]) -> None:
    next_seq = await session.scalar(
        select(func.coalesce(func.max(RunEvent.seq), -1) + 1).where(RunEvent.run_id == run_id)
    )
    session.add(RunEvent(run_id=run_id, seq=int(next_seq or 0), kind=kind, payload=payload))
    await session.flush()


async def _approval_refusal(
    session: AsyncSession, forge: Forge, run: Run, repo: Repo, actor: str | None
) -> str | None:
    """Why ``actor`` may not approve ``run`` (None = allowed); raises :class:`ForgeError` when the
    forge cannot answer (the run then keeps waiting)."""
    if not actor:
        return "no sender recorded"
    pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
    if pr is not None and pr.author and pr.author.lower() == actor.lower() and not run.trusted:
        return f"{actor} wrote this untrusted pull request and cannot approve its run"
    role = await forge.collaborator_permission(repo_ref_from_model(repo), actor)
    if role not in WRITE_ROLES:
        return f"{actor} has {role or 'no'} access to the repository, not write"
    return None


async def _announce_approved(forge: Forge, settings: Settings, run: Run, repo: Repo) -> None:
    """Complete the approval check without its button (App mode) or resolve the pending status."""
    from coverset.forge.verdict import APPROVAL_CHECK_NAME

    capabilities = forge.capabilities()
    ref = repo_ref_from_model(repo)
    approver = run.approved_by or "a maintainer"
    if run.approval_check_id is not None and capabilities.check_runs:
        await forge.update_check(
            ref,
            run.approval_check_id,
            conclusion="neutral",
            output=CheckOutput(
                title="coverset: approved",
                summary=f"Approved by {md_text(approver, limit=80)}. Tests run in a sandbox with the network blocked.",
            ),
            actions=(),
        )
    elif not capabilities.check_runs and capabilities.commit_statuses:
        await forge.set_commit_status(
            ref,
            run.head_sha,
            "success",
            context=APPROVAL_CHECK_NAME,
            description=f"Approved by {approver[:60]}; tests run with the network blocked",
            target_url=f"{settings.public_url}/runs/{run.id}",
        )


async def apply_forge_followups(
    session_factory: Callable[[], AsyncSession],
    forge: Forge,
    settings: Settings,
    result: IntakeResult,
    *,
    on_queued: OnQueued | None = None,
) -> None:
    """Make the forge calls an intake asked for, storing returned check ids on the run.

    ``approval_check`` posts the neutral "coverset: awaiting approval" check with an ``approve-run``
    button (token mode, which has no check runs, posts a pending commit status instead).
    ``approve_request`` verifies the sender (write access, not the untrusted PR's author), then
    queues the run, completes that check without the button and calls ``on_queued``; a refusal is
    recorded as an ``approval_rejected`` event and a forge failure leaves the run waiting.
    ``approved`` (an admin approval) only completes the check or status. Failures are logged,
    never raised.
    """
    from coverset.forge.verdict import APPROVAL_CHECK_NAME

    capabilities = forge.capabilities()
    for followup in result.followups:
        queued = False
        async with session_factory() as session:
            run = await session.get(Run, followup.run_id)
            repo = await session.get(Repo, run.repo_id) if run is not None else None
            if run is None or repo is None:
                continue
            ref = repo_ref_from_model(repo)
            details_url = f"{settings.public_url}/runs/{run.id}"
            try:
                if followup.kind == "approval_check" and capabilities.check_runs:
                    run.approval_check_id = await forge.create_check(
                        ref,
                        run.head_sha,
                        APPROVAL_CHECK_NAME,
                        external_id=run.id,
                        details_url=details_url,
                        conclusion="neutral",
                        output=CheckOutput(title=APPROVAL_CHECK_NAME, summary=APPROVAL_SUMMARY),
                        actions=(APPROVE_RUN,),
                    )
                    await session.commit()
                elif followup.kind == "approval_check" and capabilities.commit_statuses:
                    await forge.set_commit_status(
                        ref,
                        run.head_sha,
                        "pending",
                        context=APPROVAL_CHECK_NAME,
                        description=TOKEN_APPROVAL_DESCRIPTION,
                        target_url=details_url,
                    )
                elif followup.kind == "approve_request":
                    if run.status != "awaiting_approval":
                        continue
                    actor = followup.actor or run.approval_requested_by
                    refusal = await _approval_refusal(session, forge, run, repo, actor)
                    if refusal is not None:
                        log.warning("approval of run %s refused: %s", run.id, refusal)
                        await _add_event(
                            session, run.id, "approval_rejected", {"by": actor, "reason": refusal}
                        )
                        await session.commit()
                        continue
                    assert actor is not None
                    status = await approve_awaiting_run(session, settings, run.id, actor, via="check_run")
                    if status is None:
                        continue
                    queued = status == "queued"
                    await session.refresh(run)
                    await _announce_approved(forge, settings, run, repo)
                elif followup.kind == "approved":
                    await _announce_approved(forge, settings, run, repo)
            except ForgeError as exc:
                log.warning("forge follow-up %s for run %s failed: %s", followup.kind, run.id, exc)
            except IntegrityError as exc:  # a concurrent follow-up wrote the same run event seq
                await session.rollback()
                log.warning("follow-up %s for run %s conflicted: %s", followup.kind, followup.run_id, exc)
        if queued and on_queued is not None:
            await on_queued(followup.run_id)
