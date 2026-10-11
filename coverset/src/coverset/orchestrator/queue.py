"""The run queue: one in-process asyncio dispatcher that starts queued runs.

:class:`RunDispatcher` implements the :class:`~coverset.orchestrator.RunQueue` protocol
(``enqueue(run_id)`` returns at once) and claims ``queued`` runs, oldest first, while fewer than
``COVERSET_MAX_CONCURRENT_RUNS`` are executing. ``COVERSET_PAUSED=true`` is the kill switch: runs stay
queued and nothing starts. Modal runs past the daily budget are concluded ``neutral`` with reason
"budget exhausted" by the runner's policy check.

:func:`install_run_queue` puts a dispatcher on a Quart app (``app.extensions["run_queue"]``), started
from ``before_serving``: at boot, runs left ``queued`` or ``running`` by a previous process are
marked ``abandoned`` (with ``verdict`` and ``done`` events so SSE clients finish), or re-queued when
``COVERSET_REQUEUE_ON_BOOT=true``. Without a web app, the CLI uses :meth:`RunDispatcher.run_once` and
:meth:`RunDispatcher.drain`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy import func, select

from coverset.db.models import Run, RunEvent
from coverset.orchestrator.runner import OrchestratorContext, RunOutcome, execute_run

__all__ = ["RUN_QUEUE_EXTENSION", "RunDispatcher", "install_run_queue"]

log = logging.getLogger(__name__)

RUN_QUEUE_EXTENSION = "run_queue"
POLL_S = 2.0


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

    async def on_boot(self) -> list[str]:
        """Mark runs left ``queued``/``running`` by a previous process ``abandoned`` (or re-queue them)."""
        requeue = self.ctx.settings.requeue_on_boot
        touched: list[str] = []
        now = self.ctx.settings.now()
        async with self.ctx.db.session() as session:
            runs = list(await session.scalars(select(Run).where(Run.status.in_(("queued", "running")))))
            for run in runs:
                touched.append(run.id)
                if requeue:
                    run.status = "queued"
                    continue
                run.status = "abandoned"
                run.verdict = "neutral"
                run.reason = "abandoned: the control plane restarted before the run finished"
                run.timeline["finished"] = now.isoformat()
                seq = int(
                    await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id)) or 0
                )
                for offset, (kind, payload) in enumerate(
                    (
                        ("verdict", {"conclusion": "neutral", "reason": run.reason}),
                        ("done", {"conclusion": "neutral", "exit_code": 2, "counts": {}}),
                    ),
                    start=1,
                ):
                    session.add(RunEvent(run_id=run.id, seq=seq + offset, kind=kind, payload=payload, at=now))
            await session.commit()
        return touched

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
        """Start as many queued runs as the cap allows; returns the ids started."""
        if self.ctx.settings.paused:
            return []
        free = self.max_concurrent - len(self.active)
        if free <= 0:
            return []
        async with self.ctx.db.session() as session:
            ids = list(
                await session.scalars(
                    select(Run.id)
                    .where(Run.status == "queued", Run.id.not_in(list(self.active) or [""]))
                    .order_by(Run.created_at, Run.id)
                    .limit(free)
                )
            )
        for run_id in ids:
            task = asyncio.get_running_loop().create_task(execute_run(self.ctx, run_id))
            self.active[run_id] = task
            task.add_done_callback(lambda t, rid=run_id: self._done(rid, t))
        return ids

    def _done(self, run_id: str, task: asyncio.Task[RunOutcome]) -> None:
        self.active.pop(run_id, None)
        if not task.cancelled() and task.exception() is None:
            self.outcomes.append(task.result())
        elif not task.cancelled():
            log.error("run %s crashed: %r", run_id, task.exception())
        self._wake.set()

    async def run_once(self) -> list[RunOutcome]:
        """Start what the cap allows and wait for those runs (CLI use, no loop needed)."""
        started = await self.dispatch()
        tasks = [self.active[rid] for rid in started if rid in self.active]
        results = await asyncio.gather(*tasks, return_exceptions=True)
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
