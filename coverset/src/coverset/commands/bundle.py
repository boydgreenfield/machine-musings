"""``coverset bundle``: check, describe and convert neutral bundles (``docs/neutral-bundle.md``).

* ``coverset bundle validate PATH`` prints every problem (file, row, field, message) and exits 1
  when there is any.
* ``coverset bundle info PATH`` prints the manifest, rows per file, languages, and which limits
  or row checks the reader would apply (what it would drop or clip).
* ``coverset bundle convert PATH --to DIR`` writes a pytest-coverset bundle (``--coverset-out``
  directory or ``bundle.tar.gz``) as neutral files, through ``read_python_bundle`` and
  ``coverset_model.io.write_bundle``; with ``--source-root`` it also ships the function regions and
  their declared types, read from the sources.

PATH is a bundle directory or ``tar.gz``. Nothing here touches the database or the facts store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

__all__ = ["bundle_group"]


@click.group("bundle")
def bundle_group() -> None:
    """Check, describe and convert neutral bundles (docs/neutral-bundle.md)."""


@bundle_group.command("validate")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
@click.option("--json", "as_json", is_flag=True, help="Print the problems as JSON lines.")
def validate_command(path: Path, as_json: bool) -> None:
    """Print every problem with the neutral bundle at PATH; exit 1 when there is any."""
    from coverset_model.io import validate

    problems = validate(path)
    for problem in problems:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "file": problem.file,
                        "row": problem.row,
                        "field": problem.field,
                        "message": problem.message,
                        "code": problem.code,
                        "fatal": problem.fatal,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            click.echo(f"{problem}{' (fatal: the bundle is refused)' if problem.fatal else ''}")
    if problems:
        if not as_json:
            fatal = sum(p.fatal for p in problems)
            click.echo(
                f"{len(problems)} problem{'s' if len(problems) != 1 else ''} ({fatal} fatal)", err=True
            )
        raise SystemExit(1)
    if not as_json:
        click.echo(f"{path}: valid")


@bundle_group.command("info")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
@click.option("--json", "as_json", is_flag=True, help="Print the summary as one JSON object.")
def info_command(path: Path, as_json: bool) -> None:
    """Describe the neutral bundle at PATH: manifest, rows per file, languages, limits hit."""
    from coverset_model.io import inspect

    found = inspect(path)
    summary: dict[str, Any] = {
        "path": str(path),
        "manifest": {k: v for k, v in (found.manifest or {}).items() if k != "meta"},
        "meta_keys": sorted((found.manifest or {}).get("meta") or {}),
        "languages": sorted({found.language} - {None}),
        "files": found.files,
        "rows": found.rows,
        "bundle_bytes": found.bundle_bytes,
        "limits_hit": found.dropped,
        "problems": len(found.problems),
        "fatal": next((str(p) for p in found.problems if p.fatal), None),
    }
    if as_json:
        click.echo(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    manifest = summary["manifest"]
    producer = manifest.get("producer") or {}
    click.echo(f"bundle      {path} ({found.bundle_bytes} bytes)")
    click.echo(f"format      {manifest.get('format')} v{manifest.get('version')}")
    click.echo(f"language    {manifest.get('language')}")
    click.echo(f"producer    {producer.get('name')} {producer.get('version')}")
    click.echo(f"created_at  {manifest.get('created_at')}   shard {manifest.get('shard')}")
    if summary["meta_keys"]:
        click.echo(f"meta        {', '.join(summary['meta_keys'])}")
    click.echo("rows")
    for stem, count in found.rows.items():
        click.echo(f"  {found.files.get(stem, stem):<22} {count}")
    if found.dropped:
        click.echo("limits and checks hit (rows or values dropped or clipped)")
        for key, count in found.dropped.items():
            click.echo(f"  {key:<34} {count}")
    else:
        click.echo("limits and checks hit: none")
    if summary["fatal"]:
        click.echo(f"refused: {summary['fatal']}")
    elif found.problems:
        click.echo(f"{len(found.problems)} problems (coverset bundle validate lists them)")


@bundle_group.command("convert")
@click.argument("path", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--to",
    "out",
    required=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory to write the neutral files into (created; existing bundle files are replaced).",
)
@click.option(
    "--source-root",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="A checkout of the run's commit: ship function regions and declared types read from it.",
)
@click.option("--gzip", "compress", is_flag=True, help="Write .jsonl.gz files.")
@click.option("--shard", type=int, default=None, help="The manifest's shard index.")
def convert_command(
    path: Path, out: Path, source_root: Path | None, compress: bool, shard: int | None
) -> None:
    """Write the pytest-coverset bundle at PATH as a neutral bundle in DIR."""
    import dataclasses

    from coverset.facts.untrusted import BundleError
    from coverset.lang import read_any_bundle
    from coverset_model import __version__
    from coverset_model.io import validate, write_bundle

    try:
        bundle = read_any_bundle(path, source_root=source_root)
    except BundleError as exc:
        raise click.ClickException(f"cannot read {path}: {exc}") from exc
    meta = bundle.meta if isinstance(bundle.meta, dict) else {}
    if source_root is not None:
        bundle = dataclasses.replace(bundle, **_regions_and_types(bundle, source_root))
    producer = {"name": "pytest-coverset", "version": str(meta.get("plugin_version") or "unknown")}
    if not meta.get("plugin_version"):
        producer = {"name": "coverset-bundle-convert", "version": __version__}
    write_bundle(
        bundle,
        out,
        manifest={
            "language": "python",
            "producer": producer,
            "created_at": meta.get("finished_at") if isinstance(meta.get("finished_at"), str) else None,
            "shard": shard,
        },
        compress=compress,
    )
    problems = validate(out)
    click.echo(
        f"wrote {out}: {len(bundle.tests)} tests, {len(bundle.static)} files, "
        f"{sum(len(by) for by in bundle.coverage.values())} coverage rows, {len(bundle.functions)} functions, "
        f"{len(bundle.shapes)} shape rows; {len(problems)} problems"
    )
    for problem in problems[:20]:
        click.echo(f"  {problem}", err=True)


def _regions_and_types(bundle: Any, source_root: Path) -> dict[str, Any]:
    """Function regions and declared types of the measured files, read from ``source_root``."""
    from coverset.analysis.functions import index_functions
    from coverset.analysis.shapes import type_index
    from coverset_model.neutral import FrozenMap

    root = source_root.resolve()
    sources: dict[str, str] = {}
    for rel in sorted(bundle.static):
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file() or not rel.endswith(".py"):
            continue
        try:
            sources[rel] = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    regions = tuple(r for path, text in sources.items() for r in index_functions(path, text))
    types = type_index(sources)
    return {
        "functions": regions,
        "types": FrozenMap({triple: FrozenMap(found) for triple, found in types.items()}),
    }
