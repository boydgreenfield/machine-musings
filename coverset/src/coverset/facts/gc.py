"""Retention for the facts store: delete whole run directories, never a repo's dictionary.

The caller (``coverset gc``) decides which runs must survive (the latest baseline per repo, the
latest run per open PR) and passes them as ``keep_run_ids``; everything else older than the cutoff
goes. A run's age is its ``runs_dim.created_at``, or the directory's mtime when the run never got a
``runs_dim`` row (an ingest that crashed early).
"""

from __future__ import annotations

from datetime import UTC, datetime

from coverset.facts.store import FactsStore

__all__ = ["expire_runs"]


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
