"""Stdlib HTTP plumbing for ``coverset-ci``: JSON GETs with retry/backoff and a text/event-stream reader."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

USER_AGENT = "coverset-ci"
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
    OSError,
)


class ServerUnavailable(Exception):
    """The server kept failing (5xx or network errors) past the retry budget or the deadline."""


class SseUnavailable(Exception):
    """The events endpoint did not answer with a ``text/event-stream`` (use JSON polling instead)."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched server-sent event."""

    id: str | None
    event: str
    data: str


HEARTBEAT = SseEvent(None, "", "")
"""Yielded by :func:`iter_sse` for every comment line (the server's ``: ping``), so a caller can check
its deadline even while no event is dispatched."""


def iter_sse(lines: Iterable[bytes]) -> Iterator[SseEvent]:
    """Parse ``text/event-stream`` lines into events (multi-line data joined).

    Comment lines yield :data:`HEARTBEAT` (compare by identity) instead of being dropped silently.
    """
    event_id: str | None = None
    event_type = ""
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield SseEvent(event_id, event_type or "message", "\n".join(data))
            event_type, data = "", []
            continue
        if line.startswith(":"):
            yield HEARTBEAT
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        elif name == "event":
            event_type = value
        elif name == "id" and "\0" not in value:
            event_id = value


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """Status and decoded JSON body (None when the body is not JSON)."""

    status: int
    body: Any
    headers: Mapping[str, str]


class ApiClient:
    """GETs against the coverset API with bounded retries on 5xx and network errors."""

    def __init__(
        self,
        server: str,
        *,
        token: str | None = None,
        deadline: float,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        request_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.deadline = deadline
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.request_timeout = request_timeout
        self.clock = clock
        self.sleep = sleep

    def url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        """Absolute URL for an API path."""
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        return f"{self.server}{path}{suffix}"

    def _headers(self, accept: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    def remaining(self) -> float:
        """Seconds left before the deadline (never negative)."""
        return max(0.0, self.deadline - self.clock())

    def get_json(self, path: str, query: Mapping[str, Any] | None = None) -> JsonResponse:
        """GET a JSON resource; retry 5xx/network errors with exponential backoff.

        Returns any response below 500 (including 404, 401 and 429) to the caller. Raises
        :class:`ServerUnavailable` after ``max_retries`` consecutive failures or at the deadline.
        """
        failures = 0
        while True:
            error: str
            try:
                timeout = max(1.0, min(self.request_timeout, self.remaining()))
                request = urllib.request.Request(
                    self.url(path, query), headers=self._headers("application/json")
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return JsonResponse(response.status, _decode(response.read()), dict(response.headers))
            except urllib.error.HTTPError as exc:
                body = _decode(exc.read())
                if exc.code < 500:
                    return JsonResponse(exc.code, body, dict(exc.headers or {}))
                error = f"HTTP {exc.code} from {path}"
            except NETWORK_ERRORS as exc:
                error = f"{type(exc).__name__}: {exc}"
            failures += 1
            delay = min(self.backoff_max, self.backoff_base * 2 ** (failures - 1))
            if failures > self.max_retries or delay >= self.remaining():
                raise ServerUnavailable(f"{error} (after {failures} attempts)")
            self.sleep(delay)

    def open_stream(
        self, path: str, last_event_id: str | None, read_timeout: float
    ) -> http.client.HTTPResponse:
        """Open an SSE stream; raise :class:`SseUnavailable` for a non-200 or non-event-stream answer."""
        extra = {"Last-Event-ID": last_event_id} if last_event_id else None
        request = urllib.request.Request(self.url(path), headers=self._headers("text/event-stream", extra))
        try:
            response = urllib.request.urlopen(request, timeout=read_timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code >= 500:
                raise
            raise SseUnavailable(f"HTTP {exc.code}") from exc
        content_type = response.headers.get("Content-Type", "")
        if response.status != 200 or not content_type.startswith("text/event-stream"):
            response.close()
            raise SseUnavailable(f"HTTP {response.status} {content_type or 'without a content type'}")
        return response


def stream_lines(response: http.client.HTTPResponse, read_timeout: Callable[[], float]) -> Iterator[bytes]:
    """Lines of an open stream, each read bounded by ``read_timeout()`` seconds (asked before every line).

    The socket timeout is lowered as the caller's deadline approaches, so a server that sends nothing
    (or only partial lines) cannot hold the reader past it; a timeout raises :class:`TimeoutError`.
    """
    while True:
        set_read_timeout(response, read_timeout())
        line = response.readline()
        if not line:
            return
        yield line


def set_read_timeout(response: http.client.HTTPResponse, seconds: float) -> None:
    """Best effort: set the socket timeout of an open ``urlopen`` response (no-op if unreachable)."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if settimeout is not None:
        settimeout(max(0.05, seconds))


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
