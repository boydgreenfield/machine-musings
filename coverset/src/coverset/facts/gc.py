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
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from coverset.facts.store import FactsStore

__all__ = ["expire_runs", "remove_sources", "remove_upload_dirs"]

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _created_at(store: FactsStore, repo_id: int, run_id: str) -> datetime:
    runs_dim = store.read_table(repo_id, run_id, "runs_dim")
    if runs_dim.num_rows:
        created = runs_dim.column("created_at")[0].as_py()
        if created is not None:
            return created if created.tzinfo else created.replace(tzinfo=UTC)
    mtime = store.run_dir(repo_id, run_id).stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=UTC)


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
