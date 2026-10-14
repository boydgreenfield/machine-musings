"""``coverset flows import|windows|gaps|archetypes|prune``: production input shapes against the tests.

``import FILE`` validates a ``coverset-probe`` export (JSON lines, one window per line, optionally
gzipped) and stores its windows under the repo's facts (:mod:`coverset.facts.flows`). ``gaps``
pools the newest ``--windows`` windows and lists, per function, the input classes production
passed in that no test of the reference run did, ranked by estimated production calls, with the
catalogue's Hypothesis hint; then the functions production called that no test reached, and the
flows (requests) that went through them. ``archetypes`` clusters each function's production calls
into archetypes and names the tests that cover each one (exact, superset, partial).

The reference run is ``--run`` or the newest completed run of the repo's default branch that has
facts, the run the function pages default to. ``POST /v1/flows`` (the probe's ``HttpExporter``
target) is designed, not built: this file import is the prototype's only way in.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from sqlalchemy import select

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings

__all__ = ["flows_group"]

_REPO_HELP = "Repository (id, full name or path); default: the only one."
_LAMBDA = "<lambda>"
"""Lambdas are traced in production but never indexed as regions: not worth an "unresolved" line."""


@click.group(name="flows")
def flows_group() -> None:
    """Production input shapes (from coverset-probe) against what the tests pass in."""


async def _repo_and_run(
    settings: Settings, repo_spec: str | None, run_id: str | None
) -> tuple[Any, Any | None]:
    """The repo and its reference run (``run_id``, else the newest completed default-branch run)."""
    from coverset.db.models import Run
    from coverset.db.session import Database

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            repo = await find_repo(session, repo_spec)
            if run_id is not None:
                run = await session.get(Run, run_id)
                if run is None or run.repo_id != repo.id:
                    raise click.ClickException(f"no run {run_id!r} in {repo.full_name}")
                return repo, run
            query = (
                select(Run)
                .where(Run.repo_id == repo.id, Run.status == "completed", Run.branch == repo.default_branch)
                .order_by(Run.created_at.desc())
                .limit(5)
            )
            from coverset.facts.store import facts_store

            store = facts_store(settings)
            for run in await session.scalars(query):
                if store.run_dir(repo.id, run.id).is_dir():
                    return repo, run
            return repo, None
    finally:
        await db.engine.dispose()


# -- import ----------------------------------------------------------------------------------------


@flows_group.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--service", default=None, help="Override the service name every window carries.")
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
def flows_import_command(file: Path, repo_spec: str | None, service: str | None, as_json: bool) -> None:
    """Validate a coverset-probe export (FILE.jsonl[.gz]) and store its windows for the repo."""
    settings = load()
    result = run_async(lambda: _import(settings, file, repo_spec, service))
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        click.echo(_import_text(result))
    if result["rejected"] and not result["windows"] and not result["duplicates"]:
        raise SystemExit(1)


async def _import(
    settings: Settings, file: Path, repo_spec: str | None, service: str | None
) -> dict[str, Any]:
    from coverset.facts.flows import FlowsError, import_payloads, read_payload_file
    from coverset.facts.queries import ParquetPriorRun
    from coverset.facts.store import facts_store

    repo, run = await _repo_and_run(settings, repo_spec, None)
    store = facts_store(settings)
    regions = (
        await asyncio.to_thread(ParquetPriorRun(store, repo.id, run.id).functions) if run is not None else []
    )

    def work() -> Any:
        return import_payloads(store, repo.id, read_payload_file(file), regions=regions, service=service)

    try:
        result = await asyncio.to_thread(work)
    except FlowsError as exc:
        raise click.ClickException(str(exc)) from None
    return {
        "repo": repo.full_name,
        "file": str(file),
        "reference_run": run.id if run is not None else None,
        "windows": result.windows,
        "duplicates": result.duplicates,
        "rows": result.rows,
        "functions": result.funcs,
        "unresolved_functions": result.unresolved,
        "services": sorted(result.services),
        "rejected": [{"line": line, "problems": problems} for line, problems in result.rejected],
        "batches": [str(b) for b in result.batches],
    }


def _import_text(result: dict[str, Any]) -> str:
    lines = [
        f"{result['repo']}: imported {result['windows']} window(s) from {Path(result['file']).name}"
        f" ({result['rows']} shape rows, {result['functions']} function entries"
        + (
            f", {result['unresolved_functions']} not found in run {result['reference_run']}"
            if result["unresolved_functions"]
            else ""
        )
        + ")",
    ]
    if result["duplicates"]:
        lines.append(f"skipped {result['duplicates']} window(s) already imported")
    if result["reference_run"] is None:
        lines.append("no completed run with facts yet: functions will be resolved when gaps are computed")
    for rejected in result["rejected"][:10]:
        lines.append(f"rejected line {rejected['line']}: " + "; ".join(rejected["problems"][:3]))
    if len(result["rejected"]) > 10:
        lines.append(f"... and {len(result['rejected']) - 10} more rejected window(s)")
    return "\n".join(lines)


# -- windows and prune ---------------------------------------------------------------------------------


@flows_group.command("windows")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--service", default=None, help="Only this service.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON lines.")
def flows_windows_command(repo_spec: str | None, service: str | None, as_json: bool) -> None:
    """List the imported production windows, oldest first."""
    from coverset.facts.flows import list_windows
    from coverset.facts.store import facts_store

    settings = load()
    repo, _ = run_async(lambda: _repo_and_run(settings, repo_spec, None))
    windows = list_windows(facts_store(settings), repo.id, service)
    for w in windows:
        row = {
            "service": w.service,
            "env": w.env,
            "release": w.release,
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
            "k": w.k,
            "sample_rate": w.sample_rate,
        }
        click.echo(json.dumps(row) if as_json else "  ".join(str(v) for v in row.values()))
    if not windows and not as_json:
        click.echo(f"{repo.full_name} has no production windows (coverset flows import FILE)")


@flows_group.command("prune")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--older-than-days", type=click.IntRange(0, 3650), default=30, show_default=True)
@click.option("--dry-run", is_flag=True, help="List what would be deleted.")
def flows_prune_command(repo_spec: str | None, older_than_days: int, dry_run: bool) -> None:
    """Delete imported batches whose newest window ended more than N days ago."""
    from coverset.facts.flows import prune_windows
    from coverset.facts.store import facts_store

    settings = load()
    repo, _ = run_async(lambda: _repo_and_run(settings, repo_spec, None))
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    doomed = prune_windows(facts_store(settings), repo.id, older_than=cutoff, dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    click.echo(f"{verb} {len(doomed)} batch(es) of {repo.full_name} ended before {cutoff:%Y-%m-%d %H:%M}Z")


# -- gaps --------------------------------------------------------------------------------------------


def _window_options(func: Any) -> Any:
    for option in reversed(
        [
            click.option("--repo", "repo_spec", default=None, help=_REPO_HELP),
            click.option(
                "--run", "run_id", default=None, help="Reference run (default: newest completed baseline)."
            ),
            click.option("--service", default=None, help="Only this service's windows."),
            click.option(
                "--windows",
                "last_windows",
                type=click.IntRange(1, 100_000),
                default=1,
                show_default=True,
                help="Pool the last N window lengths of each service, every process's windows.",
            ),
            click.option(
                "--minutes",
                type=click.IntRange(1, 10_000_000),
                default=None,
                help="Pool every window that ended in the last M minutes instead of --windows.",
            ),
            click.option("--json", "as_json", is_flag=True, help="Print JSON."),
        ]
    ):
        func = option(func)
    return func


@flows_group.command("gaps")
@_window_options
@click.option(
    "--limit", type=click.IntRange(1, 10_000), default=20, show_default=True, help="Functions shown."
)
def flows_gaps_command(
    repo_spec: str | None,
    run_id: str | None,
    service: str | None,
    last_windows: int,
    minutes: int | None,
    as_json: bool,
    limit: int,
) -> None:
    """Input classes production passes in that no test of the reference run does, by call volume."""
    settings = load()
    report = run_async(lambda: _gaps(settings, repo_spec, run_id, service, last_windows, minutes))
    if as_json:
        click.echo(json.dumps(_limit(report, limit), indent=2, default=str))
    else:
        click.echo(_gaps_text(report, limit), nl=False)


def _limit(report: dict[str, Any], limit: int) -> dict[str, Any]:
    return {**report, "functions": report["functions"][:limit]}


class _Context:
    """Everything ``gaps`` and ``archetypes`` share: the windows, the regions, the joins."""

    def __init__(self, store: Any, repo: Any, run: Any, prod: Any, regions: list[Any]) -> None:
        from coverset.facts.flows import FunctionResolver, caller_index

        self.store, self.repo, self.run, self.prod = store, repo, run, prod
        self.by_key = {r.func_key: r for r in regions}
        self.callers = caller_index(regions)
        self.unresolved: set[tuple[str, str]] = set()
        self.rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.func_calls: dict[str, int] = defaultdict(int)
        cache: dict[tuple[str, str, str | None, int | None], str | None] = {}
        resolver: FunctionResolver | None = None  # built on the first unresolved row, then reused

        def key_of(row: dict[str, Any]) -> str | None:
            nonlocal resolver
            if row.get("func_key") in self.by_key:
                return row["func_key"]
            ident = (row["module"], row["qualname"], row.get("path"), row.get("line"))
            if ident not in cache:
                # Rows imported before this run existed (or renamed since): resolve against its
                # regions here. One index for all of them: an index per row was O(funcs x regions).
                resolver = resolver or FunctionResolver(regions)
                region = resolver.resolve(row)
                cache[ident] = region.func_key if region is not None else None
            if cache[ident] is None:
                self.unresolved.add((row["module"], row["qualname"]))
            return cache[ident]

        for row in prod.shapes:
            if row["k_ok"] and (key := key_of(row)):
                self.rows[key].append(row)
        for row in prod.calls:
            if row["k_ok"] and (key := key_of(row)):
                self.calls[key].append(row)
        for row in prod.funcs:
            if (key := key_of(row)) and row["calls"] is not None:
                self.func_calls[key] += row["calls"]

    def window_text(self) -> str:
        windows = self.prod.windows
        services = sorted({f"{w.service} ({w.env})" for w in windows})
        start = min(w.start for w in windows).strftime("%Y-%m-%d %H:%M")
        end = max(w.end for w in windows).strftime("%Y-%m-%d %H:%M")
        return f"{', '.join(services)}: {len(windows)} window(s), {start} to {end} UTC"


async def _context(
    settings: Settings,
    repo_spec: str | None,
    run_id: str | None,
    service: str | None,
    last_windows: int,
    minutes: int | None,
) -> _Context:
    from coverset.facts.flows import read_prod, run_has_shapes
    from coverset.facts.queries import ParquetPriorRun
    from coverset.facts.store import facts_store

    repo, run = await _repo_and_run(settings, repo_spec, run_id)
    if run is None:
        raise click.ClickException(
            f"{repo.full_name} has no completed run with facts on {repo.default_branch} yet"
        )
    store = facts_store(settings)
    if not await asyncio.to_thread(run_has_shapes, store, repo.id, run.id):
        # Without shapes every production class would read as a gap: refuse rather than mislead.
        raise click.ClickException(
            f"run {run.id} recorded no input shapes (made with --coverset-shapes off, or the shape "
            "tracer could not start); pick a run that has them with --run"
        )
    since = datetime.now(UTC) - timedelta(minutes=minutes) if minutes else None
    prod = await asyncio.to_thread(
        lambda: read_prod(store, repo.id, service=service, last_windows=last_windows, since=since)
    )
    if not prod.windows:
        raise click.ClickException(
            f"{repo.full_name} has no production windows"
            + (f" that ended in the last {minutes} minute(s)" if minutes else "")
            + " (coverset flows import FILE)"
        )
    regions = await asyncio.to_thread(ParquetPriorRun(store, repo.id, run.id).functions)
    return _Context(store, repo, run, prod, regions)


async def _gaps(
    settings: Settings,
    repo_spec: str | None,
    run_id: str | None,
    service: str | None,
    last_windows: int,
    minutes: int | None = None,
) -> dict[str, Any]:
    from coverset.analysis.archetypes import production_gaps
    from coverset.facts.flows import run_functions_reached, run_shapes_by_function

    ctx = await _context(settings, repo_spec, run_id, service, last_windows, minutes)
    seen = set(ctx.rows) | set(ctx.func_calls)
    tested = await asyncio.to_thread(run_shapes_by_function, ctx.store, ctx.repo.id, ctx.run.id, seen)
    # "Never by a test" comes from line coverage, not from shapes (run_functions_reached).
    reached = await asyncio.to_thread(run_functions_reached, ctx.store, ctx.repo.id, ctx.run.id, seen)
    functions = []
    never_tested = []
    for key in sorted(seen):
        region = ctx.by_key[key]
        prod_rows = [
            (r["param"], r["shape_key"], float(r["calls"]), int(r["traces"] or 0)) for r in ctx.rows[key]
        ]
        result = production_gaps(
            region,
            prod_rows,
            tested.get(key, []),
            func_calls=ctx.func_calls.get(key) or None,
            reached=key in reached,
        )
        name = f"{region.path}:{region.qualname}"
        if not result.tested:
            never_tested.append({"function": name, "func_key": key, "calls": result.prod_calls})
        if not result.gaps:
            continue
        functions.append(
            {
                "function": name,
                "func_key": key,
                "qualname": region.qualname,
                "path": region.path,
                "line": region.def_line,
                "prod_calls": result.prod_calls,
                "tested": result.tested,
                "prod_classes": len(result.prod_classes),
                "classes_in_both": len(result.both),
                "score": result.score,
                "gaps": [
                    {
                        "class": g.name,
                        "param": g.param,
                        "share": round(g.share, 4),
                        "calls": round(g.calls, 1),
                        "traces": g.traces,
                        "description": g.description,
                        "strategy": g.strategy,
                        "shapes": list(g.shapes),
                    }
                    for g in result.gaps
                ],
            }
        )
    functions.sort(key=lambda f: (-f["score"], f["function"]))
    never_tested.sort(key=lambda f: (-f["calls"], f["function"]))
    untested_keys = {f["func_key"] for f in never_tested}
    pooled: dict[tuple[str, ...], int] = defaultdict(int)
    for flow in ctx.prod.flows:
        if flow["k_ok"] and untested_keys & set(flow["funcs"]):
            pooled[tuple(flow["funcs"])] += flow["traces"] or 0
    flows = [
        {
            "functions": [ctx.by_key[k].qualname if k in ctx.by_key else k for k in funcs],
            "untested": sorted(ctx.by_key[k].qualname for k in funcs if k in untested_keys),
            "traces": traces,
        }
        for funcs, traces in pooled.items()
    ]
    flows.sort(key=lambda f: (-f["traces"], f["functions"]))
    return {
        "repo": ctx.repo.full_name,
        "run": ctx.run.id,
        "head_sha": ctx.run.head_sha,
        "branch": ctx.run.branch,
        "windows": ctx.window_text(),
        "functions": functions,
        "never_tested": never_tested,
        "flows_through_untested": flows[:20],
        "unresolved": sorted(f"{m}.{q}" for m, q in ctx.unresolved if not q.endswith(_LAMBDA)),
        "unresolved_lambdas": sum(1 for _, q in ctx.unresolved if q.endswith(_LAMBDA)),
    }


def _calls(value: float) -> str:
    return f"{value:,.0f}" if value >= 10 else f"{value:.1f}".rstrip("0").rstrip(".")


def _gaps_text(report: dict[str, Any], limit: int) -> str:
    lines = [
        f"{report['repo']} · production {report['windows']}",
        f"tests: run {report['run']} ({report['branch']} @ {report['head_sha'][:7]})",
        "",
    ]
    if not report["functions"]:
        lines.append("every input class production passed in was also passed in by some test")
    for func in report["functions"][:limit]:
        lines.append(
            f"{func['qualname']}  {func['path']}:{func['line']}  ~{_calls(func['prod_calls'])} calls · "
            f"{len(func['gaps'])} of {func['prod_classes']} production classes never in tests"
            + ("" if func["tested"] else " (no test calls it)")
        )
        width = max(len(g["class"]) for g in func["gaps"])
        for gap in func["gaps"]:
            lines.append(
                f"  {gap['class'].ljust(width)}  {gap['share'] * 100:5.1f}%  ~{_calls(gap['calls']):>7} calls"
                f"  {gap['strategy'] or gap['description']}"
            )
        lines.append("")
    if len(report["functions"]) > limit:
        lines += [f"... {len(report['functions']) - limit} more function(s) with gaps (--limit)", ""]
    if report["never_tested"]:
        lines.append("called in production, never by a test:")
        for func in report["never_tested"][:limit]:
            lines.append(f"  {func['function']}  ~{_calls(func['calls'])} calls")
        lines.append("")
    if report["flows_through_untested"]:
        lines.append("production flows through untested functions (traces summed over windows):")
        for flow in report["flows_through_untested"][:10]:
            names = ", ".join(f"{n}*" if n in flow["untested"] else n for n in flow["functions"])
            lines.append(f"  {flow['traces']:>6} trace(s)  {names}")
        lines.append("  (* never called by a test)")
        lines.append("")
    if report["unresolved"]:
        names = ", ".join(report["unresolved"][:5])
        more = f" and {len(report['unresolved']) - 5} more" if len(report["unresolved"]) > 5 else ""
        lines += [f"not in run {report['run']} (renamed, deleted or outside the repo): {names}{more}", ""]
    return "\n".join(lines)


# -- archetypes ----------------------------------------------------------------------------------------


@flows_group.command("archetypes")
@_window_options
@click.option(
    "--func", "func_name", default=None, help="Only this function (qualname, module.qualname or func_key)."
)
@click.option("--patterns", is_flag=True, help="Also list the closed frequent itemsets per function.")
@click.option(
    "--limit", type=click.IntRange(1, 10_000), default=10, show_default=True, help="Functions shown."
)
def flows_archetypes_command(
    repo_spec: str | None,
    run_id: str | None,
    service: str | None,
    last_windows: int,
    minutes: int | None,
    as_json: bool,
    func_name: str | None,
    patterns: bool,
    limit: int,
) -> None:
    """Cluster each function's production calls into archetypes and name the tests that cover each."""
    settings = load()
    report = run_async(
        lambda: _archetypes(settings, repo_spec, run_id, service, last_windows, func_name, minutes)
    )
    if as_json:
        click.echo(json.dumps(_limit(report, limit), indent=2, default=str))
    else:
        click.echo(_archetypes_text(report, limit, patterns), nl=False)


async def _archetypes(
    settings: Settings,
    repo_spec: str | None,
    run_id: str | None,
    service: str | None,
    last_windows: int,
    func_name: str | None,
    minutes: int | None = None,
) -> dict[str, Any]:
    from coverset.analysis.archetypes import (
        archetype_strategy,
        classify_shapes,
        find_archetypes,
        frequent_itemsets,
        match_tests,
        universal_items,
    )
    from coverset.commands.facts import match_function
    from coverset.facts.flows import run_shapes_by_function

    ctx = await _context(settings, repo_spec, run_id, service, last_windows, minutes)
    keys = sorted(ctx.calls)
    if func_name is not None:
        wanted = match_function(list(ctx.by_key.values()), func_name).func_key
        keys = [k for k in keys if k == wanted]
    tested = await asyncio.to_thread(run_shapes_by_function, ctx.store, ctx.repo.id, ctx.run.id, keys)
    functions = []
    for key in keys:
        region = ctx.by_key[key]
        rows = ctx.calls[key]
        test_rows = tested.get(key, [])
        pairs = [pair for r in rows for pair in r["vector"]] + [(p, s) for p, s, _ in test_rows]
        classes, info = classify_shapes(region, pairs)
        itemsets = [
            (
                frozenset().union(*(classes.get(tuple(pair), frozenset()) for pair in r["vector"])),
                float(r["calls"]),
                int(r["traces"] or 0),
            )
            for r in rows
        ]
        test_items: dict[str, set[str]] = defaultdict(set)
        for param, shape, nodeids in test_rows:
            for nodeid in nodeids:
                test_items[nodeid] |= classes.get((param, shape), frozenset())
        frozen = {n: frozenset(items) for n, items in test_items.items()}
        archetypes = find_archetypes(itemsets)
        entries = []
        for arch in archetypes:
            matches = match_tests(arch, frozen)
            kinds = {m.kind for m in matches}
            status = (
                "exact"
                if "exact" in kinds
                else "superset"
                if "superset" in kinds
                else "partial"
                if matches
                else "untested"
            )
            entries.append(
                {
                    "id": arch.id,
                    "name": arch.name,
                    "core": sorted(arch.core),
                    "share": round(arch.share, 4),
                    "calls": round(arch.weight, 1),
                    "traces": arch.traces,
                    "status": status,
                    "tests": [
                        {"nodeid": m.nodeid, "kind": m.kind, "overlap": round(m.overlap, 2)}
                        for m in matches[:5]
                    ],
                    "n_tests": len(matches),
                    "strategy": archetype_strategy(arch, info) if status != "exact" else "",
                }
            )
        total = sum(w for _, w, _ in itemsets)
        common = universal_items([(items, w) for items, w, _ in itemsets])
        frequent = frequent_itemsets([(items - common, w) for items, w, _ in itemsets])
        functions.append(
            {
                "function": f"{region.path}:{region.qualname}",
                "func_key": key,
                "qualname": region.qualname,
                "calls": round(total, 1),
                "untested_share": round(sum(e["share"] for e in entries if e["status"] == "untested"), 4),
                "common": sorted(common),
                "archetypes": entries,
                "patterns": [{"items": sorted(items), "support": round(s, 4)} for items, s in frequent[:10]],
            }
        )
    functions.sort(key=lambda f: (-f["untested_share"] * f["calls"], f["function"]))
    return {
        "repo": ctx.repo.full_name,
        "run": ctx.run.id,
        "head_sha": ctx.run.head_sha,
        "branch": ctx.run.branch,
        "windows": ctx.window_text(),
        "functions": functions,
    }


def _archetypes_text(report: dict[str, Any], limit: int, patterns: bool) -> str:
    lines = [
        f"{report['repo']} · production {report['windows']}",
        f"tests: run {report['run']} ({report['branch']} @ {report['head_sha'][:7]})",
        "",
    ]
    if not report["functions"]:
        lines.append("no call vectors in these windows (the probe exports them per sampled call)")
    for func in report["functions"][:limit]:
        lines.append(
            f"{func['qualname']}  {func['function'].rsplit(':', 1)[0]}  ~{_calls(func['calls'])} sampled calls"
        )
        if func["common"]:
            lines.append(f"  every call: {' + '.join(func['common'])}")
        for arch in func["archetypes"]:
            tests = ", ".join(f"{t['nodeid']} ({t['kind']})" for t in arch["tests"][:2])
            more = f" +{arch['n_tests'] - 2}" if arch["n_tests"] > 2 else ""
            lines.append(f"  [{arch['id']}] {arch['share'] * 100:5.1f}%  {arch['status']:<8}  {arch['name']}")
            if tests:
                lines.append(f"      covered by {tests}{more}")
            if arch["strategy"]:
                lines.append(f"      {arch['strategy']}")
        if patterns and func["patterns"]:
            lines.append("  frequent patterns:")
            for pattern in func["patterns"]:
                lines.append(f"    {pattern['support'] * 100:5.1f}%  {' + '.join(pattern['items'])}")
        lines.append("")
    return "\n".join(lines)
