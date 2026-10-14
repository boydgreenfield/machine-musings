"""Upload archives in the object store: what lets any replica process an upload run.

``POST /v1/bundles`` unpacks an upload into ``<runs_dir>/<run_id>/upload/shard-<id>/`` on the replica
that received it. With the local default that directory is the archive's home until retention
removes it. With an object store the runs directory is scratch (a restart or another replica does
not have it), so the unpacked shard is also packed back into
``uploads/<repo_id>/<sha>/<label>.tar.gz`` (:func:`archive_upload`), and a processing pass that finds
a shard's directory missing restores it from there (:func:`restore_upload`), extracting under the
same untrusted-bundle limits as the original request and checking the archive's recorded sha256
against the one the shard row was registered with, so an older upload of the same label is never
mistaken for the current one. Retention deletes a commit's archives with its uploaded sources
(:func:`remove_uploads`), and ``coverset repo purge`` all of a repository's (:func:`purge_uploads`).

Keys come from validated values only: the repository id (an integer), a 40-hex commit and a label
that already matched ``orchestrator.upload.LABEL_RE``; :func:`upload_key` re-checks all three.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from coverset.storage.base import BlobNotFound, BlobStore, join_key

if TYPE_CHECKING:
    from coverset.facts.untrusted import BundleLimits

__all__ = ["archive_upload", "purge_uploads", "remove_uploads", "restore_upload", "upload_key"]

_SHA = re.compile(r"^[0-9a-f]{40}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RECORD = "upload.json"
"""The shard's record (label, sha256, bytes, actor, head) written beside its members."""


def upload_key(repo_id: int, sha: str, label: str) -> str:
    """``uploads/<repo_id>/<sha>/<label>.tar.gz`` (ValueError for anything that is not valid)."""
    if not _SHA.fullmatch(sha) or not _LABEL.fullmatch(label):
        raise ValueError(f"invalid upload key parts {sha!r}, {label!r}")
    return join_key("uploads", str(int(repo_id)), sha, f"{label}.tar.gz")


def archive_upload(blobs: BlobStore, repo_id: int, sha: str, label: str, directory: Path) -> str:
    """Pack ``directory``'s regular files (no symlinks) and store them; returns the etag."""
    key = upload_key(repo_id, sha, label)
    fd, name = tempfile.mkstemp(prefix="coverset-upload-", suffix=".tar.gz")
    os.close(fd)
    archive = Path(name)
    try:
        with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
            for file in sorted(directory.rglob("*")):
                if file.is_file() and not file.is_symlink():
                    tar.add(file, arcname=file.relative_to(directory).as_posix(), recursive=False)
        return blobs.put_file(key, archive)
    finally:
        archive.unlink(missing_ok=True)


def restore_upload(
    blobs: BlobStore,
    repo_id: int,
    sha: str,
    label: str,
    target: Path,
    limits: BundleLimits,
    *,
    sha256_prefix: str | None = None,
) -> bool:
    """Extract the stored archive into ``target`` (which must not exist yet); False when there is
    none, or when its record names another upload than ``sha256_prefix`` (the shard row's)."""
    from coverset.facts.untrusted import BundleError, extract_bundle

    key = upload_key(repo_id, sha, label)
    work = Path(tempfile.mkdtemp(prefix="coverset-restore-"))
    try:
        try:
            blobs.get_file(key, work / "upload.tar.gz")
        except BlobNotFound:
            return False
        staging = work / "shard"
        try:
            extract_bundle(work / "upload.tar.gz", staging, limits)
        except BundleError:
            return False
        if sha256_prefix is not None:
            try:
                recorded = json.loads((staging / RECORD).read_text()).get("sha256", "")
            except (OSError, ValueError, AttributeError):
                return False
            if not str(recorded).startswith(sha256_prefix):
                return False
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            return target.is_dir()
        shutil.move(str(staging), str(target))
        os.chmod(target, 0o700)
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def remove_uploads(
    blobs: BlobStore, repo_id: int, shas: Iterable[str], *, dry_run: bool = False
) -> list[str]:
    """Delete every archive of each commit in ``shas``; returns the commits that had any, sorted."""
    removed = []
    for sha in sorted(set(shas)):
        if not _SHA.fullmatch(sha):
            continue
        prefix = f"uploads/{int(repo_id)}/{sha}/"
        found = (
            next(iter(blobs.list(prefix)), None) is not None if dry_run else blobs.delete_prefix(prefix) > 0
        )
        if found:
            removed.append(sha)
    return removed


def purge_uploads(blobs: BlobStore, repo_id: int) -> int:
    """Delete every upload archive of the repository; returns how many there were."""
    return blobs.delete_prefix(f"uploads/{int(repo_id)}/")
