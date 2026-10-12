"""Repo-scoped pages: the repo overview, the PR page, the treemap, tests and invariants.

Routes: ``/repos/<rid>``, ``/repos/<rid>/prs/<n>`` (``/runs/<run_id>/coverage`` redirects there),
``/repos/<rid>/map?mode=gap|lines|inputs&path=&run=``, ``/repos/<rid>/tests`` with
``POST /repos/<rid>/tests/<test_id>/quarantine`` (admin, CSRF), and ``/repos/<rid>/invariants``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from quart import Blueprint, Response, redirect, request, url_for
from sqlalchemy import func, select
from werkzeug.exceptions import Forbidden, NotFound

from coverset.ai.review import SOURCE_LABELS
from coverset.db.models import Invariant, InvariantResult, PullRequest, Run, TestCase, TestStats
from coverset.forge.comment import missing_counts
from coverset.orchestrator.repos import mirror_path
from coverset.orchestrator.verdict import summary_from_json
from coverset.web.auth import is_admin
from coverset.web.context import app_db, app_settings
from coverset.web.detail import build_cards, card_detail
from coverset.web.loaders import (
    default_run,
    facts_for,
    findings_for_run,
    invariant_rows,
    pr_run,
    require_run,
    run_counts,
)
from coverset.web.pages import MAX_DB_ID, get_repo, render_page
from coverset.web.pr_view import analyse_diff
from coverset.web.render.treemap import MODES, build_tree, render_legend, render_treemap
from coverset.web.run_view import order_rows

bp = Blueprint("repos", __name__)

HISTORY_RUNS = 12


@bp.get("/repos/<int:rid>")
async def repo(rid: int) -> Response:
    """PRs with their latest run, default-branch baselines and links to the repo's views."""
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
    return await render_page(
        "repo.html",
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
        pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
    if pr is None:
        return redirect(url_for("runs.run_page", run_id=run.id), code=302)
    return redirect(url_for("repos.pr_page", rid=pr.repo_id, n=pr.number, run=run.id), code=302)


async def _pr(rid: int, n: int) -> tuple[Any, PullRequest, Run | None]:
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
    repo_row = await get_repo(rid)
    mode = request.args.get("mode", "gap")
    if mode not in MODES:
        mode = "gap"
    async with app_db().session() as session:
        wanted = request.args.get("run")
        run = await require_run(session, wanted, rid) if wanted else await default_run(session, repo_row)
    if run is None:
        return await render_page("map.html", repo=repo_row, run=None, mode=mode)
    facts = await facts_for(run)
    nodes = await asyncio.to_thread(facts.treemap_nodes) if facts.exists else []
    root = request.args.get("path") or None
    known = {node.path for node in nodes}
    if root is not None and root not in known:
        root = None
    tree = build_tree(nodes, root=root)

    def leaf_url(item: Any) -> str:
        anchor = f"#L{item.def_line}" if item.def_line else ""
        return url_for("files.source", rid=rid, path=item.file, run=run.id) + anchor

    def dir_url(item: Any) -> str:
        if item.kind == "file":
            return url_for("files.source", rid=rid, path=item.path, run=run.id)
        return url_for("repos.map_page", rid=rid, mode=mode, path=item.path, run=run.id)

    svg = render_treemap(
        tree, mode=mode, leaf_url=leaf_url, dir_url=dir_url, label=f"Treemap of {repo_row.full_name}"
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
        legend=render_legend(mode),
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
    return await render_page("tests.html", repo=repo_row, rows=rows, sort=sort, can_quarantine=is_admin())


@bp.post("/repos/<int:rid>/tests/<int:test_id>/quarantine")
async def toggle_quarantine(rid: int, test_id: int) -> Response:
    """Flip a test's quarantine flag (admin only; the access guard has checked the CSRF token)."""
    if not is_admin():
        raise Forbidden()
    async with app_db().session() as session:
        case = await session.get(TestCase, test_id)
        if case is None or case.repo_id != rid:
            raise NotFound()
        form = await request.form
        wanted = form.get("quarantined")
        case.quarantined = (wanted == "1") if wanted in ("0", "1") else not case.quarantined
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
