"""WSGI and ASGI middleware: one request is one trace, so its observations form a flow.

Each request opens a :class:`~coverset_probe.context.TraceContext` from its W3C ``traceparent``
header (or a fresh random one), runs the application inside it and records the flow when it ends.
``subject`` is an optional callable from the WSGI ``environ`` (or the ASGI ``scope``) to a stable
identifier such as a user or tenant id; when given, k-anonymity counts distinct subjects instead of
distinct requests. The identifier is hashed with a per-process salt at once and never stored.

Both are pass-throughs while no probe runs, so they can stay installed with the probe switched off.

WSGI caveat: a streamed response body is produced after the application callable returns, outside
the request's context; calls made while the server iterates the body are sampled as untraced calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableMapping
from typing import Any

from coverset_probe.context import TRACE, TraceContext, begin_trace
from coverset_probe.probe import current

__all__ = ["AsgiMiddleware", "WsgiMiddleware"]

type WsgiApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]
type Scope = MutableMapping[str, Any]
type AsgiApp = Callable[
    [Scope, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]
]


class _ClosingBody:
    """Wraps a WSGI body so the flow is recorded when the server closes it."""

    def __init__(self, body: Iterable[bytes], ctx: TraceContext) -> None:
        self._body = body
        self._ctx = ctx

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._body)

    def close(self) -> None:
        try:
            close = getattr(self._body, "close", None)
            if close is not None:
                close()
        finally:
            probe = current()
            if probe is not None:
                probe.end_trace(self._ctx)


class WsgiMiddleware:
    """``app.wsgi_app = WsgiMiddleware(app.wsgi_app)`` (Flask) or wrap Django's ``application``."""

    def __init__(
        self, app: WsgiApp, *, subject: Callable[[dict[str, Any]], str | None] | None = None
    ) -> None:
        self.app = app
        self.subject = subject

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        probe = current()
        if probe is None:
            return self.app(environ, start_response)
        subject = _safe(self.subject, environ)
        ctx = begin_trace(probe.trace_rate, environ.get("HTTP_TRACEPARENT"), subject)
        token = TRACE.set(ctx)
        try:
            body = self.app(environ, start_response)
        except BaseException:
            probe.end_trace(ctx)
            raise
        finally:
            TRACE.reset(token)
        return _ClosingBody(body, ctx)


class AsgiMiddleware:
    """``app = AsgiMiddleware(app)``; FastAPI/Starlette: ``app.add_middleware(AsgiMiddleware)``."""

    def __init__(self, app: AsgiApp, *, subject: Callable[[Scope], str | None] | None = None) -> None:
        self.app = app
        self.subject = subject

    async def __call__(
        self, scope: Scope, receive: Callable[[], Awaitable[Any]], send: Callable[[Any], Awaitable[None]]
    ) -> None:
        probe = current()
        if probe is None or scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        traceparent = None
        for name, value in scope.get("headers") or ():
            if name == b"traceparent":
                traceparent = value
                break
        ctx = begin_trace(probe.trace_rate, traceparent, _safe(self.subject, scope))
        token = TRACE.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            TRACE.reset(token)
            probe.end_trace(ctx)


def _safe[T](subject: Callable[[T], str | None] | None, request: T) -> str | None:
    """The subject id, or None when there is no callable or it fails (a probe never breaks a request)."""
    if subject is None:
        return None
    try:
        value = subject(request)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None
