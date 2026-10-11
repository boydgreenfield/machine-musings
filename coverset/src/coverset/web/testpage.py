"""The test page: one test's identity, its result in a run, its history and its data flow.

Routes: ``/repos/<rid>/tests/<test_id>?run=`` (``test_id`` is the control database's test id, as
in the quarantine route; the run defaults to the newest completed default-branch baseline like the
other pages) and ``/repos/<rid>/tests/by-nodeid?nodeid=&run=``, which redirects to it (templates
that only know a nodeid link there through the ``test_link`` macro).

Identity: the nodeid and its file; markers and ``@given`` (property-based) from the test's own
source at the run's head commit (read from the mirror like the source view, only when the run's
facts list the file; the plugin's collected markers are not persisted); the invariants that link it
(``invariant_results.covering_test_ids`` of the run). Result: ``test_results`` and
``test_attempts`` of the run, the failure text from the run's ``test`` events (already redacted).
History: the test's last :data:`HISTORY_RUNS` results, as squares. Data flow, from the run's facts
(:func:`~coverset.facts.queries.test_touched` and :func:`~coverset.facts.queries.test_shapes`):
the functions it called with the lines it ran in each, the input classes it passed per parameter
(strips: this test vs any test) and the flow SVG (:mod:`coverset.web.render.flow`).
"""

from __future__ import annotations

import ast
import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from quart import Blueprint, Response, redirect, request, url_for
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from coverset.db.models import PullRequest, Run, RunEvent, TestAttempt, TestCase, TestResult, TestStats
from coverset.facts import queries
from coverset.lang.base import ShapeObs
from coverset.orchestrator.history import nodeid_hash
from coverset.web.context import app_db
from coverset.web.facts_view import ParamReach, RunFacts, param_reach, reached_classes, test_file
from coverset.web.loaders import default_run, facts_for, invariant_rows, read_source, require_run, store
from coverset.web.pages import get_repo, render_page
from coverset.web.render.flow import FlowNode, TestCall, render_test_flow
from coverset.web.render.shapegrid import render_strip

__all__ = ["bp", "line_ranges", "test_decorations"]

bp = Blueprint("testpage", __name__)

HISTORY_RUNS = 12
MAX_NODEID_CHARS = 512
MAX_SOURCE_CHARS = 2_000_000
FLOW_W = 1100.0


# -- source: markers and @given ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decorations:
    """What the test's source declares: pytest markers, invariant ids, ``@given``."""

    markers: tuple[str, ...] = ()
    invariant_ids: tuple[str, ...] = ()
    hypothesis: bool = False
    line: int | None = None


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _marks(nodes: list[ast.expr]) -> tuple[list[str], list[str], bool]:
    markers: list[str] = []
    invariants: list[str] = []
    given = False
    for node in nodes:
        name = _dotted(node)
        if name in ("given", "hypothesis.given"):
            given = True
        _, sep, mark = name.partition("mark.")
        if not sep or not mark or "." in mark:
            continue
        markers.append(mark)
        if mark == "invariant" and isinstance(node, ast.Call):
            invariants += [
                a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
    return markers, invariants, given


def _module_marks(tree: ast.Module) -> list[ast.expr]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            value = node.value
            return list(value.elts) if isinstance(value, ast.List | ast.Tuple) else [value]
    return []


def test_decorations(source: str, nodeid: str) -> Decorations | None:
    """Markers, invariant ids and ``@given`` of ``nodeid`` from its file's source (None when the
    source does not parse or the test function is not found: a generated or dynamic test)."""
    if len(source) > MAX_SOURCE_CHARS:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    names = [part.split("[", 1)[0] for part in nodeid.split("::")[1:]]
    if not names:
        return None
    body: list[ast.stmt] = tree.body
    decorators: list[ast.expr] = _module_marks(tree)
    for depth, name in enumerate(names):
        last = depth == len(names) - 1
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef) if last else (ast.ClassDef,)
        found = next((n for n in body if isinstance(n, kinds) and n.name == name), None)
        if found is None:
            return None
        decorators = [*decorators, *found.decorator_list]
        if isinstance(found, ast.ClassDef):
            body = found.body
            for stmt in body:
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
                ):
                    value = stmt.value
                    decorators += list(value.elts) if isinstance(value, ast.List | ast.Tuple) else [value]
        else:
            markers, invariants, given = _marks(decorators)
            return Decorations(
                markers=tuple(dict.fromkeys(markers)),
                invariant_ids=tuple(dict.fromkeys(invariants)),
                hypothesis=given,
                line=found.lineno,
            )
    return None


test_decorations.__test__ = False  # type: ignore[attr-defined]


# -- helpers ---------------------------------------------------------------------------------------


def line_ranges(lines: list[int] | tuple[int, ...]) -> list[tuple[int, int]]:
    """Sorted lines folded into ``(first, last)`` runs."""
    out: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        if out and line == out[-1][1] + 1:
            out[-1] = (out[-1][0], line)
        else:
            out.append((line, line))
    return out


@dataclass(slots=True)
class FunctionRow:
    """One function the test called, for the template."""

    func_key: str
    qualname: str
    def_line: int
    end_line: int
    lines: tuple[int, ...]
    ranges: list[tuple[int, int]]
    body_statements: int
    url: str
    source_url: str
    params: list[ParamReach] = field(default_factory=list)
    strips: list[tuple[ParamReach, Any, Any, str | None]] = field(default_factory=list)
    note: str = ""
    via: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FileRow:
    """One file the test ran lines of."""

    path: str
    run_lines: tuple[int, ...]
    statements: int
    functions: list[FunctionRow]
    module_ranges: list[tuple[int, int]]
    source_url: str | None


def _share(obs: ShapeObs) -> int:
    """This test's estimated share of a row's samples (rows aggregate every test of a caller)."""
    return max(1, math.ceil(obs.samples / max(1, len(obs.tests))))


def _test_label(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1] if "::" in nodeid else nodeid


async def _resolve(rid: int, test_id: int) -> tuple[Any, TestCase, Run | None]:
    repo = await get_repo(rid)
    async with app_db().session() as session:
        case = await session.get(TestCase, test_id)
        if case is None or case.repo_id != rid:
            raise NotFound(f"There is no test with id {test_id} in {repo.full_name}.")
        wanted = request.args.get("run")
        run = await require_run(session, wanted, rid) if wanted else await default_run(session, repo)
    return repo, case, run


@bp.get("/repos/<int:rid>/tests/by-nodeid")
async def by_nodeid(rid: int) -> Response:
    """Redirect a nodeid (``?nodeid=``, keeping ``?run=``) to its test page."""
    repo = await get_repo(rid)
    nodeid = request.args.get("nodeid", "")
    if not nodeid or len(nodeid) > MAX_NODEID_CHARS:
        raise NotFound("That is not a test id.")
    async with app_db().session() as session:
        case = await session.scalar(
            select(TestCase).where(TestCase.repo_id == rid, TestCase.nodeid_hash == nodeid_hash(nodeid))
        )
    if case is None or case.nodeid != nodeid:
        raise NotFound(f"{repo.full_name} has no test with that node id.")
    extra = {"run": request.args["run"]} if request.args.get("run") else {}
    return redirect(url_for("testpage.test_page", rid=rid, test_id=case.id, **extra), code=302)  # type: ignore[return-value]


@bp.get("/repos/<int:rid>/tests/<int:test_id>")
async def test_page(rid: int, test_id: int) -> Response:
    """Identity, result in the selected run, history and data flow of one test."""
    repo, case, run = await _resolve(rid, test_id)
    async with app_db().session() as session:
        stats = await session.get(TestStats, (rid, case.id))
        history_rows = (
            await session.execute(
                select(TestResult, Run)
                .join(Run, Run.id == TestResult.run_id)
                .where(TestResult.test_id == case.id, Run.repo_id == rid)
                .order_by(Run.created_at.desc())
                .limit(HISTORY_RUNS)
            )
        ).all()
        pr_ids = {r.pr_id for _res, r in history_rows if r.pr_id} | (
            {run.pr_id} if run and run.pr_id else set()
        )
        prs = {
            pr.id: pr
            for pr in (
                await session.scalars(select(PullRequest).where(PullRequest.id.in_(pr_ids))) if pr_ids else []
            )
        }
        result = attempts = None
        longrepr = None
        invariants: list[dict[str, Any]] = []
        if run is not None:
            result = await session.get(TestResult, (run.id, case.id))
            attempts = list(
                await session.scalars(
                    select(TestAttempt)
                    .where(TestAttempt.run_id == run.id, TestAttempt.test_id == case.id)
                    .order_by(TestAttempt.attempt)
                )
            )
            if result is not None and result.outcome in ("failed", "error"):
                longrepr = await _failure_text(session, run.id, case.nodeid)
            invariants = [row for row in await invariant_rows(session, run) if case.id in row["covering_ids"]]
    flow = None
    files: list[FileRow] = []
    decorations = None
    facts_exists = False
    test_lines: dict[str, list[int]] = {}
    if run is not None:
        facts = await facts_for(run)
        facts_exists = facts.exists
        test_path = test_file(case.nodeid)
        if facts_exists and test_path in facts.paths:
            source = await read_source(run, test_path, facts)
            if source is not None:
                decorations = await asyncio.to_thread(test_decorations, source, case.nodeid)
        if facts_exists:
            files, flow = await _data_flow(rid, run, facts, case)
            test_lines = {f.path: list(f.run_lines) for f in files}
    return await render_page(
        "test.html",
        repo=repo,
        case=case,
        run=run,
        stats=stats,
        result=result,
        attempts=attempts or [],
        longrepr=longrepr,
        invariants=invariants,
        decorations=decorations,
        history=list(reversed(history_rows)),
        prs=prs,
        facts_exists=facts_exists,
        files=files,
        flow=flow,
        test_lines=test_lines,
        test_path=test_file(case.nodeid),
        called=sum(len(f.functions) for f in files),
    )


async def _failure_text(session: Any, run_id: str, nodeid: str) -> str | None:
    """The newest ``longrepr`` the run's ``test`` events carry for ``nodeid`` (redacted at write)."""
    events = await session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.kind == "test")
        .order_by(RunEvent.seq.desc())
    )
    for event in events:
        payload = event.payload or {}
        text = payload.get("longrepr")
        if payload.get("nodeid") == nodeid and isinstance(text, str) and text:
            return text
    return None


async def _data_flow(rid: int, run: Run, facts: RunFacts, case: TestCase) -> tuple[list[FileRow], Any]:
    """The file and function rows and the flow SVG of one test in one run."""
    nodeid = case.nodeid
    facts_store = store()
    touched = await asyncio.to_thread(queries.test_touched, facts_store, rid, run.id, nodeid)
    shapes = await asyncio.to_thread(queries.test_shapes, facts_store, rid, run.id, nodeid)
    by_triple = {r.triple: r for r in facts.regions}
    by_name = {(r.path, r.qualname): r for r in facts.regions}

    def region_of(triple: tuple[str, str, int]) -> Any:
        return by_triple.get(triple) or by_name.get((triple[0], triple[1]))

    observations: dict[str, list[ShapeObs]] = defaultdict(list)
    for obs in shapes:
        region = region_of(obs.func)
        if region is not None:
            observations[region.func_key].append(obs)
    sources: dict[str, str | None] = {}
    files: list[FileRow] = []
    calls: list[TestCall] = []
    for item in touched:
        rows: list[FunctionRow] = []
        for fn in item.functions:
            region = facts.region(fn.func_key)
            function_url = url_for("files.function", rid=rid, func_key=fn.func_key, run=run.id)
            source_url = (
                url_for("files.source", rid=rid, path=item.path, run=run.id, test=case.id)
                + f"#L{fn.def_line}"
            )
            row = FunctionRow(
                func_key=fn.func_key,
                qualname=fn.qualname,
                def_line=fn.def_line,
                end_line=fn.end_line,
                lines=fn.lines,
                ranges=line_ranges(fn.lines),
                body_statements=fn.body_statements,
                url=function_url,
                source_url=source_url,
            )
            mine = observations.get(fn.func_key, [])
            if region is not None:
                if item.path not in sources:
                    sources[item.path] = await read_source(run, item.path, facts)
                view = await asyncio.to_thread(facts.function_view, region, sources[item.path])
                reached = reached_classes(view, mine)
                row.params = param_reach(view, reached)
                for reach in row.params:
                    example = next(
                        (
                            o.example
                            for o in mine
                            if o.param.lstrip("*") == reach.param and o.example and o.example_test == nodeid
                        ),
                        None,
                    )
                    row.strips.append(
                        (
                            reach,
                            render_strip(
                                reach.this_test,
                                label=f"{reach.param}: {len(reach.reached)} classes passed in by this test",
                                missing="never exercised by any test",
                            ),
                            render_strip(
                                reach.any_test,
                                label=f"{reach.param}: classes any test passed in",
                                missing=view.cov.missing_reason,
                            ),
                            example,
                        )
                    )
                if view.cov.expected and not mine:
                    sampling = facts.sampling(region)
                    row.note = (
                        "the tracer sampled no call of it for this test (sample cap reached)"
                        if sampling is not None and sampling.truncated
                        else "no sampled call of it in this test"
                    )
                calls += _calls(rid, run, fn.func_key, fn.qualname, function_url, mine, view, region_of)
            else:
                calls.append(TestCall(FlowNode(fn.func_key, fn.qualname, function_url)))
            row.via = sorted({o.caller[1] for o in mine if isinstance(o.caller, tuple)})
            rows.append(row)
        listed = item.path in facts.paths
        files.append(
            FileRow(
                path=item.path,
                run_lines=item.lines,
                statements=item.statements,
                functions=rows,
                module_ranges=line_ranges(item.module_lines),
                source_url=url_for("files.source", rid=rid, path=item.path, run=run.id, test=case.id)
                if listed
                else None,
            )
        )
    test_path = test_file(nodeid)
    test_href = (
        url_for("files.source", rid=rid, path=test_path, run=run.id)
        if test_path in facts.statics
        else url_for("testpage.test_page", rid=rid, test_id=case.id, run=run.id)
    )
    test_node = FlowNode("test", _test_label(nodeid), test_href, title=nodeid)
    flow = render_test_flow(test_node, calls, width=FLOW_W)
    return files, flow


def _calls(
    rid: int,
    run: Run,
    func_key: str,
    qualname: str,
    function_url: str,
    mine: list[ShapeObs],
    view: Any,
    region_of: Any,
) -> list[TestCall]:
    """Flow calls of one function: one per (via caller, parameter), or one bare call."""
    function = FlowNode(func_key, qualname, function_url)
    grouped: dict[tuple[tuple[str, str, int] | None, str], list[ShapeObs]] = defaultdict(list)
    for obs in mine:
        if obs.param in ("<receiver>", "return"):
            continue
        via = obs.caller if isinstance(obs.caller, tuple) else None
        grouped[(via, obs.param.lstrip("*"))].append(obs)
    if not grouped:
        return [TestCall(function)]
    calls = []
    for (via, param), group in sorted(grouped.items(), key=lambda kv: (kv[0][0] or ("", "", 0), kv[0][1])):
        classes = reached_classes(view, group).get(param, frozenset())
        via_node = None
        if via is not None:
            caller = region_of(via)
            href = (
                url_for("files.function", rid=rid, func_key=caller.func_key, run=run.id)
                if caller is not None
                else None
            )
            via_node = FlowNode(
                caller.func_key if caller is not None else f"{via[0]}:{via[1]}",
                via[1],
                href,
                title=f"{via[1]} ({via[0]}:{via[2]})",
            )
        calls.append(
            TestCall(
                function=function,
                param=param,
                param_href=function_url,
                samples=sum(_share(o) for o in group),
                classes=len(classes),
                via=via_node,
            )
        )
    return calls
