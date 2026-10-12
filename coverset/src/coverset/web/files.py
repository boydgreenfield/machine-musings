"""Source view, line → tests popover, function page and the shape-grid fragments.

Routes: ``/repos/<rid>/files/<path:path>?run=&pr=`` (source view), fragment
``/repos/<rid>/files/<path:path>/lines/<n>/tests?run=`` (covering tests of one line),
``/repos/<rid>/functions/<func_key>?run=`` (function page, with the function's history over the
default branch's last baselines) and fragment
``/repos/<rid>/functions/<func_key>/shapes?run=[&cls=]`` (the grid, or one cell's detail).
A path is served only when the run's facts list it; its text comes from the mirror at the run's
head commit.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from markupsafe import Markup
from quart import Blueprint, Response, request, url_for
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from coverset.db.models import PullRequest, Run, TestCase, TestResult
from coverset.facts.queries import function_history, test_touched
from coverset.facts.schema import CALLABLE_KINDS
from coverset.orchestrator.repos import mirror_path
from coverset.web.context import app_db, app_settings
from coverset.web.detail import function_detail
from coverset.web.facts_view import FunctionView, RunFacts, history_losses, run_point
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
from coverset.web.render.sparkline import SparkSeries, render_spark_key, render_sparkline
from coverset.web.repos import baseline_candidates
from coverset.web.run_view import order_rows

bp = Blueprint("files", __name__)

FUNC_KEY = re.compile(r"^[0-9a-f]{16}$")
HISTORY_RUNS = 12
"""Baselines in the function page's history strip."""
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


def chips_for(view: FunctionView, shapes_url: str) -> Markup | None:
    """The inlay row under a def line (None for a function that expects no input classes, such
    as a property or a function no test called: its lines already say whether they ran)."""
    if not view.cov.expected:
        return None
    return render_inlay(
        view.chips(), note=view.cov.note, shapes_url=shapes_url, missing=view.cov.missing_reason
    )


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
        raise NotFound(f"{path} is not a measured file of run {run.id}.")
    changed: set[int] = set()
    removed: dict[int, list[str]] = {}
    if pr is not None or run.pr_id is not None:
        analysis = await asyncio.to_thread(
            analyse_diff, mirror_path(app_settings(), rid), run.base_sha, run.head_sha, facts
        )
        changed = analysis.changed_lines.get(path, set())
        removed = analysis.removed.get(path, {})
    line_facts = await asyncio.to_thread(facts.line_facts, path)
    focus, focus_test = await _focus_for(rid, run, path, facts)
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
        focus=focus,
    )
    measured = sorted(facts.statics)
    index = measured.index(path) if path in measured else -1
    prefix = os.path.commonpath(measured) if len(measured) > 1 else ""
    if prefix in measured:
        prefix = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
    labels = [(p, p[len(prefix) + 1 :] if prefix else p) for p in measured]
    extra: dict[str, Any] = {"pr": pr.number} if pr is not None else {}
    lens = {"test": focus_test["test_id"]} if focus_test is not None else {}
    prev_url = (
        url_for("files.source", rid=rid, path=measured[index - 1], run=run.id, **extra, **lens)
        if index > 0
        else None
    )
    next_url = (
        url_for("files.source", rid=rid, path=measured[index + 1], run=run.id, **extra, **lens)
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
        focus=focus_test,
        focus_lines=len(focus or ()),
        clear_url=url_for("files.source", rid=rid, path=path, run=run.id, **extra),
    )


async def _focus_for(
    rid: int, run: Run, path: str, facts: RunFacts
) -> tuple[frozenset[int] | None, dict[str, Any] | None]:
    """``?test=<id>``: the lines that test ran in ``path`` (a frozenset, empty when it never
    touched the file) and the banner data (with ``in_run``: whether the test has a result in this
    run), or ``(None, None)`` without the parameter. A test of another repository is a 404; the id
    is the database's, as the test page uses it."""
    test_id = parse_int(request.args.get("test"), minimum=1)
    if test_id is None:
        return None, None
    async with app_db().session() as session:
        case = await session.get(TestCase, test_id)
        if case is None or case.repo_id != rid:
            raise NotFound(f"There is no test with id {test_id} in this repository.")
        result = await session.get(TestResult, (run.id, case.id))
    touched = await asyncio.to_thread(test_touched, facts.store, rid, run.id, case.nodeid)
    lines = frozenset(line for file in touched if file.path == path for line in file.lines)
    banner = {
        "test_id": case.id,
        "in_run": result is not None,
        "nodeid": case.nodeid,
        "url": url_for("testpage.test_page", rid=rid, test_id=case.id, run=run.id),
        "statements": sum(1 for n in lines if n in facts.line_facts(path).statements),
    }
    return lines, banner


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
        raise NotFound(f"Run {run.id} has no function with key {func_key}.")
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
    history = await function_history_view(repo, run, facts, region) if region.kind in CALLABLE_KINDS else None
    return await render_page(
        "function.html",
        repo=repo,
        run=run,
        region=region,
        detail=detail,
        invariants=invariants,
        history=history,
    )


async def function_history_view(repo: Any, run: Run, facts: RunFacts, region: Any) -> dict[str, Any]:
    """The function page's history strip: the function in the default branch's last baselines up
    to the viewed run (created at or before it, so an old baseline shows the runs before it), plus
    the viewed run as the newest point when it is not a baseline (a PR run), a sparkline of line %
    and input-class %, one square per run and the classes missing at the viewed run that an earlier
    comparable point did not miss."""
    candidates = await baseline_candidates(repo.id, repo.default_branch, HISTORY_RUNS, until=run.created_at)
    points = await asyncio.to_thread(
        function_history,
        facts.store,
        repo.id,
        region.func_key,
        repo.default_branch,
        HISTORY_RUNS,
        until=run.created_at,
        candidates=candidates,
    )
    extra = run.id not in {p.run_id for p in points}
    if extra:
        # every point is at or before the viewed run, so it goes last
        points = [*points, await asyncio.to_thread(run_point, facts, region, run.created_at, run.head_sha)]
    index = next(i for i, p in enumerate(points) if p.run_id == run.id)

    def name(i: int) -> str:
        point = points[i]
        where = f"this run ({run.branch or 'detached'})" if extra and i == len(points) - 1 else "baseline"
        return f"{where} {point.head_sha[:8]} · {point.created_at:%Y-%m-%d %H:%M} UTC"

    series = [
        SparkSeries("lines", tuple(p.line_pct for p in points)),
        SparkSeries("input classes", tuple(p.input_pct for p in points), dashed=True),
    ]
    spark = render_sparkline(
        series,
        points=[name(i) for i in range(len(points))],
        width=240,
        current=index,
        label=f"{region.qualname} over the last runs",
    )
    squares = [
        {
            "point": point,
            "label": name(i),
            "current": i == index,
            "extra": extra and i == len(points) - 1,
            "url": url_for("files.function", rid=repo.id, func_key=region.func_key, run=point.run_id)
            if point.present
            else None,
        }
        for i, point in enumerate(points)
    ]
    return {
        "points": points,
        "baselines": len(points) - (1 if extra else 0),
        "extra": extra,
        "spark": spark,
        "key": render_spark_key(series),
        "squares": squares,
        "losses": history_losses(points, index),
        "current": points[index],
    }


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
        tests = [] if cell.is_return else list(view.cov.tests.get(cell.name, ()))
        example = None if cell.is_return else view.cov.examples.get(cell.name)
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
