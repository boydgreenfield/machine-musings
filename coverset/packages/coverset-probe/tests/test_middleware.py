"""Trace context: W3C traceparent parsing, ratio sampling, the WSGI and ASGI middleware, ``trace()``."""

from __future__ import annotations

import asyncio

import coverset_probe
import pytest
from coverset_probe import AsgiMiddleware, WsgiMiddleware
from coverset_probe.context import TRACE, parse_traceparent, trace_id_ratio_sampled
from probe_helpers import func_of

APP = """
def handler(path):
    return helper(len(path))

def helper(n):
    return n
"""
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
HEADER = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (HEADER, (TRACE_ID, True)),
        (HEADER[:-2] + "00", (TRACE_ID, False)),
        (HEADER.encode(), (TRACE_ID, True)),
        (HEADER.upper(), (TRACE_ID, True)),
        (HEADER + "-future-fields", (TRACE_ID, True)),
        ("ff-" + HEADER[3:], None),  # version ff is invalid
        (f"00-{'0' * 32}-00f067aa0ba902b7-01", None),
        (f"00-{TRACE_ID}-{'0' * 16}-01", None),
        ("00-xyz-00f067aa0ba902b7-01", None),
        ("x" * 600, None),
        (b"\xff\xfe", None),
        (None, None),
    ],
)
def test_parse_traceparent(header: str | bytes | None, expected: tuple[str, bool] | None) -> None:
    assert parse_traceparent(header) == expected


def test_ratio_sampling_is_a_function_of_the_trace_id() -> None:
    low = "0" * 16 + "0000000000000001"
    high = "0" * 16 + "ffffffffffffffff"
    assert trace_id_ratio_sampled(low, 0.01)
    assert not trace_id_ratio_sampled(high, 0.99)
    assert trace_id_ratio_sampled(high, 1.0)
    assert not trace_id_ratio_sampled(low, 0.0)


def _wsgi_app(module):
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [str(module.handler(environ["PATH_INFO"])).encode()]

    return app


def test_wsgi_requests_form_flows(make_module) -> None:
    module = make_module(APP)
    coverset_probe.start(
        modules=[module.__name__], sample_rate=1.0, k_anonymity=1, window_s=0, record_returns=False
    )
    wrapped = WsgiMiddleware(_wsgi_app(module), subject=lambda environ: environ.get("REMOTE_USER"))
    for path in ("/a", "/bb"):
        body = wrapped({"PATH_INFO": path, "HTTP_TRACEPARENT": HEADER, "REMOTE_USER": "u1"}, lambda *a: None)
        assert list(body) == [str(len(path)).encode()]
        body.close()
    assert TRACE.get() is None  # the context does not leak out of the request
    payload = coverset_probe.stop()
    assert func_of(payload, "handler")["sampled"] == 2
    flows = payload["flows"]
    assert len(flows) == 1
    assert sorted(payload["funcs"][i]["qualname"] for i in flows[0]["fs"]) == ["handler", "helper"]
    assert flows[0]["traces"] == 1  # one subject, two requests


def test_an_unsampled_trace_records_nothing(make_module) -> None:
    module = make_module(APP)
    coverset_probe.start(modules=[module.__name__], sample_rate=0.5, k_anonymity=1, window_s=0)
    high = f"00-{'0' * 16}ffffffffffffffff-00f067aa0ba902b7-01"  # ratio-sampled out at 0.5
    wrapped = WsgiMiddleware(_wsgi_app(module))
    wrapped({"PATH_INFO": "/", "HTTP_TRACEPARENT": high}, lambda *a: None).close()
    payload = coverset_probe.stop()
    assert func_of(payload, "handler")["calls"] == 1
    assert func_of(payload, "handler")["sampled"] == 0
    assert payload["rows"] == [] and payload["flows"] == []


def test_wsgi_and_asgi_pass_through_without_a_probe(make_module) -> None:
    module = make_module(APP)
    body = WsgiMiddleware(_wsgi_app(module))({"PATH_INFO": "/abc"}, lambda *a: None)
    assert list(body) == [b"4"]
    seen = []

    async def inner(scope, receive, send):
        seen.append(TRACE.get())

    asyncio.run(AsgiMiddleware(inner)({"type": "http", "headers": []}, None, None))
    assert seen == [None]


def test_asgi_requests_form_flows(make_module) -> None:
    module = make_module(APP)
    coverset_probe.start(
        modules=[module.__name__], sample_rate=1.0, k_anonymity=1, window_s=0, record_returns=False
    )
    sent = []

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await asyncio.sleep(0)
        await send(module.handler(scope["path"]))

    async def send(message):
        sent.append(message)

    async def main() -> None:
        wrapped = AsgiMiddleware(app)
        await asyncio.gather(
            *(wrapped({"type": "http", "path": "/" * n, "headers": [(b"traceparent", HEADER.encode())]}, None, send)
              for n in (1, 2, 3))
        )  # fmt: skip
        await wrapped({"type": "lifespan"}, None, send)  # not a request: passed through

    asyncio.run(main())
    payload = coverset_probe.stop()
    assert sorted(sent) == [1, 2, 3]
    assert func_of(payload, "helper")["sampled"] == 3
    assert payload["flows"][0]["traces"] == 1  # one trace id, three requests: one witness


def test_trace_context_manager_for_background_jobs(make_module) -> None:
    module = make_module(APP)
    with coverset_probe.trace() as nothing:
        assert nothing is None  # no probe: a no-op
    coverset_probe.start(modules=[module.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    for job in range(3):
        with coverset_probe.trace(subject=f"tenant-{job}") as ctx:
            assert ctx is not None and ctx.sampled
            module.handler("/job")
    payload = coverset_probe.stop()
    assert payload["flows"][0]["traces"] == 3


def test_a_failing_subject_callable_never_breaks_the_request(make_module) -> None:
    module = make_module(APP)
    coverset_probe.start(modules=[module.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)

    def bad_subject(environ):
        raise KeyError("no user")

    wrapped = WsgiMiddleware(_wsgi_app(module), subject=bad_subject)
    assert list(wrapped({"PATH_INFO": "/x"}, lambda *a: None)) == [b"2"]
    with pytest.raises(ZeroDivisionError):
        WsgiMiddleware(lambda environ, start: 1 / 0)({}, lambda *a: None)
    assert TRACE.get() is None
