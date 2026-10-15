"""Orchestrator: run queue, planning, execution, verdicts, sandbox pool and budget guards.

Intake (:mod:`coverset.orchestrator.intake`) turns forge events into ``runs`` rows; the dispatcher
installed by the queue module as ``app.extensions["run_queue"]`` picks queued runs up. Until one is
installed, queued runs simply wait in the database.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Mapping
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

RUN_QUEUE_EXTENSION = "run_queue"
"""Key of the :class:`RunQueue` in ``app.extensions``."""


@runtime_checkable
class RunQueue(Protocol):
    """Accepts ids of runs whose status is ``queued``; must return quickly (no run work inline)."""

    def enqueue(self, run_id: str) -> None | Awaitable[None]:
        """Schedule ``run_id`` for dispatch (sync or async; async results are awaited)."""
        ...


async def enqueue_run(extensions: Mapping[str, Any], run_id: str) -> bool:
    """Hand ``run_id`` to the installed run queue; False (run left queued in the DB) when none is."""
    queue = extensions.get(RUN_QUEUE_EXTENSION)
    if queue is None:
        log.info("no run queue installed; run %s stays queued", run_id)
        return False
    result = queue.enqueue(run_id)
    if inspect.isawaitable(result):
        await result
    return True
