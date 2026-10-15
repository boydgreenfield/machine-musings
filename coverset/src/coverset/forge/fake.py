"""``FakeForge``: a GitHub stand-in that records every call instead of making it.

It verifies deliveries signed with its secret (the fixtures use
:data:`~coverset.forge.signing.TEST_WEBHOOK_SECRET`), parses GitHub-shaped payloads, hands out
deterministic ids, keeps check runs and comments in memory, and writes each call into the
``forge_calls`` table when given a session factory (``/forge/outbox`` renders those rows).
``fail_on`` makes chosen methods raise :class:`~coverset.forge.base.ForgeError`; ``permissions``
sets the role :meth:`FakeForge.collaborator_permission` reports per login (default ``write``).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import ForgeCall
from coverset.forge.base import (
    CheckAction,
    CheckOutput,
    CheckStatus,
    CommitState,
    Conclusion,
    ForgeCapabilities,
    ForgeError,
    ForgeEvent,
    MirrorCredentials,
    RepoRef,
    check_sha,
)
from coverset.forge.github_events import ALLOWED_EVENTS, parse_github_event
from coverset.forge.signing import TEST_WEBHOOK_SECRET, verify_signature

FIRST_CHECK_ID = 1_000
FIRST_COMMENT_ID = 5_000
FAKE_MIRROR_TOKEN = "fake-mirror-token"

SessionFactory = Callable[[], AsyncSession]


@dataclass(slots=True)
class RecordedCall:
    """One call made against the fake forge."""

    method: str
    target: str | None
    request: dict[str, Any]
    response: dict[str, Any] | None
    repo_id: int | None = None
    run_id: str | None = None


@dataclass(slots=True)
class FakeCheck:
    """A check run as the fake forge holds it."""

    id: int
    repo: str
    head_sha: str
    name: str
    external_id: str | None
    details_url: str | None
    status: str
    conclusion: str | None = None
    output: dict[str, Any] | None = None
    actions: list[dict[str, str]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class FakeComment:
    """A PR comment as the fake forge holds it (``own`` = authored by coverset)."""

    id: int
    repo: str
    pr_number: int
    body: str
    own: bool = True
    hidden: bool = False


class FakeForge:
    """The fake forge (see the module docstring)."""

    name = "fake"
    allowed_events = ALLOWED_EVENTS
    """The fixtures are GitHub-shaped, so the fake forge accepts GitHub's event names."""

    def __init__(
        self,
        *,
        webhook_secret: str | None = TEST_WEBHOOK_SECRET,
        session_factory: SessionFactory | None = None,
        fail_on: Iterable[str] = (),
        permissions: Mapping[str, str] | None = None,
        default_permission: str = "write",
    ) -> None:
        self._webhook_secret = webhook_secret
        self.permissions: dict[str, str] = {k.lower(): v for k, v in (permissions or {}).items()}
        """Per-login answers of :meth:`collaborator_permission` (case-insensitive)."""
        self.default_permission = default_permission
        """The answer for logins not in :attr:`permissions` (``write`` keeps the demo flowing)."""
        self._session_factory = session_factory
        self.fail_on: set[str] = set(fail_on)
        self.calls: list[RecordedCall] = []
        self.checks: dict[int, FakeCheck] = {}
        self.comments: dict[int, FakeComment] = {}
        self.statuses: list[dict[str, Any]] = []
        self._check_ids = itertools.count(FIRST_CHECK_ID)
        self._comment_ids = itertools.count(FIRST_COMMENT_ID)

    # -- bookkeeping -----------------------------------------------------------------------------

    def calls_named(self, method: str) -> list[RecordedCall]:
        """Recorded calls of one method, in order."""
        return [call for call in self.calls if call.method == method]

    def add_foreign_comment(self, repo: RepoRef, pr_number: int, body: str) -> int:
        """Seed a comment by someone else (tests: a forged marker must never be edited)."""
        comment = FakeComment(next(self._comment_ids), repo.full_name, pr_number, body, own=False)
        self.comments[comment.id] = comment
        return comment.id

    async def _record(
        self,
        method: str,
        repo: RepoRef | None,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None,
        *,
        run_id: str | None = None,
    ) -> None:
        if method in self.fail_on:
            raise ForgeError(f"fake forge configured to fail {method}")
        call = RecordedCall(
            method=method,
            target=repo.full_name if repo else None,
            request=dict(request),
            response=dict(response) if response is not None else None,
            repo_id=repo.id if repo else None,
            run_id=run_id,
        )
        self.calls.append(call)
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            session.add(
                ForgeCall(
                    forge="fake",
                    repo_id=call.repo_id,
                    run_id=call.run_id,
                    method=method,
                    target=call.target,
                    request=call.request,
                    response=call.response,
                    status_code=200,
                )
            )
            await session.commit()

    # -- webhooks --------------------------------------------------------------------------------

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        """HMAC check with this fake's secret (the fixtures' test secret by default)."""
        return verify_signature(self._webhook_secret, headers, raw_body)

    def parse_event(self, headers: Mapping[str, str], raw_body: bytes) -> ForgeEvent:
        """Fixtures are GitHub-shaped, so the GitHub parser applies unchanged."""
        return parse_github_event(headers, raw_body)

    # -- forge API -------------------------------------------------------------------------------

    async def fetch_mirror_credentials(self, installation_id: int | None, repo: RepoRef) -> MirrorCredentials:
        """A fixed placeholder token (the demo mirror is local and needs none)."""
        await self._record(
            "fetch_mirror_credentials", repo, {"installation_id": installation_id}, {"token": "[fake]"}
        )
        return MirrorCredentials(FAKE_MIRROR_TOKEN)

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
        """Record and keep a check run; ids count up from :data:`FIRST_CHECK_ID`."""
        check_sha(head_sha, "head_sha")
        check = FakeCheck(
            id=next(self._check_ids),
            repo=repo.full_name,
            head_sha=head_sha,
            name=name,
            external_id=external_id,
            details_url=details_url,
            status="completed" if conclusion else status,
            conclusion=conclusion,
            output=_output_dict(output),
            actions=[action.to_github() for action in actions],
            annotations=[a.to_github() for a in output.annotations] if output else [],
        )
        request = {
            "head_sha": head_sha,
            "name": name,
            "external_id": external_id,
            "details_url": details_url,
            "status": check.status,
            "conclusion": conclusion,
            "output": check.output,
            "actions": check.actions,
        }
        await self._record("create_check", repo, request, {"id": check.id}, run_id=_run_id(external_id))
        self.checks[check.id] = check
        return check.id

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
        """Record and apply an update (annotations accumulate like GitHub's)."""
        check = self.checks.get(check_id)
        request: dict[str, Any] = {
            "check_id": check_id,
            "status": status,
            "conclusion": conclusion,
            "output": _output_dict(output),
            "actions": [a.to_github() for a in actions] if actions is not None else None,
            "details_url": details_url,
        }
        await self._record(
            "update_check",
            repo,
            request,
            {"id": check_id},
            run_id=_run_id(check.external_id if check else None),
        )
        if check is None:
            raise ForgeError(f"no check run {check_id}")
        if conclusion is not None:
            check.status, check.conclusion = "completed", conclusion
        elif status is not None:
            check.status = status
        if output is not None:
            check.output = _output_dict(output)
            check.annotations.extend(a.to_github() for a in output.annotations)
        if actions is not None:
            check.actions = [a.to_github() for a in actions]
        if details_url is not None:
            check.details_url = details_url

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
        """Record a commit status."""
        status = {
            "sha": check_sha(sha, "sha"),
            "state": state,
            "context": context,
            "description": description,
            "target_url": target_url,
        }
        await self._record("set_commit_status", repo, status, None)
        self.statuses.append(status)

    async def upsert_pr_comment(
        self, repo: RepoRef, pr_number: int, marker: str, body: str, prior_id: int | None
    ) -> int:
        """Same lookup order as GitHub: stored id, then own comments carrying ``marker``, else create."""
        if marker not in body:
            body = f"{body}\n{marker}\n"
        target = self.comments.get(prior_id) if prior_id is not None else None
        if target is None or not target.own:
            target = next(
                (
                    c
                    for c in reversed(self.comments.values())
                    if c.own and c.repo == repo.full_name and c.pr_number == pr_number and marker in c.body
                ),
                None,
            )
        created = target is None
        if target is None:
            target = FakeComment(next(self._comment_ids), repo.full_name, pr_number, body)
            self.comments[target.id] = target
        else:
            target.body = body
        await self._record(
            "upsert_pr_comment",
            repo,
            {"pr_number": pr_number, "prior_id": prior_id, "body": body},
            {"id": target.id, "created": created},
        )
        return target.id

    async def hide_comment(self, repo: RepoRef, comment_id: int) -> None:
        """Record and mark a comment minimised."""
        await self._record("hide_comment", repo, {"comment_id": comment_id}, None)
        comment = self.comments.get(comment_id)
        if comment is not None:
            comment.hidden = True

    async def collaborator_permission(self, repo: RepoRef, login: str) -> str:
        """The configured role for ``login`` (:attr:`permissions`, else :attr:`default_permission`)."""
        role = self.permissions.get(login.lower(), self.default_permission)
        await self._record("collaborator_permission", repo, {"login": login}, {"role_name": role})
        return role

    def capabilities(self) -> ForgeCapabilities:
        """Everything the App mode can do."""
        return ForgeCapabilities(
            name="fake",
            check_runs=True,
            commit_statuses=True,
            pr_comments=True,
            hide_comments=True,
            mirror_credentials=True,
            check_actions=True,
        )


def _output_dict(output: CheckOutput | None) -> dict[str, Any] | None:
    if output is None:
        return None
    data = asdict(output)
    data["annotations"] = [a.to_github() for a in output.annotations]
    return data


def _run_id(external_id: str | None) -> str | None:
    return external_id if external_id and external_id.startswith("run_") else None
