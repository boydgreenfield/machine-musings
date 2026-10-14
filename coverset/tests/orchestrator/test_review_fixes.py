"""Regression tests for the correctness review: collection errors, event ordering, wording, estimates,
staleness, scoring, decoding, args files, crashed costs, prior runs, tracer loss, redaction, tracked
files and the automatic invariants scan after a baseline."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from orch_fakes import INVARIANTS, NODEIDS, FakeExecutor, Script, TestRepo, fake_env_spec
from orch_helpers import events, load_run, run_baseline, run_pr
from sqlalchemy import select

from coverset.analysis.diff import FileDiff
from coverset.analysis.invariants import (
    Invariant,
    InvariantContext,
    PreviousResult,
    Scope,
    VerifySpec,
    evaluate,
    spec_hash,
)
from coverset.analysis.scoring import ScoringContext, TestHistory, score_tests
from coverset.config import Settings
from coverset.db.models import Invariant as InvariantModel
from coverset.db.models import InvariantResult, Repo, Run, RunEvent
from coverset.db.session import Database, init_db
from coverset.lang.base import CollectedItem
from coverset.orchestrator import history
from coverset.orchestrator import runner as runner_module
from coverset.orchestrator.planner import (
    LAYOUT,
    _text,
    parse_collect_json,
    plan_run,
    tracked_changes,
)
from coverset.orchestrator.runner import (
    EventSink,
    OrchestratorContext,
    _Execution,
    _parse_test,
    create_run,
    finalize_crashed,
    tracer_warning,
)
from coverset.orchestrator.verdict import (
    ESTIMATE_LABEL,
    TestState,
    _previous_result,
    build_summary,
    file_order_estimate,
)

BROKEN = "tests/test_broken.py"
BROKEN_ERROR = {
    "nodeid": BROKEN,
    "longrepr": "ImportError while importing test module 'tests/test_broken.py'.\n"
    "E   ModuleNotFoundError: No module named 'does_not_exist'",
}


# -- C1: a module that fails to import is a failure, the rest still runs ------------------------------


async def test_a_collection_error_fails_the_run_while_the_other_tests_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    script.collect_errors = [BROKEN_ERROR]
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert (run.status, run.verdict) == ("completed", "failure")
    assert run.reason == "1 collection error"
    summary = run.summary_json or {}
    assert [f["nodeid"] for f in summary["failures"]] == [BROKEN]
    assert "No module named 'does_not_exist'" in summary["failures"][0]["message"]
    assert summary["title"] == f"{BROKEN} failed to collect"
    stored = await events(ctx, run_id)
    tested = [e.payload["nodeid"] for e in stored if e.kind == "test"]
    assert sorted(tested) == sorted(NODEIDS)  # every good test still ran
    done = next(e for e in stored if e.kind == "done")
    assert done.payload["exit_code"] == 1


async def test_only_broken_modules_is_a_failure_not_no_tests(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.collected = ()
    script.collect_errors = [BROKEN_ERROR]
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert (run.verdict, run.reason) == ("failure", "1 collection error")
    done = next(e for e in await events(ctx, run_id) if e.kind == "done")
    assert done.payload["exit_code"] == 1


async def test_collect_command_keeps_going_and_passes_one_word_options(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor, script: Script
) -> None:
    script.collect_errors = [BROKEN_ERROR]
    run_id = await create_run(
        ctx, repo_id=repo_id, head_sha=repo.main_sha, base_sha=None, branch="main", trigger="baseline"
    )
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
    assert run is not None
    plan = await plan_run(
        ctx.db, ctx.settings, ctx.store, run, executor=executor, env_spec_builder=fake_env_spec
    )
    try:
        assert [e["nodeid"] for e in plan.collect_errors] == [BROKEN]
        assert [i.nodeid for i in plan.collected] == list(NODEIDS)
        collect = next(c for c in script.commands if "--collect-only" in c)
        assert "--continue-on-collection-errors" in collect
        assert "--coverset-shapes=off" in collect
        assert any(w.startswith("--coverset-out=") and w.endswith(LAYOUT.collect_dir) for w in collect)
        assert "--coverset-out" not in collect and "--coverset-shapes" not in collect
    finally:
        await plan.cleanup()


def test_collection_errors_are_validated_and_redacted() -> None:
    token = "ghp_" + "a" * 36
    raw = json.dumps(
        {
            "items": [{"nodeid": "t.py::a", "file": "t.py"}],
            "errors": [
                {"nodeid": BROKEN, "longrepr": f"E   KeyError: {token}"},
                {"nodeid": 5, "longrepr": None},
                "junk",
            ],
        }
    ).encode()
    items, errors = parse_collect_json(raw)
    assert [i.nodeid for i in items] == ["t.py::a"]
    assert errors[0]["nodeid"] == BROKEN and token not in errors[0]["longrepr"]
    assert errors[1] == {"nodeid": "<collection>", "longrepr": None}
    assert len(errors) == 2


# -- C2: events queued during a transaction never get skipped ----------------------------------------


async def _sink_with_run(settings: Settings) -> tuple[Database, EventSink, str]:
    await init_db(settings)
    db = Database.from_url(settings.database_url)
    now = settings.now()
    async with db.session() as session:
        repo = Repo(
            forge="local",
            external_id="x",
            full_name="local/x",
            default_branch="main",
            private=True,
            language="python",
            settings_json={},
        )
        session.add(repo)
        await session.flush()
        run = Run(
            repo_id=repo.id,
            head_sha="a" * 40,
            base_sha=None,
            branch="main",
            trigger="baseline",
            status="running",
            executor="local",
            trusted=True,
            extra_env={},
            retry_increment_keys=[],
            timeline={"queued": now.isoformat()},
            created_at=now,
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    return db, EventSink(db, run_id, lock=asyncio.Lock(), now=settings.now, interval_s=10.0), run_id


async def test_events_queued_during_a_transaction_commit_with_it(settings: Settings) -> None:
    db, sink, run_id = await _sink_with_run(settings)
    try:
        sink.event("status", {"status": "running"})
        await sink.flush()
        sink.event("test", {"nodeid": "t.py::a", "outcome": "passed"})
        gate = asyncio.Event()

        async def late_event() -> None:
            await gate.wait()
            sink.event("test", {"nodeid": "t.py::b", "outcome": "passed"})

        async def work(session: Any) -> int:
            gate.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            row, _ = sink.event_row("verdict", {"conclusion": "failure", "early": True})
            session.add(row)
            return row.seq

        late = asyncio.create_task(late_event())
        verdict_seq = await sink.transact(work)
        await late
        async with db.session() as session:
            seqs = [r.seq for r in await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id))]
        assert sorted(seqs) == list(range(1, verdict_seq + 1))  # nothing below the verdict left behind
        assert sink.pending == 0
    finally:
        await db.engine.dispose()


async def test_a_failed_transaction_requeues_both_batches_in_order(settings: Settings) -> None:
    db, sink, run_id = await _sink_with_run(settings)
    try:
        sink.event("log", {"text": "one"})
        attempts = 0

        async def work(session: Any) -> None:
            nonlocal attempts
            attempts += 1
            sink.event("log", {"text": f"during {attempts}"})
            if attempts == 1:
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await sink.transact(work)
        await sink.transact(work)
        async with db.session() as session:
            rows = list(
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
        assert [r.payload["text"] for r in rows] == ["one", "during 1", "during 2"]
    finally:
        await db.engine.dispose()


# -- C4: a success with pre-existing failures counts what passed --------------------------------------


def _execution(states: dict[str, TestState]) -> _Execution:
    ex = _Execution.__new__(_Execution)
    ex.states = states
    ex.shard_states = {}
    return ex


def test_pre_existing_failures_are_not_counted_as_passed() -> None:
    states = {
        "t.py::a": TestState("t.py::a", outcome="passed", attempts=1),
        "t.py::s": TestState("t.py::s", outcome="skipped", attempts=1),
        "t.py::b": TestState(
            "t.py::b", outcome="failed", attempts=2, confirmed=True, preexisting=True, fail_sig="00" * 8
        ),
    }
    conclusion, status, reason = _execution(states)._conclusion(None)
    assert (conclusion, status) == ("success", "completed")
    assert reason == "1 passed, 1 pre-existing failure, 1 skipped"
    run = SimpleNamespace(
        timeline={},
        rank_coverset=None,
        rank_file_order=None,
        t_first_failure_file_order_est=None,
        id="r",
        created_at=datetime.now(UTC),
    )
    summary = build_summary(Settings.for_tests(), run, None, states, conclusion="success", reason=reason)  # type: ignore[arg-type]
    assert summary.title == "1 passed, 1 pre-existing failure, 1 skipped"
    clean = {"t.py::a": states["t.py::a"]}
    assert _execution(clean)._conclusion(None)[2] == "all 1 tests passed"
    assert (
        build_summary(Settings.for_tests(), run, None, clean, conclusion="success", reason="").title
        == "1 tests passed"
    )  # type: ignore[arg-type]


# -- C8: the file-order estimate starts at the first test ----------------------------------------------


def _plan_stub(n: int, durations: dict[str, float]) -> Any:
    collected = [CollectedItem(f"t.py::t{i}", "t.py", i, (), (), False) for i in range(n)]
    plan = SimpleNamespace(collected=collected, durations=durations)
    plan.file_ranks = {item.nodeid: k for k, item in enumerate(collected, start=1)}
    return plan


def test_file_order_estimate_uses_the_planner_default_without_timings() -> None:
    assert file_order_estimate(_plan_stub(100, {}), "t.py::t99", 3.0) == pytest.approx(3.0 + 99 * 0.05)
    timed = _plan_stub(3, {"t.py::t0": 1.0, "t.py::t2": 3.0})
    assert file_order_estimate(timed, "t.py::t2", 0.5) == pytest.approx(0.5 + 1.0 + 2.0)  # t1 = median
    assert ESTIMATE_LABEL == "estimated from the first test"


async def test_the_estimate_is_measured_from_the_first_test(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    run = await load_run(ctx, run_id)
    timeline = run.timeline or {}
    t0 = datetime.fromisoformat(timeline["queued"])
    first_test = (datetime.fromisoformat(timeline["first_test"]) - t0).total_seconds()
    rank = run.rank_file_order
    assert rank is not None and run.t_first_failure_file_order_est is not None
    assert run.t_first_failure_file_order_est == pytest.approx(first_test + (rank - 1) * 0.05, abs=0.002)
    assert (run.summary_json or {})["t_first_failure_file_order_est_label"] == ESTIMATE_LABEL


# -- C10: stale stays stale until fresh evidence ----------------------------------------------------------


def _inv() -> Invariant:
    return Invariant(
        id="inv",
        scope=Scope("function", "pkg.m.f"),
        statement="s",
        severity="error",
        verify=(VerifySpec("test"),),
    )


def test_stale_is_carried_forward() -> None:
    inv = _inv()
    first = evaluate(
        inv,
        InvariantContext(
            results={},
            links={},
            target_body_hash="B2",
            previous_result=PreviousResult("run1", "satisfied", "B1", spec_hash(inv)),
        ),
    )
    assert first.status == "stale"
    second = evaluate(
        inv,
        InvariantContext(
            results={},
            links={},
            target_body_hash="B2",
            previous_result=PreviousResult("run2", "stale", "B2", spec_hash(inv)),
        ),
    )
    assert second.status == "stale"


async def test_previous_result_skips_stale_rows(ctx: OrchestratorContext, repo_id: int) -> None:
    inv = _inv()
    now = ctx.settings.now()
    async with ctx.db.session() as session:
        model = InvariantModel(
            repo_id=repo_id,
            inv_key="inv",
            spec_hash=spec_hash(inv),
            spec_json={},
            first_seen_sha="a" * 40,
            last_seen_sha="a" * 40,
        )
        session.add(model)
        await session.flush()
        runs = []
        for k, status in enumerate(("satisfied", "stale", None)):
            run = Run(
                repo_id=repo_id,
                head_sha=f"{k}" * 40,
                branch="main",
                trigger="baseline",
                status="completed",
                executor="local",
                trusted=True,
                extra_env={},
                retry_increment_keys=[],
                timeline={},
                created_at=now + timedelta(seconds=k),
            )
            session.add(run)
            await session.flush()
            runs.append(run)
            if status is not None:
                session.add(
                    InvariantResult(
                        run_id=run.id,
                        invariant_id=model.id,
                        status=status,
                        trigger="run",
                        target_body_hash="B1" if status == "satisfied" else "B2",
                        covering_test_ids=[],
                        evidence={},
                    )
                )
        await session.commit()
        previous = await _previous_result(session, runs[2], inv)
    assert previous is not None and (previous.run_id, previous.status) == (runs[0].id, "satisfied")


# -- C13: a test seen in the previous run is not new ----------------------------------------------------


def test_a_test_that_ran_before_is_not_new() -> None:
    items = [CollectedItem(n, "t.py", 1, (), (), False) for n in ("t.py::old", "t.py::brand_new")]
    hist = {"t.py::old": TestHistory(last_outcome="passed", failed_in_prev_run=False, p50_s=0.1)}
    ctx = ScoringContext(
        prior=None, changed=[], changed_files=set(), test_history=hist, now=datetime.now(UTC), rng_seed=1
    )
    scored = {t.nodeid: t for t in score_tests(items, ctx)}
    assert scored["t.py::brand_new"].components["new_or_changed"] > 0
    assert scored["t.py::old"].components["new_or_changed"] == 0
    assert "new test" not in scored["t.py::old"].explanation


# -- C14: sources with a coding cookie ---------------------------------------------------------------------


def test_sources_are_decoded_like_cpython() -> None:
    latin = "# -*- coding: latin-1 -*-\ndef f(name='caf\xe9'):\n    return name\n".encode("latin-1")
    assert "caf\xe9" in _text(latin)
    assert _text(b"\xef\xbb\xbfx = 1\n") == "x = 1\n"
    assert _text(b"x = '\xff'\n") == "x = '�'\n"  # undecodable: replaced, never dropped


async def test_a_latin1_module_is_measured(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, executor: FakeExecutor
) -> None:
    source = "# -*- coding: latin-1 -*-\ndef greet(name='caf\xe9'):\n    return name\n"
    path = repo.path / "src/factspkg/latin.py"
    path.write_bytes(source.encode("latin-1"))
    from orch_fakes import git

    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "latin-1 module")
    head = git(repo.path, "rev-parse", "HEAD")
    run_id = await create_run(
        ctx, repo_id=repo_id, head_sha=head, base_sha=None, branch="main", trigger="baseline"
    )
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
    assert run is not None
    plan = await plan_run(
        ctx.db, ctx.settings, ctx.store, run, executor=executor, env_spec_builder=fake_env_spec
    )
    try:
        assert "caf\xe9" in plan.measured_sources["src/factspkg/latin.py"]
        assert any(r.qualname == "greet" for r in plan.head_functions)
    finally:
        await plan.cleanup()


# -- C15: node ids that would split across args-file lines ---------------------------------------------------


@pytest.mark.parametrize("sep", ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\0"])
def test_node_ids_that_split_an_args_line_are_dropped(sep: str, caplog: pytest.LogCaptureFixture) -> None:
    raw = json.dumps(
        {"items": [{"nodeid": f"t.py::a[x{sep}y]"}, {"nodeid": "t.py::b"}], "errors": []}
    ).encode()
    items, _ = parse_collect_json(raw)
    assert [i.nodeid for i in items] == ["t.py::b"]
    assert "would not fit on one line" in caplog.text


# -- C16: a crashed run records its real cost ---------------------------------------------------------------


async def test_finalize_crashed_replaces_the_reservation(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    run_id = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=repo.main_sha,
        base_sha=None,
        branch="main",
        trigger="baseline",
        claim=True,
    )
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.cost_estimate = 1.0  # the reservation
        await session.commit()
    assert await finalize_crashed(ctx.db, ctx.settings, run_id, "boom")
    assert (await load_run(ctx, run_id)).cost_estimate == 0.0  # nothing was acquired in this process
    other = await create_run(
        ctx,
        repo_id=repo_id,
        head_sha=repo.main_sha,
        base_sha=None,
        branch="main",
        trigger="baseline",
        claim=True,
    )
    runner_module._LIVE_COSTS[other] = lambda: 0.123456789
    assert await finalize_crashed(ctx.db, ctx.settings, other, "boom")
    assert (await load_run(ctx, other)).cost_estimate == pytest.approx(0.123457)
    assert other not in runner_module._LIVE_COSTS


# -- C17: a prior run needs usable facts ------------------------------------------------------------------------


async def test_prior_candidates_need_committed_facts(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int
) -> None:
    good = await run_baseline(ctx, repo_id, repo.main_sha)
    now = ctx.settings.now()
    async with ctx.db.session() as session:
        empty = Run(
            repo_id=repo_id,
            head_sha=repo.main_sha,
            branch="main",
            trigger="baseline",
            status="completed",
            executor="local",
            trusted=True,
            extra_env={},
            retry_increment_keys=[],
            timeline={},
            created_at=now + timedelta(minutes=1),
            coverage_complete=False,
        )
        session.add(empty)
        await session.commit()
        ctx.store.run_dir(repo_id, empty.id).mkdir(parents=True)
        candidates = await history.prior_candidates(session, repo_id, exclude="none", store=ctx.store)
    assert [c.run_id for c in candidates] == [good]


# -- C18: a lost tracer is reported ---------------------------------------------------------------------------------


@pytest.mark.security
def test_tracer_warning_words_a_lost_tracer() -> None:
    lost = {
        "convert": {"tracer_mismatch": True, "tracer": "CTracer lost at t.py::x"},
        "tracer": "CTracer",
        "tracer_lost_at": "t.py::x\x1b[2J",
    }
    assert tracer_warning(lost) == (
        "coverage lost its tracer at t.py::x[2J; per-test contexts after it may be incomplete"
    )
    foreign = {"convert": {"tracer_mismatch": True, "tracer": "PyTracer"}, "tracer": "PyTracer"}
    assert (
        tracer_warning(foreign) == "coverage was collected with PyTracer; per-test contexts may be incomplete"
    )


async def test_a_lost_tracer_reaches_the_run_events(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    script.meta = {"tracer": "CTracer", "tracer_lost_at": NODEIDS[1]}
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    warnings = [e.payload for e in await events(ctx, run_id) if e.kind == "log" and e.payload.get("warning")]
    assert any(
        w["warning"] == "tracer_mismatch"
        and w["text"].startswith(f"coverage lost its tracer at {NODEIDS[1]}")
        for w in warnings
    )


# -- C19: worker failure text is redacted before every sink ----------------------------------------------------------


GHP = "ghp_" + "Z" * 36
SKANT = "sk-ant-api03-" + "Q" * 30


@pytest.mark.security
def test_streamed_longrepr_is_redacted() -> None:
    parsed = _parse_test({"outcome": "failed", "longrepr": f"E   token={GHP} key={SKANT}"})
    assert parsed is not None and GHP not in (parsed[4] or "") and SKANT not in (parsed[4] or "")


@pytest.mark.security
async def test_failure_text_reaches_no_sink_unredacted(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orch_fakes

    original = orch_fakes.FakeWorker.run

    async def leaky(self: Any, cmd: list[str], **kwargs: Any) -> Any:
        on_line = kwargs.get("on_line")
        if on_line is not None:

            def patched(text: str, stream: str) -> None:
                on_line(text.replace("assert 'zero' == 'pos'", f"assert {GHP!r} == {SKANT!r}"), stream)

            kwargs["on_line"] = patched
        return await original(self, cmd, **kwargs)

    monkeypatch.setattr(orch_fakes.FakeWorker, "run", leaky)
    head = repo.commit({"README.md": "x\n"}, "docs", branch="pr-1")
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    run = await load_run(ctx, run_id)
    assert run.verdict == "failure"
    stored = json.dumps(run.summary_json) + (run.reason or "")
    stored += json.dumps([e.payload for e in await events(ctx, run_id)])
    from coverset.db.models import ForgeCall

    async with ctx.db.session() as session:
        calls = list(await session.scalars(select(ForgeCall).where(ForgeCall.run_id == run_id)))
    stored += json.dumps([c.request for c in calls], default=str)
    assert "ghp_[REDACTED]" in stored and "sk-ant-[REDACTED]" in stored
    assert GHP not in stored and SKANT not in stored


# -- C22: tracked files impact every test ------------------------------------------------------------------------------


def test_tracked_globs_match_from_the_right() -> None:
    diffs = [
        FileDiff("tests/conftest.py", "modified"),
        FileDiff("uv.lock", "modified"),
        FileDiff("src/a.py", "modified"),
        FileDiff("old/conftest.py", "deleted"),
    ]
    assert tracked_changes(diffs, ("uv.lock", "conftest.py")) == (
        "old/conftest.py",
        "tests/conftest.py",
        "uv.lock",
    )
    assert tracked_changes(diffs, ()) == ()


def test_a_tracked_change_impacts_every_test() -> None:
    items = [CollectedItem("t.py::a", "t.py", 1, (), (), False)]
    hist = {"t.py::a": TestHistory(last_outcome="passed", runs_n=3, p50_s=0.1)}
    base = dict(
        prior=None, changed=[], changed_files=set(), test_history=hist, now=datetime.now(UTC), rng_seed=1
    )
    quiet = score_tests(items, ScoringContext(**base))[0]  # type: ignore[arg-type]
    assert quiet.components["impacted_by_diff"] == 0
    tracked = score_tests(items, ScoringContext(**base, tracked_changes=("uv.lock",)))[0]  # type: ignore[arg-type]
    assert tracked.components["impacted_by_diff"] > 0
    assert "tracked file changed: uv.lock" in tracked.explanation


async def test_a_pr_changing_conftest_marks_every_test(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    await run_baseline(ctx, repo_id, repo.main_sha)
    head = repo.commit({"tests/conftest.py": "# fixtures\n"}, "conftest", branch="pr-1")
    run_id = await run_pr(ctx, repo_id, head, repo.main_sha)
    from coverset.db.models import TestResult

    async with ctx.db.session() as session:
        rows = list(await session.scalars(select(TestResult).where(TestResult.run_id == run_id)))
    assert rows and all(
        "tracked file changed: tests/conftest.py" in r.score_explanation["text"] for r in rows
    )


# -- C24: the invariants scan after a baseline --------------------------------------------------------------------------


REVIEWED = (
    INVARIANTS
    + """  - id: classify_names_sign
    scope: {kind: function, target: factspkg.calc.classify}
    statement: classify returns neg, zero or pos by the sign of n.
    severity: info
    verify:
      - kind: review
"""
)


async def _review_rows(ctx: OrchestratorContext, run_id: str) -> dict[str, InvariantResult]:
    async with ctx.db.session() as session:
        rows = await session.execute(
            select(InvariantModel.inv_key, InvariantResult)
            .join(InvariantResult, InvariantResult.invariant_id == InvariantModel.id)
            .where(InvariantResult.run_id == run_id)
        )
        return {key: row for key, row in rows}


async def test_a_baseline_of_the_tip_is_scanned(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    head = repo.commit({"invariants.yaml": REVIEWED}, "review invariant", branch="main")
    run_id = await run_baseline(ctx, repo_id, head)
    await ctx.finish_background()
    rows = await _review_rows(ctx, run_id)
    assert rows["classify_names_sign"].trigger == "scan"
    assert rows["classify_names_sign"].status in ("satisfied", "needs_review")
    assert rows["add_commutes"].trigger == "run"  # no review entry: not scanned


@pytest.mark.parametrize("settings_overrides", [{"ai": "off"}])
async def test_the_scan_records_statuses_with_ai_off(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    head = repo.commit({"invariants.yaml": REVIEWED}, "review invariant", branch="main")
    run_id = await run_baseline(ctx, repo_id, head)
    await ctx.finish_background()
    row = (await _review_rows(ctx, run_id))["classify_names_sign"]
    assert (row.trigger, row.status) == ("scan", "needs_review")


@pytest.mark.parametrize("settings_overrides", [{"scan_after_baseline": False}])
async def test_the_scan_can_be_turned_off(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    head = repo.commit({"invariants.yaml": REVIEWED}, "review invariant", branch="main")
    run_id = await run_baseline(ctx, repo_id, head)
    await ctx.finish_background()
    assert (await _review_rows(ctx, run_id))["classify_names_sign"].trigger == "run"


async def test_pr_runs_and_old_heads_are_not_scanned(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script
) -> None:
    script.zero_passes = True
    old = repo.commit({"invariants.yaml": REVIEWED}, "review invariant", branch="main")
    repo.commit({"README.md": "newer\n"}, "newer tip", branch="main", hour=2)
    run_id = await run_baseline(ctx, repo_id, old)  # not the tip any more
    pr = await run_pr(ctx, repo_id, repo.commit({"README.md": "pr\n"}, "pr", branch="pr-1", hour=3), old)
    await ctx.finish_background()
    assert (await _review_rows(ctx, run_id))["classify_names_sign"].trigger == "run"
    assert all(r.trigger == "run" for r in (await _review_rows(ctx, pr)).values())


async def test_a_failing_scan_never_fails_the_run(
    ctx: OrchestratorContext, repo: TestRepo, repo_id: int, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coverset.commands.invariants as scan_module

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(scan_module, "scan_invariants", broken)
    script.zero_passes = True
    run_id = await run_baseline(ctx, repo_id, repo.main_sha)
    await ctx.finish_background()
    assert (await load_run(ctx, run_id)).verdict == "success"
