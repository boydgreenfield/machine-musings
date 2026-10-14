"""``coverset facts query|tables|explain|test|line|export`` and ``coverset shapes``: read-only
views of the facts store.

The debugging subcommands each take ``--repo`` and ``--run`` (defaulting like the UI pages) and
``--json``: ``tables`` (per-table files, rows and bytes), ``explain`` (the run row, timeline,
shards, ingest log with tracer notes, coverage completeness, limits hit, bundle sizes, the prior
run), ``test NODEID`` (what one test touched and passed), ``line PATH:LINE`` (the tests that ran a
line, in run order) and ``export --to DIR`` (every table as JSON lines with a ``tables.json``
index).

``coverset shapes FUNC`` pools the shapes of the last ``--runs`` runs of one branch: by default the
repo's default branch (baseline runs only), so its ``--markdown`` table is reproducible from
baseline history and is what the README embeds; ``--branch any`` pools every branch, PR runs
included. The function's region and annotations come from the newest completed run of that branch.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from sqlalchemy import select

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings

__all__ = ["ANY_BRANCH", "FACTS_DEBUG_COMMANDS", "facts_query_command", "shapes_command"]

MAX_CELL_CHARS = 60
MAX_NOTE_CHARS = 500
"""An ingest note (a converter error, say) is cut here in ``facts explain``."""
EXPORT_MARKER = ".coverset-export"
"""File ``facts export`` writes into its target; only such a directory is written into again."""
ANY_BRANCH = "any"
"""``--branch`` value that pools runs of every branch."""


@click.command("query")
@click.argument("sql")
@click.option("--repo-id", type=int, default=None, help="Limit the views to one repo.")
@click.option("--run", "run_id", default=None, help="Limit the views to one run.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON lines instead of a table.")
def facts_query_command(sql: str, repo_id: int | None, run_id: str | None, as_json: bool) -> None:
    """Run one read-only SELECT over the facts views (test_lines, files, functions, call_shapes, ...)."""
    from coverset.facts.store import SqlNotAllowed, facts_store

    settings = load()
    store = facts_store(settings)
    try:
        table = store.query(sql, repo_id=repo_id, run_id=run_id)
    except SqlNotAllowed as exc:
        raise click.ClickException(str(exc)) from None
    except Exception as exc:  # DuckDB parser/binder errors
        raise click.ClickException(f"query failed: {exc}") from None
    rows = table.to_pylist()
    if as_json:
        for row in rows:
            click.echo(json.dumps(row, default=str))
        return
    click.echo(format_rows(table.column_names, rows))


def format_rows(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """A plain aligned text table (cells cut at 60 characters)."""

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 3] + "..."

    grid = [columns, *[[cell(row.get(c)) for c in columns] for row in rows]]
    widths = [max(len(r[i]) for r in grid) for i in range(len(columns))] if columns else []
    lines = ["  ".join(v.ljust(widths[i]) for i, v in enumerate(r)).rstrip() for r in grid]
    if lines:
        lines.insert(1, "  ".join("-" * w for w in widths))
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


@click.command("shapes")
@click.argument("func")
@click.option(
    "--repo", "repo_spec", default=None, help="Repository (id, full name or path); default: the only one."
)
@click.option(
    "--runs", "last_n", type=click.IntRange(1, 100), default=5, show_default=True, help="Runs to pool."
)
@click.option(
    "--branch",
    default=None,
    help="Pool runs of this branch only (default: the repo's default branch, i.e. baseline runs; "
    f"'{ANY_BRANCH}' pools every branch).",
)
@click.option("--markdown", is_flag=True, help="Print the input-class table as GitHub-flavoured markdown.")
@click.option("--suggest", is_flag=True, help="Suggest the narrowest annotation per parameter.")
def shapes_command(
    func: str, repo_spec: str | None, last_n: int, branch: str | None, markdown: bool, suggest: bool
) -> None:
    """Show FUNC's observed and missing input classes (FUNC: qualname, module.qualname or func_key)."""
    settings = load()
    click.echo(
        run_async(lambda: _shapes(settings, func, repo_spec, last_n, markdown, suggest, branch=branch)),
        nl=False,
    )


async def _shapes(
    settings: Settings,
    func: str,
    repo_spec: str | None,
    last_n: int,
    markdown: bool,
    suggest: bool,
    *,
    branch: str | None = None,
) -> str:
    from coverset.analysis.shapes import render_markdown
    from coverset.db.models import Run
    from coverset.db.session import Database
    from coverset.facts.queries import ParquetPriorRun, shapes_for_function
    from coverset.facts.store import facts_store
    from coverset.lang.python import PythonAdapter
    from coverset.orchestrator.repos import mirror_path, read_blobs

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    store = facts_store(settings)
    try:
        async with db.session() as session:
            repo = await find_repo(session, repo_spec)
            pooled = None if branch == ANY_BRANCH else branch or repo.default_branch
            query = select(Run).where(Run.repo_id == repo.id, Run.status == "completed")
            if pooled is not None:
                query = query.where(Run.branch == pooled)
            run = await session.scalar(query.order_by(Run.created_at.desc()).limit(1))
    finally:
        await db.engine.dispose()
    scope = "every branch" if pooled is None else f"branch {pooled}"
    if run is None or not store.run_dir(repo.id, run.id).is_dir():
        raise click.ClickException(f"{repo.full_name} has no completed run with facts on {scope} yet")
    regions = await asyncio.to_thread(ParquetPriorRun(store, repo.id, run.id).functions)
    region = match_function(regions, func)
    rows = await asyncio.to_thread(
        lambda: shapes_for_function(store, repo.id, region.func_key, last_n, branch=pooled)
    )
    observations = [_observation(region, row) for row in rows]
    sampling, tests_called = await asyncio.to_thread(_sampling, store, repo.id, run.id, region)
    paths = sorted({r.path for r in regions} | {region.path})
    blobs = await asyncio.to_thread(read_blobs, mirror_path(settings, repo.id), run.head_sha, paths)
    sources = {path: data.decode("utf-8", "replace") for path, data in blobs.items()}
    catalogue = PythonAdapter().shape_catalogue(sources)
    cov = catalogue.function_input_cov(region, observations, sampling, tests_called=tests_called)
    header = (
        f"{region.qualname} ({region.path}:{region.def_line}) · {len(rows)} shapes over the last "
        f"{last_n} runs on {scope}"
    )
    text = render_markdown(cov) if markdown else _plain(header, cov)
    if suggest:
        annotations = catalogue.annotations_of(region.triple)
        text += "\n" + _suggestions(observations, annotations or {})
    return text


def match_function(regions: list[Any], func: str) -> Any:
    """The callable region named by ``func`` (func_key, qualname or dotted ``module.qualname``)."""
    callables = [r for r in regions if r.kind in ("function", "method")]
    found = [r for r in callables if func in (r.func_key, r.qualname) or func in _dotted_names(r)]
    if not found:
        raise click.ClickException(f"no function {func!r} in the latest run")
    if len(found) > 1:
        names = ", ".join(f"{r.path}:{r.qualname}" for r in found[:10])
        raise click.ClickException(f"{func!r} is ambiguous: {names} (use module.qualname or the func_key)")
    return found[0]


def _dotted_names(region: Any) -> set[str]:
    parts = region.path.removesuffix(".py").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join([*parts[i:], region.qualname]) for i in range(len(parts))}


def _observation(region: Any, row: Any) -> Any:
    from coverset.lang.base import ShapeObs

    return ShapeObs(
        func=region.triple,
        param=row.param,
        shape_key=row.shape_key,
        caller=row.example_test or "",
        samples=row.samples,
        tests=row.tests,
        truncated=False,
        example=row.example,
        example_test=row.example_test,
    )


def _sampling(store: Any, repo_id: int, run_id: str, region: Any) -> tuple[Any, list[str]]:
    """The function's sampling summary in the run and the tests that called it.

    Built the way the pages build it (sampled nodeids from ``func_sampling``, callers from
    ``tests_covering``), so ``cap_hits`` counts the same tests here as on the function page.
    """
    from coverset.facts.queries import tests_covering
    from coverset.lang.base import FuncSampling

    rows = [
        r
        for r in store.read_table(repo_id, run_id, "func_sampling").to_pylist()
        if r["func_key"] == region.func_key
    ]
    called = tests_covering(store, repo_id, run_id, region.path, region.body_start, region.end_line)
    if not rows:
        return None, called
    ids = store.dictionary(repo_id)
    sampled = sorted({ids.nodeid(t) for r in rows for t in (r.get("test_ids") or ())})
    sampling = FuncSampling(
        func=region.triple,
        calls=sum(int(r.get("calls") or 0) for r in rows),
        tests=tuple(sampled),
        truncated=any(bool(r.get("truncated")) for r in rows),
    )
    return sampling, called


def _plain(header: str, cov: Any) -> str:
    lines = [
        header,
        f"input classes {cov.observed_expected} of {len(cov.expected)}"
        + (f" ({cov.pct:g}%)" if cov.pct is not None else ""),
    ]
    width = max((len(c.name) for c in cov.expected), default=10)
    status_width = max(len("observed"), len(cov.missing_reason))
    for cls in cov.expected:
        if cls.name in cov.observed:
            lines.append(
                f"  {cls.name.ljust(width)}  {'observed'.ljust(status_width)}  {cov.examples.get(cls.name, '')}"
            )
        else:
            lines.append(
                f"  {cls.name.ljust(width)}  {cov.missing_reason.ljust(status_width)}  {cls.hypothesis_hint}"
            )
    if cov.note:
        lines.append(f"note: {cov.note}")
    return "\n".join(lines) + "\n"


def _suggestions(observations: list[Any], declared: dict[str, str | None]) -> str:
    from coverset.analysis.shapes import suggest_annotation

    by_param: dict[str, list[Any]] = defaultdict(list)
    for obs in observations:
        by_param[obs.param].append(obs)
    lines = ["suggested annotations:"]
    for param in sorted(by_param):
        if param in ("<receiver>",):
            continue
        suggested = suggest_annotation(by_param[param])
        current = declared.get(param.lstrip("*"))
        flag = (
            ""
            if current is None or current.replace(" ", "") == suggested.replace(" ", "")
            else f"  (declared: {current})"
        )
        lines.append(f"  {param}: {suggested}{flag}")
    return "\n".join(lines) + "\n"


# -- debugging subcommands: facts tables|explain|test|line|export ----------------------------------

_REPO_HELP = "Repository (id, full name or path); default: the only one."
_RUN_HELP = "Run id (default: the newest completed default-branch baseline, else the newest completed run)."


def _emit(data: Any, text: str, as_json: bool) -> None:
    """Print ``data`` as one JSON document, or ``text``."""
    if as_json:
        click.echo(json.dumps(data, default=str, indent=2, sort_keys=False))
    else:
        click.echo(text, nl=not text.endswith("\n"))


async def _repo_and_run(
    settings: Settings, repo_spec: str | None, run_id: str | None
) -> tuple[Any, Any, Any]:
    """``(repo, run, other rows)`` for a debugging command; the run defaults like the UI pages."""
    from coverset.db.models import PullRequest, Run, Shard
    from coverset.db.session import Database

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            repo = await find_repo(session, repo_spec)
            if run_id:
                run = await session.get(Run, run_id)
                if run is None or run.repo_id != repo.id:
                    raise click.ClickException(f"{repo.full_name} has no run {run_id!r}")
            else:
                run = await _default_run(session, repo)
                if run is None:
                    raise click.ClickException(f"{repo.full_name} has no completed run yet")
            shards = list(
                await session.scalars(select(Shard).where(Shard.run_id == run.id).order_by(Shard.shard_id))
            )
            pr = await session.get(PullRequest, run.pr_id) if run.pr_id else None
            prior = await session.get(Run, run.scoring_basis_run_id) if run.scoring_basis_run_id else None
            results = await _results(session, run.id)
    finally:
        await db.engine.dispose()
    return repo, run, {"shards": shards, "pr": pr, "prior": prior, "results": results}


async def _default_run(session: Any, repo: Any) -> Any:
    """The newest completed default-branch baseline, else the newest completed run (the UI's rule,
    ``coverset.web.loaders.default_run``, without importing the web app)."""
    from coverset.db.models import Run

    completed = select(Run).where(Run.repo_id == repo.id, Run.status == "completed")
    baseline = completed.where(Run.branch == repo.default_branch, Run.trigger.in_(("baseline", "push")))
    return await session.scalar(baseline.order_by(Run.created_at.desc()).limit(1)) or await session.scalar(
        completed.order_by(Run.created_at.desc()).limit(1)
    )


async def _results(session: Any, run_id: str) -> dict[str, dict[str, Any]]:
    from coverset.db.models import TestCase, TestResult

    rows = await session.execute(
        select(TestResult, TestCase.nodeid)
        .join(TestCase, TestCase.id == TestResult.test_id)
        .where(TestResult.run_id == run_id)
    )
    return {
        nodeid: {
            "outcome": r.outcome,
            "rank": r.rank,
            "duration_ms": r.duration_ms,
            "attempts": r.attempts,
            "flaky": r.flaky,
            "shard": r.shard_id,
        }
        for r, nodeid in rows.all()
    }


def _require_facts(store: Any, repo: Any, run: Any) -> None:
    if not store.run_dir(repo.id, run.id).is_dir():
        raise click.ClickException(f"run {run.id} has no facts (never ingested, or removed by retention)")


def _ranges(lines: Any) -> str:
    out: list[list[int]] = []
    for line in sorted(set(lines)):
        if out and line == out[-1][1] + 1:
            out[-1][1] = line
        else:
            out.append([line, line])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in out)


def _size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)


@click.command("tables")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--run", "run_id", default=None, help=_RUN_HELP)
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of a table.")
def facts_tables_command(repo_spec: str | None, run_id: str | None, as_json: bool) -> None:
    """Inventory of a run's facts: every Parquet table with its files, rows and bytes."""
    from coverset.facts.queries import run_inventory
    from coverset.facts.store import facts_store

    settings = load()
    repo, run, _extra = run_async(lambda: _repo_and_run(settings, repo_spec, run_id))
    store = facts_store(settings)
    _require_facts(store, repo, run)
    inventory = run_inventory(store, repo.id, run.id)
    rows = [
        {"table": t.table, "kind": t.kind, "files": t.files, "rows": t.rows, "bytes": t.bytes}
        for t in inventory.values()
    ]
    total = sum(t.bytes for t in inventory.values())
    text = (
        f"{repo.full_name} · run {run.id} ({run.head_sha[:8]})\n"
        + format_rows(["table", "kind", "files", "rows", "bytes"], rows)
        + f"\ntotal {_size(total)} in {sum(t.files for t in inventory.values())} files\n"
    )
    _emit({"repo": repo.full_name, "run_id": run.id, "tables": rows, "bytes": total}, text, as_json)


@click.command("explain")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--run", "run_id", required=True, help="Run id.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
def facts_explain_command(repo_spec: str | None, run_id: str, as_json: bool) -> None:
    """Everything recorded about one run: the run row, timeline, shards, ingest log (with tracer
    notes), coverage completeness, limits hit, bundle sizes and the run it was scored against."""
    from coverset.facts.store import facts_store

    settings = load()
    repo, run, extra = run_async(lambda: _repo_and_run(settings, repo_spec, run_id))
    store = facts_store(settings)
    data = _explain(settings, store, repo, run, extra)
    _emit(data, _explain_text(data), as_json)


def _explain(settings: Settings, store: Any, repo: Any, run: Any, extra: dict[str, Any]) -> dict[str, Any]:
    from coverset.facts.queries import ingest_record, run_summary
    from coverset.orchestrator.verdict import t0_of

    t0 = t0_of(run)
    timeline: dict[str, float | None] = {}
    for key, value in (run.timeline or {}).items():
        try:
            timeline[key] = round((datetime.fromisoformat(value) - t0).total_seconds(), 3)
        except (TypeError, ValueError):
            timeline[key] = None
    timeline = dict(sorted(timeline.items(), key=lambda kv: (kv[1] is None, kv[1] or 0.0, kv[0])))
    has_facts = store.run_dir(repo.id, run.id).is_dir()
    summary = run_summary(store, repo.id, run.id) if has_facts else None
    records = ingest_record(store, repo.id, run.id) if has_facts else []
    limits = _limits(store, repo.id, run.id, records) if has_facts else {}
    prior = extra["prior"]
    pr = extra["pr"]
    bundles_dir = settings.runs_dir / run.id
    files_on_disk = (
        [
            {"path": str(p.relative_to(bundles_dir)), "bytes": p.stat().st_size}
            for p in sorted(bundles_dir.rglob("*"))
            if p.is_file()
        ]
        if bundles_dir.is_dir()
        else []
    )
    return {
        "run": {
            "id": run.id,
            "repo": repo.full_name,
            "status": run.status,
            "verdict": run.verdict,
            "reason": run.reason,
            "trigger": run.trigger,
            "branch": run.branch,
            "pr": pr.number if pr is not None else None,
            "head_sha": run.head_sha,
            "base_sha": run.base_sha,
            "executor": run.executor,
            "trusted": run.trusted,
            "shard_count": run.shard_count,
            "created_at": run.created_at,
        },
        "timeline_s": timeline,
        "timeline_origin": "webhook_received" if (run.timeline or {}).get("webhook_received") else "queued",
        "shards": [
            {
                "shard": s.shard_id,
                "status": s.status,
                "tests": s.test_count,
                "exit_code": s.exit_code,
                "bundle_sha256": s.bundle_sha256,
                "error": s.error,
            }
            for s in extra["shards"]
        ],
        "facts": has_facts,
        "coverage_complete": summary.coverage_complete if summary else False,
        "finalized": summary.finalized if summary else False,
        "expected_shards": summary.expected_shards if summary else None,
        "ingested_shards": summary.ingested_shards if summary else 0,
        "ingest_log": [
            {
                "shard": r.shard_id,
                "attempt": r.attempt,
                "status": r.status,
                "committed": r.committed,
                "bundle_sha256": r.bundle_sha256,
                "bundle_bytes": r.bundle_bytes,
                "tests": r.n_tests,
                "test_lines": r.n_test_lines,
                "ingested_at": r.ingested_at,
                "notes": [_safe_note(note) for note in r.tracer_notes],
            }
            for r in records
        ],
        "limits": limits,
        "bundle_bytes": sum(r.bundle_bytes or 0 for r in records if r.committed),
        "bundle_files": files_on_disk,
        "prior_run": None
        if prior is None
        else {"id": prior.id, "status": prior.status, "head_sha": prior.head_sha, "branch": prior.branch},
    }


def _safe_note(note: str) -> str:
    """An ingest note as ``explain`` prints it: credential shapes and registered secrets redacted,
    control and bidirectional characters shown as escapes, at most :data:`MAX_NOTE_CHARS`. The
    converter's error text can quote the bundle, which is untrusted."""
    from coverset.logging import redact

    text = redact(str(note))
    text = "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in text)
    return text if len(text) <= MAX_NOTE_CHARS else text[: MAX_NOTE_CHARS - 1] + "…"


def _limits(store: Any, repo_id: int, run_id: str, records: Any) -> dict[str, Any]:
    """Caps the run hit: the converter's dropped records and the shape tracer's sample caps."""
    sampling = store.read_table(repo_id, run_id, "func_sampling").to_pylist()
    capped = sorted({r["qualname"] for r in sampling if r["truncated"]})
    dropped: dict[str, int] = defaultdict(int)
    for record in records:
        if record.committed:
            for key, value in (record.details.get("dropped") or {}).items():
                if isinstance(value, int) and value:
                    dropped[key] += value
    return {
        "dropped": dict(sorted(dropped.items())),
        "functions_sample_capped": capped,
        "tests_over_sample_cap": sum(int(r["tests_truncated"] or 0) for r in sampling),
    }


def _explain_text(data: dict[str, Any]) -> str:
    run = data["run"]
    lines = [
        f"run {run['id']} · {run['repo']} · {run['trigger']} on {run['branch'] or 'detached'}"
        + (f" (PR #{run['pr']})" if run["pr"] else ""),
        f"  status {run['status']} · verdict {run['verdict'] or '-'} · {run['reason'] or ''}".rstrip(),
        f"  head {run['head_sha'][:8]} base {(run['base_sha'] or '-')[:8]} · executor {run['executor']}"
        f" · trusted {run['trusted']} · {run['shard_count']} shard(s) · created {run['created_at']}",
        f"timeline (seconds from {data['timeline_origin']}):",
        *[f"  {k:<22} {'-' if v is None else f'{v:.3f}'}" for k, v in data["timeline_s"].items()],
        "shards:",
        *[
            f"  {s['shard']}: {s['status']} · {s['tests']} tests · exit {s['exit_code']}"
            + (f" · bundle {s['bundle_sha256'][:12]}" if s["bundle_sha256"] else "")
            + (f" · error {s['error']}" if s["error"] else "")
            for s in data["shards"]
        ],
    ]
    if not data["facts"]:
        lines.append("facts: none for this run")
    else:
        lines += [
            f"facts: coverage_complete {data['coverage_complete']} · finalized {data['finalized']} · "
            f"shards {data['ingested_shards']} of {data['expected_shards']}",
            "ingest log:",
            *[
                f"  shard {r['shard']} attempt {r['attempt']}: {r['status']}{' (committed)' if r['committed'] else ''}"
                f" · {r['tests']} tests · {r['test_lines']} test lines"
                + (f" · {_size(r['bundle_bytes'])}" if r["bundle_bytes"] else "")
                + "".join(f"\n      {note}" for note in r["notes"])
                for r in data["ingest_log"]
            ],
            f"limits: dropped {data['limits']['dropped'] or 'nothing'} · sample cap hit in "
            f"{len(data['limits']['functions_sample_capped'])} function(s), "
            f"{data['limits']['tests_over_sample_cap']} test(s) over the cap",
            f"bundles: {_size(data['bundle_bytes'])} committed"
            + (
                f" · {len(data['bundle_files'])} file(s) under runs/{run['id']}/"
                if data["bundle_files"]
                else ""
            ),
        ]
    prior = data["prior_run"]
    lines.append(
        f"scored against: {prior['id']} ({prior['status']}, {prior['head_sha'][:8]} on {prior['branch']})"
        if prior
        else "scored against: nothing (no prior run)"
    )
    return "\n".join(lines) + "\n"


@click.command("test")
@click.argument("nodeid")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--run", "run_id", default=None, help=_RUN_HELP)
@click.option(
    "--history",
    "last_n",
    type=click.IntRange(1, 200),
    default=12,
    show_default=True,
    help="Runs of outcome history.",
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
def facts_test_command(
    nodeid: str, repo_spec: str | None, run_id: str | None, last_n: int, as_json: bool
) -> None:
    """What test NODEID ran and passed in a run: files, functions and lines, its input shapes per
    parameter, and its outcome in the last runs."""
    from coverset.facts.queries import test_history, test_shapes, test_touched
    from coverset.facts.store import facts_store

    settings = load()
    repo, run, extra = run_async(lambda: _repo_and_run(settings, repo_spec, run_id))
    store = facts_store(settings)
    _require_facts(store, repo, run)
    touched = test_touched(store, repo.id, run.id, nodeid)
    shapes = test_shapes(store, repo.id, run.id, nodeid)
    history = test_history(store, repo.id, nodeid, last_n)
    if not touched and not shapes and not any(h.outcome for h in history):
        raise click.ClickException(f"no facts mention test {nodeid!r} in {repo.full_name}")
    result = extra["results"].get(nodeid)
    data = {
        "nodeid": nodeid,
        "run_id": run.id,
        "result": result,
        "files": [
            {
                "path": f.path,
                "lines": list(f.lines),
                "statements": f.statements,
                "module_lines": list(f.module_lines),
                "functions": [
                    {
                        "func_key": fn.func_key,
                        "qualname": fn.qualname,
                        "def_line": fn.def_line,
                        "lines": list(fn.lines),
                        "body_statements": fn.body_statements,
                    }
                    for fn in f.functions
                ],
            }
            for f in touched
        ],
        "shapes": [
            {
                "path": o.func[0],
                "function": o.func[1],
                "param": o.param,
                "shape": o.shape_key,
                "caller": o.caller if isinstance(o.caller, str) else f"{o.caller[0]}:{o.caller[1]}",
                "samples_all_tests": o.samples,
                "tests": len(o.tests),
                "example": o.example if o.example_test == nodeid else None,
            }
            for o in shapes
        ],
        "history": [
            {
                "run_id": h.run_id,
                "created_at": h.created_at,
                "branch": h.branch,
                "pr": h.pr_number,
                "outcome": h.outcome,
            }
            for h in history
        ],
    }
    lines = [f"{nodeid} · run {run.id} ({run.head_sha[:8]})"]
    if result:
        lines.append(
            f"result: {result['outcome']}{' (flaky)' if result['flaky'] else ''} · {result['attempts']} attempt(s)"
            f" · rank {result['rank'] or '-'} · shard {result['shard']}"
        )
    lines.append("touched:")
    for f in data["files"]:
        lines.append(f"  {f['path']}: {len(f['lines'])} lines, {f['statements']} statements")
        for fn in f["functions"]:
            lines.append(
                f"    {fn['qualname']} (:{fn['def_line']}) ran {len(fn['lines'])} of {fn['body_statements']}"
                f" body statements: {_ranges(fn['lines'])}"
            )
        if f["module_lines"]:
            lines.append(f"    module-level: {_ranges(f['module_lines'])}")
    if not data["files"]:
        lines.append("  (no measured line)")
    lines.append("passed in (samples count every test of a call_shapes row):")
    table = format_rows(
        ["function", "param", "shape", "caller", "samples_all_tests", "example"], data["shapes"]
    )
    lines += ["  " + row for row in table.splitlines()]
    lines.append("history (newest first): " + " ".join(h["outcome"] or "-" for h in data["history"]))
    _emit(data, "\n".join(lines) + "\n", as_json)


@click.command("line")
@click.argument("location")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--run", "run_id", default=None, help=_RUN_HELP)
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
def facts_line_command(location: str, repo_spec: str | None, run_id: str | None, as_json: bool) -> None:
    """Which tests ran LOCATION (``path:line``) in a run, in run order, and the function it belongs to."""
    from coverset.facts.queries import ParquetPriorRun, line_tests
    from coverset.facts.schema import CALLABLE_KINDS
    from coverset.facts.store import facts_store

    path, sep, number = location.rpartition(":")
    if not sep or not path or not (number.isascii() and number.isdigit()):
        raise click.BadParameter("expected PATH:LINE, e.g. src/pkg/mod.py:42", param_hint="LOCATION")
    line = int(number)
    settings = load()
    repo, run, extra = run_async(lambda: _repo_and_run(settings, repo_spec, run_id))
    store = facts_store(settings)
    _require_facts(store, repo, run)
    nodeids = line_tests(store, repo.id, run.id, path, line)
    results = extra["results"]
    at_import = "<import>" in nodeids
    tests = sorted(
        (n for n in nodeids if n != "<import>"),
        key=lambda n: ((results.get(n) or {}).get("rank") or 10**9, n),
    )
    regions = [
        r
        for r in ParquetPriorRun(store, repo.id, run.id).functions()
        if r.path == path and r.kind in CALLABLE_KINDS and r.def_line <= line <= r.end_line
    ]
    owner = max(regions, key=lambda r: r.def_line, default=None)
    data = {
        "path": path,
        "line": line,
        "run_id": run.id,
        "function": None
        if owner is None
        else {
            "qualname": owner.qualname,
            "func_key": owner.func_key,
            "def_line": owner.def_line,
            "end_line": owner.end_line,
            "in_body": owner.body_start <= line <= owner.end_line,
        },
        "import_time": at_import,
        "tests": [{"nodeid": n, **(results.get(n) or {})} for n in tests],
    }
    where = (
        f"in {owner.qualname} ({path}:{owner.def_line}-{owner.end_line})"
        + ("" if data["function"]["in_body"] else ", on its def or decorator line")  # type: ignore[index]
        if owner is not None
        else "module-level code"
    )
    rows = [
        {"rank": t.get("rank") or "-", "outcome": t.get("outcome") or "-", "nodeid": t["nodeid"]}
        for t in data["tests"]
    ]
    text = (
        f"{path}:{line} · {where} · run {run.id}\n"
        + f"{len(tests)} test(s) ran it{' · also run at import time' if at_import else ''}\n"
        + (format_rows(["rank", "outcome", "nodeid"], rows) + "\n" if rows else "")
    )
    _emit(data, text, as_json)


@click.command("export")
@click.option("--repo", "repo_spec", default=None, help=_REPO_HELP)
@click.option("--run", "run_id", required=True, help="Run id.")
@click.option(
    "--to",
    "target",
    required=True,
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    help="Directory to write (created if missing). It must be new, empty, or an earlier export's.",
)
@click.option(
    "--force", is_flag=True, help="Write into a non-empty directory that is not an earlier export's."
)
@click.option("--json", "as_json", is_flag=True, help="Print the index as JSON.")
def facts_export_command(
    repo_spec: str | None, run_id: str, target: Path, force: bool, as_json: bool
) -> None:
    """Write every facts table of a run as JSON lines (``<table>.jsonl``) plus a ``tables.json``
    index and the repo's ``dictionary.json`` (the ids behind ``test_id``, ``file_id``, ``shape_id``).

    Facts hold no secrets: paths, node ids, line numbers, shape keys and example values that the
    tracer already redacted (and failure text is not stored in facts at all), so the export is safe
    to attach to a bug report; it is still the repository's code structure, so share it as you
    would the repository.

    The target must be new, empty, or hold the ``.coverset-export`` marker an earlier export wrote
    (``--force`` writes into any directory). Every file is created afresh (an existing name, a
    symlink included, is removed first and never followed).
    """
    from coverset.facts.dictionary import DICTIONARY_FILE
    from coverset.facts.schema import RUN_TABLES, TABLES
    from coverset.facts.store import facts_store

    settings = load()
    repo, run, _extra = run_async(lambda: _repo_and_run(settings, repo_spec, run_id))
    store = facts_store(settings)
    _require_facts(store, repo, run)
    _check_export_target(target, force)
    target.mkdir(parents=True, exist_ok=True)
    with _create(target, EXPORT_MARKER) as fh:
        fh.write("coverset facts export: this directory is rewritten by `coverset facts export`.\n")
    index: dict[str, Any] = {"repo": repo.full_name, "repo_id": repo.id, "run_id": run.id, "tables": {}}
    for table in TABLES:
        data = store.read_table(repo.id, run.id, table)
        name = f"{table}.jsonl"
        with _create(target, name) as fh:
            for row in data.to_pylist():
                fh.write(json.dumps(row, default=str) + "\n")
        index["tables"][table] = {
            "file": name,
            "kind": "run" if table in RUN_TABLES else "shard",
            "rows": data.num_rows,
            "columns": data.column_names,
            "source_files": [p.name for p in store.table_files(repo.id, run.id, table)],
        }
    dictionary = store.repo_dir(repo.id) / DICTIONARY_FILE
    if dictionary.is_file():
        with _create(target, DICTIONARY_FILE) as fh:
            fh.write(dictionary.read_text(encoding="utf-8"))
        index["dictionary"] = DICTIONARY_FILE
    with _create(target, "tables.json") as fh:
        fh.write(json.dumps(index, indent=2) + "\n")
    rows = [{"table": t, "rows": v["rows"], "file": v["file"]} for t, v in index["tables"].items()]
    _emit(
        index,
        f"wrote {len(rows)} tables to {target}\n" + format_rows(["table", "rows", "file"], rows),
        as_json,
    )


def _check_export_target(target: Path, force: bool) -> None:
    """Refuse a target that is a symlink or not a directory, and a non-empty directory without
    :data:`EXPORT_MARKER` unless ``force``."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise click.ClickException(f"{target} exists and is not a directory")
    if force or not any(target.iterdir()):
        return
    try:
        marker = os.lstat(target / EXPORT_MARKER)
    except FileNotFoundError:
        marker = None
    if marker is None or not stat.S_ISREG(marker.st_mode):
        raise click.ClickException(
            f"{target} is not empty and holds no earlier export ({EXPORT_MARKER}): pass an empty or "
            "new --to directory, or --force to write into it"
        )


def _create(target: Path, name: str) -> Any:
    """``target/name`` opened for writing as a new file: whatever has that name (a file, or a symlink,
    which is removed, not followed) goes first, and the open never follows a link."""
    path = target / name
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise click.ClickException(f"cannot replace {path}: {exc.strerror}") from None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        raise click.ClickException(f"cannot create {path}: {exc.strerror}") from None
    return os.fdopen(fd, "w", encoding="utf-8")


FACTS_DEBUG_COMMANDS: dict[str, click.Command] = {
    "tables": facts_tables_command,
    "explain": facts_explain_command,
    "test": facts_test_command,
    "line": facts_line_command,
    "export": facts_export_command,
}
"""The ``coverset facts`` debugging subcommands (registered next to ``facts query``)."""
