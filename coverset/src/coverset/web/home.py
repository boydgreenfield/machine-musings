"""Placeholder home page until the dashboard blueprint (phase 8) takes over ``/``."""

from __future__ import annotations

from quart import Blueprint, Response

bp = Blueprint("home", __name__)

_PAGE = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>coverset</title></head>'
    "<body><main><h1>coverset</h1></main></body></html>\n"
)


@bp.get("/")
async def index() -> Response:
    """A static page; exists so access control has an ordinary protected route."""
    return Response(_PAGE, mimetype="text/html")
