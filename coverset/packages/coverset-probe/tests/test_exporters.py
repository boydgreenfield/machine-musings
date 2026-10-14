"""Exporters: JSON lines on disk, gzipped POST with a bearer token, OpenTelemetry counters."""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from coverset_probe import HttpExporter, JsonlExporter, OtelMetricsExporter

PAYLOAD: dict[str, Any] = {
    "version": 1,
    "kind": "coverset.prod-flows",
    "service": "svc",
    "env": "prod",
    "release": None,
    "window": {"start": "2026-10-13T10:00:00Z", "end": "2026-10-13T10:01:00Z"},
    "probe": {},
    "funcs": [
        {
            "module": "app.mail",
            "qualname": "check",
            "line": 3,
            "path": None,
            "calls": 9,
            "sampled": 9,
            "capped": False,
        }
    ],
    "rows": [
        {
            "f": 0,
            "p": "email",
            "s": "str|ascii|kind:email/plain|len:11-100",
            "calls": 7.4,
            "traces": 5,
            "callers": {},
        },
        {"f": 0, "p": "email", "s": "str|empty|len:0", "calls": 0.2, "traces": 5, "callers": {}},
    ],
    "calls": [],
    "flows": [],
}


def test_jsonl_appends_one_line_per_window(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "flows.jsonl"
    exporter = JsonlExporter(out)
    exporter.export(PAYLOAD)
    exporter.export(PAYLOAD | {"service": "other"})
    lines = out.read_text().splitlines()
    assert [json.loads(line)["service"] for line in lines] == ["svc", "other"]
    assert out.stat().st_mode & 0o777 == 0o600


class _Collector(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, str], bytes]] = []
    status = 202

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), body))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def collector() -> Iterator[tuple[str, type[_Collector]]]:
    handler = type("Collector", (_Collector,), {"received": [], "status": 202})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/flows", handler
    finally:
        server.shutdown()
        server.server_close()


def test_http_posts_gzipped_json_with_a_bearer_token(collector) -> None:
    url, handler = collector
    exporter = HttpExporter(url, "tok-123")
    exporter.export(PAYLOAD)
    headers, body = handler.received[0]
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(body)) == PAYLOAD
    assert exporter.sent == 1
    assert "tok-123" not in repr(exporter)


def test_http_keeps_a_bounded_queue_across_failures(collector) -> None:
    url, handler = collector
    handler.status = 503
    exporter = HttpExporter(url, "t", max_queue=2)
    for n in range(3):
        with pytest.raises(Exception, match="503"):
            exporter.export(PAYLOAD | {"service": f"s{n}"})
    handler.status = 202
    exporter.export(PAYLOAD | {"service": "s3"})
    attempts = [json.loads(gzip.decompress(body))["service"] for _, body in handler.received]
    # Each failed export retried the oldest queued payload; s0 then s1 fell off the two-slot queue.
    assert attempts == ["s0", "s0", "s1", "s2", "s3"]
    assert exporter.sent == 2 and exporter.failed == 3


def test_http_refuses_plain_http_to_remote_hosts() -> None:
    with pytest.raises(ValueError, match="https"):
        HttpExporter("http://collector.example.com/v1/flows", "t")
    HttpExporter("https://coverset.example.com/v1/flows", "t")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.attacker.example/v1/flows",  # starts like localhost, is not
        "http://127.0.0.1.nip.io/v1/flows",
        "http://localhost@evil.example/v1/flows",  # userinfo, the host is evil.example
        "http://[::1/v1/flows",  # unparseable
        "http://localhost:notaport/v1/flows",
        "ftp://localhost/v1/flows",
        "https:///v1/flows",  # no host
    ],
)
def test_plain_http_is_allowed_only_to_the_loopback_host(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        HttpExporter(url, "t")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/v1/flows",
        "http://LOCALHOST:8080/v1/flows",
        "http://127.0.0.1:9/x",
        "http://[::1]:9/x",
    ],
)
def test_loopback_urls_may_use_plain_http(url: str) -> None:
    assert HttpExporter(url, "t").url == url


class _FakeCounter:
    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.adds.append((amount, attributes))


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}

    def create_counter(self, name: str, unit: str = "", description: str = "") -> _FakeCounter:
        self.counters[name] = _FakeCounter()
        return self.counters[name]


def test_otel_counter_gets_one_add_per_row() -> None:
    meter = _FakeMeter()
    OtelMetricsExporter(meter).export(PAYLOAD)
    adds = meter.counters["coverset.shape.calls"].adds
    assert adds == [
        (
            7,
            {
                "service.name": "svc",
                "deployment.environment": "prod",
                "code.namespace": "app.mail",
                "code.function": "check",
                "coverset.param": "email",
                "coverset.shape_key": "str|ascii|kind:email/plain|len:11-100",
            },
        )
    ]  # the 0.2-call row rounds to nothing and is skipped


def test_otel_without_a_meter_needs_the_optional_dependency() -> None:
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            OtelMetricsExporter()
    else:  # pragma: no cover - only where the extra is installed
        OtelMetricsExporter()
