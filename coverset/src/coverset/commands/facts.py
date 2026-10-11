"""``coverset facts query`` and ``coverset shapes``: read-only views of the facts store.

``coverset shapes FUNC`` pools the shapes of the last ``--runs`` runs of one branch: by default the
repo's default branch (baseline runs only), so its ``--markdown`` table is reproducible from
baseline history and is what the README embeds; ``--branch any`` pools every branch, PR runs
included. The function's region and annotations come from the newest completed run of that branch.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

import click
from sqlalchemy import select

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import Settings

__all__ = ["ANY_BRANCH", "facts_query_command", "shapes_command"]

MAX_CELL_CHARS = 60
ANY_BRANCH = "any"
"""``--branch`` value that pools runs of every branch."""


@click.command("query")
@click.argument("sql")
@click.option("--repo-id", type=int, default=None, help="Limit the views to one repo.")
@click.option("--run", "run_id", default=None, help="Limit the views to one run.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON lines instead of a table.")
def facts_query_command(sql: str, repo_id: int | None, run_id: str | None, as_json: bool) -> None:
    """Run one read-only SELECT over the facts views (test_lines, files, functions, call_shapes, ...)."""
    from coverset.facts.store import FactsStore, SqlNotAllowed

    settings = load()
    store = FactsStore(settings.facts_dir)
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
    from coverset.facts.store import FactsStore
    from coverset.lang.python import PythonAdapter
    from coverset.orchestrator.repos import mirror_path, read_blobs

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    store = FactsStore(settings.facts_dir)
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
    sampling = await asyncio.to_thread(_sampling, store, repo.id, run.id, region)
    paths = sorted({r.path for r in regions} | {region.path})
    blobs = await asyncio.to_thread(read_blobs, mirror_path(settings, repo.id), run.head_sha, paths)
    sources = {path: data.decode("utf-8", "replace") for path, data in blobs.items()}
    catalogue = PythonAdapter().shape_catalogue(sources)
    cov = catalogue.function_input_cov(region, observations, sampling)
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


def _sampling(store: Any, repo_id: int, run_id: str, region: Any) -> Any:
    """The function's sampling summary in the run (how many tests hit the sample cap)."""
    from coverset.lang.base import FuncSampling

    rows = [
        r
        for r in store.read_table(repo_id, run_id, "func_sampling").to_pylist()
        if r["func_key"] == region.func_key
    ]
    if not rows:
        return None
    truncated = max(int(r.get("tests_truncated") or 0) for r in rows)
    return FuncSampling(
        func=region.triple,
        calls=sum(int(r.get("calls") or 0) for r in rows),
        tests=tuple(f"<sampled test {i}>" for i in range(truncated)),
        truncated=truncated > 0,
    )


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
