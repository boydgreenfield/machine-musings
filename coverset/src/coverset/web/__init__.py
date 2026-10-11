"""HTTP surface of the control plane: blueprints, access control and request-scoped accessors."""

from __future__ import annotations

from quart import Quart, Response, request
from werkzeug.exceptions import HTTPException, InternalServerError, NotFound

from coverset.web import api, auth, dashboard, files, outbox, pages, repos, runs, setup, webhooks

MACHINE_PREFIXES = ("/v1/", "/webhooks/", "/healthz", "/static/")
"""Paths answered by machines: their errors stay JSON (``/v1``) or the framework's default."""


def register_blueprints(app: Quart) -> None:
    """Register every blueprint (``auth`` first so its access guard runs before other hooks) and
    the styled 404 and 500 pages for the UI."""
    app.register_blueprint(auth.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(repos.bp)
    app.register_blueprint(runs.bp)
    app.register_blueprint(files.bp)
    app.register_blueprint(outbox.bp)
    app.register_blueprint(webhooks.bp)
    app.register_blueprint(setup.bp)
    app.register_error_handler(404, _not_found)
    app.register_error_handler(500, _server_error)


def _machine_path() -> bool:
    return request.path.startswith(MACHINE_PREFIXES) or request.path == "/healthz"


async def _not_found(error: HTTPException) -> HTTPException | Response | tuple[Response, int]:
    """The styled "not found" page (``/v1/*`` keeps its JSON body, other machine paths the default)."""
    if _machine_path():
        from coverset.app import _http_error  # the app module imports this one

        return await _http_error(error)
    description = error.description if error.description != NotFound.description else None
    message = description or f"Nothing lives at {request.path}."
    return await pages.render_page(
        "error.html",
        status=404,
        code=404,
        heading="Not found",
        message=message,
        hint="The repository, run, pull request, function or file may never have existed in this "
        "data directory, or retention may have removed it.",
    )


async def _server_error(error: HTTPException) -> HTTPException | Response:
    """The styled error page for an unexpected failure (no traceback is ever shown)."""
    if _machine_path():
        return error if isinstance(error, HTTPException) else InternalServerError()
    try:
        return await pages.render_page(
            "error.html",
            status=500,
            code=500,
            heading="Something went wrong",
            message="The server could not build this page. The log of coverset serve has the details.",
            hint="Reload to try again.",
        )
    except Exception:  # the page itself needs the database; never fail the error handler
        return Response("Something went wrong (HTTP 500).", status=500, mimetype="text/plain")
