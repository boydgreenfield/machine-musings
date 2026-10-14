"""``coverset rust``: per-test coverage of a Rust crate, written as a neutral bundle (docs/rust.md).

* ``coverset rust run --crate PATH`` runs the crate's tests one process per test with LLVM
  source-based coverage (through cargo-nextest when it is installed, else libtest directly),
  exports each test's profile and writes ``PATH/.coverset/neutral``; then it prints the
  ``coverset report`` command that renders it.
* ``coverset rust convert CAPTURE`` re-runs only the last stage (capture directory → neutral
  bundle). It needs no Rust toolchain, which is how the committed fixture is rebuilt.

The logic lives in :mod:`coverset.lang.rust_llvm`; nothing here touches the database.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import click

__all__ = ["rust_group"]


@click.group("rust")
def rust_group() -> None:
    """Per-test coverage of Rust crates (cargo-nextest + LLVM source-based coverage)."""


def _report_hint(out: Path, root: Path) -> str:
    return f"coverset report {shlex.quote(str(out))} --trusted --repo-root {shlex.quote(str(root))}"


@rust_group.command("run")
@click.option(
    "--crate",
    "crate",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=Path("."),
    show_default=True,
    help="The crate (or workspace) directory: where Cargo.toml is.",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Where the neutral bundle goes (default: CRATE/.coverset/neutral).",
)
@click.option(
    "--jobs",
    "-j",
    type=click.IntRange(min=1),
    default=None,
    help="Tests run and profiles exported at a time (default: the CPU count).",
)
@click.option(
    "--runner",
    type=click.Choice(["nextest", "libtest"]),
    default=None,
    help="nextest (default when `cargo nextest` is installed) or libtest (coverset runs each test "
    "binary itself, one test per process).",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="Repository root the bundle's paths are relative to (default: the git work tree's top, "
    "else CRATE). Uploads need the repository root.",
)
@click.option(
    "--archive",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Also pack the bundle as a reproducible tar.gz here (what an upload sends).",
)
@click.option(
    "--keep-profiles",
    is_flag=True,
    help="Keep the per-test .profraw/.profdata files in CRATE/.coverset/rust/profiles (about 50 KB "
    "per test; deleted after export by default).",
)
@click.option("--quiet", "-q", is_flag=True, help="Print only the bundle path and the report command.")
def run_command(
    crate: Path,
    out: Path | None,
    jobs: int | None,
    runner: str | None,
    root: Path | None,
    archive: Path | None,
    keep_profiles: bool,
    quiet: bool,
) -> None:
    """Run CRATE's tests with per-test coverage and write a neutral bundle."""
    from coverset.lang.rust_llvm import CaptureError, run_rust

    def log(message: str) -> None:
        if not quiet:
            click.echo(f"coverset rust: {message}", err=True)

    try:
        summary = run_rust(
            crate,
            out=out,
            root=root,
            jobs=jobs,
            runner=runner,  # type: ignore[arg-type]
            archive=archive,
            keep_profiles=keep_profiles,
            log=log,
        )
    except CaptureError as exc:
        raise click.ClickException(str(exc)) from None
    if not quiet:
        counts = ", ".join(f"{n} {label}" for label, n in sorted(summary.outcomes.items()))
        timings = ", ".join(f"{k} {v:.2f} s" for k, v in summary.timings_s.items())
        click.echo(f"coverset rust: {summary.tests} tests ({counts}) with {summary.runner}", err=True)
        click.echo(f"coverset rust: {timings}", err=True)
        click.echo(f"coverset rust: capture in {summary.capture}", err=True)
        if summary.archive is not None:
            click.echo(f"coverset rust: archive {summary.archive}", err=True)
    click.echo(f"wrote {summary.out}")
    click.echo(f"view it with: {_report_hint(summary.out, summary.root)}")


@rust_group.command("convert")
@click.argument("capture", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option(
    "--out",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="Where the neutral bundle goes.",
)
@click.option(
    "--root",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="Repository root holding the sources (default: the capture's own root).",
)
def convert_command(capture: Path, out: Path, root: Path | None) -> None:
    """Convert a capture directory (capture.json + exports/) into a neutral bundle, no cargo needed."""
    from coverset.lang.rust_llvm import CAPTURE_FILE, CaptureError, convert_capture, write_neutral

    try:
        bundle = convert_capture(capture, root=root)
        doc = json.loads((capture / CAPTURE_FILE).read_text(encoding="utf-8"))
        write_neutral(bundle, out, created_at=doc.get("created_at"))
    except CaptureError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"wrote {out} ({len(bundle.collected)} tests, {len(bundle.functions)} functions)")
    resolved_root = root or (capture / str(doc.get("root", "."))).resolve()
    click.echo(f"view it with: {_report_hint(out, resolved_root)}")
