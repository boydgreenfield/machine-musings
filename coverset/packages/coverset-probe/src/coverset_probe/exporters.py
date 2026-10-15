"""Where finished windows go: a JSON-lines file, coverset's HTTP endpoint, or OpenTelemetry metrics.

Every exporter has one method, ``export(payload) -> None``, called from the probe's flush thread
(never from a traced call). An exporter may raise; the probe counts the error and carries on, so a
collector outage never reaches the application. The payload is the ``prod-flows`` v1 object
(``docs/schema/prod-flows.v1.json``).

* :class:`JsonlExporter` appends one payload per line; ``coverset flows import FILE.jsonl`` reads it.
  The file is created ``0600``. This is the transport the prototype is tested with.
* :class:`HttpExporter` POSTs the gzipped payload to coverset's ``/v1/flows`` with a bearer token.
  **The endpoint is designed, not built** (docs/production-flows.md, "Transport"); the exporter
  exists so the wire format is fixed. A failed POST keeps up to ``max_queue`` payloads for the
  next flush, oldest dropped first.
* :class:`OtelMetricsExporter` adds each row's call estimate to an OpenTelemetry counter
  (``coverset.shape.calls``, attributes ``code.namespace``, ``code.function``,
  ``coverset.param``, ``coverset.shape_key``), so any OTel collector pipeline carries the shapes to
  wherever metrics go. Pass a ``Meter``; with none, ``opentelemetry.metrics`` is imported lazily
  (``pip install coverset-probe[otel]``). Call vectors and flows do not map onto counters and stay
  out of this exporter.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

__all__ = ["Exporter", "HttpExporter", "JsonlExporter", "OtelMetricsExporter"]

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class Exporter(Protocol):
    """Receives each finished window's payload (on the flush thread)."""

    def export(self, payload: Mapping[str, Any]) -> None:
        """Ship one payload; raising is allowed (the probe counts it)."""
        ...


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


class JsonlExporter:
    """Appends one JSON object per window to ``path`` (created ``0600``; parents are created)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def export(self, payload: Mapping[str, Any]) -> None:
        """Append ``payload`` as one line and flush it to the OS."""
        line = _dumps(payload) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(line)


def _transport_ok(url: str) -> bool:
    """https anywhere; plain http only when the host *is* the loopback interface.

    The host is parsed, not prefix-matched: ``http://localhost.attacker.example`` and
    ``http://127.0.0.1.nip.io`` start like a loopback URL but would carry the bearer token in
    cleartext to someone else.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname  # lower-cased, brackets stripped; parsing the port validates it
        _ = parts.port
    except ValueError:
        return False
    if not host:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS)


class HttpExporter:
    """POSTs each payload, gzipped, to ``url`` (coverset's ``/v1/flows``) with ``Authorization: Bearer``."""

    def __init__(self, url: str, token: str, *, timeout_s: float = 5.0, max_queue: int = 3) -> None:
        if not _transport_ok(url):
            raise ValueError("HttpExporter needs an https:// URL (plain http only to localhost)")
        self.url = url
        self._token = token
        self.timeout_s = timeout_s
        self._queue: deque[bytes] = deque(maxlen=max_queue)
        self.sent = 0
        self.failed = 0

    def __repr__(self) -> str:
        return f"HttpExporter(url={self.url!r}, token=<redacted>)"

    def export(self, payload: Mapping[str, Any]) -> None:
        """Queue ``payload`` and try to send everything queued, oldest first."""
        self._queue.append(gzip.compress(_dumps(payload).encode("utf-8")))
        while self._queue:
            body = self._queue[0]
            request = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "User-Agent": "coverset-probe/0.1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                    response.read(1024)
            except Exception:
                self.failed += 1
                raise
            self._queue.popleft()
            self.sent += 1


class OtelMetricsExporter:
    """Feeds row call estimates into an OpenTelemetry counter (any collector forwards them)."""

    COUNTER = "coverset.shape.calls"

    def __init__(self, meter: Any = None) -> None:
        if meter is None:
            from opentelemetry import metrics  # optional dependency, imported only when used

            meter = metrics.get_meter("coverset-probe")
        self._counter = meter.create_counter(
            self.COUNTER,
            unit="{call}",
            description="Estimated calls per (function, parameter, shape key); shape keys carry no values",
        )

    def export(self, payload: Mapping[str, Any]) -> None:
        """Add every row's (rounded) call estimate with its function, parameter and shape key."""
        funcs = payload.get("funcs", [])
        base = {"service.name": payload.get("service", ""), "deployment.environment": payload.get("env", "")}
        for row in payload.get("rows", []):
            func = funcs[row["f"]]
            amount = round(float(row["calls"]))
            if amount <= 0:
                continue
            self._counter.add(
                amount,
                attributes={
                    **base,
                    "code.namespace": func["module"],
                    "code.function": func["qualname"],
                    "coverset.param": row["p"],
                    "coverset.shape_key": row["s"],
                },
            )
