"""Move a local data directory's blobs into the object store (``coverset cache seed --from DIR``).

Going from the laptop shape (SQLite + files) to the hosted one (Postgres + a bucket) moves the
rows with ``coverset db copy`` and the blobs with :func:`seed_from_local`. A plain ``aws s3 sync`` of
the facts directory is not enough: a run is visible in the bucket only through a ``MANIFEST.json``
that lists each file with its *S3* etag, and the local ``MANIFEST.json`` files carry local etags, so
readers would refuse those runs. Seeding instead

1. copies each run directory into this host's cache (``Settings.facts_dir``, keeping mtimes, so
   the summaries stay fresh) and publishes it through the facts store's own path: every file
   uploaded, then the manifest with ``If-None-Match: *`` (a run the bucket already has is skipped);
2. creates each repository's ``dictionary.json`` with ``If-None-Match: *`` (a bucket that already
   has a different dictionary for that repository id is an error: ids cannot be merged);
3. uploads production batches (``prod_windows.parquet`` last), uploaded sources (first upload
   wins, as always) and the shard archives of upload runs that still have their directory.

Mirrors are not copied: the next run (or page) clones them. Running it twice is harmless.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from coverset.storage.base import PreconditionFailed

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = ["SeedReport", "seed_from_local"]

_SKIP = frozenset({".lock", ".sync.json", "MANIFEST.json"})


@dataclass(slots=True)
class SeedReport:
    """What :func:`seed_from_local` did."""

    runs: int = 0
    runs_skipped: int = 0
    dictionaries: int = 0
    batches: int = 0
    sources: int = 0
    uploads: int = 0
    problems: list[str] = field(default_factory=list)

    def line(self) -> str:
        """One line for the CLI."""
        text = (
            f"{self.runs} runs published ({self.runs_skipped} already there), {self.dictionaries} "
            f"dictionaries, {self.batches} production batches, {self.sources} source files, "
            f"{self.uploads} upload archives"
        )
        return text + (f"; {len(self.problems)} problems" if self.problems else "")


def seed_from_local(source: Path, settings: Settings) -> SeedReport:
    """Publish the facts, sources and uploads under the data directory ``source`` to the object
    store ``settings`` name (see the module docstring)."""
    from coverset.facts.store import MANIFEST_FILE, FactsStore
    from coverset.storage import remote_store
    from coverset.storage.uploads import archive_upload

    blobs = remote_store(settings)
    if blobs is None:
        raise ValueError("COVERSET_BLOB_URL is not set: there is no object store to seed")
    source = Path(source).expanduser().resolve()
    facts = source / "facts"
    if facts.resolve() == settings.facts_dir.resolve():
        raise ValueError(f"{source} is this host's cache, not a local data directory")
    report = SeedReport()
    store = FactsStore(settings.facts_dir, blobs=blobs)
    for repo_dir in sorted(facts.glob("repo=*")):
        repo_id = int(repo_dir.name.partition("=")[2]) if repo_dir.name.partition("=")[2].isdigit() else None
        if repo_id is None or not repo_dir.is_dir():
            continue
        dictionary = repo_dir / "dictionary.json"
        if dictionary.is_file():
            key = f"facts/repo={repo_id}/dictionary.json"
            try:
                blobs.put_file(key, dictionary, if_none_match=True)
                report.dictionaries += 1
            except PreconditionFailed:
                if blobs.get_bytes(key)[0] != dictionary.read_bytes():
                    report.problems.append(
                        f"repo {repo_id}: the bucket has another dictionary; its runs are skipped"
                    )
                    continue
        for run_dir in sorted(repo_dir.glob("run=*")):
            run_id = run_dir.name.partition("=")[2]
            if blobs.head(f"facts/repo={repo_id}/run={run_id}/{MANIFEST_FILE}") is not None:
                report.runs_skipped += 1
                continue
            target = store._run_path(repo_id, run_id)
            target.mkdir(parents=True, exist_ok=True)
            for file in sorted(run_dir.iterdir()):
                if file.name not in _SKIP and file.is_file() and not file.is_symlink():
                    shutil.copy2(file, target / file.name)
            with store.run_lock(repo_id, run_id):
                pass  # the release publishes every file, then the manifest
            report.runs += 1
        prod = repo_dir / "prod"
        for batch in sorted(prod.glob("service=*/batch=*")) if prod.is_dir() else []:
            target = settings.facts_dir / batch.relative_to(facts)
            if not (target / "prod_windows.parquet").is_file():
                shutil.copytree(batch, target, dirs_exist_ok=True)
            store.push_tree(target, last="prod_windows.parquet")
            report.batches += 1
    for file in sorted((source / "sources").rglob("*")) if (source / "sources").is_dir() else []:
        if not file.is_file() or file.is_symlink():
            continue
        try:
            blobs.put_file(
                f"sources/{file.relative_to(source / 'sources').as_posix()}", file, if_none_match=True
            )
            report.sources += 1
        except PreconditionFailed:
            pass
        except ValueError as exc:  # a name the key rules refuse
            report.problems.append(f"{file}: {exc}")
    runs_dir = source / "runs"
    for record in sorted(runs_dir.glob("*/upload/shard-*/upload.json")) if runs_dir.is_dir() else []:
        run_id = record.relative_to(runs_dir).parts[0]
        repos = [p.parent.name.partition("=")[2] for p in facts.glob(f"repo=*/run={run_id}")]
        try:
            meta = json.loads(record.read_text())
            if len(repos) != 1:
                raise ValueError("its run has no facts to tell the repository")
            archive_upload(blobs, int(repos[0]), meta["head_sha"], meta["label"], record.parent)
            report.uploads += 1
        except (OSError, ValueError, KeyError) as exc:
            report.problems.append(f"{record.parent}: not archived ({exc})")
    return report
