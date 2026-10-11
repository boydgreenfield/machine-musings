"""Executing a run: shards, streamed events, retries, fail-fast, ingest and the verdict.

:func:`execute_run` drives one ``queued`` run to a terminal status:

1. **Claim and policy.** The run becomes ``running``; untrusted runs and (unless allowed)
   webhook-triggered runs never use the local executor; Modal runs stop at the daily budget.
2. **Plan** (:func:`coverset.orchestrator.planner.plan_run`): workers hold the source and their
   shard files; ``timeline.sandbox_ready`` is set.
3. **Shards.** Each shard execs the adapter's test command with ``COVERSET_EVENT_NONCE`` (a fresh
   128-bit value per exec), ``PYTHONPATH`` and the run's ``extra_env``. Lines that start with the exact
   nonce prefix are events (``collected``, ``test``, ``heartbeat``, ``bundle_written``, ``done``);
   every other line is log text. Events become ``run_events`` rows (``progress``, ``test``, ``log``,
   ``verdict``, ``done`` per Addendum A, plus the internal ``status``, ``shard`` and ``failure``) and
   ``test_results``/``test_attempts`` updates, batched by :class:`EventSink` (every 100 ms or 200
   rows).
4. **Fail-fast is owned here.** The first failed or errored result of a non-quarantined test
   launches one retry exec on the same worker (``--coverset-out run/retry-<n> --coverset-shapes
   off``, the nodeid alone in an args file, ``extra_env`` with :attr:`Run.retry_increment_keys`
   incremented; retries are never ingested and capped by ``retry_budget_per_run``). A pass makes the
   test *flaky*; a second failure *confirms* it unless its ``fail_sig`` also failed on the base
   branch (*pre-existing*). The first confirmed failure posts a verdict immediately and records
   ``rank_coverset``; with ``fail_fast = "cancel_remaining"`` the shards then get STOP files, up to
   20 s to write their bundles, and are cancelled; with ``finish_in_background`` (the default) they
   run on so the facts land. A newer queued run for the same PR cancels this one
   (``cancel_superseded``).
5. **Ingest and verdict.** Bundles are pulled before the workers are released, converted and
   ingested concurrently, the run is finalized, ``results.jsonl`` overrides streamed outcomes, then
   the patch report, input classes, invariants, AI review, ``test_stats`` and cost feed the final
   verdict, which is stored, posted to the forge and closed with a ``done`` event.

Every failure path ends the run as ``error`` or ``cancelled`` with a reason and a ``done`` event, so
SSE clients and ``coverset-ci wait`` always finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import shutil
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import ExecutorKind, Settings
from coverset.db.models import PullRequest, Run, RunEvent, Shard, TestAttempt, TestResult, WebhookDelivery
from coverset.db.session import Database
from coverset.executors.base import Executor, ExecutorError, RunResult
from coverset.facts.ingest import RunContext, finalize_run, record_shard_failure
from coverset.facts.store import FactsStore
from coverset.lang.base import LanguageAdapter
from coverset.lang.python import PythonAdapter
from coverset.logging import redact
from coverset.orchestrator import budget, history
from coverset.orchestrator.bundles import (
    IngestJob,
    IngestOutcome,
    IngestPool,
    bundle_limits,
    shared_ingest_pool,
)
from coverset.orchestrator.planner import (
    LAYOUT,
    REPO_DIR,
    EnvSpecBuilder,
    PlanError,
    RunPlan,
    ShardSpec,
    plan_run,
    worker_env,
)
from coverset.orchestrator.verdict import (
    RunReport,
    TestState,
    build_report,
    build_summary,
    file_order_estimate,
    functions_of_interest,
    persist_verdict,
    post_summary,
    t0_of,
)

__all__ = [
    "EXIT_CODES",
    "EventSink",
    "OrchestratorContext",
    "RunOutcome",
    "create_run",
    "execute_run",
    "retry_env",
]

log = logging.getLogger(__name__)

RECORD_SEPARATOR = "\x1e"
OUTCOMES = frozenset({"passed", "failed", "error", "skipped", "xfailed", "xpassed"})
FAILED = frozenset({"failed", "error"})
PHASES = frozenset({"setup", "call", "teardown"})
EXIT_CODES = {"success": 0, "failure": 1, "neutral": 0, "cancelled": 2, "timed_out": 2, "error": 2}
"""``done.exit_code`` per conclusion (pytest's codes, as ``coverset-ci wait`` exits)."""
MAX_LOG_LINES_PER_SHARD = 2000
MAX_LOG_CHARS = 2000
MAX_LONGREPR_CHARS = 8 * 1024
PROGRESS_INTERVAL_S = 0.25
RETRY_TIMEOUT_S = 600.0
BUNDLE_FILE = "bundle.tar.gz"

type EventCallback = Callable[[str, dict[str, Any]], None]


# -- context --------------------------------------------------------------------------------------


@dataclass(slots=True)
class OrchestratorContext:
    """What runs need from the process: settings, database, facts store, executors, forge, pools."""

    settings: Settings
    db: Database
    store: FactsStore
    adapter: LanguageAdapter = field(default_factory=PythonAdapter)
    forge: Any = None
    """A forge adapter; None resolves the configured one lazily, False disables posting."""
    ingest: IngestPool | None = None
    executor_factory: Callable[[ExecutorKind], Executor] | None = None
    env_spec_builder: EnvSpecBuilder | None = None
    cancel_poll_s: float = 1.0
    stop_wait_s: float = 20.0
    owns_db: bool = False
    executors: dict[str, Executor] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> OrchestratorContext:
        """A context with its own database engine (closed by :meth:`close`)."""
        db = overrides.pop("db", None)
        owns = db is None
        return cls(
            settings=settings,
            db=db or Database.from_url(settings.database_url),
            store=FactsStore(settings.facts_dir),
            owns_db=owns,
            **overrides,
        )

    @classmethod
    def from_app(cls, app: Any) -> OrchestratorContext:
        """The app's context, created once and kept in ``app.extensions["orchestrator"]``."""
        existing = app.extensions.get("orchestrator")
        if existing is None:
            settings = app.extensions["settings"]
            existing = cls(settings=settings, db=app.extensions["db"], store=FactsStore(settings.facts_dir))
            existing.forge = _app_forge(app)
            app.extensions["orchestrator"] = existing
        return existing

    def executor(self, kind: ExecutorKind) -> Executor:
        """The executor of ``kind`` (built once per context)."""
        found = self.executors.get(kind)
        if found is None:
            if self.executor_factory is not None:
                found = self.executor_factory(kind)
            else:
                from coverset.executors import get_executor

                found = get_executor(self.settings, kind)
            self.executors[kind] = found
        return found

    def get_forge(self) -> Any:
        """The forge adapter to post verdicts through, or None."""
        if self.forge is False:
            return None
        if self.forge is None:
            try:
                from coverset.forge.registry import get_forge
            except ImportError:
                self.forge = False
                return None
            try:
                self.forge = get_forge(self.settings, session_factory=self.db.sessionmaker)
            except Exception as exc:  # misconfigured forge: runs still complete, nothing is posted
                log.warning("no forge for posting verdicts: %s", exc)
                self.forge = False
                return None
        return self.forge

    def ingest_pool(self) -> IngestPool:
        """The bundle ingest pool (a shared process pool unless one was given)."""
        if self.ingest is None:
            self.ingest = shared_ingest_pool("process")
        return self.ingest

    async def close(self) -> None:
        """Close executors and, when owned, the database engine."""
        for executor in self.executors.values():
            with contextlib.suppress(Exception):
                await executor.close()
        self.executors.clear()
        if self.owns_db:
            await self.db.engine.dispose()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """How :func:`execute_run` left the run."""

    run_id: str
    status: str
    conclusion: str | None
    reason: str | None


# -- run creation ---------------------------------------------------------------------------------


async def create_run(
    ctx: OrchestratorContext,
    *,
    repo_id: int,
    head_sha: str,
    base_sha: str | None,
    branch: str | None,
    trigger: str,
    executor: ExecutorKind = "local",
    pr_number: int | None = None,
    pr_title: str = "",
    base_ref: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    retry_increment_keys: list[str] | None = None,
    trusted: bool = True,
) -> str:
    """Insert a ``queued`` run (and find or create its pull request); return the run id."""
    now = ctx.settings.now()
    async with ctx.db.session() as session:
        pr_id = None
        if pr_number is not None:
            pr = await session.scalar(
                select(PullRequest).where(PullRequest.repo_id == repo_id, PullRequest.number == pr_number)
            )
            if pr is None:
                pr = PullRequest(
                    repo_id=repo_id,
                    number=pr_number,
                    head_sha=head_sha,
                    base_sha=base_sha or head_sha,
                    head_ref=branch,
                    base_ref=base_ref,
                    title=pr_title,
                    author_association="OWNER",
                    created_at=now,
                    updated_at=now,
                )
                session.add(pr)
            else:
                pr.head_sha, pr.base_sha = head_sha, base_sha or pr.base_sha
                pr.title = pr_title or pr.title
            await session.flush()
            pr_id = pr.id
        run = Run(
            repo_id=repo_id,
            head_sha=head_sha,
            base_sha=base_sha,
            branch=branch,
            pr_id=pr_id,
            trigger=trigger,
            status="queued",
            executor=executor,
            trusted=trusted,
            extra_env=dict(extra_env or {}),
            retry_increment_keys=list(retry_increment_keys or []),
            timeline={"queued": now.isoformat()},
            created_at=now,
        )
        session.add(run)
        await session.commit()
        return run.id


def retry_env(extra_env: Mapping[str, str], increment_keys: list[str]) -> dict[str, str]:
    """``extra_env`` for a retry exec: integer values of ``increment_keys`` go up by one."""
    env = dict(extra_env)
    for key in increment_keys:
        value = env.get(key)
        if value is not None and value.lstrip("-").isdigit():
            env[key] = str(int(value) + 1)
    return env


# -- batched writes -------------------------------------------------------------------------------


class EventSink:
    """Batches ``run_events``, ``test_results``/``test_attempts`` and run/shard field updates.

    Writes are flushed every ``interval_s`` or as soon as ``max_batch`` rows are pending, under the
    execution's database lock (so a concurrent verdict write never loses a timeline key). Event
    payload strings are redacted before they are stored.
    """

    def __init__(
        self,
        db: Database,
        run_id: str,
        *,
        lock: asyncio.Lock,
        now: Callable[[], datetime],
        start_seq: int = 0,
        interval_s: float = 0.1,
        max_batch: int = 200,
        on_event: EventCallback | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._lock = lock
        self._now = now
        self._seq = start_seq
        self._interval_s = interval_s
        self._max_batch = max_batch
        self._on_event = on_event
        self._events: list[RunEvent] = []
        self._results: dict[int, dict[str, Any]] = {}
        self._attempts: dict[tuple[int, int], dict[str, Any]] = {}
        self._run: dict[str, Any] = {}
        self._timeline: dict[str, str] = {}
        self._shards: dict[int, dict[str, Any]] = {}
        self._wake = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    @property
    def pending(self) -> int:
        """Rows waiting for the next flush."""
        return len(self._events) + len(self._results) + len(self._attempts)

    def start(self) -> None:
        """Start the periodic flusher."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._pump())

    def event(self, kind: str, payload: Mapping[str, Any]) -> int:
        """Queue one run event; returns its sequence number."""
        self._seq += 1
        clean = _redacted(dict(payload))
        self._events.append(
            RunEvent(run_id=self._run_id, seq=self._seq, kind=kind, payload=clean, at=self._now())
        )
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(kind, clean)
        self._maybe_wake()
        return self._seq

    def result(self, test_id: int, **fields: Any) -> None:
        """Queue field updates of one ``test_results`` row."""
        self._results.setdefault(test_id, {}).update(fields)
        self._maybe_wake()

    def attempt(self, test_id: int, attempt: int, **fields: Any) -> None:
        """Queue one ``test_attempts`` row (insert or replace)."""
        self._attempts[(test_id, attempt)] = fields
        self._maybe_wake()

    def run_fields(self, **fields: Any) -> None:
        """Queue updates of run columns (not ``timeline``)."""
        self._run.update(fields)

    def timeline(self, key: str, at: datetime) -> None:
        """Queue one timeline key (ISO 8601)."""
        self._timeline[key] = at.isoformat()

    def shard(self, shard_id: int, **fields: Any) -> None:
        """Queue updates of one ``shards`` row."""
        self._shards.setdefault(shard_id, {}).update(fields)

    def _maybe_wake(self) -> None:
        if self.pending >= self._max_batch:
            self._wake.set()

    async def _pump(self) -> None:
        while not self._closed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
            self._wake.clear()
            try:
                await self.flush()
            except Exception:  # a failed flush keeps its rows for the next attempt
                log.exception("flushing run events of %s failed", self._run_id)

    async def flush(self) -> None:
        """Write everything pending in one transaction."""
        if not (
            self._events or self._results or self._attempts or self._run or self._timeline or self._shards
        ):
            return
        events, self._events = self._events, []
        results, self._results = self._results, {}
        attempts, self._attempts = self._attempts, {}
        run_fields, self._run = self._run, {}
        timeline, self._timeline = self._timeline, {}
        shards, self._shards = self._shards, {}
        async with self._lock, self._db.session() as session:
            try:
                await self._write(session, events, results, attempts, run_fields, timeline, shards)
                await session.commit()
            except BaseException:
                await session.rollback()
                self._requeue(events, results, attempts, run_fields, timeline, shards)
                raise

    async def _write(
        self,
        session: AsyncSession,
        events: list[RunEvent],
        results: dict[int, dict[str, Any]],
        attempts: dict[tuple[int, int], dict[str, Any]],
        run_fields: dict[str, Any],
        timeline: dict[str, str],
        shards: dict[int, dict[str, Any]],
    ) -> None:
        session.add_all(events)
        for test_id, values in results.items():
            await session.execute(
                update(TestResult)
                .where(TestResult.run_id == self._run_id, TestResult.test_id == test_id)
                .values(**values)
            )
        for (test_id, number), values in attempts.items():
            await session.merge(TestAttempt(run_id=self._run_id, test_id=test_id, attempt=number, **values))
        for shard_id, values in shards.items():
            await session.execute(
                update(Shard).where(Shard.run_id == self._run_id, Shard.shard_id == shard_id).values(**values)
            )
        if run_fields or timeline:
            run = await session.get(Run, self._run_id)
            if run is not None:
                for name, value in run_fields.items():
                    setattr(run, name, value)
                for key, value in timeline.items():
                    run.timeline[key] = value

    def _requeue(
        self,
        events: list[RunEvent],
        results: dict[int, dict[str, Any]],
        attempts: dict[tuple[int, int], dict[str, Any]],
        run_fields: dict[str, Any],
        timeline: dict[str, str],
        shards: dict[int, dict[str, Any]],
    ) -> None:
        """Put the rows of a failed flush back in front of anything queued since."""
        fresh = [
            RunEvent(run_id=e.run_id, seq=e.seq, kind=e.kind, payload=e.payload, at=e.at) for e in events
        ]
        self._events = fresh + self._events
        for test_id, values in results.items():
            self._results[test_id] = {**values, **self._results.get(test_id, {})}
        self._attempts = {**attempts, **self._attempts}
        self._run = {**run_fields, **self._run}
        self._timeline = {**timeline, **self._timeline}
        for shard_id, values in shards.items():
            self._shards[shard_id] = {**values, **self._shards.get(shard_id, {})}

    async def close(self) -> None:
        """Stop the flusher and write what is left."""
        self._closed = True
        self._wake.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None
        await self.flush()


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(v) for v in value]
    return value


# -- execution ------------------------------------------------------------------------------------


async def execute_run(
    app_or_ctx: Any,
    run_id: str,
    *,
    on_event: EventCallback | None = None,
    shards: int | None = None,
) -> RunOutcome:
    """Execute the queued run ``run_id`` to a terminal status (see the module docstring).

    ``app_or_ctx`` is an :class:`OrchestratorContext` or a Quart app; ``on_event`` receives every
    stored event (``kind``, redacted payload) as it is queued, for CLI streaming; ``shards`` forces a
    shard count.
    """
    ctx = (
        app_or_ctx
        if isinstance(app_or_ctx, OrchestratorContext)
        else OrchestratorContext.from_app(app_or_ctx)
    )
    return await _Execution(ctx, run_id, on_event=on_event, shards_override=shards).execute()


@dataclass(slots=True)
class _ShardState:
    spec: ShardSpec
    nonce: str = ""
    collected: int | None = None
    bundle_sha256: str | None = None
    bundle_written: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    result: RunResult | None = None
    error: str | None = None
    log_lines: int = 0
    exec_seconds: float = 0.0


class _Cancelled(Exception):
    """The run was cancelled (externally or superseded) while its shards ran."""


class _Execution:
    """The state of one run while :func:`execute_run` drives it."""

    def __init__(
        self,
        ctx: OrchestratorContext,
        run_id: str,
        *,
        on_event: EventCallback | None,
        shards_override: int | None,
    ) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.run_id = run_id
        self.on_event = on_event
        self.shards_override = shards_override
        self.lock = asyncio.Lock()
        self.sink: EventSink | None = None
        self.plan: RunPlan | None = None
        self.run: Run | None = None
        self.states: dict[str, TestState] = {}
        self.shard_states: dict[int, _ShardState] = {}
        self.counts: Counter[str] = Counter()
        self.retries_used = 0
        self.tasks: list[asyncio.Task[Any]] = []
        self.first_failure: str | None = None
        self.early_verdict: asyncio.Task[None] | None = None
        self.stop_requested = False
        self.cancel_reason: str | None = None
        self.base_sigs: set[bytes] = set()
        self.wave1_pending: set[str] = set()
        self.last_progress = 0.0
        self.started = time.monotonic()

    # -- lifecycle ------------------------------------------------------------------------------

    async def execute(self) -> RunOutcome:
        run = await self._claim()
        if run is None:
            return await self._outcome()
        self.run = run
        self.sink = EventSink(
            self.ctx.db,
            self.run_id,
            lock=self.lock,
            now=self.settings.now,
            start_seq=await self._max_seq(),
            on_event=self.on_event,
        )
        self.sink.start()
        self.sink.event("status", {"status": "running", "executor": run.executor})
        try:
            refusal = await self._policy(run)
            if refusal is not None:
                status, conclusion, reason = refusal
                await self._finish(status, conclusion, reason)
                return await self._outcome()
            await self._execute_plan(run)
        except _Cancelled:
            await self._finish("cancelled", "cancelled", self.cancel_reason or "cancelled")
        except PlanError as exc:
            await self._finish("error", "neutral", f"{exc.reason}: {exc}")
        except Exception as exc:
            log.exception("run %s failed", self.run_id)
            await self._finish("error", "neutral", f"internal error: {type(exc).__name__}: {exc}"[:1000])
        finally:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.plan is not None:
                await self.plan.cleanup()
            await self.sink.close()
            await asyncio.to_thread(shutil.rmtree, self.settings.runs_dir / self.run_id, True)
        return await self._outcome()

    async def _claim(self) -> Run | None:
        """Atomically move the run from ``queued`` to ``running``; None if someone else has it."""
        async with self.ctx.db.session() as session:
            claimed = await session.execute(
                update(Run).where(Run.id == self.run_id, Run.status == "queued").values(status="running")
            )
            await session.commit()
            run = await session.get(Run, self.run_id)
            if run is None:
                raise LookupError(f"no run {self.run_id}")
            return run if claimed.rowcount == 1 else None

    async def _max_seq(self) -> int:
        async with self.ctx.db.session() as session:
            value = await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == self.run_id))
            return int(value or 0)

    async def _outcome(self) -> RunOutcome:
        async with self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            assert run is not None
            return RunOutcome(run.id, run.status, run.verdict, run.reason)

    async def _policy(self, run: Run) -> tuple[str, str, str] | None:
        if run.executor == "local" and not run.trusted:
            return "skipped", "neutral", "untrusted PR runs never use the local executor"
        if run.executor == "local" and not self.settings.local_allow_webhook_runs:
            async with self.ctx.db.session() as session:
                delivery = await session.scalar(
                    select(WebhookDelivery.id).where(WebhookDelivery.run_id == run.id).limit(1)
                )
            if delivery is not None:
                return (
                    "skipped",
                    "neutral",
                    "webhook runs need COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true locally",
                )
        async with self.ctx.db.session() as session:
            if await budget.budget_exhausted(session, self.settings, run.executor):
                return "completed", "neutral", "budget exhausted"
        return None

    # -- the happy path -------------------------------------------------------------------------

    async def _execute_plan(self, run: Run) -> None:
        sink = self.sink
        assert sink is not None
        executor = self.ctx.executor(run.executor)
        async with self.lock, self.ctx.db.session() as session:
            live = await session.get(Run, run.id)
            assert live is not None
            self.plan = plan = await self._plan(session, live, executor)
            await session.commit()
            self.run = live
        sink.event(
            "status",
            {
                "status": "planned",
                "tests": len(plan.collected),
                "shards": len(plan.shards),
                "wave1": len(plan.wave1.tests),
                "prior": plan.prior.describe() if plan.prior else None,
            },
        )
        if not plan.collected:
            await self._finish("completed", "neutral", "no tests collected", exit_code=5)
            return
        self.states = {i.nodeid: TestState(i.nodeid) for i in plan.collected}
        self.wave1_pending = set(plan.wave1.tests)
        await self._load_base_failures(run)
        await self._run_shards(plan)
        if self.cancel_reason is not None:
            raise _Cancelled()
        outcomes = await self._collect_bundles(plan)
        await self._conclude(plan, outcomes, executor)

    async def _plan(self, session: AsyncSession, live: Run, executor: Executor) -> RunPlan:
        def sandbox_ready(at: datetime) -> None:
            live.timeline["sandbox_ready"] = at.isoformat()

        return await plan_run(
            session,
            self.settings,
            self.ctx.store,
            live,
            executor=executor,
            adapter=self.ctx.adapter,
            env_spec_builder=self.ctx.env_spec_builder,
            shards_override=self.shards_override,
            forge=self.ctx.get_forge(),
            on_sandbox_ready=sandbox_ready,
        )

    async def _load_base_failures(self, run: Run) -> None:
        plan = self.plan
        assert plan is not None
        async with self.ctx.db.session() as session:
            live = await session.get(Run, run.id)
            assert live is not None
            base = await history.base_branch_run(
                session, live, merge_base=plan.merge_base, default_branch=plan.default_branch
            )
            if base is not None:
                self.base_sigs = await history.failure_signatures(session, base.id)

    # -- shards ---------------------------------------------------------------------------------

    async def _run_shards(self, plan: RunPlan) -> None:
        self.shard_states = {s.shard_id: _ShardState(spec=s) for s in plan.shards}
        watcher = asyncio.get_running_loop().create_task(self._watch_cancel())
        self.tasks.append(watcher)
        try:
            await asyncio.gather(*(self._run_shard(state) for state in self.shard_states.values()))
            while True:
                pending = [t for t in self.tasks if t is not watcher and not t.done()]
                if not pending:
                    break
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _extra(
        self, plan: RunPlan, worker_path: Callable[[str], str], *, shapes: str = "on"
    ) -> dict[str, Any]:
        config = plan.config
        requirements = plan.env.spec.requirements_txt.lower() if plan.env.spec is not None else ""
        return {
            "python": "python",
            "sources": config.source,
            "trace_modules": config.trace_modules,
            "full_trace_file": worker_path(LAYOUT.full_trace_file)
            if plan.full_trace and shapes == "on"
            else None,
            "shapes": shapes,
            "xdist": "pytest-xdist" in requirements,
            "pytest_cov": "pytest-cov" in requirements,
        }

    async def _run_shard(self, state: _ShardState) -> None:
        plan, sink, run = self.plan, self.sink, self.run
        assert plan is not None and sink is not None and run is not None
        shard, worker = state.spec, state.spec.worker
        state.nonce = secrets.token_hex(16)
        prefix = f"{RECORD_SEPARATOR}coverset {state.nonce} "
        extra = {**self._extra(plan, worker.path), "stop_file": worker.path(LAYOUT.stop_file)}
        scores = worker.path(LAYOUT.scores_file) if plan.config.reorder else None
        cmd = [
            *self.ctx.adapter.test_command(
                worker.path(LAYOUT.args_file), scores, worker.path(LAYOUT.out_dir), extra
            ),
            f"--rootdir={worker.path(REPO_DIR)}",
        ]
        env = worker_env(worker, plan.config, state.nonce, run.extra_env or {})
        sink.shard(shard.shard_id, status="running", started_at=self.settings.now())
        sink.event("shard", {"shard": shard.shard_id, "status": "running", "tests": len(shard.nodeids)})

        def on_line(text: str, stream: str) -> None:
            if stream == "stdout" and text.startswith(prefix):
                try:
                    payload = json.loads(text[len(prefix) :])
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    self._on_event(state, payload)
                    return
            self._on_log(state, text)

        try:
            result = await worker.run(
                cmd,
                cwd=REPO_DIR,
                env=env,
                timeout_s=self._remaining_s(),
                on_line=on_line,
                stdout_cap=self.settings.max_stdout_bytes,
            )
        except ExecutorError as exc:
            state.error = f"exec failed: {exc}"
            result = None
        state.result = result
        if result is not None:
            state.exec_seconds += result.seconds
            state.error = state.error or self._shard_error(result)
        status = (
            "failed"
            if state.error
            else ("cancelled" if result is not None and result.cancelled else "completed")
        )
        sink.shard(
            shard.shard_id,
            status=status,
            exit_code=result.exit_code if result is not None else None,
            finished_at=self.settings.now(),
            error=state.error,
            bundle_sha256=state.bundle_sha256,
        )
        sink.event(
            "shard",
            {
                "shard": shard.shard_id,
                "status": status,
                "exit_code": result.exit_code if result is not None else None,
                "error": state.error,
            },
        )
        state.finished.set()
        state.bundle_written.set()

    def _shard_error(self, result: RunResult) -> str | None:
        if result.truncated:
            return f"output exceeded {self.settings.max_stdout_bytes} bytes"
        if result.timed_out:
            return "timed out"
        if result.cancelled:
            return None
        if result.exit_code in (0, 1, 5):
            return None
        if result.exit_code == 2 and self.stop_requested:
            return None
        return f"pytest exited with code {result.exit_code}"

    def _remaining_s(self) -> float:
        return max(30.0, self.settings.run_timeout_s - (time.monotonic() - self.started))

    def _on_log(self, state: _ShardState, text: str) -> None:
        assert self.sink is not None
        if not text.strip():
            return
        state.log_lines += 1
        if state.log_lines > MAX_LOG_LINES_PER_SHARD:
            if state.log_lines == MAX_LOG_LINES_PER_SHARD + 1:
                self.sink.event("log", {"shard": state.spec.shard_id, "text": "(further output not stored)"})
            return
        self.sink.event("log", {"shard": state.spec.shard_id, "text": text[:MAX_LOG_CHARS]})

    def _on_event(self, state: _ShardState, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "test":
            self._on_test(state, payload)
        elif kind == "collected":
            count = payload.get("count")
            state.collected = count if isinstance(count, int) else None
        elif kind == "bundle_written":
            digest = payload.get("sha256")
            state.bundle_sha256 = digest if isinstance(digest, str) and len(digest) == 64 else None
            state.bundle_written.set()
        elif kind == "heartbeat":
            self._progress(force=False)

    def _on_test(self, state: _ShardState, payload: dict[str, Any]) -> None:
        plan, sink = self.plan, self.sink
        assert plan is not None and sink is not None
        nodeid = payload.get("nodeid")
        test = self.states.get(nodeid) if isinstance(nodeid, str) else None
        if test is None or test.attempts >= 1:
            return
        parsed = _parse_test(payload)
        if parsed is None:
            return
        outcome, duration, phase, sig, longrepr = parsed
        test.outcome, test.duration_s, test.failed_phase = outcome, duration, phase
        test.fail_sig, test.longrepr = sig, longrepr
        test.shard_id, test.attempts = state.spec.shard_id, 1
        test_id = plan.test_ids[test.nodeid]
        sink.result(
            test_id,
            outcome=outcome,
            failed_phase=phase,
            fail_sig=_sig_bytes(sig),
            duration_ms=round(duration * 1000, 3),
            shard_id=state.spec.shard_id,
            attempts=1,
        )
        sink.attempt(
            test_id, 1, outcome=outcome, duration_ms=round(duration * 1000, 3), shard_id=state.spec.shard_id
        )
        self.counts[outcome] += 1
        sink.event("test", self._test_payload(test, flaky=False))
        now = self.settings.now()
        if self.counts.total() == 1:
            sink.timeline("first_test", now)
            if not plan.wave1.tests:  # an empty wave 1 is finished as soon as testing starts
                sink.timeline("wave1_finished", now)
        if test.nodeid in self.wave1_pending:
            self.wave1_pending.discard(test.nodeid)
            if not self.wave1_pending:
                sink.timeline("wave1_finished", now)
        self._progress(force=outcome in FAILED)
        if outcome in FAILED and test.nodeid not in plan.quarantined:
            self._on_failure(state, test)

    def _test_payload(self, test: TestState, *, flaky: bool, retry: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "nodeid": test.nodeid,
            "outcome": test.outcome,
            "duration_s": round(test.duration_s or 0.0, 6),
            "failed_phase": test.failed_phase,
            "longrepr": test.longrepr,
            "shard": test.shard_id,
            "flaky": flaky,
        }
        if retry is not None:
            payload["retry"] = retry
        return payload

    def _progress(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self.last_progress < PROGRESS_INTERVAL_S:
            return
        self.last_progress = now
        plan, sink = self.plan, self.sink
        assert plan is not None and sink is not None
        sink.event(
            "progress",
            {
                "done": self.counts.total(),
                "total": len(plan.collected),
                "passed": self.counts["passed"],
                "failed": self.counts["failed"] + self.counts["error"],
                "shards": len(plan.shards),
                "elapsed_s": round(now - self.started, 3),
            },
        )

    # -- failures, retries, fail-fast -------------------------------------------------------------

    def _on_failure(self, state: _ShardState, test: TestState) -> None:
        if self.cancel_reason is not None:
            return
        if self.retries_used < self.settings.retry_budget_per_run and not self.stop_requested:
            self.retries_used += 1
            task = asyncio.get_running_loop().create_task(self._retry(state, test, self.retries_used))
            self.tasks.append(task)
        else:
            self._confirm(test, retried=False)

    async def _retry(self, state: _ShardState, test: TestState, n: int) -> None:
        plan, sink, run = self.plan, self.sink, self.run
        assert plan is not None and sink is not None and run is not None
        worker = state.spec.worker
        nonce = secrets.token_hex(16)
        prefix = f"{RECORD_SEPARATOR}coverset {nonce} "
        captured: dict[str, Any] = {}

        def on_line(text: str, stream: str) -> None:
            if stream == "stdout" and text.startswith(prefix):
                with contextlib.suppress(ValueError):
                    payload = json.loads(text[len(prefix) :])
                    if (
                        isinstance(payload, dict)
                        and payload.get("kind") == "test"
                        and payload.get("nodeid") == test.nodeid
                        and not captured
                    ):
                        captured.update(payload)

        result: RunResult | None = None
        try:
            await worker.write_file(LAYOUT.retry_args(n), (test.nodeid + "\n").encode("utf-8"))
            cmd = [
                *self.ctx.adapter.test_command(
                    worker.path(LAYOUT.retry_args(n)),
                    None,
                    worker.path(LAYOUT.retry_dir(n)),
                    self._extra(plan, worker.path, shapes="off"),
                ),
                f"--rootdir={worker.path(REPO_DIR)}",
            ]
            env = worker_env(
                worker, plan.config, nonce, retry_env(run.extra_env or {}, run.retry_increment_keys or [])
            )
            result = await worker.run(
                cmd,
                cwd=REPO_DIR,
                env=env,
                timeout_s=min(RETRY_TIMEOUT_S, self._remaining_s()),
                on_line=on_line,
                stdout_cap=self.settings.max_stdout_bytes,
            )
            state.exec_seconds += result.seconds
        except ExecutorError as exc:
            log.warning("retry %d of %s failed to run: %s", n, test.nodeid, exc)
        parsed = _parse_test(captured) if captured else None
        outcome = parsed[0] if parsed else "error"
        duration = parsed[1] if parsed else (result.seconds if result else 0.0)
        test_id = plan.test_ids[test.nodeid]
        sink.attempt(
            test_id, 2, outcome=outcome, duration_ms=round(duration * 1000, 3), shard_id=state.spec.shard_id
        )
        test.attempts = 2
        if self.cancel_reason is not None:
            return
        if outcome in ("passed", "xpassed"):
            self.counts[test.outcome] -= 1
            self.counts["passed"] += 1
            test.outcome, test.flaky = "passed", True
            sink.result(test_id, outcome="passed", flaky=True, attempts=2)
            sink.event("test", self._test_payload(test, flaky=True, retry=n))
            self._progress(force=True)
        else:
            sink.result(test_id, attempts=2)
            self._confirm(test, retried=True, note=None if parsed else "the retry produced no result")

    def _confirm(self, test: TestState, *, retried: bool, note: str | None = None) -> None:
        plan, sink = self.plan, self.sink
        assert plan is not None and sink is not None
        test.confirmed = True
        sig = _sig_bytes(test.fail_sig)
        test.preexisting = sig is not None and sig in self.base_sigs
        sink.event(
            "failure",
            {
                "nodeid": test.nodeid,
                "confirmed": True,
                "retried": retried,
                "preexisting": test.preexisting,
                "rank": plan.ranks.get(test.nodeid),
                "note": note,
            },
        )
        if test.preexisting or self.first_failure is not None:
            return
        self.first_failure = test.nodeid
        now = self.settings.now()
        assert self.run is not None
        t0 = t0_of(self.run)
        sandbox_ready_s = (plan.sandbox_ready_at - t0).total_seconds()
        estimate = file_order_estimate(plan, test.nodeid, sandbox_ready_s)
        self.run.rank_coverset = plan.ranks.get(test.nodeid)
        self.run.rank_file_order = plan.file_ranks.get(test.nodeid)
        self.run.t_first_failure_file_order_est = estimate
        self.run.timeline["first_failure"] = now.isoformat()
        sink.timeline("first_failure", now)
        sink.run_fields(
            rank_coverset=self.run.rank_coverset,
            rank_file_order=self.run.rank_file_order,
            t_first_failure_file_order_est=estimate,
        )
        self.early_verdict = asyncio.get_running_loop().create_task(self._post_early_verdict(test))
        self.tasks.append(self.early_verdict)
        if plan.config.fail_fast == "cancel_remaining":
            self.tasks.append(asyncio.get_running_loop().create_task(self._stop_remaining()))

    async def _post_early_verdict(self, test: TestState) -> None:
        sink = self.sink
        assert sink is not None and self.plan is not None
        await sink.flush()
        reason = f"first confirmed failure: {test.nodeid}"
        now = self.settings.now()
        async with self.lock, self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            assert run is not None
            run.timeline["verdict"] = now.isoformat()
            summary = build_summary(
                self.settings, run, self.plan, self.states, conclusion="failure", reason=reason
            )
            await persist_verdict(session, run, summary)
            await session.commit()  # the fake forge records its calls through its own session
            await post_summary(session, run, summary, self.ctx.get_forge())
            await session.commit()
        sink.event(
            "verdict", {"conclusion": "failure", "reason": reason, "label": summary.label, "early": True}
        )

    async def _stop_remaining(self) -> None:
        """``cancel_remaining``: STOP files, up to ``stop_wait_s`` for bundles, then cancel."""
        self.stop_requested = True
        running = [s for s in self.shard_states.values() if not s.finished.is_set()]
        for state in running:
            with contextlib.suppress(ExecutorError):
                await state.spec.worker.write_file(LAYOUT.stop_file, b"stop\n")
        waits = [asyncio.create_task(s.bundle_written.wait()) for s in running]
        if waits:
            _, pending = await asyncio.wait(waits, timeout=self.ctx.stop_wait_s)
            for task in pending:
                task.cancel()
        for state in running:
            if not state.finished.is_set():
                with contextlib.suppress(ExecutorError):
                    await state.spec.worker.cancel()

    async def _watch_cancel(self) -> None:
        """Cancel the run when someone marks it cancelled or a newer run of the same PR is queued."""
        while True:
            await asyncio.sleep(self.ctx.cancel_poll_s)
            reason = await self._cancel_requested()
            if reason is None:
                continue
            self.cancel_reason = reason
            for state in self.shard_states.values():
                with contextlib.suppress(ExecutorError):
                    await state.spec.worker.cancel()
            return

    async def _cancel_requested(self) -> str | None:
        async with self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            if run is None:
                return "run deleted"
            if run.status == "cancelled":
                return run.reason or "cancelled"
            if not self.settings.cancel_superseded or run.pr_id is None:
                return None
            newer = await session.scalar(
                select(Run.id)
                .where(
                    Run.pr_id == run.pr_id,
                    Run.id != run.id,
                    Run.created_at > run.created_at,
                    Run.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            return f"superseded by run {newer}" if newer is not None else None

    # -- bundles and the verdict -----------------------------------------------------------------

    async def _collect_bundles(self, plan: RunPlan) -> list[IngestOutcome]:
        """Pull every shard's bundle, release the workers, then convert and ingest concurrently."""
        target = self.settings.runs_dir / self.run_id
        target.mkdir(parents=True, exist_ok=True)
        pulled: dict[int, Path] = {}
        for shard_id, state in self.shard_states.items():
            try:
                data = await state.spec.worker.get_file(f"{LAYOUT.out_dir}/{BUNDLE_FILE}")
            except ExecutorError as exc:
                state.error = state.error or f"no bundle: {exc}"
                continue
            if len(data) > self.settings.max_bundle_bytes:
                state.error = state.error or f"bundle larger than {self.settings.max_bundle_bytes} bytes"
                continue
            path = target / f"shard-{shard_id}.tar.gz"
            await asyncio.to_thread(path.write_bytes, data)
            pulled[shard_id] = path
        await plan.release_workers()
        rctx = await self._run_context(plan)
        interest = functions_of_interest(plan)
        jobs = [
            IngestJob(
                facts_dir=str(self.ctx.store.root),
                ctx=rctx,
                shard_id=shard_id,
                tarball=str(path),
                limits=bundle_limits(self.settings),
                source_root=str(plan.checkout),
                head_paths=plan.head_paths,
                functions=tuple(plan.head_functions),
                keep_paths=frozenset(plan.changed_files),
                keep_funcs=frozenset(r.triple for r in interest.values()),
                language=self.ctx.adapter.name,
            )
            for shard_id, path in sorted(pulled.items())
        ]
        outcomes = await self.ctx.ingest_pool().run(jobs) if jobs else []
        for shard_id, state in self.shard_states.items():
            if shard_id not in pulled:
                await asyncio.to_thread(
                    record_shard_failure, self.ctx.store, rctx, shard_id, reason=state.error or "no bundle"
                )
        for outcome in outcomes:
            if not outcome.ok:
                self.shard_states[outcome.shard_id].error = self.shard_states[outcome.shard_id].error or (
                    f"bundle rejected: {outcome.error}"
                )
        return outcomes

    async def _run_context(self, plan: RunPlan) -> RunContext:
        assert self.run is not None
        async with self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            assert run is not None
            pr_number = None
            if run.pr_id is not None:
                pr = await session.get(PullRequest, run.pr_id)
                pr_number = pr.number if pr is not None else None
        return RunContext(
            repo_id=plan.repo_id,
            run_id=self.run_id,
            head_sha=plan.head_sha,
            base_sha=plan.base_sha,
            branch=run.branch,
            pr_number=pr_number,
            created_at=run.created_at,
            scoring_basis_run_id=plan.prior.run_id if plan.prior else None,
        )

    def _reconcile(self, plan: RunPlan, outcomes: list[IngestOutcome]) -> None:
        """``results.jsonl`` is authoritative for attempt 1; retried tests keep their retry verdict."""
        sink = self.sink
        assert sink is not None
        for outcome in outcomes:
            for row in outcome.tests:
                test = self.states.get(row.nodeid)
                if test is None:
                    continue
                test_id = plan.test_ids[row.nodeid]
                if test.attempts >= 2:
                    continue
                changed = (row.outcome, row.fail_sig) != (test.outcome, test.fail_sig) or test.attempts == 0
                if not changed:
                    continue
                if test.attempts:
                    self.counts[test.outcome] -= 1
                self.counts[row.outcome] += 1
                test.outcome, test.duration_s = row.outcome, row.duration_s
                test.failed_phase, test.fail_sig = row.failed_phase, row.fail_sig
                test.longrepr = (row.longrepr or "")[:MAX_LONGREPR_CHARS] or None
                test.shard_id, test.attempts = outcome.shard_id, 1
                sink.result(
                    test_id,
                    outcome=row.outcome,
                    failed_phase=row.failed_phase if row.failed_phase in PHASES else None,
                    fail_sig=_sig_bytes(row.fail_sig),
                    duration_ms=round(row.duration_s * 1000, 3),
                    shard_id=outcome.shard_id,
                    attempts=1,
                )
                sink.attempt(
                    test_id,
                    1,
                    outcome=row.outcome,
                    duration_ms=round(row.duration_s * 1000, 3),
                    shard_id=outcome.shard_id,
                )
                if row.outcome in FAILED and row.nodeid not in plan.quarantined and not test.confirmed:
                    self._confirm(test, retried=False, note="reported only in the bundle")

    async def _conclude(self, plan: RunPlan, outcomes: list[IngestOutcome], executor: Executor) -> None:
        sink = self.sink
        assert sink is not None
        self._reconcile(plan, outcomes)
        if self.early_verdict is not None:
            await asyncio.gather(self.early_verdict, return_exceptions=True)
        rctx = await self._run_context(plan)
        catalogue = _catalogue(self.ctx.adapter, plan.measured_sources)
        finalized = await asyncio.to_thread(
            finalize_run, self.ctx.store, rctx, expected_shards=len(plan.shards), catalogue=catalogue
        )
        await sink.flush()
        conclusion, status, reason = self._conclusion(plan)
        cost = sum(
            executor.cost_usd(plan.resources, state.exec_seconds) for state in self.shard_states.values()
        )
        not_run = sum(1 for t in self.states.values() if t.outcome == "not_run")
        async with self.lock, self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            assert run is not None
            report: RunReport = await build_report(
                session, self.settings, self.ctx.store, run, plan, outcomes, self.states, catalogue=catalogue
            )
            await history.update_test_stats(session, run, list(plan.test_ids.values()))
            run.coverage_complete = finalized.coverage_complete
            run.cost_estimate = round(cost, 6)
            run.cancelled_tests = not_run
            now = self.settings.now()
            run.timeline.setdefault("verdict", now.isoformat())
            run.timeline["finished"] = now.isoformat()
            run.status = status
            summary = build_summary(
                self.settings, run, plan, self.states, conclusion=conclusion, reason=reason, report=report
            )
            await persist_verdict(session, run, summary)
            await session.commit()
            await post_summary(session, run, summary, self.ctx.get_forge())
            await session.commit()
        sink.event("verdict", {"conclusion": conclusion, "reason": reason, "label": summary.label})
        sink.event(
            "done",
            {"conclusion": conclusion, "exit_code": self._exit_code(conclusion), "counts": self._counts()},
        )

    def _conclusion(self, plan: RunPlan) -> tuple[str, str, str]:
        """``(conclusion, status, reason)`` from confirmed failures, shard errors and outcomes."""
        confirmed = [t for t in self.states.values() if t.confirmed and not t.preexisting]
        preexisting = [t for t in self.states.values() if t.preexisting]
        flaky = [t for t in self.states.values() if t.flaky]
        errors = [f"shard {k}: {s.error}" for k, s in sorted(self.shard_states.items()) if s.error]
        executed = sum(1 for t in self.states.values() if t.outcome != "not_run")
        notes = []
        if flaky:
            notes.append(f"{len(flaky)} flaky")
        if preexisting:
            notes.append(f"{len(preexisting)} pre-existing failure{'s' if len(preexisting) != 1 else ''}")
        notes += errors
        notes += self._exit_code_mismatches()
        suffix = f" ({'; '.join(notes)})" if notes else ""
        if confirmed:
            noun = "failure" if len(confirmed) == 1 else "failures"
            return "failure", "error" if errors else "completed", f"{len(confirmed)} confirmed {noun}{suffix}"
        if errors:
            return "neutral", "error", f"incomplete run{suffix}"
        if executed == 0:
            return "neutral", "completed", f"no tests ran{suffix}"
        return "success", "completed", f"all {executed} tests passed{suffix}"

    def _exit_code_mismatches(self) -> list[str]:
        """Shards whose pytest exit code disagrees with the outcomes they reported."""
        notes = []
        for shard_id, state in sorted(self.shard_states.items()):
            if state.result is None or state.result.exit_code not in (0, 1):
                continue
            failed = any(
                t.shard_id == shard_id and t.attempts >= 1 and (t.outcome in FAILED or t.flaky)
                for t in self.states.values()
            )
            if failed != (state.result.exit_code == 1):
                notes.append(
                    f"shard {shard_id} exit code {state.result.exit_code} disagrees with its results"
                )
        return notes

    def _counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("passed", "failed", "error", "skipped", "xfailed", "xpassed", "not_run"), 0)
        for test in self.states.values():
            counts[test.outcome] = counts.get(test.outcome, 0) + 1
        return counts

    @staticmethod
    def _exit_code(conclusion: str) -> int:
        return EXIT_CODES.get(conclusion, 2)

    async def _finish(
        self, status: str, conclusion: str, reason: str, *, exit_code: int | None = None
    ) -> None:
        """Terminal bookkeeping for every early exit: status, verdict, forge post, ``done`` event."""
        sink = self.sink
        assert sink is not None
        with contextlib.suppress(Exception):
            await sink.flush()
        now = self.settings.now()
        summary = None
        async with self.lock, self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            if run is None:
                return
            run.status = status  # type: ignore[assignment]
            run.timeline.setdefault("verdict", now.isoformat())
            run.timeline["finished"] = now.isoformat()
            summary = build_summary(
                self.settings, run, self.plan, self.states, conclusion=conclusion, reason=reason
            )
            await persist_verdict(session, run, summary)
            await session.commit()
            with contextlib.suppress(Exception):
                await post_summary(session, run, summary, self.ctx.get_forge())
                await session.commit()
        sink.event("verdict", {"conclusion": conclusion, "reason": reason, "label": summary.label})
        code = (
            exit_code
            if exit_code is not None
            else (2 if status in ("error", "cancelled") else self._exit_code(conclusion))
        )
        sink.event("done", {"conclusion": conclusion, "exit_code": code, "counts": self._counts()})


def _app_forge(app: Any) -> Any:
    """The app's own forge adapter (shared with the webhook routes), or None to resolve lazily."""
    try:
        from coverset.forge.registry import forge_for_app
    except ImportError:
        return app.extensions.get("forge")
    try:
        return forge_for_app(app)
    except Exception as exc:  # misconfigured forge: verdicts are stored but not posted
        log.warning("no forge for posting verdicts: %s", exc)
        return False


def _catalogue(adapter: LanguageAdapter, sources: Mapping[str, str]) -> Any:
    try:
        return adapter.shape_catalogue(sources)  # type: ignore[call-arg]
    except TypeError:
        return adapter.shape_catalogue()


def _parse_test(payload: Mapping[str, Any]) -> tuple[str, float, str | None, str | None, str | None] | None:
    """Validate a ``test`` event: ``(outcome, duration_s, failed_phase, fail_sig, longrepr)``."""
    outcome = payload.get("outcome")
    if outcome not in OUTCOMES:
        return None
    duration = payload.get("duration_s")
    seconds = float(duration) if isinstance(duration, int | float) and not isinstance(duration, bool) else 0.0
    if not 0 <= seconds < 1e7:
        seconds = 0.0
    phase = payload.get("failed_phase")
    sig = payload.get("fail_sig")
    longrepr = payload.get("longrepr")
    return (
        str(outcome),
        seconds,
        phase if phase in PHASES else None,
        sig if isinstance(sig, str) and _sig_bytes(sig) is not None else None,
        longrepr[:MAX_LONGREPR_CHARS] if isinstance(longrepr, str) else None,
    )


def _sig_bytes(sig: str | None) -> bytes | None:
    if not sig or len(sig) != 16:
        return None
    try:
        return bytes.fromhex(sig)
    except ValueError:
        return None
