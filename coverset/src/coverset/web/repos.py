"""Repo-scoped pages: the repo overview, the PR page, the treemap, tests and invariants.

The repo page opens with an overview of the default branch's baselines (tiles from the latest one,
the trend of line % and input-class %, a Gap treemap thumbnail, what changed since the previous
baseline and the largest gaps), read from the facts store; the dashboard's repository rows get the
latest line % and input-class % with a sparkline through :func:`_dashboard_trends`.

Routes: ``/repos/<rid>``, ``/repos/<rid>/prs/<n>`` (``/runs/<run_id>/coverage`` redirects there),
``/repos/<rid>/map?mode=gap|lines|inputs&path=&run=``, ``/repos/<rid>/tests`` with
``POST /repos/<rid>/tests/<test_id>/quarantine`` (admin, CSRF), and ``/repos/<rid>/invariants``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import defaultdict
from datetime import datetime
from typing import Any

from markupsafe import Markup
from quart import Blueprint, Response, g, redirect, request, url_for
from sqlalchemy import func, select
from werkzeug.exceptions import NotFound

from coverset.ai.review import SOURCE_LABELS
from coverset.db.models import Invariant, InvariantResult, PullRequest, Repo, Run, TestCase, TestStats
from coverset.facts.queries import (
    BaselinePoint,
    baseline_candidate_limit,
    baseline_trend,
    class_changes,
    top_gaps,
)
from coverset.forge.comment import missing_counts
from coverset.orchestrator.repos import mirror_path
from coverset.orchestrator.verdict import summary_from_json
from coverset.web.audit import record_audit
from coverset.web.context import app_db, app_settings
from coverset.web.detail import build_cards, card_detail
from coverset.web.facts_view import run_facts
from coverset.web.loaders import (
    default_run,
    facts_for,
    findings_for_run,
    invariant_rows,
    pr_run,
    require_run,
    run_counts,
    store,
)
from coverset.web.pages import MAX_DB_ID, get_repo, render_page
from coverset.web.pr_view import analyse_diff
from coverset.web.principal import current_principal, require_repo
from coverset.web.render.sparkline import SparkSeries, render_sparkline
from coverset.web.render.treemap import (
    ALGORITHMS,
    MAP_COOKIE,
    MODES,
    MODULE_WEIGHTS,
    MODULE_WHY,
    Rect,
    TreeItem,
    anchor_to,
    build_tree,
    explicit_view_prefs,
    layout,
    reader_map_choice,
    render_legend,
    render_treemap,
    resolve_view,
    weigh_modules,
)
from coverset.web.run_view import order_rows

bp = Blueprint("repos", __name__)

HISTORY_RUNS = 12
TOP_GAPS = 8
THUMB_W, THUMB_H = 520, 220


async def request_view_prefs() -> dict[str, Any]:
    """The scoped settings' view preferences someone actually chose for this request
    (:func:`~coverset.web.render.treemap.explicit_view_prefs` of the settings layer's
    ``view_prefs``): ``user > repo > org > instance``. Values left at their built-in default are
    left out, so the reader's own choice in this browser (localStorage, applied by the page's
    script) still comes before the default. Read once per request; a page never breaks over its view
    settings."""
    found = g.get("_coverset_view_choices")
    if found is None:
        prefs: Any = None
        try:
            from coverset.web.settings_pages import request_view_prefs as settings_view_prefs

            prefs = await settings_view_prefs()
        except Exception:  # noqa: BLE001 - no settings layer (or no database): the defaults
            prefs = None
        found = explicit_view_prefs(prefs)
        g._coverset_view_choices = found
    return found


def trend_sparklines(points: list[BaselinePoint], *, width: int = 220, label: str = "") -> dict[str, Markup]:
    """``lines`` (line %, solid) and ``inputs`` (input-class %, dashed) over ``points``: one
    sparkline each, so each fits its own range (line % sits near 90 while input-class % sits
    near 35, and on one shared axis both would flatten into lines)."""
    names = [f"{p.head_sha[:8]} · {p.created_at:%Y-%m-%d %H:%M} UTC" for p in points]
    lines = SparkSeries("lines run", tuple(p.line_pct for p in points))
    inputs = SparkSeries("input classes", tuple(p.input_pct for p in points), dashed=True)
    return {
        "lines": render_sparkline([lines], points=names, width=width, label=label, noun="baseline"),
        "inputs": render_sparkline([inputs], points=names, width=width, label=label, noun="baseline"),
    }


def trend_span(first: datetime, last: datetime, count: int) -> str:
    """How the trend's baselines spread in time, after "N baselines": within two days the span
    and the newest stamp ("over 3 min, newest 2026-10-12 01:50 UTC"), further apart the two dates
    ("2026-10-01 → 2026-10-11"); one baseline is just its stamp."""
    newest = f"{last:%Y-%m-%d %H:%M} UTC"
    if count <= 1:
        return newest
    seconds = max(0.0, (last - first).total_seconds())
    if seconds < 60:
        return f"within a minute, newest {newest}"
    if seconds < 3600:
        return f"over {round(seconds / 60)} min, newest {newest}"
    if seconds < 2 * 86400:
        return f"over {round(seconds / 3600)} h, newest {newest}"
    return f"{first:%Y-%m-%d} → {last:%Y-%m-%d}"


async def baseline_candidates(
    rid: int, branch: str, last_n: int, *, until: datetime | None = None
) -> list[str]:
    """The newest runs of ``branch`` with no PR (at or before ``until``), from the database, newest
    first: the only runs whose ``runs_dim`` the baseline queries then read (see
    :func:`coverset.facts.queries.baseline_candidate_limit`)."""
    query = select(Run.id).where(Run.repo_id == rid, Run.pr_id.is_(None), Run.branch == branch)
    if until is not None:
        query = query.where(Run.created_at <= until)
    query = query.order_by(Run.created_at.desc(), Run.id.desc()).limit(baseline_candidate_limit(last_n))
    async with app_db().session() as session:
        return list(await session.scalars(query))


THUMB_MIN_TILE = 64.0
"""Tiles of the overview's thumbnail smaller than this (px², about 8 × 8) are merged."""


def thumbnail_tree(
    tree: TreeItem, width: float, height: float, *, min_area: float = THUMB_MIN_TILE
) -> TreeItem:
    """``tree`` for a thumbnail: under each directory or file, the file and function tiles that
    would be drawn smaller than ``min_area`` px² are merged into one tile of the same total size
    (statements and lines run summed, input-class share weighted by statements) that opens the map
    at that directory or file. The proportions and colours stay, the demo's tiles (all far larger)
    are untouched, and a large repository (24,000 functions of 3 px² each at bench scale) ships a
    few dozen tiles instead of tens of thousands."""
    leaves = layout(tree, Rect(0.0, 0.0, width, height))
    area = {id(leaf.item): leaf.rect.area for leaf in leaves}

    def prune(item: TreeItem) -> TreeItem:
        if not item.children:
            return item
        keep: list[TreeItem] = []
        tiny: list[TreeItem] = []
        for child in item.children:
            if child.kind != "dir" and area.get(id(child), 0.0) < min_area:
                tiny.append(child)
            else:
                keep.append(prune(child))
        if len(tiny) > 1:
            keep.append(_merged(item, tiny))
        else:
            keep.extend(prune(c) for c in tiny)
        return dataclasses.replace(item, children=keep)

    return prune(tree)


def _leaves(item: TreeItem) -> list[TreeItem]:
    if not item.children:
        return [item]
    return [leaf for child in item.children for leaf in _leaves(child)]


def _merged(parent: TreeItem, items: list[TreeItem]) -> TreeItem:
    """One leaf standing for ``items`` (functions of a file, or files of a directory)."""
    weighed = [
        (leaf.input_cov, leaf.statements)
        for item in items
        for leaf in _leaves(item)
        if leaf.input_cov is not None
    ]
    weight = sum(w for _, w in weighed)
    input_cov = None
    if weighed:
        input_cov = sum(v * w for v, w in weighed) / weight if weight else weighed[0][0]
    noun = "files" if all(i.kind == "file" for i in items) else "functions"
    name = f"{len(items)} small {noun}"
    return TreeItem(
        name=name,
        path=f"{parent.path}::{name}",
        kind="function",
        statements=sum(i.statements for i in items),
        covered=sum(i.covered for i in items),
        input_cov=input_cov,
        file=parent.path if noun == "files" else (items[0].file or parent.path),
        weight=float(sum(i.size for i in items)),
    )


def _overview_facts(rid: int, branch: str, candidates: list[str] | None = None) -> dict[str, Any] | None:
    """The facts behind the repo overview (blocking; run it in a thread), or None without a
    finalized baseline of ``branch``."""
    facts_store = store()
    trend = baseline_trend(facts_store, rid, branch, HISTORY_RUNS, candidates=candidates)
    if not trend:
        return None
    latest = trend[-1]
    prior = trend[-2] if len(trend) > 1 else None
    changes = class_changes(facts_store, rid, latest.run_id, prior.run_id) if prior else []
    gaps = top_gaps(facts_store, rid, latest.run_id, TOP_GAPS)
    nodes = run_facts(facts_store, rid, latest.run_id).treemap_nodes()
    return {
        "trend": trend,
        "latest": latest,
        "prior": prior,
        "changes": changes,
        "gaps": gaps,
        "nodes": nodes,
    }


async def repo_overview(repo_row: Repo, module_weight: str = "thin") -> dict[str, Any] | None:
    """The repo page's overview section (see the module docstring), or None without a baseline.
    The Gap thumbnail weighs module-level code like the map (``view.map_module_weight``)."""
    weight = module_weight if module_weight in MODULE_WEIGHTS else "thin"
    candidates = await baseline_candidates(repo_row.id, repo_row.default_branch, HISTORY_RUNS)
    found = await asyncio.to_thread(_overview_facts, repo_row.id, repo_row.default_branch, candidates)
    if found is None:
        return None
    latest: BaselinePoint = found["latest"]
    rid, run_id = repo_row.id, latest.run_id
    map_url = url_for("repos.map_page", rid=rid, run=run_id)

    def dir_url(item: Any) -> str:
        # Every tile opens the map: a directory or file zoomed in, a function at its file.
        return url_for("repos.map_page", rid=rid, run=run_id, path=item.path)

    def leaf_url(item: Any) -> str:
        return url_for("repos.map_page", rid=rid, run=run_id, path=item.file)

    thumb = render_treemap(
        thumbnail_tree(weigh_modules(build_tree(found["nodes"]), weight), THUMB_W, THUMB_H),  # type: ignore[arg-type]
        mode="gap",
        width=THUMB_W,
        height=THUMB_H,
        leaf_url=leaf_url,
        dir_url=dir_url,
        label=f"Gap treemap of {repo_row.full_name} (thumbnail; each tile opens the coverage map)",
        module_weight=weight,  # type: ignore[arg-type]
    )
    trend = found["trend"]
    return {
        **found,
        "run_id": run_id,
        "map_url": map_url,
        "thumb": thumb,
        "sparks": trend_sparklines(trend, label=f"{repo_row.default_branch} baselines"),
        "legend": render_legend("gap", weight),  # type: ignore[arg-type]
        "first": trend[0],
        "span": trend_span(trend[0].created_at, trend[-1].created_at, len(trend)),
    }


@bp.app_context_processor
async def _dashboard_trends() -> dict[str, Any]:
    """On the dashboard only: per repository, the latest baseline's line % and input-class % and
    a sparkline of both over the last baselines (``repo_trends``, by repo id)."""
    if request.endpoint != "dashboard.index":
        return {}
    async with app_db().session() as session:
        repos = [(r.id, r.default_branch, r.full_name) for r in await session.scalars(select(Repo))]
    candidates = {rid: await baseline_candidates(rid, branch, HISTORY_RUNS) for rid, branch, _ in repos}

    def load() -> dict[int, dict[str, Any]]:
        facts_store = store()
        out: dict[int, dict[str, Any]] = {}
        for rid, branch, name in repos:
            trend = baseline_trend(facts_store, rid, branch, HISTORY_RUNS, candidates=candidates[rid])
            if trend:
                out[rid] = {
                    "latest": trend[-1],
                    "count": len(trend),
                    "sparks": trend_sparklines(trend, width=160, label=f"{name} {branch} baselines"),
                }
        return out

    return {"repo_trends": await asyncio.to_thread(load)}


@bp.get("/repos/<int:rid>")
async def repo(rid: int) -> Response:
    """PRs with their latest run, default-branch baselines and links to the repo's views."""
    require_repo(rid)
    repo_row = await get_repo(rid)
    async with app_db().session() as session:
        prs = list(
            await session.scalars(
                select(PullRequest).where(PullRequest.repo_id == rid).order_by(PullRequest.number.desc())
            )
        )
        latest = {pr.id: await pr_run(session, pr) for pr in prs}
        baselines = list(
            await session.scalars(
                select(Run)
                .where(Run.repo_id == rid, Run.pr_id.is_(None))
                .order_by(Run.created_at.desc())
                .limit(HISTORY_RUNS)
            )
        )
        current = await default_run(session, repo_row)
        counts = await run_counts(session, [r.id for r in [*baselines, *(x for x in latest.values() if x)]])
        tests = await session.scalar(
            select(func.count()).select_from(TestCase).where(TestCase.repo_id == rid)
        )
    modules, _ = resolve_view(
        "map_module_weight",
        request.args.get("modules"),
        await request_view_prefs(),
        MODULE_WEIGHTS,
        "thin",
        reader_map_choice(request.cookies.get(MAP_COOKIE)).get("map_module_weight"),
    )
    overview = await repo_overview(repo_row, modules)
    return await render_page(
        "repo.html",
        overview=overview,
        repo=repo_row,
        prs=prs,
        latest=latest,
        baselines=baselines,
        current=current,
        counts=counts,
        tests=tests or 0,
    )


@bp.get("/runs/<run_id>/coverage")
async def run_coverage(run_id: str) -> Response:
    """A PR run's coverage lives on its PR page; other runs go to their run page."""
    async with app_db().session() as session:
        run = await require_run(session, run_id)
        require_repo(run.repo_id)
        pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
    if pr is None:
        return redirect(url_for("runs.run_page", run_id=run.id), code=302)
    return redirect(url_for("repos.pr_page", rid=pr.repo_id, n=pr.number, run=run.id), code=302)


async def _pr(rid: int, n: int) -> tuple[Any, PullRequest, Run | None]:
    require_repo(rid)
    repo_row = await get_repo(rid)
    async with app_db().session() as session:
        pr = await session.scalar(
            select(PullRequest).where(PullRequest.repo_id == rid, PullRequest.number == n)
        )
        if pr is None:
            raise NotFound(f"There is no pull request #{n} in {repo_row.full_name}.")
        wanted = request.args.get("run")
        run = await require_run(session, wanted, rid) if wanted else await pr_run(session, pr)
        if run is not None and run.pr_id != pr.id:
            raise NotFound()
    return repo_row, pr, run


def summary_missing_counts(summary: dict[str, Any]) -> tuple[int, int] | None:
    """``(in changed functions, more in invariant targets)`` from a stored verdict summary, the
    same split the sticky comment's headline uses (None for a summary that does not parse)."""
    if not summary:
        return None
    try:
        parsed = summary_from_json(summary)
    except (TypeError, ValueError, KeyError):
        return None
    return missing_counts(parsed.function_rows, parsed.missing_input_classes)


def target_gaps(summary: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """``(function, never-exercised classes)`` of the invariant targets the run's changed-function
    rows do not cover: the "M more in invariant targets" the PR page and the comment count."""
    try:
        parsed = summary_from_json(summary) if summary else None
    except (TypeError, ValueError, KeyError):
        return []
    if parsed is None:
        return []
    changed = {row.qualname for row in parsed.function_rows}
    gaps: dict[str, list[str]] = defaultdict(list)
    for item in parsed.missing_input_classes:
        if item.function not in changed and item.class_name not in gaps[item.function]:
            gaps[item.function].append(item.class_name)
    return sorted(gaps.items())


@bp.get("/repos/<int:rid>/prs/<int:n>")
async def pr_page(rid: int, n: int) -> Response:
    """The PR page: verdict band, needs-attention cards and collapsed detail sections."""
    if n > MAX_DB_ID:
        raise NotFound(f"There is no pull request #{n}.")
    repo_row, pr, run = await _pr(rid, n)
    if run is None:
        return await render_page("pr.html", repo=repo_row, pr=pr, run=None, summary={}, cards=[])
    facts = await facts_for(run)
    analysis = None
    if facts.exists:
        analysis = await asyncio.to_thread(
            analyse_diff, mirror_path(app_settings(), rid), run.base_sha, run.head_sha, facts
        )
    async with app_db().session() as session:
        _order, results = await order_rows(session, run)
        invariants = await invariant_rows(session, run)
        keys = [c.region.func_key for c in (analysis.source_changes if analysis else []) if c.region]
        findings = await findings_for_run(session, run, keys)
        other_runs = list(
            await session.scalars(
                select(Run).where(Run.pr_id == pr.id).order_by(Run.created_at.desc()).limit(10)
            )
        )
    cards = []
    if analysis is not None and analysis.patch is not None:
        cards = await asyncio.to_thread(build_cards, facts, analysis, results, invariants, findings)
    first_detail = None
    if cards and cards[0].severity > 0:
        first_detail = await card_detail(repo_row, run, facts, cards[0], analysis, results, pr=pr)
    summary = run.summary_json or {}
    changed_invariants = summary.get("invariants_changed") or []
    return await render_page(
        "pr.html",
        missing=summary_missing_counts(summary),
        repo=repo_row,
        pr=pr,
        run=run,
        summary=summary,
        cards=cards,
        first_detail=first_detail,
        analysis=analysis,
        results=results,
        invariants=invariants,
        changed_invariants=changed_invariants,
        findings=findings,
        source_labels=SOURCE_LABELS,
        other_runs=other_runs,
        facts_exists=facts.exists,
    )


@bp.get("/repos/<int:rid>/prs/<int:n>/functions/<func_key>")
async def pr_card(rid: int, n: int, func_key: str) -> Response:
    """The expanded content of one needs-attention card (fragment)."""
    repo_row, pr, run = await _pr(rid, n)
    if run is None:
        raise NotFound()
    facts = await facts_for(run)
    analysis = await asyncio.to_thread(
        analyse_diff, mirror_path(app_settings(), rid), run.base_sha, run.head_sha, facts
    )
    async with app_db().session() as session:
        _order, results = await order_rows(session, run)
        invariants = await invariant_rows(session, run)
        findings = await findings_for_run(session, run, [func_key])
    cards = await asyncio.to_thread(build_cards, facts, analysis, results, invariants, findings)
    card = next((c for c in cards if c.region.func_key == func_key), None)
    if card is None:
        raise NotFound()
    detail = await card_detail(repo_row, run, facts, card, analysis, results, pr=pr)
    return await render_page("pr_card.html", repo=repo_row, pr=pr, run=run, card=card, detail=detail)


@bp.get("/repos/<int:rid>/map")
async def map_page(rid: int) -> Response:
    """The treemap (Gap mode by default) with a ranked table of the largest gaps."""
    require_repo(rid)
    repo_row = await get_repo(rid)
    prefs = await request_view_prefs()
    reader = reader_map_choice(request.cookies.get(MAP_COOKIE))
    mode, mode_from = resolve_view(
        "map_mode", request.args.get("mode"), prefs, MODES, "gap", reader.get("map_mode")
    )
    modules, modules_from = resolve_view(
        "map_module_weight",
        request.args.get("modules"),
        prefs,
        MODULE_WEIGHTS,
        "thin",
        reader.get("map_module_weight"),
    )
    algorithm = request.args.get("layout") if request.args.get("layout") in ALGORITHMS else "ordered"
    # the view parameters the page's own links keep (the mode is always in them)
    keep = {k: str(request.args.get(k)) for k in ("modules", "layout")}
    keep = {k: v for k, v in keep.items() if v in (MODULE_WEIGHTS if k == "modules" else ALGORITHMS)}
    map_view = {"mode": mode, "mode_from": mode_from, "modules": modules, "modules_from": modules_from}
    async with app_db().session() as session:
        wanted = request.args.get("run")
        run = await require_run(session, wanted, rid) if wanted else await default_run(session, repo_row)
        baseline = await default_run(session, repo_row) if run is not None and run.pr_id is not None else None
    if run is None:
        return await render_page("map.html", repo=repo_row, run=None, mode=mode, map_view=map_view)
    facts = await facts_for(run)
    nodes = await asyncio.to_thread(facts.treemap_nodes) if facts.exists else []
    root = request.args.get("path") or None
    known = {node.path for node in nodes}
    if root is not None and root not in known:
        root = None
    tree = weigh_modules(build_tree(nodes, root=root), modules)  # type: ignore[arg-type]
    frame_from = None
    if baseline is not None and baseline.id != run.id:
        # a PR run in its baseline's frame: the same rectangles, the PR's colours
        base_facts = await facts_for(baseline)
        if base_facts.exists:
            base_nodes = await asyncio.to_thread(base_facts.treemap_nodes)
            frame_root = root if root is None or root in {n.path for n in base_nodes} else None
            if root is None or frame_root is not None:
                frame = weigh_modules(build_tree(base_nodes, root=frame_root), modules)  # type: ignore[arg-type]
                tree, differs = anchor_to(tree, frame)
                frame_from = baseline if differs else None

    def leaf_url(item: Any) -> str:
        anchor = f"#L{item.def_line}" if item.def_line else ""
        return url_for("files.source", rid=rid, path=item.file, run=run.id) + anchor

    def dir_url(item: Any) -> str:
        if item.kind == "file":
            return url_for("files.source", rid=rid, path=item.path, run=run.id)
        return url_for("repos.map_page", rid=rid, mode=mode, path=item.path, run=run.id, **keep)

    svg = render_treemap(
        tree,
        mode=mode,  # type: ignore[arg-type]
        leaf_url=leaf_url,
        dir_url=dir_url,
        label=f"Treemap of {repo_row.full_name}",
        module_weight=modules,  # type: ignore[arg-type]
        algorithm=algorithm,  # type: ignore[arg-type]
    )
    leaves = [
        n for n in nodes if n.kind == "function" and n.statements and (not root or n.path.startswith(root))
    ]
    gaps, never_called, quiet = gap_rows(leaves)
    crumbs = []
    if root:
        parts = root.split("/")
        crumbs = [("/".join(parts[: i + 1]), part) for i, part in enumerate(parts)]
    return await render_page(
        "map.html",
        repo=repo_row,
        run=run,
        mode=mode,
        modes=MODES,
        svg=svg,
        legend=render_legend(mode, modules),  # type: ignore[arg-type]
        module_weights=MODULE_WEIGHTS,
        modules=modules,
        module_why=MODULE_WHY,
        keep=keep,
        map_view=map_view,
        frame_from=frame_from,
        gaps=gaps[:MAX_GAP_ROWS],
        gaps_total=len(gaps),
        never_called=never_called,
        quiet=quiet,
        root=root,
        crumbs=crumbs,
    )


MAX_GAP_ROWS = 15


def gap_rows(leaves: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """The "largest gaps" table: ``(gaps, never called, counts left out)``.

    A gap needs something to compare: a function some test called that expects input classes.
    Functions no test called are listed apart ("never called"); called functions that expect no
    input class (properties, no-argument functions) and module-level code are only counted.
    """
    gaps: list[dict[str, Any]] = []
    never: list[dict[str, Any]] = []
    quiet = {"functions": 0, "modules": 0}
    for node in leaves:
        file, _, name = node.path.partition("::")
        line = 100.0 * node.covered / node.statements
        row = {"node": node, "file": file, "name": name, "line": line}
        if node.func_key is None:
            quiet["modules"] += 1
        elif node.covered == 0:
            never.append(row)
        elif node.input_cov is None:
            quiet["functions"] += 1
        else:
            inputs = 100.0 * node.input_cov
            gaps.append({**row, "inputs": inputs, "gap": max(0.0, line - inputs)})
    gaps.sort(key=lambda g: (-g["gap"], -g["node"].statements, g["file"], g["name"]))
    never.sort(key=lambda g: (-g["node"].statements, g["file"], g["name"]))
    return gaps, never, quiet


async def _test_rows(rid: int, test_id: int | None = None) -> list[dict[str, Any]]:
    async with app_db().session() as session:
        query = (
            select(TestCase, TestStats)
            .outerjoin(
                TestStats, (TestStats.test_id == TestCase.id) & (TestStats.repo_id == TestCase.repo_id)
            )
            .where(TestCase.repo_id == rid)
        )
        if test_id is not None:
            query = query.where(TestCase.id == test_id)
        rows = (await session.execute(query)).all()
    out = []
    for case, stats in rows:
        runs_n = stats.runs_n if stats else 0
        out.append(
            {
                "id": case.id,
                "nodeid": case.nodeid,
                "quarantined": case.quarantined,
                "runs": runs_n,
                "fail_rate": (stats.fails_n / runs_n) if stats and runs_n else None,
                "fail_ewma": stats.fail_ewma if stats else 0.0,
                "flaky_n": stats.flaky_n if stats else 0,
                "flaky_score": stats.flaky_score if stats else 0.0,
                "p50": stats.p50_ms if stats else None,
                "p95": stats.p95_ms if stats else None,
                "last_fail_at": stats.last_fail_at if stats else None,
            }
        )
    return out


@bp.get("/repos/<int:rid>/tests")
async def tests_page(rid: int) -> Response:
    """Every test with fail rate, flakiness, durations and the quarantine toggle."""
    require_repo(rid)
    repo_row = await get_repo(rid)
    rows = await _test_rows(rid)
    sort = request.args.get("sort", "risk")
    if sort == "slow":
        rows.sort(key=lambda r: -(r["p95"] or 0.0))
    elif sort == "name":
        rows.sort(key=lambda r: r["nodeid"])
    else:
        sort = "risk"
        rows.sort(
            key=lambda r: (
                not r["quarantined"],
                -(r["fail_ewma"] or 0),
                -(r["flaky_score"] or 0),
                r["nodeid"],
            )
        )
    return await render_page(
        "tests.html", repo=repo_row, rows=rows, sort=sort, can_quarantine=current_principal().can(rid, "write")
    )


@bp.post("/repos/<int:rid>/tests/<int:test_id>/quarantine")
async def toggle_quarantine(rid: int, test_id: int) -> Response:
    """Flip a test's quarantine flag (needs ``write`` on the repo; the guard checked the CSRF token)."""
    principal = require_repo(rid, "write")
    async with app_db().session() as session:
        case = await session.get(TestCase, test_id)
        if case is None or case.repo_id != rid:
            raise NotFound()
        form = await request.form
        wanted = form.get("quarantined")
        case.quarantined = (wanted == "1") if wanted in ("0", "1") else not case.quarantined
        record_audit(
            session, principal, "quarantine", target_kind="test", target_id=test_id, repo_id=rid,
            details={"quarantined": case.quarantined},
        )
        await session.commit()
        quarantined = await session.scalar(
            select(func.count()).select_from(TestCase).where(TestCase.repo_id == rid, TestCase.quarantined)
        )
    rows = await _test_rows(rid, test_id)
    repo_row = await get_repo(rid)
    if request.headers.get("HX-Request") != "true":
        return redirect(url_for("repos.tests_page", rid=rid), code=303)  # type: ignore[return-value]
    return await render_page(
        "tests_row.html",
        repo=repo_row,
        row=rows[0],
        can_quarantine=True,
        oob=True,
        quarantined=quarantined or 0,
    )


@bp.get("/repos/<int:rid>/invariants")
async def invariants_page(rid: int) -> Response:
    """Invariants with status history and coverage lens; ``?run=`` compares a PR run with its base."""
    require_repo(rid)
    repo_row = await get_repo(rid)
    async with app_db().session() as session:
        specs = list(
            await session.scalars(select(Invariant).where(Invariant.repo_id == rid).order_by(Invariant.id))
        )
        history_rows = (
            await session.execute(
                select(InvariantResult, Invariant.inv_key, Run)
                .join(Invariant, Invariant.id == InvariantResult.invariant_id)
                .join(Run, Run.id == InvariantResult.run_id)
                .where(Invariant.repo_id == rid)
                .order_by(Run.created_at.desc())
            )
        ).all()
        wanted = request.args.get("run")
        selected = await require_run(session, wanted, rid) if wanted else None
        base_run = None
        if selected is not None and selected.scoring_basis_run_id:
            base_run = await session.get(Run, selected.scoring_basis_run_id)
        if selected is None:
            selected = await default_run(session, repo_row)
        selected_rows = await invariant_rows(session, selected) if selected else []
        base_rows = await invariant_rows(session, base_run) if base_run else []
        pr = await session.get(PullRequest, selected.pr_id) if selected and selected.pr_id else None
        pr_runs = list(
            await session.scalars(
                select(Run)
                .where(Run.repo_id == rid, Run.pr_id.is_not(None))
                .order_by(Run.created_at.desc())
                .limit(10)
            )
        )
    latest_spec: dict[str, Invariant] = {}
    for spec in specs:
        latest_spec[spec.inv_key] = spec
    history: dict[str, list[tuple[InvariantResult, Run]]] = defaultdict(list)
    for result, key, run in history_rows:
        if len(history[key]) < HISTORY_RUNS:
            history[key].append((result, run))
    base_status = {row["key"]: row["status"] for row in base_rows}
    changed = (selected.summary_json or {}).get("invariants_changed", []) if selected else []
    return await render_page(
        "invariants.html",
        target_gaps=target_gaps(selected.summary_json or {}) if selected else [],
        repo=repo_row,
        latest_spec=latest_spec,
        history=history,
        selected=selected,
        selected_rows=selected_rows,
        base_run=base_run,
        base_status=base_status,
        changed=changed,
        pr=pr,
        pr_runs=pr_runs,
    )
