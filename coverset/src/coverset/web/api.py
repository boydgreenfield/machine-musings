"""Machine-facing HTTP API (contract: plan Addendum A). Later phases add the ``/v1`` routes."""

from __future__ import annotations

from quart import Blueprint, Response, jsonify

from coverset import __version__

bp = Blueprint("api", __name__)


@bp.get("/healthz")
async def healthz() -> Response:
    """Liveness probe: ``{"status": "ok", "version": "<coverset version>"}``."""
    return jsonify({"status": "ok", "version": __version__})
