"""Reverse-proxy awareness: the client address and scheme from the immediate upstream's headers.

Behind Caddy, Fly's edge, an ALB or Modal's web gateway the TCP peer is the proxy, so without help
``request.remote_addr`` is the proxy's address (every caller then shares one login and rate-limit
budget) and ``request.scheme`` is ``http`` (so ``url_for(..., _external=True)`` builds ``http://``
links on an ``https://`` site). ``COVERSET_TRUST_PROXY=true`` installs
:class:`ProxyHeadersMiddleware`, which rewrites the ASGI scope's ``client`` and ``scheme`` from
``X-Forwarded-For`` and ``X-Forwarded-Proto``.

Only the **last** value of each header is used: the one the immediate upstream appended. Earlier
values were written by whoever called that proxy, and anyone can send ``X-Forwarded-For: 127.0.0.1``,
so trusting the first value (what many frameworks do) would let a caller pick its own rate-limit key.
This is Hypercorn's ``ProxyFixMiddleware(mode="legacy", trusted_hops=1)`` with two deliberate
differences: ``X-Forwarded-Host`` is ignored (coverset builds external links from
``COVERSET_PUBLIC_URL``, and its open-mode Host check must see the real ``Host``), and a value that
is not an IP address or a known scheme is ignored rather than copied into the scope.

One platform needs a different header. On Fly.io the proxy *appends its own ingress address*, so
the last ``X-Forwarded-For`` value is the app's Fly IP for every caller, and Fly documents
``Fly-Client-IP`` (set by its proxy) as the client address to use. When the process runs on a Fly
Machine (``FLY_APP_NAME`` is set, as Fly does on every Machine), :meth:`ProxyHeadersMiddleware.for_environment`
reads the client from ``Fly-Client-IP`` instead; the scheme still comes from ``X-Forwarded-Proto``.
Modal needs no middleware at all: its ASGI runtime puts the real client and ``https`` in the scope
and strips client-sent ``X-Forwarded-*`` (measured; see docs/deploy.md).

The forwarding headers stay on the request: :mod:`coverset.web.setup` treats their presence as "not
the local operator", which is exactly right behind a proxy. Enable this only when every request
reaches coverset through a proxy that sets (or appends to) both headers; on a directly exposed port
a caller could forge them.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from typing import Any

from hypercorn.typing import ASGIFramework, ASGIReceiveCallable, ASGISendCallable, Scope

__all__ = ["FLY_CLIENT_IP", "ProxyHeadersMiddleware", "last_forwarded_value"]

_FORWARDED_FOR = b"x-forwarded-for"
_FORWARDED_PROTO = b"x-forwarded-proto"
FLY_CLIENT_IP = b"fly-client-ip"
"""The client address Fly.io's proxy sets (its ``X-Forwarded-For`` ends with the app's own IP)."""
_SCHEMES = {
    "http": {"http": "http", "https": "https"},
    "websocket": {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"},
}
"""Accepted ``X-Forwarded-Proto`` values per scope type, mapped to the ASGI scheme."""


def last_forwarded_value(headers: Any, name: bytes) -> str | None:
    """The last comma-separated value of every ``name`` header in an ASGI header list, or None.

    Repeated headers are concatenated in order (RFC 9110 field combination), so the last value is
    the one the nearest proxy added whether it appended to the list or added its own header line.
    """
    values: list[str] = []
    for header_name, header_value in headers:
        if header_name.lower() == name:
            values.extend(part.strip() for part in header_value.decode("latin-1").split(","))
    values = [value for value in values if value]
    return values[-1] if values else None


def _client_address(value: str) -> str | None:
    """``value`` as a normalised IP address (``[v6]:port`` and ``v4:port`` forms accepted), else None."""
    host = value
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.partition(":")[0]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


class ProxyHeadersMiddleware:
    """ASGI middleware: ``scope["client"]`` and ``scope["scheme"]`` from the nearest proxy's headers.

    Lifespan scopes pass through untouched. A request with no (or an unusable) header keeps the
    value the server put in the scope.
    """

    def __init__(self, app: ASGIFramework, *, client_header: bytes = _FORWARDED_FOR) -> None:
        self.app = app
        self.client_header = client_header

    @classmethod
    def for_environment(
        cls, app: ASGIFramework, environ: Mapping[str, str] | None = None
    ) -> ProxyHeadersMiddleware:
        """The middleware for the platform this process runs on (``Fly-Client-IP`` on Fly.io)."""
        env = os.environ if environ is None else environ
        return cls(app, client_header=FLY_CLIENT_IP if env.get("FLY_APP_NAME") else _FORWARDED_FOR)

    async def __call__(self, scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        kind = scope["type"]
        if kind == "http" or kind == "websocket":
            scope = self.rewrite(scope, client_header=self.client_header)
        await self.app(scope, receive, send)

    @staticmethod
    def rewrite(scope: Any, *, client_header: bytes = _FORWARDED_FOR) -> Any:
        """A shallow copy of an http/websocket ``scope`` with ``client``/``scheme`` from the headers."""
        headers = scope.get("headers", ())
        updated = dict(scope)
        forwarded_for = last_forwarded_value(headers, client_header)
        client = _client_address(forwarded_for) if forwarded_for else None
        if client is not None:
            updated["client"] = (client, 0)
        proto = last_forwarded_value(headers, _FORWARDED_PROTO)
        scheme = _SCHEMES[scope["type"]].get(proto.lower()) if proto else None
        if scheme is not None:
            updated["scheme"] = scheme
        return updated
