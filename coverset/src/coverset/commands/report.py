"""``coverset report``: write the static coverage report of one test run.

SOURCE is a ``pytest --coverset-out`` directory, a ``bundle.tar.gz``, or a neutral bundle directory
or archive; ``--run RUN_ID`` reads a run from the facts store instead (sources from the repo's
mirror at the run's head commit). The logic lives in :mod:`coverset.report`.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

import click

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
    help="Read source files from here (default: the bundle's git_root or rootdir, else the current "
    "directory). Nothing outside it is read.",
)
@click.option(
    "--run", "run_id", default=None, help="Build from this run in the facts store instead of SOURCE."
)
@click.option("--repo", "repo_spec", default=None, help="With --run: the repository (id, full name or path).")
def report_command(
    source: Path | None,
    out: Path | None,
    open_browser: bool,
    title: str | None,
    repo_root: Path | None,
    run_id: str | None,
    repo_spec: str | None,
) -> None:
    """Write a static HTML coverage report (lines, branches, tests per line, input classes) of one run."""
    from coverset.facts.untrusted import BundleError
    from coverset.report import OutDirError, build_report, default_out
    from coverset.report.load import load_bundle, load_run

    if (source is None) == (run_id is None):
        raise click.UsageError("pass either SOURCE or --run RUN_ID")
    if repo_spec is not None and run_id is None:
        raise click.UsageError("--repo goes with --run")
    try:
        if run_id is not None:
            from coverset.commands.common import load, run_async

            settings = load()
            inp = run_async(lambda: load_run(settings, run_id, repo_spec, title=title))
        else:
            assert source is not None
            inp = load_bundle(source, repo_root=repo_root, title=title)
        result = build_report(inp, out or default_out(source))
    except (BundleError, OutDirError, LookupError) as exc:
        raise click.ClickException(str(exc)) from None
    total = sum(result.pages.values())
    click.echo(
        f"wrote {total} pages ({result.pages['files']} files, {result.pages['functions']} functions, "
        f"{result.pages['tests']} tests) to {result.out_dir}",
        err=True,
    )
    click.echo(str(result.index))
    if open_browser:
        webbrowser.open(result.index.as_uri())
