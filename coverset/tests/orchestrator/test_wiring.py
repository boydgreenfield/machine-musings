"""Cross-feature wiring of the orchestrator: pytest_args, the test-economy inputs at finalize, the
dispatcher and upload runs, check-run buttons, retention of upload files, the purge, git batch
parsing and the cache warm-up after an ingest."""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from orch_fakes import NODEIDS, Script, TestRepo, pr_change, pyproject
from orch_helpers import load_run, run_baseline, run_pr
from sqlalchemy import select

from coverset import settings_scopes
from coverset.config import Settings
from coverset.db.models import PullRequest, Repo, Run, ScopedSetting
from coverset.facts.economy import EconomyInputs
from coverset.facts.sources import source_store
from coverset.forge.base import RERUN_ALL, RERUN_FAILED
from coverset.forge.fake import FakeForge
from coverset.forge.verdict import VerdictSummary, verdict_actions
from coverset.orchestrator import history
from coverset.orchestrator import runner as runner_module
from coverset.orchestrator.queue import RunDispatcher
from coverset.orchestrator.repos import mirror_path, read_blobs
from coverset.orchestrator.runner import OrchestratorContext, economy_inputs, schedule_warm_up
from coverset.orchestrator.verdict import post_summary

ARGS = ["-p", "no:randomly"]


# -- [tool.coverset] pytest_args ---------------------------------------------------------------


async def test_pytest_args_reach_the_collect_and_every_shard(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    """The planner appends ``pytest_args`` after the test paths; the runner's shards get them last
    from the adapter (only ``--rootdir`` follows), exactly the commands ``coverset doctor`` shows."""
    script.zero_passes = True
    head = repo.commit({"pyproject.toml": pyproject() + 'pytest_args = ["-p", "no:randomly"]\n'}, "args")
    run_id = await run_baseline(ctx, repo_id, head)
    assert (await load_run(ctx, run_id)).status == "completed"
    collects = [c for c in script.commands if "--collect-only" in c]
    shards = [c for c in script.commands if any(w.startswith("@") for w in c)]
    assert collects and shards
    for cmd in collects:
        assert cmd[-3:] == ["tests", *ARGS]
    for cmd in shards:
        assert cmd[-3:-1] == ARGS and cmd[-1].startswith("--rootdir=")


async def test_without_pytest_args_nothing_is_appended(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    collect = next(c for c in script.commands if "--collect-only" in c)
    assert collect[-1] == "tests" and "no:randomly" not in collect


# -- test-economy inputs at finalize ---------------------------------------------------------------


async def _set_threshold(ctx: OrchestratorContext, repo_id: int, value: float) -> None:
    async with ctx.db.session() as session:
        await settings_scopes.set_value(
            session, "policy.test_tax_threshold", value, scope="repo", scope_id=repo_id
        )


async def test_economy_inputs_carry_the_scoped_threshold_and_p50s(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    default = await economy_inputs(ctx.db, ctx.settings, repo_id, NODEIDS)
    assert isinstance(default, EconomyInputs) and default.policy is not None
    assert default.policy.test_tax_threshold == 8.0
    assert default.durations is not None and set(default.durations) == set(NODEIDS)
    assert all(value == pytest.approx(0.01) for value in default.durations.values())  # the fake's 10 ms
    await _set_threshold(ctx, repo_id, 3.5)
    scoped = await economy_inputs(ctx.db, ctx.settings, repo_id, [*NODEIDS, "tests/new.py::test_new"])
    assert scoped is not None and scoped.policy is not None
    assert scoped.policy.test_tax_threshold == 3.5
    assert scoped.durations is not None and "tests/new.py::test_new" not in scoped.durations


async def test_economy_inputs_never_fail_a_run(
    ctx: OrchestratorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database gone")

    monkeypatch.setattr(history, "p50_seconds", broken)
    assert await economy_inputs(ctx.db, ctx.settings, 1, ["t"]) is None


async def test_the_runner_finalizes_with_the_scoped_policy(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored test-tax flag is computed against the repository's threshold, not the built-in 8."""
    script.zero_passes = True
    seen: list[EconomyInputs | None] = []
    real = runner_module.finalize_run

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("economy"))
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "finalize_run", recording)
    await run_baseline(ctx, repo_id, repo.main_sha)  # no stats yet: durations None
    await _set_threshold(ctx, repo_id, 2.5)
    second = repo.commit({"README.md": "x\n"}, "docs")
    await run_baseline(ctx, repo_id, second)
    assert len(seen) == 2 and all(inputs is not None and inputs.policy is not None for inputs in seen)
    assert seen[0].policy.test_tax_threshold == 8.0 and seen[0].durations is None  # type: ignore[union-attr]
    assert seen[1].policy.test_tax_threshold == 2.5  # type: ignore[union-attr]
    assert seen[1].durations and set(seen[1].durations) == set(NODEIDS)  # type: ignore[union-attr]


# -- the dispatcher and upload runs ----------------------------------------------------------------


async def test_boot_takeover_skips_upload_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    """An upload run is never executed by the dispatcher, and a ``running`` one waits for shards by
    design: the takeover neither abandons nor re-queues it, while a managed orphan is taken over."""
    async with ctx.db.session() as session:
        upload = Run(
            repo_id=repo_id, head_sha=repo.main_sha, trigger="upload", status="running", executor="local"
        )
        managed = Run(
            repo_id=repo_id, head_sha=repo.main_sha, trigger="baseline", status="running", executor="local"
        )
        session.add_all([upload, managed])
        await session.commit()
        upload_id, managed_id = upload.id, managed.id
    ctx.settings.requeue_on_boot = True
    touched = await RunDispatcher(ctx, max_concurrent=1).on_boot()
    assert managed_id in touched and upload_id not in touched
    async with ctx.db.session() as session:
        assert (await session.get(Run, upload_id)).status == "running"  # type: ignore[union-attr]
        assert (await session.get(Run, managed_id)).status == "abandoned"  # type: ignore[union-attr]
        copies = list(await session.scalars(select(Run).where(Run.trigger == "upload")))
        assert [c.id for c in copies] == [upload_id]


async def test_resume_schedules_only_running_upload_runs(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """:meth:`UploadWorker.resume` schedules a pass for each ``running`` upload run, oldest first, and
    for nothing else (finished upload runs, managed runs)."""
    from coverset.orchestrator.upload import UploadWorker

    now = ctx.settings.now()
    async with ctx.db.session() as session:
        rows = [
            Run(
                repo_id=repo_id,
                head_sha=repo.main_sha,
                trigger=trigger,
                status=status,
                executor="local",
                created_at=now - timedelta(minutes=age),
            )
            for trigger, status, age in (
                ("upload", "running", 1),
                ("upload", "running", 5),
                ("upload", "completed", 3),
                ("upload", "error", 3),
                ("baseline", "running", 2),
            )
        ]
        session.add_all(rows)
        await session.commit()
        newer, older = rows[0].id, rows[1].id
    scheduled: list[str] = []
    monkeypatch.setattr(UploadWorker, "schedule", lambda self, run_id: scheduled.append(run_id))
    assert await UploadWorker(ctx).resume() == [older, newer]
    assert scheduled == [older, newer]


async def test_serve_start_resumes_upload_runs_after_the_takeover(
    settings: Settings, ctx: OrchestratorContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher's ``before_serving`` hook starts it (boot takeover first), then resumes the
    upload runs on the app's upload worker; a failing resume never stops serving."""
    from coverset.app import create_app
    from coverset.orchestrator.queue import install_run_queue, resume_upload_runs
    from coverset.orchestrator.upload import UploadWorker
    from coverset.web.uploads import upload_worker

    order: list[str] = []
    real_boot = RunDispatcher.on_boot

    async def on_boot(self: RunDispatcher) -> list[str]:
        order.append("on_boot")
        return await real_boot(self)

    async def resume(self: UploadWorker) -> list[str]:
        order.append("resume")
        assert self is upload_worker(app)
        return []

    monkeypatch.setattr(RunDispatcher, "on_boot", on_boot)
    monkeypatch.setattr(UploadWorker, "resume", resume)
    app = create_app(settings)
    install_run_queue(app, ctx=ctx)
    async with app.test_app():
        assert order == ["on_boot", "resume"]

    async def broken(self: UploadWorker) -> list[str]:
        raise RuntimeError("database gone")

    monkeypatch.setattr(UploadWorker, "resume", broken)
    assert await resume_upload_runs(app) == []


# -- check-run buttons -----------------------------------------------------------------------------


def test_upload_runs_get_no_rerun_buttons() -> None:
    failed = VerdictSummary(conclusion="failure", title="1 failed")
    passed = VerdictSummary(conclusion="success", title="ok")
    assert verdict_actions(failed) == (RERUN_FAILED, RERUN_ALL)
    assert verdict_actions(passed, trigger="pull_request") == (RERUN_ALL,)
    assert verdict_actions(failed, trigger="upload") == ()
    assert verdict_actions(passed, trigger="upload") == ()


@pytest.mark.parametrize(
    ("trigger", "buttons"), [("upload", []), ("pull_request", ["rerun-failed", "rerun-all"])]
)
async def test_post_summary_passes_the_run_trigger(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, trigger: str, buttons: list[str]
) -> None:
    forge = ctx.forge
    assert isinstance(forge, FakeForge)
    async with ctx.db.session() as session:
        pr = PullRequest(repo_id=repo_id, number=7, head_sha=repo.main_sha, base_sha=repo.main_sha, title="t")
        session.add(pr)
        await session.flush()
        run = Run(repo_id=repo_id, head_sha=repo.main_sha, pr_id=pr.id, trigger=trigger, status="completed")
        session.add(run)
        await session.commit()  # the fake forge records its calls in a session of its own
        run_id = run.id
    async with ctx.db.session() as session:
        stored = await session.get(Run, run_id)
        assert stored is not None
        summary = VerdictSummary(conclusion="failure", title="1 failed")
        errors = await post_summary(session, stored, summary, forge)
        await session.commit()
    assert errors == []
    update = forge.calls_named("update_check")[-1]
    assert [a["identifier"] for a in update.request["actions"]] == buttons


# -- retention of upload files ---------------------------------------------------------------------

SHA_OLD = "1" * 40
SHA_SHARED = "2" * 40


class _Sources:
    """A commit's uploaded sources, stored and checked through the configured source store (local
    files, or the object store under ``COVERSET_TEST_BLOB_URL``)."""

    def __init__(self, settings: Settings, repo_id: int, sha: str) -> None:
        self.settings, self.repo_id, self.sha = settings, repo_id, sha

    def exists(self) -> bool:
        return source_store(self.settings).has(self.repo_id, self.sha)

    is_dir = exists


def _upload_files(settings: Settings, repo_id: int, run_id: str, sha: str) -> tuple[_Sources, Path]:
    uploaded = settings.runs_dir / f"sources-{run_id}"
    (uploaded / "pkg").mkdir(parents=True, exist_ok=True)
    (uploaded / "pkg" / "a.py").write_text("x = 1\n")
    source_store(settings).store(repo_id, sha, uploaded, allowed={"pkg/a.py"})
    shutil.rmtree(uploaded)
    upload = settings.runs_dir / run_id / "upload" / "shard-0"
    upload.mkdir(parents=True)
    (upload / "bundle.tar.gz").write_bytes(b"bundle")
    return _Sources(settings, repo_id, sha), upload.parent


async def _upload_run(ctx: OrchestratorContext, repo_id: int, sha: str, *, days: int) -> str:
    async with ctx.db.session() as session:
        run = Run(
            repo_id=repo_id,
            head_sha=sha,
            trigger="upload",
            status="completed",
            executor="local",
            created_at=ctx.settings.now() - timedelta(days=days),
        )
        session.add(run)
        await session.commit()
        return run.id


async def test_gc_removes_expired_upload_archives_and_orphaned_sources(
    ctx: OrchestratorContext, repo_id: int
) -> None:
    old = await _upload_run(ctx, repo_id, SHA_OLD, days=60)
    shared_old = await _upload_run(ctx, repo_id, SHA_SHARED, days=60)
    shared_new = await _upload_run(ctx, repo_id, SHA_SHARED, days=1)
    old_sources, old_upload = _upload_files(ctx.settings, repo_id, old, SHA_OLD)
    shared_sources, shared_upload = _upload_files(ctx.settings, repo_id, shared_old, SHA_SHARED)
    _, new_upload = _upload_files(ctx.settings, repo_id, shared_new, SHA_SHARED)

    dry = await history.collect_garbage(ctx.db, ctx.settings, ctx.store, dry_run=True)
    assert sorted(dry.uploads) == sorted([old, shared_old])
    assert dry.sources == [f"{repo_id}/{SHA_OLD}"]
    assert old_sources.is_dir() and old_upload.is_dir()

    report = await history.collect_garbage(ctx.db, ctx.settings, ctx.store)
    assert sorted(report.uploads) == sorted([old, shared_old]) and report.sources == [f"{repo_id}/{SHA_OLD}"]
    assert not old_sources.exists() and not old_upload.exists() and not old_upload.parent.exists()
    assert shared_sources.is_dir(), "a remaining run still has that head commit"
    assert not shared_upload.exists() and new_upload.is_dir()
    assert any("uploaded sources of 1 commit(s)" in line for line in report.lines(ctx.settings))


async def test_expire_pr_runs_removes_their_upload_files(ctx: OrchestratorContext, repo_id: int) -> None:
    async with ctx.db.session() as session:
        pr = PullRequest(
            repo_id=repo_id, number=9, head_sha=SHA_OLD, base_sha=SHA_OLD, title="t", state="open"
        )
        session.add(pr)
        await session.flush()
        ids = []
        for days, sha in ((90, SHA_OLD), (1, SHA_SHARED)):
            run = Run(
                repo_id=repo_id,
                head_sha=sha,
                pr_id=pr.id,
                trigger="upload",
                status="completed",
                created_at=ctx.settings.now() - timedelta(days=days),
            )
            session.add(run)
            await session.flush()
            ids.append(run.id)
        await session.commit()
        pr_id = pr.id
    old_sources, old_upload = _upload_files(ctx.settings, repo_id, ids[0], SHA_OLD)
    new_sources, new_upload = _upload_files(ctx.settings, repo_id, ids[1], SHA_SHARED)
    assert await history.expire_pr_runs(ctx.db, ctx.settings, ctx.store, pr_id) == [ids[0]]
    assert not old_sources.exists() and not old_upload.exists()
    assert new_sources.is_dir() and new_upload.is_dir()


# -- coverset repo purge ---------------------------------------------------------------------------


async def test_purge_forgets_the_repo_scope_and_removes_its_uploads(
    ctx: OrchestratorContext, repo_id: int
) -> None:
    from coverset.commands.maintenance import _purge

    await _set_threshold(ctx, repo_id, 4.0)
    run_id = await _upload_run(ctx, repo_id, SHA_OLD, days=1)
    sources, upload = _upload_files(ctx.settings, repo_id, run_id, SHA_OLD)
    async with ctx.db.session() as session:
        assert (
            await session.scalar(select(ScopedSetting).where(ScopedSetting.scope_kind == "repo")) is not None
        )
    await _purge(ctx.settings, str(repo_id), yes=True)
    async with ctx.db.session() as session:
        assert await session.get(Repo, repo_id) is None
        rows = list(await session.scalars(select(ScopedSetting).where(ScopedSetting.scope_kind == "repo")))
        assert rows == []
    assert not sources.exists() and not upload.exists()
    assert not (ctx.settings.data_dir / "sources" / str(repo_id)).exists()  # the repository's directory too


# -- git cat-file batches --------------------------------------------------------------------------


def test_read_blobs_skips_a_missing_path_with_a_space(repo: TestRepo) -> None:
    """git echoes the requested name for a missing object (``<sha>:gen/my file.py missing``)."""
    git_dir = repo.path / ".git"
    wanted = ["pyproject.toml", "gen/my file.py", "dir with spaces/x missing.py", "invariants.yaml"]
    blobs = read_blobs(git_dir, repo.main_sha, wanted)
    assert set(blobs) == {"pyproject.toml", "invariants.yaml"}
    assert blobs["pyproject.toml"].startswith(b"[project]")


def test_read_blobs_reads_an_existing_path_with_spaces(repo: TestRepo) -> None:
    sha = repo.commit({"gen/my file.py": "x = 1\n", "gen/ends missing": "y\n"}, "spaces")
    blobs = read_blobs(repo.path / ".git", sha, ["gen/my file.py", "gen/ends missing", "gen/not here.py"])
    assert blobs == {"gen/my file.py": b"x = 1\n", "gen/ends missing": b"y\n"}


# -- the cache warm-up after an ingest -------------------------------------------------------------


async def test_the_warm_up_is_scheduled_after_finalize(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.web import loaders

    script.zero_passes = True
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(loaders, "warm_run", lambda *args: calls.append(args))
    await run_baseline(ctx, repo_id, repo.main_sha)
    await ctx.finish_background()
    assert calls == [], "a CLI context does not warm the web's caches"

    ctx.warm_caches = True
    run_id = await run_baseline(ctx, repo_id, repo.commit({"README.md": "x\n"}, "docs"))
    await ctx.finish_background()
    assert len(calls) == 1
    store, rid, warmed, mirror, sha, base = calls[0]
    assert (store, rid, warmed) == (ctx.store, repo_id, run_id)
    assert mirror == mirror_path(ctx.settings, repo_id) and sha == (await load_run(ctx, run_id)).head_sha
    assert base is None, "a baseline has no diff to analyse"
    pr_run = await run_pr(ctx, repo_id, pr_change(repo), repo.main_sha)
    await ctx.finish_background()
    assert [call[2] for call in calls] == [run_id, pr_run] and calls[1][5] == repo.main_sha


async def test_a_failing_warm_up_never_fails_the_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.web import loaders

    def broken(*args: Any) -> None:
        raise RuntimeError("warm-up exploded")

    script.zero_passes = True
    monkeypatch.setattr(loaders, "warm_run", broken)
    ctx.warm_caches = True
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    await ctx.finish_background()
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict) == ("completed", "success")


async def test_warm_run_fills_the_run_facts_source_and_diff_caches(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.web import loaders, pr_view

    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    head = pr_change(repo)
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    mirror = mirror_path(ctx.settings, repo_id)
    assert (mirror / "HEAD").is_file()
    monkeypatch.setattr(pr_view, "_cache", type(pr_view._cache)())
    loaders.clear_caches()
    try:
        await asyncio.to_thread(loaders.warm_run, ctx.store, repo_id, run_id, mirror, head, repo.main_sha)
        facts = loaders.cached_run_facts(ctx.store, repo_id, run_id)
        assert {"statics", "_bounds"} <= set(facts.__dict__), "the static facts and line index are loaded"
        assert facts.statics and all(
            loaders._cached_blob(mirror, head, path) is not None for path in facts.statics
        )
        key = pr_view.cache_key(mirror, repo.main_sha, head, facts)
        assert key in pr_view._cache, "the PR page's diff analysis is cached under the page's key"
        assert pr_view._cache[key].changes, "the PR's change to add() is classified"
    finally:
        loaders.clear_caches()


def test_schedule_warm_up_is_a_no_op_outside_serve(ctx: OrchestratorContext) -> None:
    assert ctx.warm_caches is False
    assert schedule_warm_up(ctx, 1, "run", "0" * 40) is None
