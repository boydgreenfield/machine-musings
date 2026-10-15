"""``coverset cache status|clear``: the local cache in front of the blob store.

Facts runs, git mirrors and uploaded sources are cached under ``COVERSET_CACHE_DIR`` and bounded by
``COVERSET_CACHE_MAX_GB`` (:mod:`coverset.storage.cachedir`; ``coverset gc`` evicts beyond the
bound). ``status`` shows what is there; ``clear`` removes every entry not in use, which is always
safe (everything comes back from the blob store, or by one clone per repository for a mirror) and is
the fix for a cache directory that a disk-full or a hand edit left in a doubtful state.
"""

from __future__ import annotations

import json

import click

from coverset.commands.common import load

__all__ = ["cache_group"]


@click.group(name="cache")
def cache_group() -> None:
    """The local cache of the blob store (COVERSET_CACHE_DIR): status, clear."""


@cache_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def cache_status(as_json: bool) -> None:
    """Size of the cache by kind, its bound and the entries in use."""
    from coverset.storage import blob_url_of, cachedir

    settings = load()
    found = cachedir.status(settings)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "directory": str(found.directory),
                    "blob_url": blob_url_of(settings),
                    "max_bytes": found.max_bytes,
                    "total_bytes": found.total_bytes,
                    "by_kind": {k: {"entries": n, "bytes": b} for k, (n, b) in sorted(found.by_kind.items())},
                    "busy": found.busy,
                },
                indent=2,
            )
        )
        return
    click.echo(f"cache {found.directory} for {blob_url_of(settings)}")
    click.echo(
        f"  {found.total_bytes / 1e9:.2f} GB of {found.max_bytes / 1e9:.2f} GB (COVERSET_CACHE_MAX_GB)"
    )
    for kind in ("run", "mirror", "sources"):
        count, size = found.by_kind.get(kind, (0, 0))
        click.echo(f"  {kind:<8} {count:>6} entries  {size / 1e6:>10.1f} MB")
    click.echo(f"  in use   {found.busy:>6} entries (never evicted)")


@cache_group.command("clear")
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def cache_clear(dry_run: bool, yes: bool) -> None:
    """Remove every cached entry not in use (it is fetched again when needed)."""
    from coverset.storage import cachedir

    settings = load()
    if (
        not dry_run
        and not yes
        and not click.confirm(f"Clear the cache under {settings.cache_dir}?", default=True)
    ):
        raise click.Abort()
    report = cachedir.clear(settings, dry_run=dry_run)
    prefix = "would remove" if dry_run else "removed"
    click.echo(
        f"{prefix} {len(report.removed)} entries ({report.freed_bytes / 1e6:.1f} MB); "
        f"{report.skipped_busy} in use kept"
    )
