"""Retention for the facts store: delete whole run directories, never a repo's dictionary.

The caller (``coverset gc``) decides which runs must survive (the latest baseline per repo, the
latest run per open PR) and passes them as ``keep_run_ids``; everything else older than the cutoff
goes. A run's age is its ``runs_dim.created_at``, or the directory's mtime when the run never got a
``runs_dim`` row (an ingest that crashed early).

Upload mode keeps two more things on disk, removed by the same retention passes
(``orchestrator/history.py``): the uploaded sources of a commit, ``<data_dir>/sources/<repo_id>/
<sha>/`` (:func:`remove_sources`, once no remaining run of the repository has that head commit),
and an upload run's shard archives, ``<runs_dir>/<run_id>/upload/`` (:func:`remove_upload_dirs`,
when the run expires). ``coverset repo purge`` removes the repository's whole sources directory.

Over an object store the same passes delete objects as well as files: :func:`expire_runs` goes
through :meth:`FactsStore.delete_run <coverset.facts.store.FactsStore.delete_run>` (manifest first,
then the run's prefix, then the cached copy), the sources through
:meth:`SourceStore.remove <coverset.facts.sources.SourceStore.remove>` and the upload archives
through :func:`coverset.storage.uploads.remove_uploads`; :func:`purge_repo_files` is ``repo purge``'s
whole file side. A run's age is read from its manifest (one small GET per candidate run: the
manifest carries ``runs_dim.created_at``), so a gc pass never downloads a run's files; a manifest
without it (a run that never got a ``runs_dim`` row) falls back to the manifest's own modification
time, and a run this host is still writing (no manifest yet) to the local files, as before.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from coverset.facts.store import FactsStore

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = ["expire_runs", "purge_repo_files", "remove_sources", "remove_upload_dirs"]

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _created_at(store: FactsStore, repo_id: int, run_id: str) -> datetime:
    if store.remote:
        manifest = store.remote_manifest(repo_id, run_id)
        if manifest is not None:
            created = _parse(manifest.get("created_at"))
            if created is not None:
                return created
            if "runs_dim.parquet" not in (manifest.get("files") or {}):
                modified = store.run_modified(repo_id, run_id)
                return modified if modified is not None else datetime.fromtimestamp(0, tz=UTC)
            # A manifest from before created_at was recorded: read the row (this pulls the run).
    runs_dim = store.read_table(repo_id, run_id, "runs_dim")
    if runs_dim.num_rows:
        created = runs_dim.column("created_at")[0].as_py()
        if created is not None:
            return created if created.tzinfo else created.replace(tzinfo=UTC)
    modified = store.run_modified(repo_id, run_id)
    return modified if modified is not None else datetime.fromtimestamp(0, tz=UTC)


def expire_runs(
    store: FactsStore,
    *,
    keep_run_ids: set[str],
    older_than: datetime,
    repo_id: int | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Delete every run created before ``older_than`` whose id is not in ``keep_run_ids``.

    Scans every repo, or only ``repo_id``. With ``dry_run`` nothing is deleted. Returns the run ids
    that were (or would be) deleted, sorted. A naive ``older_than`` is taken as UTC.
    """
    cutoff = older_than if older_than.tzinfo else older_than.replace(tzinfo=UTC)
    repos = store.list_repos() if repo_id is None else [repo_id]
    expired: list[str] = []
    for repo in repos:
        for run_id in store.list_runs(repo):
            if run_id in keep_run_ids or _created_at(store, repo, run_id) >= cutoff:
                continue
            if not dry_run:
                store.delete_run(repo, run_id)
            expired.append(run_id)
    return sorted(expired)


def remove_sources(data_dir: Path, repo_id: int, shas: Iterable[str], *, dry_run: bool = False) -> list[str]:
    """Delete the uploaded sources of each commit in ``shas`` (``<data_dir>/sources/<repo_id>/<sha>``).

    The caller passes only commits no remaining run of the repository has as its head. Names that
    are not a 40-hex SHA are ignored, and a symlink is never followed. Returns the commits whose
    directory existed (and, unless ``dry_run``, is gone), sorted.
    """
    root = data_dir / "sources" / str(int(repo_id))
    removed: list[str] = []
    for sha in sorted(set(shas)):
        target = root / sha
        if not _SHA.fullmatch(sha) or target.is_symlink() or not target.is_dir():
            continue
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
        removed.append(sha)
    if not dry_run and root.is_dir() and not root.is_symlink() and not any(root.iterdir()):
        root.rmdir()
    return removed


def remove_upload_dirs(runs_dir: Path, run_ids: Iterable[str], *, dry_run: bool = False) -> list[str]:
    """Delete the upload shard archives of each run (``<runs_dir>/<run_id>/upload``), and the run's
    scratch directory when that leaves it empty. Returns the run ids that had one, sorted."""
    removed: list[str] = []
    for run_id in sorted(set(run_ids)):
        if not _RUN_ID.fullmatch(run_id):
            continue
        run_dir = runs_dir / run_id
        upload = run_dir / "upload"
        if run_dir.is_symlink() or upload.is_symlink() or not upload.is_dir():
            continue
        if not dry_run:
            shutil.rmtree(upload, ignore_errors=True)
            if not any(run_dir.iterdir()):
                run_dir.rmdir()
        removed.append(run_id)
    return removed


def purge_repo_files(settings: Settings, repo_id: int, run_ids: Iterable[str]) -> None:
    """Everything ``coverset repo purge`` deletes besides rows: the repository's facts (objects and
    cached copies), its mirror, its uploaded sources, its upload archives and its runs' upload
    directories under the runs directory."""
    from coverset.facts.sources import source_store
    from coverset.facts.store import facts_store
    from coverset.orchestrator.repos import mirror_path
    from coverset.storage import remote_store
    from coverset.storage.uploads import purge_uploads

    store = facts_store(settings)
    if store.blobs is not None:
        store.blobs.delete_prefix(f"facts/repo={int(repo_id)}/")
    shutil.rmtree(store.repo_dir(repo_id), ignore_errors=True)
    shutil.rmtree(mirror_path(settings, repo_id), ignore_errors=True)
    source_store(settings).purge(repo_id)
    blobs = remote_store(settings)
    if blobs is not None:
        purge_uploads(blobs, repo_id)
    remove_upload_dirs(settings.runs_dir, run_ids)
