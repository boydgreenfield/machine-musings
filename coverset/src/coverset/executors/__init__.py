"""Executors: where shards run (a local subprocess pool or Modal Sandboxes).

:mod:`coverset.executors.base` defines the :class:`Executor`/:class:`Worker` protocols every backend
implements; :func:`get_executor` builds the configured one (``modal`` is imported lazily, so the
local path never needs the Modal client).
"""

from __future__ import annotations

from coverset.config import ExecutorKind, Settings
from coverset.executors.base import Executor, ExecutorError

__all__ = ["ExecutorError", "get_executor"]


def get_executor(settings: Settings, kind: ExecutorKind | None = None) -> Executor:
    """Build the executor named ``kind`` (default ``settings.executor``).

    ``modal`` is imported only when chosen (the Modal client is a heavy import).
    """
    chosen = kind or settings.executor
    if chosen == "local":
        from coverset.executors.local import LocalExecutor

        return LocalExecutor(settings)
    if chosen == "modal":
        from coverset.executors.modal_ import ModalExecutor

        return ModalExecutor(settings)
    raise ExecutorError(f"unknown executor {chosen!r}")
