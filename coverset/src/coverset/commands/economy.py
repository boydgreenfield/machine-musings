"""``coverset tests economy|redundant|minimal|tax``: the test economy from a terminal.

Every command reads one run, either a local bundle (SOURCE: a ``pytest --coverset-out`` directory,
a ``bundle.tar.gz`` or a neutral bundle, read exactly as ``coverset report`` reads it, sources
from the current git work tree or ``--repo-root``) analysed on the spot with
:func:`coverset.report.economy.economy_for_input`, or a run of the facts store (``--run RUN_ID``,
optionally ``--repo``) whose economy ``finalize_run`` stored. Both give one
:class:`~coverset.report.economy.EconomyView`, so the numbers are the pages' numbers.

* ``economy``: totals, the leverage histogram as text, and the tests sorted by ``--sort``.
* ``redundant``: equivalence groups, dominated tests and the removable candidates (always with the
  "candidates only" caveat).
* ``minimal``: the covering set's keeps and drops; ``--write FILE`` writes the drops as a ``pytest``
  argument file (``pytest @FILE``, pytest 8.2+).
* ``tax``: the PR test tax against ``--compare BASE`` (or the stored tax of a ``--run``): the line
  the sticky comment shows, the new tests with the lowest leverage and those that repeat an
  existing test's flow. The base's own test files come from ``--base-ref REF`` (a commit of the
  repository) or ``--base-root DIR`` (a checkout of the base); without either the statement count
  is "not measured". ``--check`` exits 1 when the tax is over the threshold (a CI gate).

``--json`` prints machine-readable output for any of them (``dataclasses.asdict`` of the rows).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from coverset.report.economy import EconomyView

__all__ = ["economy_group"]

_SORTS = ("leverage", "cost", "statements", "asserts", "unique", "duration", "nodeid")
_FILTERS = ("removable", "grouped", "dominated", "assertion_free", "stale", "unmeasured")


@click.group(name="economy")
def economy_group() -> None:
    """The test economy: what each test costs against what it alone covers (docs/economy.md).

    SOURCE is a pytest --coverset-out directory, a bundle.tar.gz or a neutral bundle (as for
    coverset report); --run RUN_ID reads a run's stored economy from the facts store instead.
    """


def _source_options(command: Any) -> Any:
    """The options every subcommand shares (SOURCE or --run, sources, --json)."""
    decorators = [
        click.argument("source", required=False, type=click.Path(path_type=Path, exists=True)),
        click.option(
            "--repo-root",
            type=click.Path(path_type=Path, file_okay=False, exists=True),
            default=None,
            help="Read test and source files from here (git-tracked files only in a work tree); "
            "default: the current work tree for a --coverset-out directory.",
        ),
        click.option(
            "--run", "run_id", default=None, help="Read this run's stored economy instead of SOURCE."
        ),
        click.option(
            "--repo", "repo_spec", default=None, help="With --run: the repository (id, name or path)."
        ),
        click.option(
            "--trusted", is_flag=True, help="Read an archive or neutral bundle under the local limits."
        ),
        click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text."),
    ]
    for decorator in reversed(decorators):
        command = decorator(command)
    return command


def _check_source(
    source: Path | None, run_id: str | None, repo_root: Path | None, repo_spec: str | None
) -> None:
    if (source is None) == (run_id is None):
        raise click.UsageError("pass either SOURCE or --run RUN_ID")
    if repo_spec is not None and run_id is None:
        raise click.UsageError("--repo goes with --run")
    if repo_root is not None and run_id is not None:
        raise click.UsageError("--repo-root does not go with --run (the stored economy read its mirror)")


def _store_view(run_id: str, repo_spec: str | None, threshold: float | None) -> EconomyView:
    """The stored economy of ``run_id`` (re-flagged with ``threshold``, else the repo's policy)."""
    import asyncio

    from coverset.commands.common import find_repo, load, prepare_db, run_async
    from coverset.db.models import Repo, Run
    from coverset.db.session import Database
    from coverset.facts.store import FactsStore
    from coverset.report.economy import view_from_store

    settings = load()

    async def resolve() -> EconomyView:
        await prepare_db(settings)
        db = Database.from_url(settings.database_url)
        try:
            async with db.session() as session:
                run = await session.get(Run, run_id)
                if run is None:
                    raise LookupError(f"there is no run {run_id!r}")
                repo = (
                    await find_repo(session, repo_spec) if repo_spec else await session.get(Repo, run.repo_id)
                )
                if repo is None or repo.id != run.repo_id:
                    raise LookupError(f"run {run_id} does not belong to that repository")
                limit = threshold
                if limit is None:
                    from coverset.settings_scopes import effective_policy

                    limit = float((await effective_policy(session, repo, settings)).test_tax_threshold)
        finally:
            await db.engine.dispose()
        view = await asyncio.to_thread(
            view_from_store, FactsStore(settings.facts_dir), run.repo_id, run_id, threshold=limit
        )
        if view is None:
            raise LookupError(
                f"run {run_id} has no stored test economy (it predates it or was never finalized)"
            )
        return view

    return run_async(resolve)


def _bundle_view(
    source: Path,
    repo_root: Path | None,
    *,
    trusted: bool,
    compare: Path | None = None,
    base_ref: str | None = None,
    base_root: Path | None = None,
    threshold: float | None = None,
) -> EconomyView:
    """Analyse a local bundle (and, with ``compare``, the tax against a base bundle)."""
    from coverset.analysis.economy import EconomyPolicy
    from coverset.commands.report import limit_message
    from coverset.facts.untrusted import BundleError
    from coverset.report.economy import dir_reader, economy_for_input, git_reader
    from coverset.report.load import load_base, load_bundle

    try:
        inp = load_bundle(source, repo_root=repo_root, trusted=trusted)
        base = load_base(compare, repo_root=repo_root, trusted=trusted) if compare is not None else None
    except BundleError as exc:
        raise click.ClickException(limit_message(exc)) from None
    reader = None
    if base_ref is not None:
        root = Path(str(inp.meta.get("root") or repo_root or Path.cwd()))
        try:
            reader = git_reader(root, base_ref)
        except LookupError as exc:
            raise click.ClickException(str(exc)) from None
    elif base_root is not None:
        reader = dir_reader(base_root)
    policy = EconomyPolicy(test_tax_threshold=threshold) if threshold is not None else None
    view = economy_for_input(inp, base, base_sources=reader, policy=policy)
    if view is None or view.totals is None:
        notes = "; ".join(view.notes) if view is not None else "no tests in the bundle"
        raise click.ClickException(f"no test economy: {notes}")
    return view


def _view(
    source: Path | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
    trusted: bool,
    **extra: Any,
) -> EconomyView:
    _check_source(source, run_id, repo_root, repo_spec)
    if run_id is not None:
        return _store_view(run_id, repo_spec, extra.get("threshold"))
    assert source is not None
    return _bundle_view(source, repo_root, trusted=trusted, **extra)


def _emit(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _row(row: Any) -> dict[str, Any]:
    return dataclasses.asdict(row)


def _ms(value: float | None) -> str:
    from coverset.units import fmt_ms

    return fmt_ms(value)


def _notes(view: EconomyView) -> None:
    for note in view.notes:
        click.echo(f"not measured: {note}", err=True)


def _flags(row: Any) -> str:
    flags = []
    if row.removable:
        flags.append("candidate")
    if row.group_id:
        flags.append("same flow")
    if row.dominated_by:
        flags.append("subset")
    if row.assertion_free:
        flags.append("no assert")
    if row.stale:
        flags.append("stale")
    if not row.measured:
        flags.append("not measured")
    return ", ".join(flags)


@economy_group.command("economy")
@_source_options
@click.option(
    "--sort", type=click.Choice(_SORTS), default="leverage", show_default=True, help="Order of the tests."
)
@click.option("--reverse", is_flag=True, help="Flip the sort's default direction.")
@click.option("--only", type=click.Choice(_FILTERS), default=None, help="Only one kind of test.")
@click.option(
    "--limit", type=click.IntRange(min=0), default=20, show_default=True, help="Tests to list (0: all)."
)
def economy_command(
    source: Path | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
    trusted: bool,
    as_json: bool,
    sort: str,
    reverse: bool,
    only: str | None,
    limit: int,
) -> None:
    """Totals, leverage and the tests that cost the most for what they alone cover."""
    from coverset.report.economy import leverage_bins, median_leverage, sort_tests

    view = _view(source, repo_root, run_id, repo_spec, trusted)
    rows = sort_tests(view.tests, sort, reverse=reverse, only=only)
    shown = rows if limit == 0 else rows[:limit]
    if as_json:
        totals = dataclasses.asdict(view.totals) if view.totals else None
        _emit(
            {
                "run_id": view.run_id,
                "totals": totals,
                "median_leverage": median_leverage(view.tests),
                "notes": list(view.notes),
                "weights": dict(view.weights),
                "sort": sort,
                "tests": [_row(r) for r in shown],
                "tests_total": len(rows),
            }
        )
        return
    t = view.totals
    assert t is not None
    median = median_leverage(view.tests)
    click.echo(f"Test economy · {t.tests} tests · {_ms(t.total_duration_ms)}")
    click.echo(
        f"  minimal set   {t.minimal_set_size} of {t.tests} tests keep every fact "
        f"({_ms(t.minimal_set_duration_ms)} of {_ms(t.total_duration_ms)})"
    )
    click.echo(
        f"  removable     {t.removable_candidates} candidates ({_ms(t.removable_duration_ms)}): "
        "covered by the others, candidates only"
    )
    click.echo(
        f"  redundancy    {t.equivalence_groups} groups of identical flows ({t.tests_in_groups} tests), {t.dominated} dominated"
    )
    click.echo(
        f"  families      {t.collapsible_families} of {t.families} parametrized families repeat a flow "
        f"({t.redundant_cases} redundant cases)"
    )
    click.echo(
        f"  weight        {t.statements} test statements (+{t.helper_statements} in helpers), {t.asserts} assertions, {t.assertion_free} tests with none"
    )
    handlers = "n/m" if t.handlers_total is None else f"{t.handlers_unexercised} of {t.handlers_total}"
    click.echo(
        f"  functions     {t.indirect_only_functions} tested only indirectly, {t.untested_functions} of "
        f"{t.functions} untested, {handlers} exception handlers never ran"
    )
    click.echo(f"  stale         {'n/m' if t.stale_tests is None else t.stale_tests}")
    bins = " · ".join(f"{b.label}: {b.count}" for b in leverage_bins(view.tests))
    lead = "n/m" if median is None else f"{median:.2g}"
    click.echo(f"  leverage      median {lead} · {bins}")
    click.echo("")
    title = f"{len(shown)} of {len(rows)} tests by {sort}{' (reversed)' if reverse else ''}"
    click.echo(title + (f", only {only}" if only else ""))
    click.echo(
        f"{'LEVERAGE':>8} {'STMTS':>6} {'ASSERTS':>7} {'UNIQUE':>6} {'COST/FACT':>9} {'DURATION':>9}  TEST"
    )
    for r in shown:
        lev = "–" if r.leverage is None else (f"{r.leverage:.2g}" if r.leverage < 10 else f"{r.leverage:.0f}")
        stmts = "n/m" if r.statements is None else str(r.statements + (r.helper_statements or 0))
        asserts = "–" if r.asserts is None else str(r.asserts)
        cost = "∞" if r.cost_per_fact_ms is None and r.unique_facts == 0 else _ms(r.cost_per_fact_ms)
        flags = _flags(r)
        click.echo(
            f"{lev:>8} {stmts:>6} {asserts:>7} {r.unique_facts:>6} {cost:>9} {_ms(r.duration_ms):>9}  "
            f"{r.nodeid}{'  [' + flags + ']' if flags else ''}"
        )
    _notes(view)


@economy_group.command("redundant")
@_source_options
def redundant_command(
    source: Path | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
    trusted: bool,
    as_json: bool,
) -> None:
    """Identical flows, dominated tests and removable candidates (candidates, never verdicts)."""
    from coverset.report.economy import redundancy_of

    view = _view(source, repo_root, run_id, repo_spec, trusted)
    red = redundancy_of(view)
    if as_json:
        _emit(
            {
                "run_id": view.run_id,
                "caveat": red.caveat,
                "groups": [dataclasses.asdict(g) for g in red.groups],
                "dominated": [{"test": a, "dominated_by": b} for a, b in red.dominated],
                "removable": [r.nodeid for r in red.removable],
                "removable_duration_ms": red.removable_duration_ms,
                "minimal_set_size": red.minimal_set_size,
                "tests": red.tests,
            }
        )
        return
    click.echo(red.caveat)
    click.echo("")
    click.echo(f"{len(red.groups)} groups of identical flows (same lines, arcs and input shapes)")
    for g in red.groups:
        click.echo(f"  {len(g.nodeids)} tests, {_ms(g.duration_ms)} together:")
        for nodeid in g.nodeids:
            click.echo(f"    {'keep ' if nodeid == g.keep else 'drop?'}  {nodeid}")
    click.echo("")
    click.echo(f"{len(red.dominated)} tests covered entirely by another test (at most 2x slower)")
    for test, by in red.dominated:
        click.echo(f"  {test}\n      <= {by}")
    click.echo("")
    click.echo(
        f"{len(red.removable)} removable candidates ({_ms(red.removable_duration_ms)}); "
        f"the minimal set keeps {red.minimal_set_size} of {red.tests}: coverset tests minimal --write FILE"
    )
    _notes(view)


@economy_group.command("minimal")
@_source_options
@click.option(
    "--write",
    "write_to",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write the drops as a pytest argument file (one --deselect= per line): pytest @FILE.",
)
def minimal_command(
    source: Path | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
    trusted: bool,
    as_json: bool,
    write_to: Path | None,
) -> None:
    """The smallest suite (by a duration-weighted greedy cover) that keeps every fact."""
    from coverset.analysis.economy import REMOVABLE_CAVEAT
    from coverset.report.economy import deselect_args, minimal_split

    view = _view(source, repo_root, run_id, repo_spec, trusted)
    split = minimal_split(view)
    if write_to is not None:
        write_to.write_text(deselect_args(view), encoding="utf-8")
    if as_json:
        _emit(
            {
                "run_id": view.run_id,
                "keep": [r.nodeid for r in split.keep],
                "drop": [r.nodeid for r in split.drop],
                "uncompared": [r.nodeid for r in split.uncompared],
                "keep_ms": split.keep_ms,
                "drop_ms": split.drop_ms,
                "caveat": REMOVABLE_CAVEAT,
                "written": str(write_to) if write_to is not None else None,
            }
        )
        return
    total = len(view.tests)
    click.echo(f"keeps {len(split.keep)} of {total} tests ({_ms(split.keep_ms)})")
    click.echo(f"drops {len(split.drop)} removable candidates ({_ms(split.drop_ms)})")
    if split.uncompared:
        click.echo(f"{len(split.uncompared)} tests have no production fact and are never dropped")
    click.echo(REMOVABLE_CAVEAT)
    for row in split.drop:
        click.echo(f"  drop?  {row.nodeid}  ({_ms(row.duration_ms)})")
    if write_to is not None:
        click.echo(
            f"wrote {len(split.drop)} --deselect arguments to {write_to}: pytest @{write_to}", err=True
        )
    _notes(view)


@economy_group.command("tax")
@_source_options
@click.option(
    "--compare",
    "compare",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="The base run's bundle (any SOURCE kind); required with SOURCE.",
)
@click.option(
    "--base-ref", default=None, help="Read the base's test and source files at this commit of the repository."
)
@click.option(
    "--base-root",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="Read the base's test and source files from this checkout of the base.",
)
@click.option(
    "--threshold",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Statements per newly covered fact (default: the policy, 8).",
)
@click.option("--check", is_flag=True, help="Exit 1 when the tax is over the threshold.")
def tax_command(
    source: Path | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
    trusted: bool,
    as_json: bool,
    compare: Path | None,
    base_ref: str | None,
    base_root: Path | None,
    threshold: float | None,
    check: bool,
) -> None:
    """The PR test tax: test statements added per newly covered line and input class."""
    from coverset.forge.comment import test_tax_line
    from coverset.report.economy import tax_line_of

    if source is not None and compare is None:
        raise click.UsageError("pass --compare BASE (the base run's bundle) with SOURCE")
    if run_id is not None and (compare or base_ref or base_root):
        raise click.UsageError(
            "--compare, --base-ref and --base-root go with SOURCE (a --run has its stored base)"
        )
    if base_ref is not None and base_root is not None:
        raise click.UsageError("pass either --base-ref or --base-root, not both")
    extra: dict[str, Any] = {"threshold": threshold}
    if source is not None:
        extra.update(compare=compare, base_ref=base_ref, base_root=base_root)
    view = _view(source, repo_root, run_id, repo_spec, trusted, **extra)
    line = tax_line_of(view)
    if as_json:
        _emit(
            {
                "run_id": view.run_id,
                "tax": dataclasses.asdict(line) if line is not None else None,
                "line": test_tax_line(line),
                "notes": list(view.notes),
            }
        )
    elif line is None:
        click.echo("no test tax: " + ("; ".join(n for n in view.notes if "tax" in n) or "no base run"))
    else:
        click.echo((test_tax_line(line) or "Test tax: no test statement added").replace("**", ""))
        if line.statements_measured:
            click.echo(f"  {line.tax:.1f} statements per newly covered fact (threshold {line.threshold:g})")
        click.echo(f"  {line.new_tests} new tests")
        if line.lowest_leverage:
            click.echo("  lowest leverage:")
            for t in line.lowest_leverage:
                lev = "–" if t.leverage is None else f"{t.leverage:.2g}"
                click.echo(f"    {lev:>5}  {t.nodeid}")
        if line.duplicates:
            click.echo("  repeat an existing flow (candidates):")
            for t in line.duplicates:
                click.echo(
                    f"    {t.nodeid}\n        {'same flow as' if t.how == 'same flow' else 'covered by'} {t.duplicates}"
                )
    if not as_json:
        _notes(view)
    if check and line is not None and line.flagged:
        sys.exit(1)
