"""Mirrors and git helpers, test history statistics and the batched event sink."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from orch_fakes import TestRepo, git, make_repo, pr_change
from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import Repo, Run, RunEvent, TestCase, TestResult, TestStats
from coverset.forge.base import MirrorCredentials
from coverset.orchestrator import history, repos
from coverset.orchestrator.repos import (
    RepoError,
    archive,
    ensure_mirror,
    extract_archive,
    merge_base,
    read_blob,
    read_blobs,
    register_local_repo,
    resolve_commit,
    tree_paths,
)
from coverset.orchestrator.runner import EventSink, OrchestratorContext

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


async def test_register_local_repo_is_idempotent(ctx: OrchestratorContext, repo: TestRepo) -> None:
    async with ctx.db.session() as session:
        first = await register_local_repo(session, repo.path)
        second = await register_local_repo(session, str(repo.path) + "/")
        await session.commit()
    assert first.id == second.id
    assert first.forge == "local" and first.external_id == repos.local_external_id(repo.path)
    assert first.full_name == "local/src-repo" and first.default_branch == "main"
    with pytest.raises(RepoError):
        async with ctx.db.session() as session:
            await register_local_repo(session, "https://user:pw@example.com/x.git")


async def test_mirror_clone_refresh_and_git_helpers(
    settings: Settings, repo: TestRepo, tmp_path: Path
) -> None:
    row = Repo(
        id=7, forge="local", external_id="x", full_name="local/x", settings_json={"source": str(repo.path)}
    )
    mirror = ensure_mirror(settings, None, row)
    assert mirror == settings.mirrors_dir / "7.git"
    assert resolve_commit(mirror, "main") == repo.main_sha
    head = pr_change(repo)
    with pytest.raises(RepoError):
        resolve_commit(mirror, head)
    ensure_mirror(settings, None, row)  # refresh picks up the new branch
    assert resolve_commit(mirror, "pr-1") == head
    assert merge_base(mirror, repo.main_sha, head) == repo.main_sha
    assert "src/factspkg/calc.py" in tree_paths(mirror, head)
    assert b"return b + a" in (read_blob(mirror, head, "src/factspkg/calc.py") or b"")
    assert read_blob(mirror, head, "nope.py") is None
    blobs = read_blobs(mirror, repo.main_sha, ["pyproject.toml", "missing", "invariants.yaml"])
    assert set(blobs) == {"pyproject.toml", "invariants.yaml"}
    checkout = extract_archive(archive(mirror, head), tmp_path / "checkout")
    assert (checkout / "tests" / "test_calc.py").is_file() and not (checkout / ".git").exists()
    with pytest.raises(RepoError):
        archive(mirror, "main")
    with pytest.raises(RepoError):
        resolve_commit(mirror, "--upload-pack=evil")


def test_forge_mirror_credentials_stay_out_of_urls_and_config(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_git(args: list[str], *, timeout: float = 0, secret_args: int = 0) -> bytes:
        calls.append(args)
        if "clone" in args:
            target = Path(args[-1])
            target.mkdir(parents=True)
            (target / "HEAD").write_text("ref: refs/heads/main\n")
            (target / "config").write_text('[remote "origin"]\n\turl = https://github.com/o/r.git\n')
        return b""

    monkeypatch.setattr(repos, "_git", fake_git)
    row = Repo(id=9, forge="github", external_id="1", full_name="o/r", settings_json={})
    credentials = MirrorCredentials(token="ghs_" + "s" * 40)
    mirror = ensure_mirror(settings, None, row, credentials=credentials)
    ensure_mirror(settings, None, row, credentials=credentials)
    clone, fetch = calls
    assert clone[0] == "-c" and clone[1].startswith("http.extraHeader=Authorization: Basic ")
    assert "https://github.com/o/r.git" in clone and all("ghs_" not in a for a in clone if "://" in a)
    assert fetch[0] == "-c" and "fetch" in fetch
    assert "ghs_" not in (mirror / "config").read_text()


async def test_update_test_stats_windows_and_flaky(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    async with ctx.db.session() as session:
        tests = await history.register_tests(session, repo_id, ["t.py::a", "t.py::b[1]"], run_id=None)
        await session.flush()
        test_a = tests["t.py::a"].id
        runs = []
        for n, (outcome, flaky, ms) in enumerate(
            [
                ("passed", False, 100.0),
                ("failed", False, 300.0),
                ("passed", True, 200.0),
                ("not_run", False, None),
            ]
        ):
            run = Run(
                repo_id=repo_id,
                head_sha=repo.main_sha,
                trigger="baseline",
                status="completed",
                created_at=T0 + timedelta(hours=n),
            )
            session.add(run)
            await session.flush()
            runs.append(run)
            session.add(
                TestResult(run_id=run.id, test_id=test_a, outcome=outcome, flaky=flaky, duration_ms=ms)
            )
        await session.flush()
        written = await history.update_test_stats(session, runs[-1], [test_a])
        stats = await session.get(TestStats, (repo_id, test_a))
    assert written == 1 and stats is not None
    assert (stats.runs_n, stats.fails_n, stats.flaky_n) == (3, 1, 1)
    assert stats.p50_ms == 200.0 and stats.p95_ms == 300.0
    assert stats.last_fail_run_id == runs[2].id  # a flaky pass still records the failed attempt
    assert stats.flaky_score == pytest.approx(1 / 3, abs=1e-6)
    assert 0 < stats.fail_ewma < 1


async def test_load_test_history_family_fallback(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    async with ctx.db.session() as session:
        tests = await history.register_tests(session, repo_id, ["t.py::p[1]", "t.py::p[2]"], run_id=None)
        session.add(TestStats(repo_id=repo_id, test_id=tests["t.py::p[1]"].id, runs_n=3, p50_ms=500.0))
        await session.flush()
        hist = await history.load_test_history(
            session, repo_id, {n: t.id for n, t in tests.items()}, previous_run_id=None
        )
    assert hist["t.py::p[1]"].p50_s == 0.5 and hist["t.py::p[1]"].runs_n == 3
    assert hist["t.py::p[2]"].p50_s == 0.5 and hist["t.py::p[2]"].runs_n == 0
    assert history.family_of("t.py::p[2]") == "t.py::p"
    assert len(history.nodeid_hash("x")) == 16


async def test_register_tests_finds_existing_rows(ctx: OrchestratorContext, repo_id: int) -> None:
    async with ctx.db.session() as session:
        first = await history.register_tests(session, repo_id, ["t.py::a"], run_id=None)
        again = await history.register_tests(session, repo_id, ["t.py::a", "t.py::b"], run_id=None)
        await session.commit()
        count = len(list(await session.scalars(select(TestCase).where(TestCase.repo_id == repo_id))))
    assert first["t.py::a"].id == again["t.py::a"].id and count == 2


async def test_event_sink_batches_and_redacts(ctx: OrchestratorContext, repo: TestRepo, repo_id: int) -> None:
    async with ctx.db.session() as session:
        run = Run(
            repo_id=repo_id, head_sha=repo.main_sha, trigger="baseline", status="running", created_at=T0
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    seen: list[str] = []
    sink = EventSink(
        ctx.db,
        run_id,
        lock=asyncio.Lock(),
        now=ctx.settings.now,
        max_batch=50,
        interval_s=10.0,
        on_event=lambda k, p: seen.append(k),
    )
    sink.start()
    token = "ghp_" + "a" * 36
    for n in range(120):
        sink.event("log", {"shard": 0, "text": f"line {n} {token}"})
    sink.timeline("first_test", T0)
    await asyncio.sleep(0.2)  # the 50-row threshold wakes the flusher long before its 10 s interval
    async with ctx.db.session() as session:
        stored = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
    assert len(stored) >= 50
    await sink.close()
    async with ctx.db.session() as session:
        stored = list(
            await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))
        )
        live = await session.get(Run, run_id)
    assert [e.seq for e in stored] == list(range(1, 121))
    assert all(token not in e.payload["text"] for e in stored)
    assert live is not None and live.timeline["first_test"] == T0.isoformat()
    assert len(seen) == 120


def test_make_repo_helper_builds_a_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    head = pr_change(repo)
    assert git(repo.path, "rev-parse", "pr-1") == head != repo.main_sha
