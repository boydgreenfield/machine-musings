"""Executing a run: shards, streamed events, retries, fail-fast, ingest and the verdict.

:func:`execute_run` drives one ``queued`` run to a terminal status:

1. **Claim and policy.** The run becomes ``running`` with ``claimed_by`` set to this process (inline
   CLI runs are inserted that way) and a ``heartbeat_at`` refreshed every 10 s; the cancel watcher
   starts. Untrusted runs and (unless allowed) webhook-triggered runs never use the local executor;
   Modal runs stop at the daily budget, and an admitted Modal run reserves its predicted cost in
   ``cost_estimate`` until its real cost replaces it. An admitted PR run with a forge gets its
   ``coverset`` check created ``in_progress`` (:func:`coverset.forge.verdict.start_check`,
   ``external_id`` = the run id, ``details_url`` = the run page) before planning; the id is stored in
   ``runs.check_run_id`` so the verdict completes that same check.
2. **Plan** (:func:`coverset.orchestrator.planner.plan_run`): workers hold the source and their
   shard files; ``timeline.sandbox_ready`` is set. Planning never holds the execution lock or a
   write transaction, so the run's events keep flowing; a cancellation interrupts it.
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
   ``rank_coverset`` (planned position) and ``rank_executed`` (how many tests had reported when it
   did); with ``fail_fast = "cancel_remaining"`` the shards then get STOP files, up to
   20 s to write their bundles, and are cancelled; with ``finish_in_background`` (the default) they
   run on so the facts land. A newer queued run for the same PR cancels this one
   (``cancel_superseded``).
5. **Ingest and verdict.** Bundles are pulled before the workers are released, converted and
   ingested concurrently, the run is finalized, ``results.jsonl`` overrides streamed outcomes, then
   the patch report, input classes, invariants, AI review, ``test_stats`` and cost feed the final
   verdict. The status change, the verdict and the ``verdict`` and ``done`` events are written in
   one transaction (retried with backoff while the database is busy), then the verdict is posted.
   A shard whose ``meta.json`` names a tracer other than coverage.py's C tracer gets a ``log``
   event saying per-test contexts may be incomplete (``tracer_mismatch`` in its ``ingest_log``).
6. **Retention.** After a PR run, :func:`coverset.orchestrator.history.expire_pr_runs` applies the
   per-PR rule (outside ``COVERSET_ENV=test``).

Every failure path ends the run as ``error`` or ``cancelled`` with a reason and a ``done`` event, so
SSE clients and ``coverset-ci wait`` always finish; if even that write fails,
:func:`finalize_crashed` (also called by the dispatcher and the CLI for a crashed task) marks the run
``error`` with ``verdict``/``done`` events. ``done.exit_code`` is 0 for success, 1 for a failure, 5
when no tests were collected and 2 otherwise (error, cancelled, neutral).

``cost_estimate`` is ``Σ executor.cost_usd(resources, seconds each worker was held)``, from acquire
to release (see :class:`~coverset.orchestrator.planner.WorkerLifetimes`), on every terminal path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import ExecutorKind, Settings
from coverset.db.models import (
    PullRequest,
    Repo,
    Run,
    RunEvent,
    Shard,
    TestAttempt,
    TestResult,
    WebhookDelivery,
)
from coverset.db.session import Database
from coverset.executors.base import Executor, ExecutorError, Resources, RunResult
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
    WRITE_ATTEMPTS,
    WRITE_RETRY_DELAY_S,
    EnvSpecBuilder,
    PlanError,
    RunPlan,
    ShardSpec,
    WorkerLifetimes,
    plan_run,
    run_transaction,
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
    "exit_code_for",
    "finalize_crashed",
    "process_claimant",
    "retry_env",
]

log = logging.getLogger(__name__)

RECORD_SEPARATOR = "\x1e"
OUTCOMES = frozenset({"passed", "failed", "error", "skipped", "xfailed", "xpassed"})
FAILED = frozenset({"failed", "error"})
PHASES = frozenset({"setup", "call", "teardown"})
EXIT_CODES = {"success": 0, "failure": 1, "neutral": 2, "cancelled": 2, "timed_out": 2, "error": 2}
"""``done.exit_code`` per conclusion (pytest's codes, as ``coverset-ci wait`` exits)."""
NO_TESTS_EXIT_CODE = 5
HEARTBEAT_S = 10.0
"""How often a running run refreshes ``runs.heartbeat_at``."""
PRE_PLAN_RESERVE_S = 600.0
"""Worker-seconds a budgeted run reserves before it is planned (one worker for ten minutes)."""
PER_SHARD_OVERHEAD_S = 30.0
"""Seconds added to each shard's predicted duration when the reservation is re-estimated."""
MAX_LOG_LINES_PER_SHARD = 2000
MAX_LOG_CHARS = 2000
MAX_LONGREPR_CHARS = 8 * 1024
PROGRESS_INTERVAL_S = 0.25
RETRY_TIMEOUT_S = 600.0
BUNDLE_FILE = "bundle.tar.gz"

type EventCallback = Callable[[str, dict[str, Any]], None]


# -- context --------------------------------------------------------------------------------------


def process_claimant(role: str) -> str:
    """``claimed_by`` of runs this process executes: ``<role>@<host>:<pid>:<nonce>``.

    ``role`` is ``serve`` for the web app's dispatcher and ``cli`` for inline runs; at boot, ``serve``
    abandons runs a previous ``serve`` on the same host claimed, runs whose claiming process is gone,
    and runs whose heartbeat is stale, never a live CLI run.
    """
    host = socket.gethostname()[:64] or "localhost"
    return f"{role}@{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


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
    claimant: str = field(default_factory=lambda: process_claimant("cli"))
    """This process's ``runs.claimed_by`` value."""
    budget_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serialises the budget check and reservation of concurrent runs in this process."""
    heartbeat_s: float = HEARTBEAT_S

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
            existing = cls(
                settings=settings,
                db=app.extensions["db"],
                store=FactsStore(settings.facts_dir),
                claimant=process_claimant("serve"),
            )
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


def exit_code_for(status: str, conclusion: str, override: int | None = None) -> int:
    """``done.exit_code``: 1 for any failure, 2 for error/cancelled/abandoned/neutral, 0 for success."""
    if override is not None:
        return override
    if conclusion == "failure":
        return 1
    if status in ("error", "cancelled", "abandoned"):
        return 2
    return EXIT_CODES.get(conclusion, 2)


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
    claim: bool = False,
) -> str:
    """Insert a run (and find or create its pull request); return the run id.

    The run is ``queued`` for the dispatcher, or with ``claim`` already ``running`` and claimed by
    this context (an inline run: no dispatcher can pick it up, and :func:`execute_run` in this
    process starts it).
    """
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
            status="running" if claim else "queued",
            claimed_by=ctx.claimant if claim else None,
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


async def finalize_crashed(
    db: Database, settings: Settings, run_id: str, reason: str, *, after_seq: int = 0
) -> bool:
    """Last resort for a run whose execution died: ``error`` with ``verdict`` and ``done`` events.

    Does nothing (returns False) when the run already reached a terminal status. ``after_seq`` is
    the highest event sequence number the dead execution may have handed out.
    """

    async def work(session: AsyncSession) -> bool:
        run = await session.get(Run, run_id)
        if run is None or run.status not in ("queued", "running"):
            return False
        now = settings.now()
        run.status = "error"
        run.verdict = "neutral"
        run.reason = reason[:1000]
        timeline = dict(run.timeline or {})
        timeline.setdefault("verdict", now.isoformat())
        timeline["finished"] = now.isoformat()
        run.timeline = timeline
        seq = max(after_seq, int(await session.scalar(_max_seq_query(run_id)) or 0))
        session.add_all(
            [
                RunEvent(
                    run_id=run_id,
                    seq=seq + 1,
                    kind="verdict",
                    payload={"conclusion": "neutral", "reason": run.reason},
                    at=now,
                ),
                RunEvent(
                    run_id=run_id,
                    seq=seq + 2,
                    kind="done",
                    payload={"conclusion": "neutral", "exit_code": 2, "counts": {}},
                    at=now,
                ),
            ]
        )
        return True

    return await run_transaction(db, work)


def _max_seq_query(run_id: str) -> Any:
    return select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)


# -- batched writes -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Batch:
    events: list[RunEvent]
    results: dict[int, dict[str, Any]]
    attempts: dict[tuple[int, int], dict[str, Any]]
    run_fields: dict[str, Any]
    timeline: dict[str, str]
    shards: dict[int, dict[str, Any]]


class EventSink:
    """Batches ``run_events``, ``test_results``/``test_attempts`` and run/shard field updates.

    Writes are flushed every ``interval_s`` or as soon as ``max_batch`` rows are pending, under the
    execution's database lock (so a concurrent verdict write never loses a timeline key), as bulk
    statements (one per table). :meth:`transact` writes whatever is pending plus a caller's own
    work in one transaction, retried with backoff while the database is busy. Event payload strings
    are redacted before they are stored.
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

    @property
    def seq(self) -> int:
        """The last sequence number handed out."""
        return self._seq

    def start(self) -> None:
        """Start the periodic flusher."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._pump())

    def event(self, kind: str, payload: Mapping[str, Any]) -> int:
        """Queue one run event; returns its sequence number."""
        row, clean = self.event_row(kind, payload)
        self._events.append(row)
        self.notify(kind, clean)
        self._maybe_wake()
        return row.seq

    def event_row(self, kind: str, payload: Mapping[str, Any]) -> tuple[RunEvent, dict[str, Any]]:
        """A run event with the next sequence number, for the caller to add to its own transaction.

        Call :meth:`notify` with the returned payload once that transaction commits.
        """
        self._seq += 1
        clean = _redacted(dict(payload))
        return RunEvent(run_id=self._run_id, seq=self._seq, kind=kind, payload=clean, at=self._now()), clean

    def notify(self, kind: str, payload: dict[str, Any]) -> None:
        """Hand a stored event to ``on_event`` (errors there never reach the run)."""
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(kind, payload)

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

    def _take(self) -> _Batch | None:
        if not (
            self._events or self._results or self._attempts or self._run or self._timeline or self._shards
        ):
            return None
        batch = _Batch(self._events, self._results, self._attempts, self._run, self._timeline, self._shards)
        self._events, self._results, self._attempts = [], {}, {}
        self._run, self._timeline, self._shards = {}, {}, {}
        return batch

    async def flush(self) -> None:
        """Write everything pending in one transaction (one attempt; failed rows are kept)."""
        if self._take_peek():
            await self._transact_once(None)

    def _take_peek(self) -> bool:
        return bool(
            self._events or self._results or self._attempts or self._run or self._timeline or self._shards
        )

    async def transact[T](
        self, work: Callable[[AsyncSession], Awaitable[T]], *, attempts: int = WRITE_ATTEMPTS
    ) -> T:
        """Write what is pending plus ``work(session)`` in one transaction; retry while busy.

        ``work`` must be safe to repeat (a failed attempt is rolled back and the pending rows are
        put back first).
        """
        from sqlalchemy.exc import OperationalError

        for attempt in range(1, attempts + 1):
            try:
                return await self._transact_once(work)
            except OperationalError:
                if attempt == attempts:
                    raise
                log.warning("run %s: database busy (attempt %d of %d)", self._run_id, attempt, attempts)
                await asyncio.sleep(WRITE_RETRY_DELAY_S * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    async def _transact_once(self, work: Callable[[AsyncSession], Awaitable[Any]] | None) -> Any:
        async with self._lock, self._db.session() as session:
            batch = self._take()
            try:
                if batch is not None:
                    await self._write(session, batch)
                result = await work(session) if work is not None else None
                await session.commit()
            except BaseException:
                with contextlib.suppress(Exception):
                    await session.rollback()
                if batch is not None:
                    self._requeue(batch)
                raise
        return result

    async def _write(self, session: AsyncSession, batch: _Batch) -> None:
        session.add_all(batch.events)
        if batch.results:
            await session.execute(
                update(TestResult),
                [{"run_id": self._run_id, "test_id": tid, **values} for tid, values in batch.results.items()],
            )
        if batch.attempts:
            await _upsert_attempts(
                session,
                [
                    {"run_id": self._run_id, "test_id": tid, "attempt": number, **values}
                    for (tid, number), values in batch.attempts.items()
                ],
            )
        if batch.shards:
            await session.execute(
                update(Shard),
                [{"run_id": self._run_id, "shard_id": sid, **values} for sid, values in batch.shards.items()],
            )
        if batch.run_fields or batch.timeline:
            run = await session.get(Run, self._run_id)
            if run is not None:
                for name, value in batch.run_fields.items():
                    setattr(run, name, value)
                if batch.timeline:
                    run.timeline = {**(run.timeline or {}), **batch.timeline}

    def _requeue(self, batch: _Batch) -> None:
        """Put the rows of a failed flush back in front of anything queued since."""
        fresh = [
            RunEvent(run_id=e.run_id, seq=e.seq, kind=e.kind, payload=e.payload, at=e.at)
            for e in batch.events
        ]
        self._events = fresh + self._events
        for test_id, values in batch.results.items():
            self._results[test_id] = {**values, **self._results.get(test_id, {})}
        self._attempts = {**batch.attempts, **self._attempts}
        self._run = {**batch.run_fields, **self._run}
        self._timeline = {**batch.timeline, **self._timeline}
        for shard_id, values in batch.shards.items():
            self._shards[shard_id] = {**values, **self._shards.get(shard_id, {})}

    async def close(self) -> None:
        """Stop the flusher and write what is left."""
        self._closed = True
        self._wake.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None
        if self._take_peek():
            await self.transact(_nothing)


async def _nothing(session: AsyncSession) -> None:
    return None


async def _upsert_attempts(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Insert or replace ``test_attempts`` rows in one statement (per distinct column set)."""
    await history.upsert_rows(session, TestAttempt, rows, ("run_id", "test_id", "attempt"))


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
    shard count. A run created with ``create_run(..., claim=True)`` by the same context is executed
    too (once).
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
    line_errors: int = 0


class _Cancelled(Exception):
    """The run was cancelled (externally or superseded) while it was planned or its shards ran."""


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
        self.executor: Executor | None = None
        self.lifetimes = WorkerLifetimes()
        self.states: dict[str, TestState] = {}
        self.shard_states: dict[int, _ShardState] = {}
        self.counts: Counter[str] = Counter()
        self.retries_used = 0
        self.tasks: list[asyncio.Task[Any]] = []
        self.plan_task: asyncio.Task[RunPlan] | None = None
        self.watcher: asyncio.Task[None] | None = None
        self.heartbeat: asyncio.Task[None] | None = None
        self.first_failure: str | None = None
        self.early_verdict: asyncio.Task[None] | None = None
        self.stop_requested = False
        self.cancel_reason: str | None = None
        self.released = False
        """The workers are gone (bundles pulled): nothing may be sent to them any more."""
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
        sink = self.sink = EventSink(
            self.ctx.db,
            self.run_id,
            lock=self.lock,
            now=self.settings.now,
            start_seq=await self._max_seq(),
            on_event=self.on_event,
        )
        sink.start()
        sink.event("status", {"status": "running", "executor": run.executor})
        loop = asyncio.get_running_loop()
        self.watcher = loop.create_task(self._watch_cancel())
        self.heartbeat = loop.create_task(self._heartbeat())
        crash: str | None = None
        try:
            try:
                refusal = await self._policy(run)
                if refusal is not None:
                    await self._finish(*refusal)
                else:
                    await self._start_check(run)
                    await self._execute_plan(run)
            except _Cancelled:
                await self._finish("cancelled", "cancelled", self.cancel_reason or "cancelled")
            except PlanError as exc:
                await self._finish("error", "neutral", f"{exc.reason}: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("run %s failed", self.run_id)
                await self._finish("error", "neutral", f"internal error: {type(exc).__name__}: {exc}"[:1000])
        except asyncio.CancelledError:
            raise  # the process is stopping: the run is abandoned at the next boot
        except Exception as exc:  # even the terminal write failed: the last resort below
            log.exception("finishing run %s failed", self.run_id)
            crash = f"internal error: {type(exc).__name__}: {exc}"
        finally:
            await self._stop_background()
            if self.plan is not None:
                with contextlib.suppress(Exception):
                    await self.plan.cleanup()
            try:
                await sink.close()
            except Exception:
                log.exception("closing the event sink of run %s failed", self.run_id)
            await asyncio.to_thread(shutil.rmtree, self.settings.runs_dir / self.run_id, True)
        if crash is not None:
            with contextlib.suppress(Exception):
                await finalize_crashed(self.ctx.db, self.settings, self.run_id, crash, after_seq=sink.seq)
        if run.pr_id is not None and self.settings.env != "test":
            await self._expire_pr_runs(run.pr_id)
        return await self._outcome()

    async def _expire_pr_runs(self, pr_id: int) -> None:
        """The per-PR retention rule (best effort: retention never fails a run)."""
        try:
            expired = await history.expire_pr_runs(self.ctx.db, self.settings, self.ctx.store, pr_id)
        except Exception:
            log.exception("per-PR gc after run %s failed", self.run_id)
            return
        if expired:
            log.info("per-PR gc after run %s deleted %d run(s)", self.run_id, len(expired))

    async def _stop_background(self) -> None:
        tasks = [t for t in (self.watcher, self.heartbeat, self.plan_task, *self.tasks) if t is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _claim(self) -> Run | None:
        """Atomically make the run ``running`` and ours; None if someone else has it (or had it).

        A ``queued`` run is claimed; so is a ``running`` run this context inserted with
        ``create_run(claim=True)`` and nobody started yet (no heartbeat).
        """
        now = self.settings.now()

        async def work(session: AsyncSession) -> int:
            claimed = await session.execute(
                update(Run)
                .where(
                    Run.id == self.run_id,
                    (Run.status == "queued")
                    | (
                        (Run.status == "running")
                        & (Run.claimed_by == self.ctx.claimant)
                        & Run.heartbeat_at.is_(None)
                    ),
                )
                .values(status="running", claimed_by=self.ctx.claimant, heartbeat_at=now)
            )
            return int(claimed.rowcount)  # type: ignore[attr-defined]

        rowcount = await run_transaction(self.ctx.db, work)
        async with self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
        if run is None:
            raise LookupError(f"no run {self.run_id}")
        return run if rowcount == 1 else None

    async def _max_seq(self) -> int:
        async with self.ctx.db.session() as session:
            value = await session.scalar(_max_seq_query(self.run_id))
            return int(value or 0)

    async def _outcome(self) -> RunOutcome:
        async with self.ctx.db.session() as session:
            run = await session.get(Run, self.run_id)
            assert run is not None
            return RunOutcome(run.id, run.status, run.verdict, run.reason)

    async def _heartbeat(self) -> None:
        assert self.sink is not None
        while True:
            await asyncio.sleep(self.ctx.heartbeat_s)
            self.sink.run_fields(heartbeat_at=self.settings.now())

    async def _policy(self, run: Run) -> tuple[str, str, str] | None:
        if run.executor == "local" and not run.trusted:
            return "skipped", "neutral", "untrusted PR runs never use the local executor"
        if run.executor == "local" and not self.settings.local_allow_webhook_runs:
            webhook = "webhook_received" in (run.timeline or {})
            if not webhook:
                async with self.ctx.db.session() as session:
                    webhook = (
                        await session.scalar(
                            select(WebhookDelivery.id).where(WebhookDelivery.run_id == run.id).limit(1)
                        )
                        is not None
                    )
            if webhook:
                return (
                    "skipped",
                    "neutral",
                    "webhook runs need COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true locally",
                )
        if run.executor in budget.BUDGETED_EXECUTORS:
            amount = self._pre_plan_reservation(run)
            async with self.ctx.budget_lock:
                admitted = await run_transaction(
                    self.ctx.db,
                    lambda session: budget.reserve(session, self.settings, run.id, run.executor, amount),
                )
            if not admitted:
                return "completed", "neutral", "budget exhausted"
        return None

    async def _start_check(self, run: Run) -> None:
        """Create the ``in_progress`` verdict check of a PR run and store its id (best effort)."""
        forge = self.ctx.get_forge() if run.pr_id is not None and run.check_run_id is None else None
        if forge is None:
            return
        from coverset.forge.verdict import start_check
        from coverset.orchestrator.verdict import repo_ref

        sink = self.sink
        assert sink is not None
        try:
            async with self.ctx.db.session() as session:
                repo = await session.get(Repo, run.repo_id)
            if repo is None:
                return
            check_id = await start_check(
                forge,
                repo_ref(repo),
                run.head_sha,
                external_id=run.id,
                details_url=f"{self.settings.public_url}/runs/{run.id}",
            )
        except Exception as exc:  # the forge never fails a run
            log.warning("starting the check of run %s failed: %s", self.run_id, exc)
            return
        if check_id is None:
            return

        async def work(session: AsyncSession) -> None:
            live = await session.get(Run, self.run_id)
            if live is not None and live.check_run_id is None:
                live.check_run_id = check_id

        await sink.transact(work)
        run.check_run_id = check_id

    def _resources(self, run: Run) -> Resources:
        return Resources(
            cpu=self.settings.shard_cpu_limit,
            memory_mib=self.settings.shard_memory_mib,
            network=run.network or self.settings.sandbox_network,
            timeout_s=self.settings.run_timeout_s,
        )

    def _pre_plan_reservation(self, run: Run) -> float:
        try:
            executor = self.ctx.executor(run.executor)
            seconds = min(float(self.settings.run_timeout_s), PRE_PLAN_RESERVE_S)
            return round(executor.cost_usd(self._resources(run), seconds), 6)
        except Exception as exc:  # no executor yet: the check above still applies
            log.warning("cannot price run %s before planning: %s", run.id, exc)
            return 0.0

    def _cost(self) -> float:
        if self.executor is None or not len(self.lifetimes):
            return 0.0
        try:
            return round(self.lifetimes.cost_usd(self.executor), 6)
        except Exception as exc:  # a broken rate card never blocks the verdict
            log.warning("cannot price run %s: %s", self.run_id, exc)
            return 0.0

    # -- the happy path -------------------------------------------------------------------------

    async def _execute_plan(self, run: Run) -> None:
        sink = self.sink
        assert sink is not None
        if self.cancel_reason is not None:
            raise _Cancelled()
        executor = self.executor = self.ctx.executor(run.executor)
        self.plan_task = asyncio.get_running_loop().create_task(self._plan(run, executor))
        try:
            plan = await self.plan_task
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self.cancel_reason is not None and (task is None or task.cancelling() == 0):
                raise _Cancelled() from None
            raise
        self.plan = plan
        if self.cancel_reason is not None:
            raise _Cancelled()
        if run.executor in budget.BUDGETED_EXECUTORS:
            predicted = self.lifetimes.cost_usd(executor) + sum(
                executor.cost_usd(plan.resources, s.predicted_s + PER_SHARD_OVERHEAD_S) for s in plan.shards
            )
            sink.run_fields(cost_estimate=round(predicted, 6))
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
            await self._finish("completed", "neutral", "no tests collected", exit_code=NO_TESTS_EXIT_CODE)
            return
        self.states = {i.nodeid: TestState(i.nodeid) for i in plan.collected}
        self.wave1_pending = set(plan.wave1.tests)
        await self._load_base_failures(run)
        await self._run_shards(plan)
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        if self.cancel_reason is not None:
            raise _Cancelled()
        outcomes = await self._collect_bundles(plan)
        await self._conclude(plan, outcomes, executor)

    async def _plan(self, run: Run, executor: Executor) -> RunPlan:
        sink = self.sink
        assert sink is not None
        return await plan_run(
            self.ctx.db,
            self.settings,
            self.ctx.store,
            run,
            executor=executor,
            adapter=self.ctx.adapter,
            env_spec_builder=self.ctx.env_spec_builder,
            shards_override=self.shards_override,
            forge=self.ctx.get_forge(),
            on_sandbox_ready=lambda at: sink.timeline("sandbox_ready", at),
            transact=sink.transact,
            lifetimes=self.lifetimes,
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
        if self.cancel_reason is not None:  # cancelled between planning and now
            return
        await asyncio.gather(*(self._run_shard(state) for state in self.shard_states.values()))
        while True:
            pending = [t for t in self.tasks if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)

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
        if not shard.nodeids:  # never exec an empty args file: pytest would run the whole suite
            sink.shard(shard.shard_id, status="completed", finished_at=self.settings.now())
            sink.event("shard", {"shard": shard.shard_id, "status": "completed", "tests": 0})
            state.finished.set()
            state.bundle_written.set()
            return
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
            try:
                if stream == "stdout" and text.startswith(prefix):
                    try:
                        payload = json.loads(text[len(prefix) :])
                    except (ValueError, RecursionError):
                        payload = None
                    if isinstance(payload, dict):
                        self._on_event(state, payload)
                        return
                self._on_log(state, text)
            except Exception:  # one bad line (or a bug handling it) never stops the stream
                state.line_errors += 1
                if state.line_errors == 1:
                    log.exception("run %s shard %d: a line could not be handled", self.run_id, shard.shard_id)

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
        test.exec_index = self.counts.total()
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
        if (
            self.retries_used < self.settings.retry_budget_per_run
            and not self.stop_requested
            and not self.released
        ):
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
            if stream == "stdout" and text.startswith(prefix) and not captured:
                with contextlib.suppress(ValueError, RecursionError):
                    payload = json.loads(text[len(prefix) :])
                    if (
                        isinstance(payload, dict)
                        and payload.get("kind") == "test"
                        and payload.get("nodeid") == test.nodeid
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
                "executed": test.exec_index,
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
        self.run.rank_executed = test.exec_index
        self.run.rank_file_order = plan.file_ranks.get(test.nodeid)
        self.run.t_first_failure_file_order_est = estimate
        self.run.timeline = {**(self.run.timeline or {}), "first_failure": now.isoformat()}
        sink.timeline("first_failure", now)
        sink.run_fields(
            rank_coverset=self.run.rank_coverset,
            rank_executed=self.run.rank_executed,
            rank_file_order=self.run.rank_file_order,
            t_first_failure_file_order_est=estimate,
        )
        self.early_verdict = asyncio.get_running_loop().create_task(self._post_early_verdict(test))
        self.tasks.append(self.early_verdict)
        if plan.config.fail_fast == "cancel_remaining" and not self.released:
            self.tasks.append(asyncio.get_running_loop().create_task(self._stop_remaining()))

    async def _post_early_verdict(self, test: TestState) -> None:
        sink, plan = self.sink, self.plan
        assert sink is not None and plan is not None
        reason = f"first confirmed failure: {test.nodeid}"
        now = self.settings.now()

        async def work(session: AsyncSession) -> tuple[Any, dict[str, Any]]:
            run = await session.get(Run, self.run_id)
            assert run is not None
            run.timeline = {"verdict": now.isoformat(), **(run.timeline or {})}
            summary = build_summary(
                self.settings, run, plan, self.states, conclusion="failure", reason=reason
            )
            await persist_verdict(session, run, summary)
            row, clean = sink.event_row(
                "verdict", {"conclusion": "failure", "reason": reason, "label": summary.label, "early": True}
            )
            session.add(row)
            return summary, clean

        summary, clean = await sink.transact(work)
        sink.notify("verdict", clean)
        await self._post(summary)

    async def _post(self, summary: Any) -> None:
        """Post ``summary`` through the forge (best effort; the run row keeps the check id)."""
        forge = self.ctx.get_forge()
        if forge is None:
            return
        try:
            async with self.ctx.db.session() as session:
                run = await session.get(Run, self.run_id)
                if run is None:
                    return
                await post_summary(session, run, summary, forge)
                await session.commit()
        except Exception as exc:  # the forge never fails a run
            log.warning("posting the verdict of run %s failed: %s", self.run_id, exc)

    async def _stop_remaining(self) -> None:
        """``cancel_remaining``: STOP files, up to ``stop_wait_s`` for bundles, then cancel."""
        if self.released:
            return
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
        """Cancel the run when someone marks it cancelled or a newer run of the same PR exists.

        Runs from the claim until the shards are done, so a cancellation also interrupts planning.
        """
        while True:
            await asyncio.sleep(self.ctx.cancel_poll_s)
            try:
                reason = await self._cancel_requested()
            except Exception as exc:  # a busy database only delays the check
                log.debug("cancel check of run %s failed: %s", self.run_id, exc)
                continue
            if reason is None:
                continue
            self.cancel_reason = reason
            if self.plan_task is not None and not self.plan_task.done():
                self.plan_task.cancel()
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
            if not state.spec.nodeids:
                continue
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
        self.released = True
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
                adapter=self.ctx.adapter,
            )
            for shard_id, path in sorted(pulled.items())
        ]
        outcomes = await self.ctx.ingest_pool().run(jobs) if jobs else []
        for shard_id, state in self.shard_states.items():
            if shard_id not in pulled and state.spec.nodeids:
                await asyncio.to_thread(
                    record_shard_failure, self.ctx.store, rctx, shard_id, reason=state.error or "no bundle"
                )
        for outcome in outcomes:
            if not outcome.ok:
                self.shard_states[outcome.shard_id].error = self.shard_states[outcome.shard_id].error or (
                    f"bundle rejected: {outcome.error}"
                )
            elif (tracer := tracer_warning(outcome.meta)) is not None:
                assert self.sink is not None
                self.sink.event(
                    "log", {"shard": outcome.shard_id, "text": tracer, "warning": "tracer_mismatch"}
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

    async def _report(self, plan: RunPlan, outcomes: list[IngestOutcome], catalogue: Any) -> RunReport:
        """The report (its own short transaction for invariant and review rows); degraded on error.

        A concurrent run of the repo inserting the same new invariant definition first is retried
        once (the second attempt finds its row).
        """
        from sqlalchemy.exc import IntegrityError

        for attempt in (1, 2):
            try:
                async with self.ctx.db.session() as session:
                    run = await session.get(Run, self.run_id)
                    assert run is not None
                    report = await build_report(
                        session,
                        self.settings,
                        self.ctx.store,
                        run,
                        plan,
                        outcomes,
                        self.states,
                        catalogue=catalogue,
                    )
                    await session.commit()
                    return report
            except IntegrityError:
                if attempt == 1:
                    continue
                log.exception("building the report of run %s failed", self.run_id)
                return RunReport(notes=["report unavailable: a concurrent run wrote the same rows"])
            except Exception as exc:  # the verdict still lands, without the extras
                log.exception("building the report of run %s failed", self.run_id)
                return RunReport(notes=[f"report unavailable: {type(exc).__name__}: {exc}"[:300]])
        raise AssertionError("unreachable")

    async def _conclude(self, plan: RunPlan, outcomes: list[IngestOutcome], executor: Executor) -> None:
        sink = self.sink
        assert sink is not None
        self._reconcile(plan, outcomes)
        if self.early_verdict is not None:
            await asyncio.gather(self.early_verdict, return_exceptions=True)
        rctx = await self._run_context(plan)
        catalogue = await asyncio.to_thread(_catalogue, self.ctx.adapter, plan.measured_sources)
        finalized = await asyncio.to_thread(
            finalize_run, self.ctx.store, rctx, expected_shards=len(plan.shards), catalogue=catalogue
        )
        await sink.flush()
        conclusion, status, reason = self._conclusion(plan)
        cost = self._cost()
        not_run = sum(1 for t in self.states.values() if t.outcome == "not_run")
        report = await self._report(plan, outcomes, catalogue)
        code = exit_code_for(status, conclusion)
        counts = self._counts()
        test_ids = list(plan.test_ids.values())

        async def work(session: AsyncSession) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
            run = await session.get(Run, self.run_id)
            assert run is not None
            await history.update_test_stats(session, run, test_ids)
            run.coverage_complete = finalized.coverage_complete
            run.cost_estimate = cost
            run.cancelled_tests = not_run
            now = self.settings.now().isoformat()
            run.timeline = {"verdict": now, **(run.timeline or {}), "finished": now}
            run.status = status  # type: ignore[assignment]
            summary = build_summary(
                self.settings, run, plan, self.states, conclusion=conclusion, reason=reason, report=report
            )
            await persist_verdict(session, run, summary)
            notes = self._terminal_events(
                session,
                {"conclusion": conclusion, "reason": reason, "label": summary.label},
                {"conclusion": conclusion, "exit_code": code, "counts": counts},
            )
            return summary, notes

        summary, notes = await sink.transact(work)
        for kind, payload in notes:
            sink.notify(kind, payload)
        await self._post(summary)

    def _terminal_events(
        self, session: AsyncSession, verdict: dict[str, Any], done: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Add the ``verdict`` and ``done`` events to the terminal transaction."""
        assert self.sink is not None
        notes = []
        for kind, payload in (("verdict", verdict), ("done", done)):
            row, clean = self.sink.event_row(kind, payload)
            session.add(row)
            notes.append((kind, clean))
        return notes

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

    async def _finish(
        self, status: str, conclusion: str, reason: str, *, exit_code: int | None = None
    ) -> None:
        """Terminal bookkeeping for every early exit: status, cost, verdict and the ``verdict`` and
        ``done`` events in one (retried) transaction, then the forge post."""
        sink = self.sink
        assert sink is not None
        code = exit_code_for(status, conclusion, exit_code)
        cost = self._cost()
        counts = self._counts()

        async def work(session: AsyncSession) -> tuple[Any, list[tuple[str, dict[str, Any]]]] | None:
            run = await session.get(Run, self.run_id)
            if run is None:
                return None
            now = self.settings.now().isoformat()
            run.status = status  # type: ignore[assignment]
            run.cost_estimate = cost
            run.timeline = {"verdict": now, **(run.timeline or {}), "finished": now}
            summary = build_summary(
                self.settings, run, self.plan, self.states, conclusion=conclusion, reason=reason
            )
            await persist_verdict(session, run, summary)
            notes = self._terminal_events(
                session,
                {"conclusion": conclusion, "reason": reason, "label": summary.label},
                {"conclusion": conclusion, "exit_code": code, "counts": counts},
            )
            return summary, notes

        written = await sink.transact(work)
        if written is None:
            return
        summary, notes = written
        for kind, payload in notes:
            sink.notify(kind, payload)
        await self._post(summary)


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


def tracer_warning(meta: Mapping[str, Any]) -> str | None:
    """The ``log`` text for a shard whose coverage did not come from coverage.py's C tracer."""
    convert = meta.get("convert")
    if not isinstance(convert, Mapping) or not convert.get("tracer_mismatch"):
        return None
    name = str(convert.get("tracer") or meta.get("tracer") or "an unknown tracer")[:64]
    return f"coverage was collected with {name}; per-test contexts may be incomplete"


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
