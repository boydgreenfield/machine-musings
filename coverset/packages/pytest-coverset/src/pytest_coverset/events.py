"""JSON-line events on the real stdout, framed so the runner can tell them from log text.

Each event is one line: ``"\\x1ecoverset <nonce> " + json + "\\n"``, written to ``sys.__stdout__`` and
flushed. The JSON is ASCII-only (``\\u`` escapes), so any stdout encoding can carry it. The nonce comes
from ``COVERSET_EVENT_NONCE`` (``-`` when unset) and is removed from ``os.environ`` when the session
starts, before conftest files and tests are imported. It separates events from log text that merely
looks like one; it is not a security boundary (test code shares the process and can still find it),
so ``results.jsonl`` in the bundle stays the authoritative record and the runner reconciles streamed
rows against it. Event kinds: ``collected``, ``test``, ``heartbeat``, ``bundle_written``,
``neutral_written`` (with ``--coverset-neutral``), ``done``. If pytest's terminal writer is mid-line (progress dots), a newline is written first so the
prefix always starts a line.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

NONCE_ENV = "COVERSET_EVENT_NONCE"
RECORD_SEPARATOR = "\x1e"
HEARTBEAT_INTERVAL_S = 10.0


def event_prefix(nonce: str) -> str:
    """The exact line prefix of every event for ``nonce``."""
    return f"{RECORD_SEPARATOR}coverset {nonce} "


def parse_event_line(line: str, nonce: str) -> dict[str, Any] | None:
    """Parse one stdout line; None unless it starts with the exact prefix for ``nonce``."""
    prefix = event_prefix(nonce)
    if not line.startswith(prefix):
        return None
    try:
        payload = json.loads(line[len(prefix) :])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class EventWriter:
    """Writes framed events and tracks when the last one was emitted (for heartbeats).

    Without an explicit ``nonce`` the writer takes (and removes) ``COVERSET_EVENT_NONCE`` from the
    environment, so tests and the subprocesses they start do not inherit it.
    """

    def __init__(
        self,
        nonce: str | None = None,
        stream: TextIO | None = None,
        mid_line: Callable[[], bool] | None = None,
    ) -> None:
        self.nonce = nonce if nonce is not None else (os.environ.pop(NONCE_ENV, None) or "-")
        self._prefix = event_prefix(self.nonce)
        self._stream = stream
        self._mid_line = mid_line
        self.last_emit = time.monotonic()

    def emit(self, kind: str, **payload: Any) -> None:
        """Write one event line and flush."""
        stream = self._stream if self._stream is not None else sys.__stdout__
        if stream is None:
            return
        record = {"kind": kind, "t": round(time.time(), 6), **payload}
        line = self._prefix + json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        if self._mid_line is not None and self._mid_line():
            line = "\n" + line
        stream.write(line)
        stream.flush()
        self.last_emit = time.monotonic()

    def heartbeat_due(self, now: float | None = None) -> bool:
        """True when no event was written for :data:`HEARTBEAT_INTERVAL_S` seconds."""
        return ((now if now is not None else time.monotonic()) - self.last_emit) >= HEARTBEAT_INTERVAL_S
