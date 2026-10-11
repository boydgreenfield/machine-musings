"""Page plumbing shared by the UI blueprints: full page vs fragment, filters and the nav context.

Every UI route renders through :func:`render_page`. An htmx request (``HX-Request: true``) that is
not a boosted navigation gets the fragment (the template's ``main`` block inside
``_fragment.html``); anything else gets the full layout. Responses vary on ``HX-Request``.
Displayed times are relative to ``settings.now()``, so ``COVERSET_FAKE_NOW`` pins them.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from quart import Blueprint, Response, render_template, request
from sqlalchemy import select
from werkzeug.exceptions import NotFound

import coverset
from coverset.db.models import Repo
from coverset.web.context import app_db, app_settings

__all__ = [
    "bp",
    "is_fragment",
    "load_numbers",
    "render_page",
]

bp = Blueprint("pages", __name__)

HTMX_CONFIG = json.dumps(
    {
        "selfRequestsOnly": True,
        "allowScriptTags": False,
        "allowEval": False,
        "includeIndicatorStyles": False,
        "historyCacheSize": 0,
        "defaultSwapStyle": "innerHTML",
    },
    separators=(",", ":"),
)
NUMBERS_FILE = Path(coverset.__file__).resolve().parents[2] / "docs" / "numbers.json"


def is_fragment() -> bool:
    """True for an htmx request that is not a boosted page navigation."""
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-Boosted") != "true"


async def render_page(template: str, *, status: int = 200, **context: Any) -> Response:
    """Render ``template`` as a fragment or a full page (see the module docstring)."""
    context.setdefault("layout", "_fragment.html" if is_fragment() else "layout.html")
    body = await render_template(template, **context)
    response = Response(body, status=status, mimetype="text/html")
    response.headers["Vary"] = "HX-Request"
    return response


def load_numbers(path: Path | None = None) -> dict[str, Any]:
    """``docs/numbers.json`` (empty when absent or unreadable)."""
    target = path or NUMBERS_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_int(value: str | None, *, default: int | None = None, minimum: int = 0) -> int | None:
    """An ASCII integer query value (else ``default``)."""
    if value is None or not value.isascii() or not value.isdigit() or len(value) > 12:
        return default
    number = int(value)
    return number if number >= minimum else default


async def get_repo(rid: int) -> Repo:
    """The repo with id ``rid`` or 404."""
    async with app_db().session() as session:
        repo = await session.get(Repo, rid)
    if repo is None:
        raise NotFound()
    return repo


# -- filters ----------------------------------------------------------------------------------------


@bp.app_template_filter("ago")
def ago(moment: datetime | None) -> str:
    """``3 min ago`` relative to ``settings.now()``."""
    if moment is None:
        return "never"
    seconds = (app_settings().now() - moment).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)} s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


@bp.app_template_filter("stamp")
def stamp(moment: datetime | None) -> str:
    """``2026-10-11 13:21 UTC``."""
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment is not None else ""


@bp.app_template_filter("secs")
def secs(value: float | None) -> str:
    """``1.0 s``, ``41 s``, ``3 min``."""
    if value is None:
        return "–"
    if value < 10:
        return f"{value:.1f} s"
    if value < 600:
        return f"{value:.0f} s"
    return f"{value / 60:.0f} min"


@bp.app_template_filter("ms")
def ms(value: float | None) -> str:
    """Milliseconds as ``12 ms`` or ``1.4 s``."""
    if value is None:
        return "–"
    if value < 10:
        return f"{value:.1f} ms"
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.1f} s"


@bp.app_template_filter("pct")
def pct(value: float | None) -> str:
    """``71%`` (one decimal when needed), ``n/a`` for None."""
    if value is None:
        return "n/a"
    return f"{value:.0f}%" if abs(value - round(value)) < 0.05 else f"{value:.1f}%"


@bp.app_template_filter("ordinal")
def ordinal(number: int | None) -> str:
    """``1st``, ``66th``."""
    if number is None:
        return "–"
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


@bp.app_template_filter("sha")
def short_sha(value: str | None) -> str:
    """The first 8 characters of a SHA."""
    return (value or "")[:8]


@bp.app_template_filter("plural")
def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``1 test``, ``2 tests``."""
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


@bp.app_context_processor
async def _nav() -> dict[str, Any]:
    """Repos for the header nav and the repo the current page belongs to (skipped for SSE frames)."""
    if request.endpoint == "runs.run_events":
        return {}
    async with app_db().session() as session:
        repos = list(await session.scalars(select(Repo).order_by(Repo.id).limit(20)))
    settings = app_settings()
    args = request.view_args or {}
    current = None
    if isinstance(args.get("rid"), int):
        current = next((r for r in repos if r.id == args["rid"]), None)
    return {
        "nav_repos": repos,
        "nav_repo": current or (repos[0] if repos else None),
        "htmx_config": HTMX_CONFIG,
        "settings_paused": settings.paused,
        "ui_settings": {
            "executor": settings.executor,
            "modal_environment": settings.modal_environment,
            "modal_daily_budget_usd": settings.modal_daily_budget_usd,
            "sandbox_network": settings.sandbox_network,
            "paused": settings.paused,
            "max_concurrent_runs": settings.max_concurrent_runs,
            "admin_token_set": settings.admin_token is not None,
        },
    }
