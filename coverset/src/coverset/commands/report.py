"""``coverset report``: write the static coverage report of one test run.

SOURCE is a ``pytest --coverset-out`` directory, a ``bundle.tar.gz``, or a neutral bundle directory
or archive; ``--run RUN_ID`` reads a run from the facts store instead (sources from the repo's
mirror at the run's head commit). ``--compare BASE`` (any source the report accepts) or
``--compare-run RUN_ID`` adds a Changes page against that base. The logic lives in
:mod:`coverset.report`.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from coverset.report import ReportInput

__all__ = ["report_command"]


@click.command("report")
@click.argument("source", required=False, type=click.Path(path_type=Path, exists=True))
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Output directory (default: SOURCE/report for a directory source, else ./coverset-report). "
    "It must be new, empty, or a previous report's.",
)
@click.option(
    "--open", "open_browser", is_flag=True, help="Open the report in the browser when it is written."
)
@click.option("--title", default=None, help="Title shown on every page.")
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="Read source files from here: only plain relative paths that resolve inside it, and only "
    "files git tracks there when it is a git work tree (when it tracks none of the bundle's paths "
    "but the top of its work tree does, from that top). Without it, a --coverset-out directory reads "
    "the git-tracked files of the current directory's work tree (when that is one), and an archive "
    "or a neutral bundle reads no source. The bundle never chooses. Not with --run.",
)
@click.option(
    "--run", "run_id", default=None, help="Build from this run in the facts store instead of SOURCE."
)
@click.option("--repo", "repo_spec", default=None, help="With --run: the repository (id, full name or path).")
@click.option(
    "--compare",
    "compare_source",
    type=click.Path(path_type=Path),
    default=None,
    help="Compare with this base (any source the report accepts: a --coverset-out directory, a "
    "bundle.tar.gz, a neutral bundle) and add a Changes page. Only its facts are needed: its function "
    "index comes from the rows it ships (a --coverset-out directory's neutral/ copy, a neutral "
    "bundle's own) where it has them; its sources are read under the same rules as SOURCE's and "
    "never shown.",
)
@click.option(
    "--compare-run",
    "compare_run_id",
    default=None,
    help="Compare with this run in the facts store (works with SOURCE or --run).",
)
def report_command(
    source: Path | None,
    out: Path | None,
    open_browser: bool,
    title: str | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
    compare_source: Path | None,
    compare_run_id: str | None,
) -> None:
    """Write a static HTML coverage report (lines, branches, tests per line, input classes) of one run."""
    from coverset.facts.untrusted import BundleError
    from coverset.report import OutDirError, build_report, default_out
    from coverset.report.load import load_bundle, load_run

    if (source is None) == (run_id is None):
        raise click.UsageError("pass either SOURCE or --run RUN_ID")
    if repo_spec is not None and run_id is None:
        raise click.UsageError("--repo goes with --run")
    if repo_root is not None and run_id is not None:
        raise click.UsageError(
            "--repo-root does not go with --run (its sources come from the repository's mirror)"
        )
    if compare_source is not None and compare_run_id is not None:
        raise click.UsageError("pass either --compare BASE or --compare-run RUN_ID, not both")
    settings = None
    try:
        if run_id is not None or compare_run_id is not None:
            from coverset.commands.common import load

            settings = load()
        if run_id is not None:
            from coverset.commands.common import run_async

            inp = run_async(lambda: load_run(settings, run_id, repo_spec, title=title))
        else:
            assert source is not None
            inp = load_bundle(source, repo_root=repo_root, title=title)
        base: ReportInput | None = None
        if compare_source is not None:
            base = _load_base(compare_source, repo_root)
        elif compare_run_id is not None:
            from coverset.commands.common import run_async

            async def base_run() -> ReportInput:
                try:
                    return await load_run(settings, compare_run_id, repo_spec)
                except LookupError as exc:
                    raise LookupError(f"cannot read the base run: {exc}") from None

            base = run_async(base_run)
        result = build_report(inp, out or default_out(source), base=base)
    except (BundleError, OutDirError, LookupError) as exc:
        raise click.ClickException(str(exc)) from None
    total = sum(result.pages.values())
    if inp.notice:
        click.echo(f"sources not read: {inp.notice}", err=True)
    click.echo(
        f"wrote {total} pages ({result.pages['files']} files, {result.pages['functions']} functions, "
        f"{result.pages['tests']} tests) to {result.out_dir}",
        err=True,
    )
    if result.changes is not None:
        c = result.changes
        click.echo(
            f"changes since base: {_plural(c.lines_lost, 'line')} lost, "
            f"{_plural(c.classes_lost, 'class', 'classes')} lost, "
            f"{_plural(c.tests_changed, 'test')} changed (changes.html)",
            err=True,
        )
        for note in c.warnings:
            click.echo(f"warning: {note}", err=True)
    click.echo(str(result.index))
    if open_browser:
        webbrowser.open(result.index.as_uri())


def _load_base(base: Path, repo_root: Path | None) -> ReportInput:
    """The ``--compare`` base as a report input; a missing or unreadable base is a clean error."""
    import sqlite3

    from coverset.facts.untrusted import BundleError
    from coverset.report.load import load_base

    if not base.exists():
        raise click.ClickException(f"the base {base} does not exist")
    try:
        return load_base(base, repo_root=repo_root, title="base")
    except (BundleError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        raise click.ClickException(f"cannot read the base {base}: {exc}") from None


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"
