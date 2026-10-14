"""The agent API: ``/v1/agent``, the MCP tools over HTTP.

``GET /v1/agent`` describes every tool (name, title, description, JSON Schema of its arguments):
the OpenAPI-ish self-description ``coverset mcp serve --server`` discovers tools from.
``POST /v1/agent/<tool>`` takes the arguments as a JSON object and answers the tool's JSON;
``GET /v1/agent/<tool>?arg=value`` does the same from the query string (numbers, booleans and
comma-separated lists are converted by the schema), for curl and browsers. The tools are
:class:`~coverset.agent.service.AgentService` over the facts store, so the HTTP answer is byte for
byte what the MCP server returns for the same call.

Access: the path is not public, so the access guard has already resolved a principal (the admin
token, a signed-in user, or an ``Authorization: Bearer cst_...`` API token) or answered 401. Every
repository a tool touches goes through :func:`~coverset.web.principal.require_repo` (``read``), so a
repository the caller cannot see answers 404 exactly like one that does not exist, and
``list_repos`` lists only visible ones. An API token must carry the ``read``, ``write`` or ``admin``
scope: an ``upload``-only token (made for CI bundle uploads) is refused with 403
``insufficient_scope``. Bearer requests need no CSRF token; a browser session's ``POST`` does.

Rate limit: :data:`AGENT_RPM` calls a minute per principal (a token, a user, or the admin), with the
API's :class:`~coverset.web.api.SlidingWindowLimiter`; beyond it 429 ``rate_limited`` with
``Retry-After``. The self-description is not charged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from quart import Blueprint, Response, current_app, jsonify, request

from coverset import __version__
from coverset.agent.tools import MAX_ROWS, TOOLS, ToolError, tool_catalogue
from coverset.web.api import SlidingWindowLimiter
from coverset.web.context import app_db, app_settings
from coverset.web.principal import Principal, current_principal, require_repo, visible_repo_ids

if TYPE_CHECKING:
    from coverset.agent.service import AgentService

__all__ = ["AGENT_RPM", "bp"]

bp = Blueprint("agent_api", __name__)

AGENT_RPM: Final = 120
"""Tool calls a minute per principal."""
LIMITER_EXTENSION: Final = "agent_api_limiter"
READ_SCOPES: Final = frozenset({"read", "write", "admin"})
MAX_BODY_BYTES: Final = 64 * 1024


def _limiter() -> SlidingWindowLimiter:
    found = current_app.extensions.get(LIMITER_EXTENSION)
    if found is None:
        found = current_app.extensions[LIMITER_EXTENSION] = SlidingWindowLimiter(AGENT_RPM)
    return found


def _principal_key(principal: Principal) -> str:
    """The rate-limit key: one budget per token, per user, or for the instance admin."""
    if principal.token_id is not None:
        return f"token:{principal.token_id}"
    if principal.user_id is not None:
        return f"user:{principal.user_id}"
    return f"{principal.kind}:{principal.label}"


def _error(exc: ToolError, headers: dict[str, str] | None = None) -> tuple[Response, int]:
    response = jsonify(exc.to_json())
    response.headers.update(headers or {})
    if exc.status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="coverset"'
    return response, exc.status


def _check_principal() -> Principal:
    principal = current_principal()
    if principal.is_public:
        raise ToolError("unauthorized", "an API token, a session or the admin token is required")
    if principal.kind == "token" and not (principal.scopes & READ_SCOPES):
        raise ToolError("insufficient_scope", "this token has no read scope")
    return principal


def _service() -> AgentService:
    """The tools over the facts store, authorized for the current principal.

    Imported here, not at module level: :mod:`coverset.agent` reads the pages' facts views, which
    live in this package, whose ``__init__`` imports this module."""
    from coverset.agent.backends import StoreBackend
    from coverset.agent.service import AgentService

    def authorize(repo_id: int) -> None:
        require_repo(repo_id, "read")

    return AgentService(StoreBackend(app_settings(), app_db(), authorize=authorize, visible=visible_repo_ids))


@bp.get("/v1/agent")
async def describe() -> Response | tuple[Response, int]:
    """The tool list with schemas (for ``coverset mcp serve --server`` and humans)."""
    try:
        _check_principal()
    except ToolError as exc:
        return _error(exc)
    return jsonify(
        {
            "service": "coverset agent API",
            "version": __version__,
            "tools": tool_catalogue(),
            "call": {"method": "POST", "path": "/v1/agent/<tool>", "body": "the arguments as a JSON object"},
            "limits": {"calls_per_minute": AGENT_RPM, "max_rows": MAX_ROWS},
            "mcp": "coverset mcp serve --server <this URL> --token <cst_ token with the read scope>",
        }
    )


@bp.route("/v1/agent/<tool>", methods=["GET", "POST"])
async def call(tool: str) -> Response | tuple[Response, int]:
    """Run one tool for the current principal (see the module docstring)."""
    try:
        principal = _check_principal()
    except ToolError as exc:
        return _error(exc)
    retry_after = _limiter().hit(_principal_key(principal))
    if retry_after is not None:
        return _error(
            ToolError("rate_limited", f"more than {AGENT_RPM} calls a minute"),
            {"Retry-After": str(int(retry_after + 0.999))},
        )
    try:
        args = await _arguments(tool)
        result = await _service().call(tool, args)
    except ToolError as exc:
        return _error(exc)
    return jsonify(result)


async def _arguments(tool: str) -> dict[str, Any]:
    """The call's arguments: the JSON body of a POST, or the query string of a GET (typed by the
    tool's schema; validation proper happens in the service)."""
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolError("unknown_tool", f"there is no tool {tool!r}", {"tools": sorted(TOOLS)})
    if request.method == "POST":
        if (request.content_length or 0) > MAX_BODY_BYTES:
            raise ToolError("invalid_argument", "the request body is too large")
        raw = await request.get_data(cache=False)
        if len(raw) > MAX_BODY_BYTES:
            raise ToolError("invalid_argument", "the request body is too large")
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise ToolError("invalid_argument", "the body must be a JSON object") from None
        if not isinstance(body, dict):
            raise ToolError("invalid_argument", "the body must be a JSON object")
        return body
    props = spec.input_schema.get("properties", {})
    out: dict[str, Any] = {}
    for key in request.args:
        prop = props.get(key)
        if prop is None:
            out[key] = request.args.get(key)
            continue
        values = request.args.getlist(key)
        kind = prop.get("type")
        try:
            if kind == "integer":
                out[key] = int(values[-1])
            elif kind == "number":
                out[key] = float(values[-1])
            elif kind == "boolean":
                out[key] = values[-1].strip().lower() in ("1", "true", "yes", "on")
            elif kind == "array":
                out[key] = [part.strip() for value in values for part in value.split(",") if part.strip()]
            else:
                out[key] = values[-1]
        except ValueError:
            raise ToolError("invalid_argument", f"{tool}.{key} must be a {kind}") from None
    return out
