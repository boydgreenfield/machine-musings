"""A threaded stub of the coverset HTTP API (plan Addendum A) for ``coverset-ci`` tests."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

RUN_ID = "run-1"
SHA = "a" * 40


@dataclass
class Scenario:
    """What the stub answers.

    ``requests`` logs every (path, lower-cased headers). ``verdicts`` are consumed one per ``/v1/verdict`` request (the last one repeats). ``sse`` is
    ``ok`` (stream everything after Last-Event-ID), ``unsupported`` (406), ``drop_first`` (the first
    connection closes after ``drop_after`` events), ``reset_each`` (every connection sends at most
    ``drop_after`` events and is then reset with a TCP RST, so the client sees a network error),
    ``empty`` (every connection closes without events) or ``error`` (500). ``fail_all`` answers 500
    to everything.
    """

    verdicts: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    run: dict[str, Any] = field(default_factory=dict)
    sse: str = "ok"
    drop_after: int = 1
    poll_done: bool = True
    fail_all: bool = False
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    sse_connections: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_verdict(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            if len(self.verdicts) > 1:
                return self.verdicts.pop(0)
            return self.verdicts[0]


def verdict_found(state: str = "running") -> tuple[int, dict[str, Any]]:
    """A 200 ``/v1/verdict`` answer for :data:`RUN_ID`."""
    return 200, {
        "state": state,
        "conclusion": None,
        "run_id": RUN_ID,
        "summary": "running",
        "summary_url": f"http://coverset.test/runs/{RUN_ID}",
    }


NOT_FOUND = (404, {"error": "not_found"})


def event_list(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Number ``(kind, payload)`` pairs from seq 1."""
    return [{"seq": i, "kind": kind, "payload": payload} for i, (kind, payload) in enumerate(items, start=1)]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        scenario = self.server.scenario
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        scenario.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if scenario.fail_all:
            self._json(500, {"error": "boom"})
        elif url.path == "/v1/verdict":
            self._json(*scenario.next_verdict())
        elif url.path == f"/v1/runs/{RUN_ID}/events":
            if "text/event-stream" in self.headers.get("Accept", ""):
                self._sse(scenario)
            else:
                since = int(query.get("since", ["0"])[0])
                pending = [e for e in scenario.events if e["seq"] > since]
                self._json(200, {"events": pending, "done": scenario.poll_done})
        elif url.path == f"/v1/runs/{RUN_ID}":
            self._json(200, scenario.run)
        else:
            self._json(404, {"error": "not_found"})

    def _sse(self, scenario: Scenario) -> None:
        with scenario.lock:
            scenario.sse_connections += 1
            connection = scenario.sse_connections
        if scenario.sse == "unsupported":
            self._json(406, {"error": "not_acceptable"})
            return
        if scenario.sse == "error":
            self._json(500, {"error": "boom"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if scenario.sse == "empty":
            self.wfile.write(b": ping\n\n")
            return
        last = int(self.headers.get("Last-Event-ID") or 0)
        pending = [e for e in scenario.events if e["seq"] > last]
        if (scenario.sse == "drop_first" and connection == 1) or scenario.sse == "reset_each":
            pending = pending[: scenario.drop_after]
        self.wfile.write(b": ping\n\n")
        for event in pending:
            frame = f"id: {event['seq']}\nevent: {event['kind']}\ndata: {json.dumps(event['payload'])}\n\n"
            self.wfile.write(frame.encode())
            self.wfile.flush()
        if scenario.sse == "reset_each":
            time.sleep(0.2)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, scenario: Scenario) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.scenario = scenario


@contextmanager
def serve(scenario: Scenario) -> Iterator[str]:
    """Run the stub on a free loopback port; yield its base URL."""
    server = _Server(scenario)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
