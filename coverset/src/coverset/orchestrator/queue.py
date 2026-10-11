"""The run queue: one in-process asyncio dispatcher that starts queued runs.

:class:`RunDispatcher` implements the :class:`~coverset.orchestrator.RunQueue` protocol
(``enqueue(run_id)`` returns at once) and claims ``queued`` runs, oldest first, while fewer than
``COVERSET_MAX_CONCURRENT_RUNS`` are executing. With ``COVERSET_CANCEL_SUPERSEDED=true`` a queued run
of a pull request that already has a newer run is cancelled instead of started. ``COVERSET_PAUSED=true``
is the kill switch: runs stay queued and nothing starts. Modal runs past the daily budget are concluded
``neutral`` with reason "budget exhausted" by the runner's policy check. A run task that dies with an
exception is finalized ``error`` (with ``verdict`` and ``done`` events) by :meth:`RunDispatcher._done`.

:func:`install_run_queue` puts a dispatcher on a Quart app (``app.extensions["run_queue"]``), started
from ``before_serving``. At boot, runs a previous process left behind are handled:

* ``queued`` runs are marked ``abandoned`` (left queued with ``COVERSET_REQUEUE_ON_BOOT=true``);
* ``running`` runs are taken over only when their executor is gone: claimed by an earlier ``serve`` on
  this host, by a process on this host that no longer exists, by nobody, or with a heartbeat older
  than :data:`STALE_HEARTBEAT_S`. A live ``coverset run``/``coverset demo`` keeps its runs. A taken-over
  run is ``abandoned`` (with ``verdict`` and ``done`` events so SSE clients finish); with
  ``COVERSET_REQUEUE_ON_BOOT=true`` a fresh queued copy (same trigger, PR, ``extra_env`` and webhook
  arrival time) is created first, because the old run's plan rows already exist.

Without a web app, the CLI uses :meth:`RunDispatcher.run_once` and :meth:`RunDispatcher.drain`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from coverset.db.models import Run, RunEvent
from coverset.orchestrator.planner import run_transaction
from coverset.orchestrator.runner import OrchestratorContext, RunOutcome, execute_run, finalize_crashed

__all__ = ["RUN_QUEUE_EXTENSION", "STALE_HEARTBEAT_S", "RunDispatcher", "install_run_queue"]

log = logging.getLogger(__name__)

RUN_QUEUE_EXTENSION = "run_queue"
POLL_S = 2.0
STALE_HEARTBEAT_S = 300.0
"""A ``running`` run whose heartbeat is older than this has lost its executor."""
_COPIED_FIELDS = (
    "repo_id",
    "head_sha",
    "base_sha",
    "branch",
    "pr_id",
    "trigger",
    "executor",
    "trusted",
    "network",
    "approved_by",
    "extra_env",
    "retry_increment_keys",
)


class RunDispatcher:
    """Claims queued runs under the concurrency cap and executes them as asyncio tasks."""

    def __init__(
        self, ctx: OrchestratorContext, *, max_concurrent: int | None = None, poll_s: float = POLL_S
    ) -> None:
        self.ctx = ctx
        self.max_concurrent = max_concurrent or ctx.settings.max_concurrent_runs
        self.poll_s = poll_s
        self.active: dict[str, asyncio.Task[RunOutcome]] = {}
        self.outcomes: list[RunOutcome] = []
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._finalizers: set[asyncio.Task[Any]] = set()

    @property
    def started(self) -> bool:
        """Whether the dispatch loop is running."""
        return self._loop_task is not None

    # -- RunQueue protocol ------------------------------------------------------------------------

    def enqueue(self, run_id: str) -> None:
        """Note that ``run_id`` is queued; the dispatcher picks it up on its next pass."""
        log.debug("run %s enqueued", run_id)
        self._wake.set()

    # -- lifecycle --------------------------------------------------------------------------------

    def _executor_gone(self, run: Run) -> bool:
        """Whether a ``running`` run has nobody executing it any more (see the module docstring)."""
        now = self.ctx.settings.now()
        heartbeat = run.heartbeat_at
        if heartbeat is not None and now - heartbeat > timedelta(seconds=STALE_HEARTBEAT_S):
            return True
        claimed = run.claimed_by
        if not claimed:
            return True
        if claimed == self.ctx.claimant:
            return False
        role, _, rest = claimed.partition("@")
        host, _, tail = rest.partition(":")
        pid_text = tail.split(":", 1)[0]
        if host != (socket.gethostname()[:64] or "localhost"):
            return heartbeat is None and now - run.created_at > timedelta(seconds=STALE_HEARTBEAT_S)
        mine = self.ctx.claimant.split("@", 1)[0]
        if role == "serve" and mine == "serve":
            return True  # an earlier serve of this host: one dispatcher per database
        return not pid_text.isdigit() or not _pid_alive(int(pid_text))

    async def on_boot(self) -> list[str]:
        """Abandon (or re-queue) runs a previous process left behind; returns the ids touched."""
        requeue = self.ctx.settings.requeue_on_boot
        touched: list[str] = []
        async with self.ctx.db.session() as session:
            leftovers = list(await session.scalars(select(Run).where(Run.status.in_(("queued", "running")))))
        for run in leftovers:
            if run.status == "running" and not self._executor_gone(run):
                continue
            if run.status == "queued" and requeue:
                touched.append(run.id)  # nothing was planned yet: it simply stays queued
                continue
            await run_transaction(
                self.ctx.db, lambda session, rid=run.id: self._abandon(session, rid, requeue)
            )
            touched.append(run.id)
        return touched

    async def _abandon(self, session: Any, run_id: str, requeue: bool) -> str | None:
        """Abandon ``run_id`` with ``verdict``/``done`` events (re-queuing a fresh copy first)."""
        run = await session.get(Run, run_id)
        if run is None or run.status not in ("queued", "running"):
            return None
        now = self.ctx.settings.now()
        replacement = None
        reason = "abandoned: the control plane restarted before the run finished"
        if requeue:
            copy = Run(**{name: getattr(run, name) for name in _COPIED_FIELDS})
            copy.status = "queued"
            copy.created_at = now
            timeline = {"queued": now.isoformat(), "requeued_from": run.id}
            if run.timeline and run.timeline.get("webhook_received"):
                timeline["webhook_received"] = run.timeline["webhook_received"]
            copy.timeline = timeline
            copy.extra_env = dict(run.extra_env or {})
            copy.retry_increment_keys = list(run.retry_increment_keys or [])
            session.add(copy)
            await session.flush()
            replacement = copy.id
            reason = f"abandoned: the control plane restarted; re-queued as run {copy.id}"
        run.status = "abandoned"
        run.verdict = "neutral"
        run.reason = reason
        run.timeline = {**(run.timeline or {}), "finished": now.isoformat()}
        seq = int(await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id)) or 0)
        for offset, (kind, payload) in enumerate(
            (
                ("verdict", {"conclusion": "neutral", "reason": reason}),
                ("done", {"conclusion": "neutral", "exit_code": 2, "counts": {}}),
            ),
            start=1,
        ):
            session.add(RunEvent(run_id=run.id, seq=seq + offset, kind=kind, payload=payload, at=now))
        return replacement

    async def start(self) -> None:
        """Handle leftovers from a previous process, then start the dispatch loop."""
        await self.on_boot()
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Stop dispatching and cancel the runs in flight (they end as ``running`` → abandoned at boot)."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
        self.active.clear()
        await asyncio.gather(*self._finalizers, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_s)
            self._wake.clear()
            try:
                await self.dispatch()
            except Exception:
                log.exception("run dispatch failed")

    # -- dispatching ------------------------------------------------------------------------------

    async def dispatch(self) -> list[str]:
        """Start as many queued runs as the cap allows; returns the ids started.

        Queued runs already superseded by a newer run of the same PR are cancelled, not started.
        """
        if self.ctx.settings.paused:
            return []
        started: list[str] = []
        while (free := self.max_concurrent - len(self.active)) > 0:
            async with self.ctx.db.session() as session:
                runs = list(
                    await session.scalars(
                        select(Run)
                        .where(Run.status == "queued", Run.id.not_in([*self.active, *started] or [""]))
                        .order_by(Run.created_at, Run.id)
                        .limit(free)
                    )
                )
            if not runs:
                break
            superseded = await self._superseded(runs)
            for run in runs:
                if run.id in superseded:
                    await self._cancel_superseded(run.id, superseded[run.id])
                    continue
                task = asyncio.get_running_loop().create_task(execute_run(self.ctx, run.id))
                self.active[run.id] = task
                task.add_done_callback(lambda t, rid=run.id: self._done(rid, t))
                started.append(run.id)
            if not superseded:
                break
        return started

    async def _superseded(self, runs: list[Run]) -> dict[str, str]:
        """Queued runs among ``runs`` that a newer queued/running run of the same PR replaces."""
        if not self.ctx.settings.cancel_superseded:
            return {}
        found: dict[str, str] = {}
        async with self.ctx.db.session() as session:
            for run in runs:
                if run.pr_id is None:
                    continue
                newer = await session.scalar(
                    select(Run.id)
                    .where(
                        Run.pr_id == run.pr_id,
                        Run.id != run.id,
                        Run.created_at > run.created_at,
                        Run.status.in_(("queued", "running")),
                    )
                    .order_by(Run.created_at.desc())
                    .limit(1)
                )
                if newer is not None:
                    found[run.id] = newer
        return found

    async def _cancel_superseded(self, run_id: str, newer: str) -> None:
        reason = f"superseded by run {newer}"
        now = self.ctx.settings.now()

        async def work(session: Any) -> None:
            run = await session.get(Run, run_id)
            if run is None or run.status != "queued":
                return
            run.status = "cancelled"
            run.verdict = "cancelled"
            run.reason = reason
            run.timeline = {**(run.timeline or {}), "finished": now.isoformat()}
            seq = int(
                await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0
            )
            session.add_all(
                [
                    RunEvent(
                        run_id=run_id,
                        seq=seq + 1,
                        kind="verdict",
                        payload={"conclusion": "cancelled", "reason": reason},
                        at=now,
                    ),
                    RunEvent(
                        run_id=run_id,
                        seq=seq + 2,
                        kind="done",
                        payload={"conclusion": "cancelled", "exit_code": 2, "counts": {}},
                        at=now,
                    ),
                ]
            )

        await run_transaction(self.ctx.db, work)
        log.info("run %s %s", run_id, reason)

    def _done(self, run_id: str, task: asyncio.Task[RunOutcome]) -> None:
        self.active.pop(run_id, None)
        if not task.cancelled() and task.exception() is None:
            self.outcomes.append(task.result())
        elif not task.cancelled():
            exc = task.exception()
            log.error("run %s crashed: %r", run_id, exc)
            finalizer = asyncio.get_running_loop().create_task(
                self._finalize(run_id, f"internal error: {type(exc).__name__}: {exc}")
            )
            self._finalizers.add(finalizer)
            finalizer.add_done_callback(self._finalizers.discard)
        self._wake.set()

    async def _finalize(self, run_id: str, reason: str) -> None:
        try:
            await finalize_crashed(self.ctx.db, self.ctx.settings, run_id, reason)
        except Exception:
            log.exception("could not finalize crashed run %s", run_id)

    async def run_once(self) -> list[RunOutcome]:
        """Start what the cap allows and wait for those runs (CLI use, no loop needed)."""
        started = await self.dispatch()
        tasks = [self.active[rid] for rid in started if rid in self.active]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if self._finalizers:
            await asyncio.gather(*self._finalizers, return_exceptions=True)
        return [r for r in results if isinstance(r, RunOutcome)]

    async def drain(self) -> list[RunOutcome]:
        """Run every queued run (in waves of at most ``max_concurrent``) until none is left."""
        finished: list[RunOutcome] = []
        while True:
            batch = await self.run_once()
            if not batch:
                if self.active:
                    await asyncio.gather(*self.active.values(), return_exceptions=True)
                    continue
                return finished
            finished += batch


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def install_run_queue(app: Any, *, ctx: OrchestratorContext | None = None) -> RunDispatcher:
    """Attach a :class:`RunDispatcher` to a Quart app and start/stop it with the server.

    Idempotent: a dispatcher already installed on the app is returned unchanged.
    """
    existing = app.extensions.get(RUN_QUEUE_EXTENSION)
    if isinstance(existing, RunDispatcher):
        return existing
    context = ctx or OrchestratorContext.from_app(app)
    dispatcher = RunDispatcher(context)
    app.extensions[RUN_QUEUE_EXTENSION] = dispatcher

    @app.before_serving
    async def _start_run_queue() -> None:
        await dispatcher.start()

    @app.after_serving
    async def _stop_run_queue() -> None:
        await dispatcher.stop()

    return dispatcher
