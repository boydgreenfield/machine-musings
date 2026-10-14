"""The Economy pages: what each test costs against what it alone covers (docs/economy.md).

Routes (every one calls :func:`~coverset.web.principal.require_repo` first, so a repository the
caller cannot see is a 404 before anything is read):

``GET /repos/<rid>/economy?run=&tab=``
    One run's test economy in five sections: ``overview`` (tiles, the leverage histogram, the
    biggest tests that add nothing alone, the PR test tax for a PR run), ``leverage`` (every test:
    statements, assertions, unique facts, leverage, cost per unique fact, duration and flags; sorted
    by ``sort``/``dir``, narrowed by ``only``), ``redundant`` (equivalence groups, dominated tests,
    the covering set's keeps and drops, always with the "candidates only" caveat), ``families``
    (parametrize cases against distinct flows) and ``functions`` (tested only indirectly, untested,
    exception handlers no test ran, linked into the source view). The run defaults to the newest
    default-branch baseline, as on the other pages.
``GET /repos/<rid>/economy/tests``
    The leverage table alone (the htmx fragment the column headers fetch; without JavaScript the
    same links load the full page).
``GET /repos/<rid>/economy/deselect.txt?run=``
    The removable candidates as a ``pytest`` argument file (``pytest @deselect.txt``).
``GET /repos/<rid>/economy/pr-tax?run=``
    The PR page's "Test tax" card, computed from the stored economy with the repository's current
    ``policy.test_tax_threshold`` (the PR page renders the verdict's copy directly when the summary
    carries one, and fetches this only for runs that predate it).

Speed: the numbers were computed at ``finalize_run``; a view reads the run's four economy tables
(and its base run's test ids) once, keeps the result in a small LRU keyed by the tables' files and
mtimes (a re-ingest changes them), and the templates only format. Sorting a 10,000-test table is
one Python sort over rows already in memory.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from typing import Any, Final

from quart import Blueprint, Response, request, url_for
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from coverset.db.models import PullRequest, Repo, Run
from coverset.facts.schema import ECONOMY_TABLES
from coverset.facts.store import FactsStore
from coverset.report.economy import (
    FILTERS,
    SORTS,
    EconomyView,
    deselect_args,
    fmt_leverage,
    leverage_bins,
    median_leverage,
    minimal_split,
    redundancy_of,
    render_histogram,
    sort_tests,
    tax_line_of,
    view_from_store,
    zero_unique_biggest,
)
from coverset.web.context import app_db, app_settings
from coverset.web.loaders import default_run, require_run, store
from coverset.web.pages import get_repo, parse_int, render_page
from coverset.web.principal import require_repo

__all__ = ["DEFAULT_LIMIT", "TABS", "bp", "cached_view", "clear_cache", "leverage_filter"]

bp = Blueprint("economy", __name__)

TABS: Final = (
    ("overview", "Overview"),
    ("leverage", "Leverage"),
    ("redundant", "Redundant"),
    ("families", "Families"),
    ("functions", "Functions"),
)
DEFAULT_LIMIT: Final = 200
"""Rows the leverage table shows before "show all" (a 10,000-test page stays light)."""
COLUMNS: Final = (
    ("nodeid", "Test", False),
    ("statements", "Statements", True),
    ("asserts", "Asserts", True),
    ("unique", "Unique facts", True),
    ("leverage", "Leverage", True),
    ("cost", "Cost / fact", True),
    ("duration", "Duration", True),
)
"""Sortable columns of the leverage table: (sort name, header, numeric)."""
CACHE_SIZE: Final = 8
RECENT_RUNS: Final = 8

_cache: OrderedDict[tuple[Any, ...], EconomyView | None] = OrderedDict()
_lock = threading.Lock()


def _files_key(facts: FactsStore, rid: int, run_id: str) -> tuple[tuple[str, int, int], ...]:
    """The economy tables' files with mtime and size: a re-ingest or re-finalize changes the key."""
    found = []
    for table in ECONOMY_TABLES:
        for path in facts.table_files(rid, run_id, table):
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            found.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(found)


def cached_view(facts: FactsStore, rid: int, run_id: str, threshold: float | None) -> EconomyView | None:
    """:func:`~coverset.report.economy.view_from_store`, remembered per (run, tables, threshold)."""
    key = (str(facts.root), rid, run_id, threshold, _files_key(facts, rid, run_id))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    view = view_from_store(facts, rid, run_id, threshold=threshold)
    with _lock:
        _cache[key] = view
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return view


def clear_cache() -> None:
    """Forget every cached view (tests)."""
    with _lock:
        _cache.clear()


async def _threshold(session: Any, repo: Repo) -> float | None:
    """The repository's effective ``policy.test_tax_threshold`` (None if it cannot be resolved)."""
    try:
        from coverset.settings_scopes import effective_policy

        return float((await effective_policy(session, repo, app_settings())).test_tax_threshold)
    except Exception:  # a broken settings row must not take the page down: the stored flag stands
        return None


async def _load(rid: int) -> tuple[Repo, Run | None, EconomyView | None, dict[str, Any]]:
    """Authorize, then the repository, the selected run, its economy view and the page's context."""
    require_repo(rid, "read")
    repo = await get_repo(rid)
    async with app_db().session() as session:
        wanted = request.args.get("run")
        run = await require_run(session, wanted, rid) if wanted else await default_run(session, repo)
        threshold = await _threshold(session, repo)
        recent = list(
            await session.scalars(
                select(Run)
                .where(Run.repo_id == rid, Run.status == "completed")
                .order_by(Run.created_at.desc())
                .limit(RECENT_RUNS)
            )
        )
        pr_ids = {r.pr_id for r in recent if r.pr_id} | (
            {run.pr_id} if run is not None and run.pr_id else set()
        )
        prs = (
            {p.id: p for p in await session.scalars(select(PullRequest).where(PullRequest.id.in_(pr_ids)))}
            if pr_ids
            else {}
        )
    view = await asyncio.to_thread(cached_view, store(), rid, run.id, threshold) if run is not None else None
    return repo, run, view, {"recent": recent, "prs": prs, "threshold": threshold}


def _table_args() -> dict[str, Any]:
    """The leverage table's sort, direction, filter and row limit from the query (bad values: defaults)."""
    sort = request.args.get("sort", "leverage")
    sort = sort if sort in SORTS else "leverage"
    reverse = request.args.get("dir") == "rev"
    only = request.args.get("only") or None
    only = only if only in FILTERS else None
    raw = request.args.get("limit")
    limit = None if raw == "all" else (parse_int(raw, default=DEFAULT_LIMIT, minimum=1) or DEFAULT_LIMIT)
    return {"sort": sort, "reverse": reverse, "only": only, "limit": limit}


def _table(view: EconomyView, args: dict[str, Any]) -> dict[str, Any]:
    rows = sort_tests(view.tests, args["sort"], reverse=args["reverse"], only=args["only"])
    limit = args["limit"]
    return {
        "rows": rows if limit is None else rows[:limit],
        "rows_total": len(rows),
        "columns": COLUMNS,
        "filters": FILTERS,
        "sort_label": next(label for key, label, _numeric in COLUMNS if key == args["sort"]),
        "descending": SORTS[args["sort"]][1] != args["reverse"],
        **args,
    }


def _links(rid: int, run: Run) -> dict[str, Any]:
    """URL builders the templates share (the full page and the table fragment keep each other's args)."""

    def page(**params: Any) -> str:
        return url_for("economy.economy_page", rid=rid, run=run.id, **{k: v for k, v in params.items() if v})

    def fragment(**params: Any) -> str:
        return url_for(
            "economy.tests_fragment", rid=rid, run=run.id, **{k: v for k, v in params.items() if v}
        )

    return {"page_url": page, "fragment_url": fragment}


@bp.get("/repos/<int:rid>/economy")
async def economy_page(rid: int) -> Response:
    """The Economy page of one run (see the module docstring for the sections)."""
    repo, run, view, extra = await _load(rid)
    tab = request.args.get("tab", "overview")
    tab = tab if tab in dict(TABS) else "overview"
    context: dict[str, Any] = {"repo": repo, "run": run, "view": view, "tab": tab, "tabs": TABS, **extra}
    if run is not None:
        context.update(_links(rid, run))
    if view is not None and view.totals is not None:
        if tab == "overview":
            context["histogram"] = render_histogram(leverage_bins(view.tests))
            context["median"] = median_leverage(view.tests)
            context["zero_big"] = zero_unique_biggest(view.tests)
            context["tax"] = tax_line_of(view)
        elif tab == "leverage":
            context.update(_table(view, _table_args()))
        elif tab == "redundant":
            context["redundancy"] = redundancy_of(view)
            context["minimal"] = minimal_split(view)
        elif tab == "functions":
            context["indirect"] = [f for f in view.functions if f.indirect_only]
            context["untested"] = [f for f in view.functions if f.untested]
            context["handlers"] = [f for f in view.functions if f.handlers_unexercised]
            context["not_sampled"] = sum(1 for f in view.functions if f.covered and not f.observed)
    return await render_page("economy/page.html", **context)


@bp.get("/repos/<int:rid>/economy/tests")
async def tests_fragment(rid: int) -> Response:
    """The leverage table alone (htmx swaps it in place when a header is clicked)."""
    repo, run, view, _extra = await _load(rid)
    if run is None or view is None or view.totals is None:
        raise NotFound("This run has no test economy.")
    context = {"repo": repo, "run": run, "view": view, **_links(rid, run), **_table(view, _table_args())}
    return await render_page("economy/tests_fragment.html", layout="_fragment.html", **context)


@bp.get("/repos/<int:rid>/economy/deselect.txt")
async def deselect(rid: int) -> Response:
    """The removable candidates as a ``pytest`` argument file (``pytest @<file>``)."""
    repo, run, view, _extra = await _load(rid)
    if run is None or view is None or view.totals is None:
        raise NotFound("This run has no test economy.")
    response = Response(deselect_args(view), mimetype="text/plain")
    name = f"coverset-deselect-{run.id}.txt"
    response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/repos/<int:rid>/economy/pr-tax")
async def pr_tax(rid: int) -> Response:
    """The "Test tax" card of a PR run (empty when the run has no tax: the slot then disappears)."""
    repo, run, view, _extra = await _load(rid)
    tax = tax_line_of(view) if view is not None else None
    if run is None or tax is None:
        return Response("", mimetype="text/html")
    return await render_page(
        "economy/tax_fragment.html", layout="_fragment.html", repo=repo, run=run, tax=tax
    )


@bp.app_template_filter("leverage")
def leverage_filter(value: float | None) -> str:
    """:func:`~coverset.report.economy.fmt_leverage`: ``0``, ``0.38``, ``2.5``, ``14``, ``–``."""
    return fmt_leverage(value)
