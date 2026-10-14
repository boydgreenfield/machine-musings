"""What a finished run tells the forge: :class:`VerdictSummary` and :func:`post_verdict`.

The orchestrator (phase 5a) fills a :class:`VerdictSummary` from the run, its facts and its
invariants, then calls :func:`post_verdict`, which completes the check run (App mode) or posts a
commit status (token mode) and upserts the one sticky PR comment. Ids come back in
:class:`PostedVerdict` for the caller to store (``runs.check_run_id``, ``pull_requests.comment_id``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from coverset.forge.base import (
    RERUN_ALL,
    RERUN_FAILED,
    Annotation,
    CheckAction,
    CheckOutput,
    CommitState,
    Forge,
    ForgeError,
    PullRef,
    RepoRef,
)
from coverset.forge.markdown import safe_url

log = logging.getLogger(__name__)

CHECK_NAME = "coverset"
"""The check-run name (bind branch protection to it with the App's ``integration_id``)."""

APPROVAL_CHECK_NAME = "coverset: awaiting approval"
"""The neutral check an untrusted PR gets until someone with write access presses Approve run."""

STICKY_MARKER = "<!-- coverset:sticky -->"

ESTIMATE_LABEL = "estimated from the first test"
"""How every reader labels ``t_first_failure_file_order_est`` (the pages, the comment, the API)."""
"""Hidden marker that identifies coverset's sticky PR comment."""

VerdictConclusion = Literal["success", "failure", "neutral", "cancelled", "timed_out"]
ChangeKind = Literal["added", "deleted", "modified", "trivial", "renamed", "moved"]

MAX_ANNOTATIONS = 200


@dataclass(frozen=True, slots=True)
class InvariantRow:
    """One invariant's status (``satisfied | violated | untested | stale | needs_review``, or for
    ``invariants_changed`` a change such as ``removed`` or ``weakened``)."""

    id: str
    status: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class FunctionRow:
    """One changed function in the comment's table."""

    qualname: str
    change_kind: ChangeKind
    tests: int
    line_pct: float | None
    input_classes_missing: tuple[str, ...] = ()
    invariants: tuple[InvariantRow, ...] = ()
    inherited: bool = False


@dataclass(frozen=True, slots=True)
class MissingClass:
    """An input class no test exercised, with a suggested Hypothesis strategy."""

    function: str
    param: str
    class_name: str
    hypothesis_snippet: str | None = None


@dataclass(frozen=True, slots=True)
class FailureRow:
    """A failing test, located for an annotation when ``path``/``line`` are known."""

    nodeid: str
    message: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class VerdictSummary:
    """Everything the check run and the sticky comment show for one run."""

    conclusion: VerdictConclusion
    title: str
    reason: str = ""
    first_failure_s: float | None = None
    rank_coverset: int | None = None
    rank_file_order: int | None = None
    t_first_failure_file_order_est: float | None = None
    patch_pct: float | None = None
    patch_covered: int = 0
    patch_total: int = 0
    function_rows: tuple[FunctionRow, ...] = ()
    zero_test_functions: int = 0
    missing_input_classes: tuple[MissingClass, ...] = ()
    invariants: tuple[InvariantRow, ...] = ()
    invariants_changed: tuple[InvariantRow, ...] = ()
    failures: tuple[FailureRow, ...] = ()
    details_url: str | None = None
    ai_source: str | None = None
    tests_total: int | None = None

    @property
    def has_gaps(self) -> bool:
        """Untested changed functions, unexercised input classes, partial patch coverage or
        invariants that are not known to hold."""
        open_invariants = any(row.status in ("untested", "stale", "needs_review") for row in self.invariants)
        partial_patch = self.patch_pct is not None and self.patch_pct < 100.0
        return bool(
            self.zero_test_functions or self.missing_input_classes or open_invariants or partial_patch
        )

    @property
    def label(self) -> str:
        """``FAIL``, ``PASS``, ``PASS with gaps``, ``NEUTRAL``, ``CANCELLED`` or ``TIMED OUT``."""
        if self.conclusion == "success":
            return "PASS with gaps" if self.has_gaps else "PASS"
        return {"failure": "FAIL", "neutral": "NEUTRAL", "cancelled": "CANCELLED"}.get(
            self.conclusion, "TIMED OUT"
        )


@dataclass(frozen=True, slots=True)
class PostedVerdict:
    """Ids to store after posting; ``errors`` lists forge failures (posting is best effort)."""

    check_id: int | None
    comment_id: int | None
    status_posted: bool
    errors: tuple[str, ...] = ()


def commit_state(conclusion: VerdictConclusion) -> CommitState:
    """Commit-status state for a conclusion. Statuses have no neutral, and a required ``coverset``
    status must never turn green for a run that errored, ran no tests or hit its budget, so
    ``neutral`` (like ``cancelled`` and ``timed_out``) is ``error``; only ``success`` is green."""
    if conclusion == "success":
        return "success"
    if conclusion == "failure":
        return "failure"
    return "error"


def failure_annotations(summary: VerdictSummary) -> tuple[Annotation, ...]:
    """One ``failure`` annotation per located failing test (capped at :data:`MAX_ANNOTATIONS`)."""
    annotations = []
    for row in summary.failures:
        if not row.path or not row.line or row.line < 1:
            continue
        annotations.append(
            Annotation(
                path=row.path,
                start_line=row.line,
                end_line=row.line,
                level="failure",
                message=row.message or "test failed",
                title=row.nodeid[:255],
            )
        )
    return tuple(annotations[:MAX_ANNOTATIONS])


def verdict_actions(summary: VerdictSummary, *, trigger: str | None = None) -> tuple[CheckAction, ...]:
    """Buttons on a finished check run.

    An upload run (``trigger == "upload"``) gets none: its tests ran in the repository's own CI, so
    "Re-run all" would queue a managed run that may have no executor, and "Re-run failed" has no
    worker to retry on; the way to re-run is the CI job (docs/upload.md).
    """
    if trigger == "upload":
        return ()
    return (RERUN_FAILED, RERUN_ALL) if summary.conclusion == "failure" else (RERUN_ALL,)


async def start_check(
    forge: Forge, repo: RepoRef, head_sha: str, *, external_id: str, details_url: str | None
) -> int | None:
    """Create the ``in_progress`` verdict check when a run starts (None when the forge has no checks)."""
    if not forge.capabilities().check_runs:
        return None
    return await forge.create_check(
        repo,
        head_sha,
        CHECK_NAME,
        external_id=external_id,
        details_url=safe_url(details_url),
        status="in_progress",
    )


async def post_verdict(
    forge: Forge,
    repo: RepoRef,
    pr: PullRef | int | None,
    head_sha: str,
    summary: VerdictSummary,
    *,
    check_id: int | None,
    comment_id: int | None,
    external_id: str | None = None,
    trigger: str | None = None,
) -> PostedVerdict:
    """Complete the check run (or post a commit status) and upsert the sticky comment.

    ``check_id``/``comment_id`` are the stored ids (None creates them). ``trigger`` is the run's
    (``runs.trigger``): an upload run's check carries no re-run buttons (:func:`verdict_actions`).
    A forge failure in one step is logged and reported in ``errors``; the other step still runs.
    """
    from coverset.forge.comment import render_check_summary, render_sticky_comment

    capabilities = forge.capabilities()
    details_url = safe_url(summary.details_url)
    errors: list[str] = []
    status_posted = False
    if capabilities.check_runs:
        output = CheckOutput(
            title=(summary.title or summary.label)[:255],
            summary=render_check_summary(summary),
            annotations=failure_annotations(summary),
        )
        actions = verdict_actions(summary, trigger=trigger) if capabilities.check_actions else ()
        try:
            if check_id is None:
                check_id = await forge.create_check(
                    repo,
                    head_sha,
                    CHECK_NAME,
                    external_id=external_id,
                    details_url=details_url,
                    status="in_progress",
                )
            await forge.update_check(
                repo,
                check_id,
                conclusion=summary.conclusion,
                output=output,
                actions=actions,
                details_url=details_url,
            )
        except ForgeError as exc:
            log.warning("posting the check run for %s failed: %s", repo.full_name, exc)
            errors.append(f"check: {exc}")
    elif capabilities.commit_statuses:
        try:
            await forge.set_commit_status(
                repo,
                head_sha,
                commit_state(summary.conclusion),
                context=CHECK_NAME,
                description=f"{summary.label}: {summary.title}"[:140],
                target_url=details_url,
            )
            status_posted = True
        except ForgeError as exc:
            log.warning("posting the commit status for %s failed: %s", repo.full_name, exc)
            errors.append(f"status: {exc}")
    pr_number = pr.number if isinstance(pr, PullRef) else pr
    if pr_number is not None and capabilities.pr_comments:
        try:
            comment_id = await forge.upsert_pr_comment(
                repo, pr_number, STICKY_MARKER, render_sticky_comment(summary), comment_id
            )
        except ForgeError as exc:
            log.warning("upserting the sticky comment on %s#%s failed: %s", repo.full_name, pr_number, exc)
            errors.append(f"comment: {exc}")
    return PostedVerdict(check_id, comment_id, status_posted, tuple(errors))
