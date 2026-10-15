"""The agent tools of a deployed coverset, over HTTP: what ``coverset mcp serve --server`` uses.

:class:`RemoteService` has the same ``call`` and ``tools`` surface as a local
:class:`~coverset.agent.service.AgentService`, so the MCP server cannot tell them apart. It discovers
the tools from ``GET /v1/agent`` (a newer server's tools and descriptions win over this build's) and
calls ``POST /v1/agent/<tool>`` with the arguments as JSON and an ``Authorization: Bearer cst_...``
API token with the ``read`` scope.

The token is sent only to the configured server, never logged (the redactor masks ``cst_`` strings
anyway), and the URL must be ``https://`` unless it names a loopback host, so a token never crosses
a network in clear text. Errors come back as :class:`~coverset.agent.tools.ToolError` with the
server's ``error`` slug, so an agent sees the same failures over both transports.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from coverset.agent.tools import TOOLS, ToolError, ToolSpec

__all__ = ["RemoteService", "check_server_url"]

TIMEOUT_S: Final = 60.0
USER_AGENT: Final = "coverset-mcp"


def check_server_url(url: str) -> str:
    """``url`` without a trailing slash; raise ValueError unless it is https (or http on loopback)."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"--server needs an http(s) URL, got {url!r}")
    if parts.username or parts.password:
        raise ValueError("--server must not carry credentials in the URL: pass --token")
    if parts.scheme == "http" and not _loopback(host):
        raise ValueError("--server must use https:// (http:// only for localhost)")
    return url.strip().rstrip("/")


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class RemoteService:
    """The tools of a deployed coverset (see the module docstring)."""

    def __init__(self, server: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.server = check_server_url(server)
        if not token:
            raise ValueError("--server needs an API token (--token or COVERSET_TOKEN)")
        self._client = httpx.AsyncClient(
            base_url=self.server,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=TIMEOUT_S,
            transport=transport,
            follow_redirects=False,
        )
        self._specs: dict[str, ToolSpec] | None = None
        self.label = f"coverset at {self.server}"

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def tools(self) -> list[ToolSpec]:
        """The server's tools (``GET /v1/agent``), falling back to this build's on a malformed answer."""
        if self._specs is None:
            body = await self._request("GET", "/v1/agent")
            specs: dict[str, ToolSpec] = {}
            for row in body.get("tools", []) if isinstance(body, dict) else []:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    continue
                schema = row.get("input_schema")
                specs[row["name"]] = ToolSpec(
                    row["name"],
                    str(row.get("title") or row["name"]),
                    str(row.get("description") or ""),
                    schema if isinstance(schema, dict) else {"type": "object"},
                )
            self._specs = specs or dict(TOOLS)
        return list(self._specs.values())

    async def call(self, name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``POST /v1/agent/<name>`` with ``args``; the server validates and authorizes."""
        if not name.replace("_", "").isalnum():
            raise ToolError("unknown_tool", f"there is no tool {name!r}")
        body = await self._request("POST", f"/v1/agent/{name}", json=dict(args or {}))
        if not isinstance(body, dict):
            raise ToolError("internal", "the server sent something that is not a JSON object")
        return body

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError("unavailable", f"cannot reach {self.server}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                details = {k: v for k, v in body.items() if k not in ("error", "message")}
                message = str(body.get("message") or body["error"])
                code = body["error"] if body["error"] in ToolError.STATUS else _code(response.status_code)
                raise ToolError(code, message, details)
            raise ToolError(_code(response.status_code), f"HTTP {response.status_code} from {self.server}")
        return body


def _code(status: int) -> str:
    return {
        400: "invalid_argument",
        401: "unavailable",
        403: "not_found",
        404: "not_found",
        429: "rate_limited",
    }.get(status, "internal" if status >= 500 else "invalid_argument")
