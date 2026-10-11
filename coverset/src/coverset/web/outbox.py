"""``/forge/outbox``: every recorded forge call, with comments and check summaries rendered.

Sticky comments and check-run summaries are shown as GitHub-flavoured markdown (markdown-it-py,
``html=False``) under "Would be posted to GitHub (fake forge)"; other calls show their request.
"""

from __future__ import annotations

import json
from typing import Any

from quart import Blueprint, Response, request
from sqlalchemy import func, select

from coverset.db.models import ForgeCall, Repo
from coverset.web.context import app_db
from coverset.web.pages import parse_int, render_page
from coverset.web.render.markdown import render_sticky

bp = Blueprint("outbox", __name__)

PAGE = 50


def _view(call: ForgeCall) -> dict[str, Any]:
    req = call.request or {}
    body = None
    title = None
    conclusion = None
    if call.method == "upsert_pr_comment":
        body = render_sticky(req.get("body"))
        title = f"Sticky comment on PR #{req.get('pr_number')}"
    elif call.method in ("create_check", "update_check"):
        output = req.get("output") or {}
        if output.get("summary"):
            body = render_sticky(output.get("summary"))
        title = output.get("title") or req.get("name") or "check run"
        conclusion = req.get("conclusion") or req.get("status")
    elif call.method == "set_commit_status":
        title = f"{req.get('context')}: {req.get('state')}"
    shown = {k: v for k, v in req.items() if k not in ("body", "output")}
    return {
        "call": call,
        "body": body,
        "title": title,
        "conclusion": conclusion,
        "request_json": json.dumps(shown, indent=2, sort_keys=True, default=str),
        "response_json": json.dumps(call.response, indent=2, sort_keys=True, default=str)
        if call.response
        else None,
    }


@bp.get("/forge/outbox")
async def outbox() -> Response:
    """The newest forge calls first; ``?method=`` filters, ``?page=`` pages."""
    page = parse_int(request.args.get("page"), default=1, minimum=1) or 1
    method = request.args.get("method") or None
    async with app_db().session() as session:
        query = select(ForgeCall)
        total_query = select(func.count()).select_from(ForgeCall)
        if method:
            query = query.where(ForgeCall.method == method)
            total_query = total_query.where(ForgeCall.method == method)
        calls = list(
            await session.scalars(query.order_by(ForgeCall.id.desc()).offset((page - 1) * PAGE).limit(PAGE))
        )
        total = await session.scalar(total_query) or 0
        methods = [m for (m,) in (await session.execute(select(ForgeCall.method).distinct())).all()]
        repos = {r.id: r for r in await session.scalars(select(Repo))}
    return await render_page(
        "outbox.html",
        calls=[_view(c) for c in calls],
        repos=repos,
        page=page,
        pages=max(1, (total + PAGE - 1) // PAGE),
        total=total,
        method=method,
        methods=sorted(methods),
    )
