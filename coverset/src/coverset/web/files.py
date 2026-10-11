"""Source view, line → tests popover, function page and the shape-grid fragments.

Routes: ``/repos/<rid>/files/<path:path>?run=&pr=`` (source view), fragment
``/repos/<rid>/files/<path:path>/lines/<n>/tests?run=`` (covering tests of one line),
``/repos/<rid>/functions/<func_key>?run=`` (function page) and fragment
``/repos/<rid>/functions/<func_key>/shapes?run=[&cls=]`` (the grid, or one cell's detail).
A path is served only when the run's facts list it; its text comes from the mirror at the run's
head commit.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from quart import Blueprint, Response, request, url_for
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from coverset.db.models import PullRequest, Run
from coverset.orchestrator.repos import mirror_path
from coverset.web.context import app_db, app_settings
from coverset.web.detail import function_detail
from coverset.web.facts_view import FunctionView, RunFacts
from coverset.web.loaders import (
    default_run,
    facts_for,
    findings_for_run,
    invariant_rows,
    pr_run,
    read_source,
    require_run,
)
from coverset.web.pages import get_repo, parse_int, render_page
from coverset.web.pr_view import analyse_diff
from coverset.web.render.shapegrid import render_grid, render_inlay
from coverset.web.render.source import RegionMark, render_source
from coverset.web.run_view import order_rows

bp = Blueprint("files", __name__)

FUNC_KEY = re.compile(r"^[0-9a-f]{16}$")
CLASS_NAME = re.compile(r"^[^\x00-\x1f]{1,200}$")


async def _run_for(rid: int, repo: Any) -> tuple[Run | None, PullRequest | None]:
    """``?run=`` or ``?pr=`` (its latest run), else the repo's default run."""
    async with app_db().session() as session:
        pr = None
        number = parse_int(request.args.get("pr"), minimum=1)
        if number is not None:
            pr = await session.scalar(
                select(PullRequest).where(PullRequest.repo_id == rid, PullRequest.number == number)
            )
            if pr is None:
                raise NotFound()
        wanted = request.args.get("run")
        if wanted:
            run = await require_run(session, wanted, rid)
        elif pr is not None:
            run = await pr_run(session, pr)
        else:
            run = await default_run(session, repo)
    return run, pr


def chips_for(view: FunctionView, shapes_url: str) -> str:
    """The inlay row under a def line."""
    note = view.cov.note or (
        "never called by a test" if not view.observations else "no input classes expected"
    )
    return render_inlay(view.chips(), note=note, shapes_url=shapes_url)


@bp.get("/repos/<int:rid>/files/<path:path>")
async def source(rid: int, path: str) -> Response:
    """The source view of one file in one run."""
    repo = await get_repo(rid)
    run, pr = await _run_for(rid, repo)
    if run is None:
        raise NotFound()
    facts = await facts_for(run)
    text = await read_source(run, path, facts)
    if text is None:
        raise NotFound()
    changed: set[int] = set()
    removed: dict[int, list[str]] = {}
    if pr is not None or run.pr_id is not None:
        analysis = await asyncio.to_thread(
            analyse_diff, mirror_path(app_settings(), rid), run.base_sha, run.head_sha, facts
        )
        changed = analysis.changed_lines.get(path, set())
        removed = analysis.removed.get(path, {})
    line_facts = await asyncio.to_thread(facts.line_facts, path)
    regions = []
    hollow_total = 0
    for region in facts.callables(path):
        view = await asyncio.to_thread(facts.function_view, region, text)
        hollow = view.hollow if view.observations else 0
        hollow_total += hollow
        shapes_url = url_for("files.shapes", rid=rid, func_key=region.func_key, run=run.id)
        regions.append(
            RegionMark(
                start=region.def_line,
                end=region.end_line,
                qualname=region.qualname,
                kind=region.kind,
                func_key=region.func_key,
                hollow=hollow,
                url=url_for("files.function", rid=rid, func_key=region.func_key, run=run.id),
                chips=chips_for(view, shapes_url),
            )
        )
    for region in facts.regions:
        if region.path == path and region.kind == "class":
            regions.append(RegionMark(region.def_line, region.end_line, region.qualname, "class"))

    def tests_url(line: int) -> str:
        return url_for("files.line_tests", rid=rid, path=path, n=line, run=run.id)

    html = render_source(
        text,
        line_facts,
        path=path,
        regions=regions,
        changed=changed,
        removed=removed,
        tests_url=tests_url,
    )
    measured = sorted(facts.statics)
    index = measured.index(path) if path in measured else -1
    prefix = os.path.commonpath(measured) if len(measured) > 1 else ""
    if prefix in measured:
        prefix = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
    labels = [(p, p[len(prefix) + 1 :] if prefix else p) for p in measured]
    extra = {"pr": pr.number} if pr is not None else {}
    prev_url = (
        url_for("files.source", rid=rid, path=measured[index - 1], run=run.id, **extra) if index > 0 else None
    )
    next_url = (
        url_for("files.source", rid=rid, path=measured[index + 1], run=run.id, **extra)
        if 0 <= index < len(measured) - 1
        else None
    )
    stats = {
        "statements": len(line_facts.statements - line_facts.excluded),
        "missed": sum(1 for n in line_facts.statements if line_facts.state(n) == "mis"),
        "partial": len(line_facts.partial),
        "hollow": hollow_total,
    }
    return await render_page(
        "source.html",
        repo=repo,
        run=run,
        pr=pr,
        path=path,
        html=html,
        measured=labels,
        common_prefix=prefix,
        prev_url=prev_url,
        next_url=next_url,
        stats=stats,
        known=line_facts.known,
        changed=len(changed),
    )


@bp.get("/repos/<int:rid>/files/<path:path>/lines/<int:n>/tests")
async def line_tests(rid: int, path: str, n: int) -> Response:
    """The tests that executed one line (fragment for the tests popover)."""
    repo = await get_repo(rid)
    run, _pr = await _run_for(rid, repo)
    if run is None:
        raise NotFound()
    facts = await facts_for(run)
    if path not in facts.paths:
        raise NotFound()
    tests = await asyncio.to_thread(facts.tests_in_range, path, n, n)
    at_import = await asyncio.to_thread(facts.import_covers, path, n)
    async with app_db().session() as session:
        _order, results = await order_rows(session, run)
    by_id = {r["nodeid"]: r for r in results}
    rows = sorted(
        ((t, by_id.get(t, {})) for t in tests), key=lambda item: (item[1].get("rank") or 10**9, item[0])
    )
    return await render_page(
        "line_tests.html", repo=repo, run=run, path=path, line=n, rows=rows, at_import=at_import
    )


async def _function(rid: int, func_key: str) -> tuple[Any, Run, RunFacts, Any]:
    if not FUNC_KEY.fullmatch(func_key):
        raise NotFound()
    repo = await get_repo(rid)
    run, _pr = await _run_for(rid, repo)
    if run is None:
        raise NotFound()
    facts = await facts_for(run)
    region = facts.region(func_key)
    if region is None:
        raise NotFound()
    return repo, run, facts, region


@bp.get("/repos/<int:rid>/functions/<func_key>")
async def function(rid: int, func_key: str) -> Response:
    """One function: shape grid, flow ribbon, source with coverage, covering tests, invariants."""
    repo, run, facts, region = await _function(rid, func_key)
    async with app_db().session() as session:
        _order, results = await order_rows(session, run)
        findings = await findings_for_run(session, run, [func_key])
        invariants = [i for i in await invariant_rows(session, run) if i["target_func_key"] == func_key]
    analysis = None
    if run.pr_id is not None:
        analysis = await asyncio.to_thread(
            analyse_diff, mirror_path(app_settings(), rid), run.base_sha, run.head_sha, facts
        )
    detail = await function_detail(
        repo, run, facts, region, analysis=analysis, results=results, finding=findings.get(func_key)
    )
    return await render_page(
        "function.html", repo=repo, run=run, region=region, detail=detail, invariants=invariants
    )


@bp.get("/repos/<int:rid>/functions/<func_key>/shapes")
async def shapes(rid: int, func_key: str) -> Response:
    """The shape grid of a function, or with ``cls=`` the detail of one cell (fragments)."""
    repo, run, facts, region = await _function(rid, func_key)
    text = await read_source(run, region.path, facts)
    view = await asyncio.to_thread(facts.function_view, region, text)
    wanted = request.args.get("cls")
    if wanted:
        if not CLASS_NAME.fullmatch(wanted):
            raise NotFound()
        cells = [*view.cells(), *view.return_cells]
        cell = next((c for c in cells if c.name == wanted), None)
        if cell is None:
            raise NotFound()
        tests = list(view.cov.tests.get(cell.name, ()))
        example = view.cov.examples.get(cell.name)
        return await render_page(
            "shape_cell.html",
            repo=repo,
            run=run,
            region=region,
            cell=cell,
            tests=tests,
            example=example,
            missing_reason=view.cov.missing_reason,
        )
    target = f"sg-{func_key}-src"
    grid = render_grid(
        view.grid(),
        cell_url=lambda cell: url_for("files.shapes", rid=rid, func_key=func_key, run=run.id, cls=cell.name),
        target=target,
    )
    return await render_page("shapes.html", repo=repo, run=run, region=region, grid=grid)
