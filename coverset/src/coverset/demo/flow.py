"""The demo flow behind ``coverset demo``: seed, register, run the history and both PRs, replay.

Idempotent: the seeded repository has fixed SHAs, the repository row is found again by its forge
identity, and :func:`~coverset.orchestrator.history.seed_history` reuses completed runs, so a second
``coverset demo`` only serves. The demo repository is registered with the identity the webhook
fixtures use (``coverset-demo/ledger``, repository id 424242) with its mirror source pointed at the
local bare repository, so a replayed ``pull_request`` delivery lands on the same repo and its runs
fetch from disk, never from GitHub.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import Repo, Run
from coverset.demo.seed import PR_OPENED_FIXTURE, demo_identity, read_shas, seed_demo_repo
from coverset.orchestrator.history import SeededRun, seed_history
from coverset.orchestrator.runner import OrchestratorContext

__all__ = [
    "DemoResult",
    "format_table",
    "register_demo_repo",
    "replay_fixture",
    "run_demo",
]

REPLAY_WAIT_S = 300.0


@dataclass(frozen=True, slots=True)
class DemoResult:
    """What :func:`run_demo` did."""

    repo_id: int
    shas: dict[str, Any]
    runs: list[SeededRun]


async def register_demo_repo(session: AsyncSession, demo_git: Path) -> Repo:
    """Find or create the demo repository row (forge identity from the fixtures, local source)."""
    ident = demo_identity()
    repo = await session.scalar(
        select(Repo).where(Repo.forge == "github", Repo.external_id == str(ident.external_id))
    )
    if repo is None:
        repo = Repo(forge="github", external_id=str(ident.external_id), full_name=ident.full_name)
        session.add(repo)
    repo.full_name = ident.full_name
    repo.default_branch = "main"
    repo.private = False
    repo.language = "python"
    repo.owner_id = ident.owner_id
    repo.installation_id = ident.installation_id
    repo.ai_review_enabled = True
    repo.settings_json = {**(repo.settings_json or {}), "source": str(demo_git.resolve()), "demo": True}
    await session.flush()
    return repo


async def run_demo(
    ctx: OrchestratorContext,
    *,
    out: Path,
    executor: str = "local",
    on_event: Any = None,
    on_run: Any = None,
) -> DemoResult:
    """Seed the demo repository at ``out`` and run its six-commit history and both PRs."""
    existing = read_shas(out)
    shas = await asyncio.to_thread(seed_demo_repo, out)
    if existing is not None and existing != shas:  # pragma: no cover - SHAs are pinned by a test
        raise RuntimeError("re-seeding the demo produced different SHAs")
    async with ctx.db.session() as session:
        repo = await register_demo_repo(session, out)
        await session.commit()
        repo_id = repo.id
    commits = sorted(shas["commits"].items(), key=lambda kv: int(kv[0].rsplit("-", 1)[1]))
    runs = await seed_history(
        ctx, repo_id, commits, prs=shas.get("prs", []), executor=executor, on_event=on_event, on_run=on_run
    )
    return DemoResult(repo_id=repo_id, shas=shas, runs=runs)


def format_table(runs: list[SeededRun]) -> str:
    """The demo summary table: run id, trigger, verdict, first failure, ranks."""
    header = ("run", "name", "trigger", "verdict", "first failure s", "rank coverset/file order")
    rows = [
        (
            run.run_id,
            run.name,
            run.trigger,
            (run.verdict or "-") + (" (reused)" if run.reused else ""),
            f"{run.first_failure_s:.2f}" if run.first_failure_s is not None else "-",
            f"{run.rank_coverset}/{run.rank_file_order}" if run.rank_coverset is not None else "-",
        )
        for run in runs
    ]
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in [header, *rows]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


async def replay_fixture(app: Any, settings: Settings, fixture: Path = PR_OPENED_FIXTURE) -> dict[str, Any]:
    """Post a recorded webhook fixture through ``app`` in-process, re-signed with the configured secret.

    Returns the response JSON plus ``http_status``; when it queued a run, waits (up to 5 minutes)
    for the app's run queue to finish it.
    """
    from coverset.forge.signing import SIGNATURE_HEADER, sign_body

    data = json.loads(fixture.read_text(encoding="utf-8"))
    headers = {str(k): str(v) for k, v in data["headers"].items()}
    body = str(data["body"]).encode("utf-8")
    secret = settings.github_webhook_secret.get_secret_value() if settings.github_webhook_secret else ""
    if secret:
        headers[SIGNATURE_HEADER] = sign_body(secret, body)
    client = app.test_client()
    response = await client.post("/webhooks/github", data=body, headers=headers)
    payload: dict[str, Any] = dict(await response.get_json() or {})
    payload["http_status"] = response.status_code
    run_id = payload.get("run_id")
    if run_id and payload.get("status") == "created":
        payload["run_status"] = await _wait_for_run(app, str(run_id))
    return payload


async def _wait_for_run(app: Any, run_id: str) -> str:
    database = app.extensions["db"]
    deadline = time.monotonic() + REPLAY_WAIT_S
    status = "queued"
    while time.monotonic() < deadline:
        async with database.session() as session:
            run = await session.get(Run, run_id)
            status = run.status if run is not None else "missing"
        if status not in ("queued", "running", "paused", "awaiting_approval"):
            return status
        await asyncio.sleep(0.5)
    return status
