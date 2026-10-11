"""``/runs/<run_id>``: the run page, and ``/runs/<run_id>/events``: its server-sent events.

``?replay=1&speed=0.25`` replays a stored run (``freeze=<seconds>`` stops the replay at that point
of the run's clock). A run awaiting approval shows the Approve button (``POST /runs/<id>/approve``,
served by :mod:`coverset.web.api`) to admins.
"""

from __future__ import annotations

from quart import Blueprint, Response, request, stream_with_context, url_for
from werkzeug.exceptions import NotFound

from coverset.web.api import RUN_ID_PARAM, SEQ_PARAM
from coverset.web.auth import is_admin
from coverset.web.context import app_db
from coverset.web.loaders import run_counts
from coverset.web.pages import render_page
from coverset.web.run_view import TERMINAL, build_state, load_bundle, load_events, order_rows, regions_context
from coverset.web.runstate import RunState
from coverset.web.sse import page_stream, parse_speed

bp = Blueprint("runs", __name__)


def _freeze() -> float | None:
    raw = request.args.get("freeze")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value == value and 0 <= value < 86400 else None


@bp.get("/runs/<run_id>")
async def run_page(run_id: str) -> Response:
    """Timeline, shard Gantt, why-this-order, failures and the test table of one run."""
    if not RUN_ID_PARAM.fullmatch(run_id):
        raise NotFound()
    replay = request.args.get("replay") == "1"
    speed = parse_speed(request.args.get("speed"))
    freeze = _freeze()
    async with app_db().session() as session:
        bundle = await load_bundle(session, run_id)
        if bundle is None:
            raise NotFound()
        events = await load_events(session, run_id)
        _order, results = await order_rows(session, bundle.run)
        counts = (await run_counts(session, [run_id])).get(run_id, {})
    final = events[-1].at if events else None
    state = RunState(run_id) if replay else build_state(run_id, events)
    context = regions_context(bundle, state, replay=replay, final_clock=final, first_render=True)
    run = bundle.run
    stream_url = None
    if replay and events:
        params = {"replay": "1", "speed": f"{speed:g}"}
        if freeze is not None:
            params["freeze"] = f"{freeze:g}"
        stream_url = url_for("runs.run_events", run_id=run_id, **params)
    elif run.status not in TERMINAL:
        stream_url = url_for("runs.run_events", run_id=run_id, after=state.last_seq)
    return await render_page(
        "run.html",
        **context,
        results=results,
        result_counts=counts,
        stream_url=stream_url,
        speed=speed,
        freeze=freeze,
        can_approve=is_admin() and run.status == "awaiting_approval",
        has_events=bool(events),
    )


@bp.get("/runs/<run_id>/events")
async def run_events(run_id: str) -> Response:
    """The run page's SSE stream of re-rendered regions (see :mod:`coverset.web.sse`)."""
    if not RUN_ID_PARAM.fullmatch(run_id):
        raise NotFound()
    async with app_db().session() as session:
        bundle = await load_bundle(session, run_id)
    if bundle is None:
        raise NotFound()
    replay = request.args.get("replay") == "1"
    after_raw = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
    after = int(after_raw) if SEQ_PARAM.fullmatch(after_raw) else 0
    stream = stream_with_context(page_stream)(
        app_db().sessionmaker,
        run_id,
        after=max(0, after),
        replay=replay,
        speed=parse_speed(request.args.get("speed")),
        freeze=_freeze(),
    )
    response = Response(
        stream,  # type: ignore[arg-type]
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    response.timeout = None
    return response
