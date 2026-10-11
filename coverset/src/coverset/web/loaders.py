"""Database and mirror lookups shared by the UI blueprints.

Source text is read only with :func:`coverset.orchestrator.repos.read_blob` from the repo's bare
mirror at a run's head commit, and only for paths listed in that run's facts ``files`` table: a URL
path is never joined to the filesystem.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from werkzeug.exceptions import NotFound

from coverset.db.models import (
    Invariant,
    InvariantResult,
    PullRequest,
    Repo,
    ReviewFinding,
    Run,
    TestCase,
    TestResult,
)
from coverset.facts.store import FactsStore
from coverset.orchestrator.repos import RepoError, mirror_path, read_blob
from coverset.web.api import RUN_ID_PARAM
from coverset.web.context import app_settings
from coverset.web.facts_view import RunFacts, run_facts

__all__ = [
    "default_run",
    "facts_for",
    "findings_for_run",
    "invariant_rows",
    "pr_run",
    "read_source",
    "require_run",
    "run_counts",
]

BASELINE_TRIGGERS = ("baseline", "push")


def store() -> FactsStore:
    """The app's facts store."""
    return FactsStore(app_settings().facts_dir)


async def facts_for(run: Run) -> RunFacts:
    """The (cached) facts of ``run``."""
    return await asyncio.to_thread(run_facts, store(), run.repo_id, run.id)


async def require_run(session: AsyncSession, run_id: str | None, repo_id: int | None = None) -> Run:
    """The run with ``run_id`` (of ``repo_id`` when given) or 404."""
    if not run_id or not RUN_ID_PARAM.fullmatch(run_id):
        raise NotFound()
    run = await session.get(Run, run_id)
    if run is None or (repo_id is not None and run.repo_id != repo_id):
        raise NotFound()
    return run


async def default_run(session: AsyncSession, repo: Repo) -> Run | None:
    """The newest completed default-branch baseline, else the newest completed run."""
    run = await session.scalar(
        select(Run)
        .where(
            Run.repo_id == repo.id,
            Run.status == "completed",
            Run.branch == repo.default_branch,
            Run.trigger.in_(BASELINE_TRIGGERS),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = await session.scalar(
            select(Run)
            .where(Run.repo_id == repo.id, Run.status == "completed")
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    return run


async def pr_run(session: AsyncSession, pr: PullRequest) -> Run | None:
    """The latest run for the PR's head commit, else the PR's latest run."""
    run = await session.scalar(
        select(Run)
        .where(Run.pr_id == pr.id, Run.head_sha == pr.head_sha)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = await session.scalar(
            select(Run).where(Run.pr_id == pr.id).order_by(Run.created_at.desc()).limit(1)
        )
    return run


async def read_source(run: Run, path: str, facts: RunFacts) -> str | None:
    """``path`` at the run's head commit, when the run's facts list it (else None)."""
    if path not in facts.paths:
        return None
    mirror = mirror_path(app_settings(), run.repo_id)
    if not (mirror / "HEAD").is_file():
        return None
    try:
        blob = await asyncio.to_thread(read_blob, mirror, run.head_sha, path)
    except RepoError:
        return None
    if blob is None:
        return None
    return blob.decode("utf-8", "replace")


async def run_counts(session: AsyncSession, run_ids: list[str]) -> dict[str, dict[str, int]]:
    """Outcome counts per run."""
    if not run_ids:
        return {}
    rows = await session.execute(
        select(TestResult.run_id, TestResult.outcome, func.count())
        .where(TestResult.run_id.in_(run_ids))
        .group_by(TestResult.run_id, TestResult.outcome)
    )
    out: dict[str, dict[str, int]] = {}
    for run_id, outcome, count in rows.all():
        out.setdefault(run_id, {})[outcome] = int(count)
    return out


async def findings_for_run(
    session: AsyncSession, run: Run, func_keys: list[str] | None = None
) -> dict[str, ReviewFinding]:
    """The newest review finding per func_key recorded by ``run`` (or reused for its bodies)."""
    query = select(ReviewFinding).where(ReviewFinding.run_id == run.id)
    if func_keys:
        query = select(ReviewFinding).where(
            (ReviewFinding.run_id == run.id) | ReviewFinding.func_key.in_(func_keys)
        )
    found: dict[str, ReviewFinding] = {}
    for finding in await session.scalars(query.order_by(ReviewFinding.created_at.desc())):
        found.setdefault(finding.func_key, finding)
    return found


async def invariant_rows(session: AsyncSession, run: Run) -> list[dict[str, Any]]:
    """Invariant results of ``run`` with their spec, newest spec per key."""
    rows = (
        await session.execute(
            select(InvariantResult, Invariant)
            .join(Invariant, Invariant.id == InvariantResult.invariant_id)
            .where(InvariantResult.run_id == run.id)
            .order_by(Invariant.inv_key)
        )
    ).all()
    out = []
    for result, inv in rows:
        spec = inv.spec_json or {}
        evidence = result.evidence or {}
        out.append(
            {
                "key": inv.inv_key,
                "invariant_id": inv.id,
                "status": result.status,
                "statement": spec.get("statement", ""),
                "severity": spec.get("severity", ""),
                "target": (spec.get("scope") or {}).get("target", ""),
                "verify": [v.get("kind") for v in spec.get("verify", []) if isinstance(v, dict)],
                "lens": evidence.get("lens", ""),
                "removed_in_head": bool(evidence.get("removed_in_head")),
                "entries": evidence.get("entries", []),
                "target_func_key": result.target_func_key,
                "covering": len(result.covering_test_ids or []),
            }
        )
    return out


async def nodeid_map(session: AsyncSession, test_ids: list[int]) -> dict[int, str]:
    """Nodeids of control-DB test ids."""
    if not test_ids:
        return {}
    rows = await session.execute(select(TestCase.id, TestCase.nodeid).where(TestCase.id.in_(test_ids)))
    return {tid: nodeid for tid, nodeid in rows.all()}
