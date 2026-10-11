"""Executors: where shards run (a local subprocess pool or Modal Sandboxes).

:mod:`coverset.executors.base` defines the :class:`Executor`/:class:`Worker` protocols every backend
implements; :func:`get_executor` builds the configured one (``modal`` is imported lazily, so the
local path never needs the Modal client).
"""

from __future__ import annotations

from coverset.config import ExecutorKind, Settings
from coverset.executors.base import (
    Capabilities,
    CapacityError,
    EnvRef,
    Executor,
    ExecutorError,
    Resources,
    RunResult,
    SnapshotRef,
    Worker,
    sanitize_env,
)

__all__ = [
    "Capabilities",
    "CapacityError",
    "EnvRef",
    "Executor",
    "ExecutorError",
    "Resources",
    "RunResult",
    "SnapshotRef",
    "Worker",
    "get_executor",
    "sanitize_env",
]


def get_executor(settings: Settings, kind: ExecutorKind | None = None) -> Executor:
    """Build the executor named ``kind`` (default ``settings.executor``).

    ``modal`` needs :mod:`coverset.executors.modal_` (phase 5b); its absence is a clear
    :class:`ExecutorError`, never an ImportError deep inside a run.
    """
    chosen = kind or settings.executor
    if chosen == "local":
        from coverset.executors.local import LocalExecutor

        return LocalExecutor(settings)
    if chosen == "modal":
        try:
            from coverset.executors import modal_
        except ImportError as exc:
            raise ExecutorError(f"the Modal executor is not available: {exc}") from exc
        factory = getattr(modal_, "ModalExecutor", None)
        if factory is None:
            raise ExecutorError("coverset.executors.modal_ defines no ModalExecutor")
        return factory(settings)
    raise ExecutorError(f"unknown executor {chosen!r}")
