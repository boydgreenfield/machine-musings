"""Upload mode on the control plane: bundles from the repository's own CI become a run and a verdict.

In upload mode coverset runs no tests. The repository's CI runs ``pytest --coverset`` (or Vitest
with ``coverset-vitest``) and ``coverset-ci upload`` posts the result to ``POST /v1/bundles``
(``web/uploads.py``, which authenticates the caller and streams the body to disk). This module does
the rest, in two halves:

**Registration** (:func:`register_upload`, in the request): the archive is unpacked under the
untrusted-bundle limits (:func:`unpack_upload`), then one short transaction finds or creates the
run for ``(repository, commit)`` (``trigger = "upload"``) and its shard for the upload's label.
Every label is one shard: shard ids are handed out in arrival order, and the label and the
archive's sha256 are kept in ``shards.worker_ref`` (``upload:<sha256 prefix>:<label>``). That makes
uploads idempotent on ``(repository, commit, label, sha256)``: a replay of the same archive is
answered with the existing run and changes nothing; a different archive under the same label
replaces that shard (a re-run of the CI job), and a new label adds a shard. The archive's content
is unpacked under ``<runs_dir>/<run_id>/upload/shard-<id>/`` for the processing pass and packed into
the blob store (:mod:`coverset.storage.uploads`), from which any replica's pass restores a shard
directory it lacks (both are removed with the run by retention). An upload run is never executed, so the dispatcher's boot takeover leaves it
alone (``orchestrator/queue.py``): a run waiting for its matrix legs when the server restarts stays
``running``, and the late legs join it (:func:`_reusable_run`). At start-up ``serve`` schedules one
pass for every upload run still ``running`` (:meth:`UploadWorker.resume`), so a pass that the restart
cut off still concludes its run.

**Processing** (:class:`UploadWorker`, in the background, one pass per run at a time; uploads that
land during a pass trigger exactly one more): the pass is always computed from *every* shard of the
run, never incrementally, so it is correct whatever order shards arrive in and however often they
are replayed:

1. the head: refresh the repository's mirror with forge credentials when it can (the same
   :func:`~coverset.orchestrator.repos.ensure_mirror_async` as managed runs; a failure falls back
   to an existing mirror) and export the head commit; else use the uploaded sources
   (:mod:`coverset.facts.sources`, written here from each shard's ``sources/``); else nothing;
2. the diff ``merge-base(base, head)...head`` on the mirror when both commits are there, classified
   into changed functions by the adapter of the bundles' language (:func:`_diff_language`): Python
   exactly as the planner does (its own helpers), Rust by
   :meth:`~coverset.lang.rust.RustAdapter.changed_functions` over both sides' ``.rs`` sources;
3. ingest every shard (:func:`~coverset.orchestrator.bundles.ingest_one` on the ingest pool;
   re-ingesting an unchanged shard is a no-op in the facts store). With a head checkout the pytest
   bundle is ingested with coverset's own function index of the head sources, as in managed mode;
   without one the neutral bundle is ingested with the regions and declared types it ships
   (``IngestJob.regions_from_bundle``);
4. :func:`~coverset.facts.ingest.finalize_run` with one expected shard per label (or the uploader's
   ``--shards`` count) and the same test-economy inputs as a managed run
   (:func:`~coverset.orchestrator.runner.economy_inputs`: the scoped test-tax threshold and the
   ``test_stats`` p50s), then results, test history, and the report and summary through the same
   :func:`~coverset.orchestrator.verdict.build_report` / ``build_summary`` / ``persist_verdict`` /
   ``post_summary`` as a managed run, fed by a :class:`~coverset.orchestrator.planner.RunPlan` with
   no workers. A run that names ``--shards N`` gets its verdict, events and forge post only once N
   labels are in; otherwise every pass concludes, and a later shard re-opens the run and concludes it
   again (the sticky comment and check run are updated in place, never duplicated).

What upload mode cannot do: order tests (they already ran), retry failures (a failure is final,
there is no flaky detection), warm anything, or report results before CI finishes. Without a
mirror or a base there is no patch coverage and no changed-function table: the reason says
:data:`NO_DIFF_REASON`, and the comment still carries the run's totals, its invariants (when the
invariants file came with the sources) and the largest input-class gaps.

The results are self-reported by the repository's CI, which runs the pull request's code: an upload
proves *which repository* sent it (token or OIDC), not that its tests are honest. Runs from
``pull_request_target`` OIDC tokens are recorded untrusted (that event runs base-branch workflows
with secrets, and a workflow that checks out the PR head runs its code with them).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import gzip
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.analysis.cover import CoverSet
from coverset.analysis.diff import GitDiffError
from coverset.config import Settings
from coverset.db.leader import transaction_lock
from coverset.db.models import InvariantResult as InvariantResultModel
from coverset.db.models import PullRequest, Repo, Run, RunEvent, Shard, TestAttempt, TestResult
from coverset.executors.base import EnvRef, Resources
from coverset.facts import sources as source_store
from coverset.facts.ingest import RunContext, finalize_run
from coverset.facts.queries import top_gaps
from coverset.facts.untrusted import BundleError, BundleLimits, extract_bundle
from coverset.lang.base import Catalogue, CollectedItem, FunctionChange, LanguageAdapter
from coverset.lang.python import CoversetConfig, PythonAdapter
from coverset.logging import redact
from coverset.orchestrator import history
from coverset.orchestrator.bundles import IngestJob, IngestOutcome, bundle_limits
from coverset.orchestrator.planner import (
    RunPlan,
    _changes,
    _diff,
    _index_head,
    _load_config,
    run_transaction,
)
from coverset.orchestrator.repos import (
    RepoError,
    archive,
    ensure_mirror_async,
    extract_archive,
    merge_base,
    mirror_path,
    read_blobs,
    resolve_commit,
    tree_paths,
)
from coverset.orchestrator.runner import (
    OrchestratorContext,
    economy_inputs,
    finalize_crashed,
    schedule_warm_up,
)
from coverset.orchestrator.verdict import (
    RunReport,
    TestState,
    build_report,
    build_summary,
    functions_of_interest,
    outcome_tally,
    persist_verdict,
    post_summary,
)
from coverset.storage import blob_url_of, from_settings
from coverset.storage.uploads import archive_upload, restore_upload
from coverset_model.catalogue import Catalogue as TypeCatalogue

__all__ = [
    "LABEL_RE",
    "NO_DIFF_REASON",
    "Registration",
    "UnpackedUpload",
    "UploadError",
    "UploadMeta",
    "UploadWorker",
    "parse_worker_ref",
    "process_run",
    "register_upload",
    "unpack_upload",
    "upload_limits",
]

log = logging.getLogger(__name__)

NO_DIFF_REASON = "no diff available: install the GitHub App or set a base"
"""The reason suffix of an upload run whose patch coverage could not be computed."""
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKER_REF_PREFIX = "upload:"
SHA256_PREFIX_CHARS = 32
"""How much of the archive's sha256 ``shards.worker_ref`` keeps (128 bits: collisions are not a
concern, and the column is 128 characters)."""
MAX_UPLOAD_MEMBERS = 8192
"""Members an upload archive may hold: the bundles plus up to a few thousand source files."""
KNOWN_MEMBERS = frozenset({"bundle.tar.gz", "neutral.tar.gz", "meta.json", "neutral", "sources"})
MAX_META_BYTES = 1024 * 1024
GAP_FUNCTIONS = 3
"""Functions named in a no-diff run's reason (the repository page lists the rest)."""
FAILED = frozenset({"failed", "error"})
UPLOAD_EXECUTOR = "local"
"""``runs.executor`` of an upload run (the column is required; nothing is executed or billed)."""
TERMINAL = frozenset({"completed", "error", "cancelled", "abandoned", "skipped"})
REUSABLE = frozenset({"running", "completed"})
"""Statuses of an earlier upload run of the same commit that a new upload joins. A ``running`` one
is waiting for more shards (or mid-pass), and stays so across a control-plane restart: the
dispatcher's boot takeover skips upload runs. An errored, cancelled or (from an older version's
takeover) abandoned one is left as it is, and a fresh run starts."""
DIFF_SUFFIXES: dict[str, tuple[str, ...]] = {"rust": (".rs",)}
"""Source suffixes whose diff a non-Python adapter classifies (:func:`_language_changes`); other
neutral languages ship no source indexer, so their uploads get no changed-function table."""


class UploadError(Exception):
    """A refused upload: answered as ``{"error": slug, "detail": detail}`` with ``status``."""

    def __init__(self, status: int, slug: str, detail: str = "") -> None:
        super().__init__(detail or slug)
        self.status = status
        self.slug = slug
        self.detail = detail


@dataclass(frozen=True, slots=True)
class UploadMeta:
    """Who uploaded what for which commit (from the authenticated request's headers)."""

    repo_id: int
    head_sha: str
    base_sha: str | None
    branch: str | None
    pr_number: int | None
    label: str
    archive_sha256: str
    archive_bytes: int
    expected_shards: int | None = None
    pr_title: str | None = None
    trusted: bool = True
    actor: str = ""
    """Short description for the run's log (``repo token abcd1234``, ``oidc owner/name``)."""


@dataclass(frozen=True, slots=True)
class UnpackedUpload:
    """An upload archive extracted and checked; ``root`` holds its members."""

    root: Path
    has_python: bool
    has_neutral: bool
    has_sources: bool
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Registration:
    """What :func:`register_upload` decided: the run, the shard, and whether anything changed."""

    run_id: str
    shard_id: int
    state: str
    duplicate: bool = False
    created: bool = False


def upload_limits(settings: Settings) -> BundleLimits:
    """The strict bundle limits, with the upload byte cap and room for source files."""
    return dataclasses.replace(
        bundle_limits(settings),
        max_bundle_bytes=settings.max_upload_bytes,
        max_members=MAX_UPLOAD_MEMBERS,
    )


def worker_ref(label: str, sha256: str) -> str:
    """``shards.worker_ref`` of an uploaded shard."""
    return f"{WORKER_REF_PREFIX}{sha256[:SHA256_PREFIX_CHARS]}:{label}"


def parse_worker_ref(value: str | None) -> tuple[str, str] | None:
    """``(sha256 prefix, label)`` of an uploaded shard's ``worker_ref``, else None."""
    if not value or not value.startswith(WORKER_REF_PREFIX):
        return None
    digest, sep, label = value[len(WORKER_REF_PREFIX) :].partition(":")
    return (digest, label) if sep and label else None


# -- unpacking ------------------------------------------------------------------------------------


def _pack_dir(directory: Path, target: Path) -> None:
    """``directory``'s regular files as a ``tar.gz`` at ``target`` (members at the archive root)."""
    with tarfile.open(target, "w:gz") as tar:
        for file in sorted(directory.rglob("*")):
            if file.is_file() and not file.is_symlink():
                tar.add(file, arcname=file.relative_to(directory).as_posix(), recursive=False)


def unpack_upload(path: Path, dest: Path, settings: Settings) -> UnpackedUpload:
    """Extract the upload archive at ``path`` into the empty ``dest`` and check what it holds.

    The outer archive gets the strict limits with the upload byte cap (:func:`upload_limits`); each
    inner bundle must fit ``COVERSET_MAX_BUNDLE_BYTES`` like a managed run's. Unknown top-level
    members are removed (a newer client may send more). A ``neutral/`` directory is packed into
    ``neutral.tar.gz`` so every shard is ingested from a tarball. Raises :class:`UploadError` (400).
    """
    try:
        extract_bundle(path, dest, upload_limits(settings))
    except BundleError as exc:
        raise UploadError(400, "bad_archive", str(exc)[:300]) from None
    for entry in list(dest.iterdir()):
        if entry.name not in KNOWN_MEMBERS:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for name in ("bundle.tar.gz", "neutral.tar.gz", "meta.json"):
        if (dest / name).is_dir():
            raise UploadError(400, "bad_archive", f"{name} must be a file")
    for name in ("neutral", "sources"):
        if (dest / name).exists() and not (dest / name).is_dir():
            raise UploadError(400, "bad_archive", f"{name} must be a directory")
    for name in ("bundle.tar.gz", "neutral.tar.gz"):
        target = dest / name
        if target.is_file() and target.stat().st_size > settings.max_bundle_bytes:
            raise UploadError(413, "bundle_too_large", f"{name} exceeds COVERSET_MAX_BUNDLE_BYTES")
    if (dest / "neutral").is_dir() and not (dest / "neutral.tar.gz").exists():
        _pack_dir(dest / "neutral", dest / "neutral.tar.gz")
        shutil.rmtree(dest / "neutral")
    has_python = (dest / "bundle.tar.gz").is_file()
    has_neutral = (dest / "neutral.tar.gz").is_file()
    if not has_python and not has_neutral:
        raise UploadError(400, "empty_upload", "the archive holds neither bundle.tar.gz nor a neutral bundle")
    meta: dict[str, Any] = {}
    meta_path = dest / "meta.json"
    if meta_path.is_file() and meta_path.stat().st_size <= MAX_META_BYTES:
        with contextlib.suppress(ValueError, OSError):
            loaded = json.loads(meta_path.read_bytes())
            meta = loaded if isinstance(loaded, dict) else {}
    return UnpackedUpload(
        root=dest,
        has_python=has_python,
        has_neutral=has_neutral,
        has_sources=(dest / "sources").is_dir(),
        meta=meta,
    )


def shard_dir(settings: Settings, run_id: str, shard_id: int) -> Path:
    """Where an uploaded shard's members are kept: ``<runs_dir>/<run_id>/upload/shard-<id>``."""
    return settings.runs_dir / run_id / "upload" / f"shard-{shard_id}"


# -- registration ---------------------------------------------------------------------------------


async def _pull_request(session: AsyncSession, meta: UploadMeta, now: datetime) -> PullRequest | None:
    """The PR row for ``meta.pr_number`` (created when no webhook recorded it), moved to this head.

    The newest upload of a PR decides its head: without webhooks nothing else tells coverset that
    the branch moved, and with webhooks the next ``synchronize`` sets it again anyway.
    """
    if meta.pr_number is None:
        return None
    pr = await session.scalar(
        select(PullRequest).where(PullRequest.repo_id == meta.repo_id, PullRequest.number == meta.pr_number)
    )
    if pr is None:
        pr = PullRequest(
            repo_id=meta.repo_id,
            number=meta.pr_number,
            head_sha=meta.head_sha,
            base_sha=meta.base_sha or meta.head_sha,
            head_ref=meta.branch,
            title=(meta.pr_title or "")[:255],
            author_association="NONE",
            created_at=now,
            updated_at=now,
        )
        session.add(pr)
        await session.flush()
        return pr
    pr.head_sha = meta.head_sha
    if meta.base_sha:
        pr.base_sha = meta.base_sha
    if meta.branch and not pr.head_ref:
        pr.head_ref = meta.branch
    if meta.pr_title and not pr.title:
        pr.title = meta.pr_title[:255]
    pr.updated_at = now
    return pr


async def _reusable_run(session: AsyncSession, meta: UploadMeta) -> Run | None:
    """The upload run of ``(repository, commit)`` a new upload joins, or None for a fresh one.

    The newest upload run of the commit decides: ``running`` or ``completed`` is joined (a later
    shard re-opens a completed run by setting it ``running``), anything else starts a fresh run.
    The dispatcher never touches upload runs (``RunDispatcher.on_boot`` skips them), so a run
    waiting for its matrix legs is still ``running`` after a restart.
    """
    run = await session.scalar(
        select(Run)
        .where(Run.repo_id == meta.repo_id, Run.head_sha == meta.head_sha, Run.trigger == "upload")
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )
    return run if run is not None and run.status in REUSABLE else None


async def register_upload(
    db: Any, settings: Settings, meta: UploadMeta, unpacked: UnpackedUpload, *, claimant: str | None = None
) -> Registration:
    """Find or create the run and the shard for ``meta`` and move ``unpacked`` into place.

    Callers serialise registrations of one ``(repository, commit)`` (the web view holds a lock), so
    two shards arriving together never create two runs. Returns a duplicate registration, and
    leaves ``unpacked`` where it is, when the same archive was already registered for the label.
    """
    now = settings.now()
    digest = meta.archive_sha256[:SHA256_PREFIX_CHARS]

    async def work(session: AsyncSession) -> tuple[Registration, Path | None]:
        # The caller's lock is per process; replicas serialise here too.
        await transaction_lock(session, f"upload:{meta.repo_id}:{meta.head_sha}")
        run = await _reusable_run(session, meta)
        created = run is None
        if created:
            pr = await _pull_request(session, meta, now)
            run = Run(
                repo_id=meta.repo_id,
                head_sha=meta.head_sha,
                base_sha=meta.base_sha
                or (pr.base_sha if pr is not None and pr.base_sha != meta.head_sha else None),
                branch=meta.branch or (pr.head_ref if pr is not None else None),
                pr_id=pr.id if pr is not None else None,
                trigger="upload",
                status="running",
                claimed_by=claimant,
                executor=UPLOAD_EXECUTOR,
                trusted=meta.trusted,
                timeline={"queued": now.isoformat(), "uploaded": now.isoformat()},
                created_at=now,
            )
            session.add(run)
            await session.flush()
        assert run is not None
        shards = list(await session.scalars(select(Shard).where(Shard.run_id == run.id)))
        by_label = {}
        for row in shards:
            parsed = parse_worker_ref(row.worker_ref)
            if parsed is not None:
                by_label[parsed[1]] = (row, parsed[0])
        found = by_label.get(meta.label)
        if (
            found is not None
            and found[1] == digest
            and found[0].status in ("completed", "running", "pending")
            and shard_dir(settings, run.id, found[0].shard_id).is_dir()  # else: its move failed
        ):
            return Registration(run.id, found[0].shard_id, _state(run), duplicate=True), None
        if found is not None:
            shard = found[0]
            shard.worker_ref = worker_ref(meta.label, meta.archive_sha256)
            shard.status = "pending"
            shard.error = None
            shard.started_at, shard.finished_at = now, None
        else:
            next_id = max((row.shard_id for row in shards), default=-1) + 1
            shard = Shard(
                run_id=run.id,
                shard_id=next_id,
                status="pending",
                worker_ref=worker_ref(meta.label, meta.archive_sha256),
                started_at=now,
            )
            session.add(shard)
        if not created:
            if meta.base_sha and run.base_sha is None:
                run.base_sha = meta.base_sha
            if meta.pr_number is not None and run.pr_id is None:
                pr = await _pull_request(session, meta, now)
                run.pr_id = pr.id if pr is not None else None
            run.status = "running"
            run.claimed_by = claimant
            run.timeline = {**(run.timeline or {}), "uploaded": now.isoformat()}
        if meta.expected_shards is not None:
            run.shard_count = max(meta.expected_shards, len(by_label) + (found is None))
        else:
            run.shard_count = max(run.shard_count or 0, len(by_label) + (found is None))
        target = shard_dir(settings, run.id, shard.shard_id)
        text = f"upload {meta.label} received ({meta.archive_bytes} bytes, {meta.actor or 'unknown'})"
        await _event(session, run.id, "log", {"shard": shard.shard_id, "text": text}, now)
        return Registration(run.id, shard.shard_id, "running", created=created), target

    registration, target = await run_transaction(db, work)
    if target is not None:
        await asyncio.to_thread(_move_into_place, unpacked.root, target, meta)
        # Any replica's pass (or this one's after a restart) restores the shard from there.
        await asyncio.to_thread(
            archive_upload, from_settings(settings), meta.repo_id, meta.head_sha, meta.label, target
        )
    return registration


def _state(run: Run) -> str:
    return "completed" if run.status in TERMINAL else "running"


def _move_into_place(source: Path, target: Path, meta: UploadMeta) -> None:
    """Replace ``target`` (a shard's directory) with the unpacked upload, 0700, plus its record."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(source), str(target))
    os.chmod(target, 0o700)
    record = {
        "label": meta.label,
        "sha256": meta.archive_sha256,
        "bytes": meta.archive_bytes,
        "actor": meta.actor,
        "head_sha": meta.head_sha,
    }
    (target / "upload.json").write_text(json.dumps(record, sort_keys=True))


async def _event(
    session: AsyncSession, run_id: str, kind: str, payload: Mapping[str, Any], at: datetime
) -> RunEvent:
    """Append one event to the run (the next sequence number; callers hold the run's writer)."""
    last = await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    row = RunEvent(run_id=run_id, seq=int(last or 0) + 1, kind=kind, payload=dict(payload), at=at)
    session.add(row)
    await session.flush()
    return row


# -- processing -----------------------------------------------------------------------------------


class UploadWorker:
    """Processes runs in the background: at most one pass per run at a time, coalescing uploads.

    :meth:`schedule` starts a pass, or, when one is already running for the run, asks for exactly
    one more after it (the in-flight pass may have read the shard list before the new shard
    landed). Everything happens on one event loop, so the bookkeeping needs no lock.
    """

    def __init__(self, ctx: OrchestratorContext) -> None:
        self.ctx = ctx
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._again: set[str] = set()

    def schedule(self, run_id: str) -> None:
        """Process ``run_id`` soon (see the class docstring)."""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            self._again.add(run_id)
            return
        self._tasks[run_id] = self.ctx.spawn(self._loop(run_id), name=f"upload {run_id}")

    async def _loop(self, run_id: str) -> None:
        try:
            while True:
                self._again.discard(run_id)
                await process_run(self.ctx, run_id)
                if run_id not in self._again:
                    return
        finally:
            self._tasks.pop(run_id, None)

    async def resume(self) -> list[str]:
        """Schedule a pass for every upload run left ``running``; returns their ids, oldest first.

        ``coverset serve`` calls this at start-up, after the dispatcher's boot takeover (which skips
        upload runs: :func:`~coverset.orchestrator.queue.install_run_queue`). A pass that a restart
        or crash cut off left its run ``running`` with every shard on disk, and no upload may ever
        come again to schedule it; this pass concludes it. A run still waiting for matrix legs
        (``--shards N``) gets a pass that re-ingests what it has (a no-op in the facts store) and
        writes nothing final, exactly as an upload's pass would.
        """
        async with self.ctx.db.session() as session:
            run_ids = list(
                await session.scalars(
                    select(Run.id)
                    .where(Run.trigger == "upload", Run.status == "running")
                    .order_by(Run.created_at, Run.id)
                )
            )
        for run_id in run_ids:
            self.schedule(run_id)
        if run_ids:
            log.info("resuming %d upload run(s) left running: %s", len(run_ids), ", ".join(run_ids))
        return run_ids

    async def idle(self) -> None:
        """Wait until no pass is running or scheduled (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


@dataclass(slots=True)
class _Head:
    """The head commit as far as coverset can see it."""

    checkout: Path
    head_paths: frozenset[str] | None
    mirror: Path
    mirror_ok: bool
    source: str
    """``mirror``, ``sources`` or ``none``."""
    owns_checkout: bool = False


async def _head(ctx: OrchestratorContext, run: Run, repo: Repo, shard_dirs: Sequence[Path]) -> _Head:
    """The mirror's head checkout, else the uploaded sources, else an empty directory."""
    settings = ctx.settings
    # Stored whether or not the mirror answers: the pages keep working if access is lost later.
    await asyncio.to_thread(_store_uploaded_sources, settings, run, shard_dirs)
    mirror = mirror_path(settings, repo.id)
    forge = await ctx.current_forge()
    if _can_fetch(repo, forge):
        try:
            mirror = await ensure_mirror_async(settings, repo, forge=forge)
        except Exception as exc:  # no credentials, private repo, network: fall back to what exists
            log.info("upload run %s: mirror refresh failed (%s); using what exists", run.id, exc)
    if (mirror / "HEAD").is_file():
        try:
            await asyncio.to_thread(resolve_commit, mirror, run.head_sha)
            data = await asyncio.to_thread(archive, mirror, run.head_sha)
            checkout = Path(tempfile.mkdtemp(prefix="coverset-upload-"))
            await asyncio.to_thread(extract_archive, data, checkout)
            paths = frozenset(await asyncio.to_thread(tree_paths, mirror, run.head_sha))
            return _Head(checkout, paths, mirror, True, "mirror", owns_checkout=True)
        except (RepoError, GitDiffError, OSError) as exc:
            log.info("upload run %s: head %s not in the mirror (%s)", run.id, run.head_sha[:12], exc)
    uploaded = source_store.source_store(settings)  # the blob store, through its cache
    if await asyncio.to_thread(uploaded.has, repo.id, run.head_sha):
        stored = await asyncio.to_thread(uploaded.checkout, repo.id, run.head_sha)
        return _Head(stored, None, mirror, False, "sources")
    return _Head(
        Path(tempfile.mkdtemp(prefix="coverset-upload-")), None, mirror, False, "none", owns_checkout=True
    )


def _restore_shards(settings: Settings, run: Run, shards: Sequence[Shard], dirs: Mapping[int, Path]) -> None:
    """Restore each shard whose directory this replica lacks from its archive in the blob store
    (:mod:`coverset.storage.uploads`; the runs directory is scratch)."""
    blobs = from_settings(settings)
    for shard in shards:
        parsed = parse_worker_ref(shard.worker_ref)
        if parsed is None or dirs[shard.shard_id].is_dir():
            continue
        digest, label = parsed
        restore_upload(
            blobs,
            run.repo_id,
            run.head_sha,
            label,
            dirs[shard.shard_id],
            upload_limits(settings),
            sha256_prefix=digest,
        )


def _can_fetch(repo: Repo, forge: Any) -> bool:
    """Whether a mirror refresh can work: a recorded local source, or a real forge to ask for
    credentials (the fake forge hands out a placeholder token, so a fetch would only fail slowly)."""
    if (repo.settings_json or {}).get("source"):
        return True
    if repo.forge != "github" or forge is None or forge is False:
        return False
    return getattr(forge, "name", "") != "fake"


def _bundle_paths(shard: Path, limits: BundleLimits) -> set[str]:
    """Every path the shard's bundle lists (measured files and test files): the source allow-list."""
    from coverset.lang import read_any_bundle

    for name in ("neutral.tar.gz", "bundle.tar.gz"):
        tarball = shard / name
        if not tarball.is_file():
            continue
        try:
            bundle = read_any_bundle(tarball, limits=limits)
        except BundleError:
            continue
        paths = set(bundle.static) | {item.file for item in bundle.collected}
        paths |= {p for by_path in bundle.coverage.values() for p in by_path}
        return paths
    return set()


def _invariants_name(sources_dir: Path) -> str:
    """``[tool.coverset] invariants`` of an uploaded ``pyproject.toml`` (default ``invariants.yaml``)."""
    pyproject = sources_dir / "pyproject.toml"
    if not pyproject.is_file() or pyproject.is_symlink():
        return "invariants.yaml"
    try:
        from coverset.lang.python import read_tool_config

        return read_tool_config(pyproject.read_text(encoding="utf-8")).invariants
    except Exception:  # an unreadable config: the default name
        return "invariants.yaml"


def _store_uploaded_sources(settings: Settings, run: Run, shard_dirs: Sequence[Path]) -> None:
    """Store each shard's ``sources/`` (only paths its own bundle lists, plus the config files)."""
    limits = bundle_limits(settings)
    for shard in shard_dirs:
        uploaded = shard / "sources"
        if not uploaded.is_dir() or uploaded.is_symlink():
            continue
        allowed = _bundle_paths(shard, limits)
        result = source_store.source_store(settings).store(
            run.repo_id,
            run.head_sha,
            uploaded,
            allowed=allowed,
            extra_allowed=[_invariants_name(uploaded)],
        )
        if result.refused:
            log.info("upload run %s: sources refused %s", run.id, result.refused)


@dataclass(slots=True)
class _Diff:
    merge_base: str | None = None
    changes: list[Any] = field(default_factory=list)
    changed_lines: dict[str, set[int]] = field(default_factory=dict)
    changed_files: set[str] = field(default_factory=set)
    diff_text: dict[str, str] = field(default_factory=dict)
    ok: bool = False


def _neutral_languages(directories: Sequence[Path]) -> dict[Path, str]:
    """``shard directory -> language`` of each shard's neutral bundle (:func:`_neutral_language`).

    Blocking (it may decompress whole archives when a manifest comes late): a pass calls it once,
    off the event loop, and hands the result to :func:`_diff_language` and the ingest jobs."""
    return {
        directory: _neutral_language(directory / "neutral.tar.gz")
        for directory in directories
        if (directory / "neutral.tar.gz").is_file()
    }


def _diff_language(directories: Sequence[Path], neutral: Mapping[Path, str] | None = None) -> str:
    """The language whose adapter classifies the diff: the one language of the uploaded neutral
    bundles when no shard carries a pytest bundle (a Rust crate's ``coverset rust run``), else
    ``python`` (a mix, or anything unreadable, keeps the Python classification). ``neutral`` is
    :func:`_neutral_languages` of the same directories when the caller already has it."""
    known = neutral if neutral is not None else _neutral_languages(directories)
    languages: set[str] = set()
    for directory in directories:
        if (directory / "bundle.tar.gz").is_file():
            languages.add("python")
        elif directory in known:
            languages.add(known[directory])
    return languages.pop() if len(languages) == 1 else "python"


def _diff_adapter(language: str) -> LanguageAdapter:
    """The adapter registered for ``language`` (``coverset.lang.adapter_for_language``), else Python's."""
    from coverset.lang import adapter_for_language

    try:
        return adapter_for_language(language)
    except BundleError:
        return PythonAdapter()


def _language_changes(
    adapter: LanguageAdapter, mirror: Path, base: str, head_sha: str, diffs: Mapping[str, Any]
) -> list[FunctionChange]:
    """Function changes of the diff's files in ``adapter``'s language (:data:`DIFF_SUFFIXES`), each
    file classified by ``adapter.changed_functions`` from both sides' text in the mirror."""
    suffixes = DIFF_SUFFIXES.get(adapter.name, ())
    files = {
        p: d
        for p, d in diffs.items()
        if suffixes and (p.endswith(suffixes) or d.base_path.endswith(suffixes))
    }
    if not files:
        return []
    base_blobs = read_blobs(mirror, base, [d.base_path for d in files.values() if d.status != "added"])
    head_blobs = read_blobs(mirror, head_sha, [d.path for d in files.values() if d.status != "deleted"])
    changes: list[FunctionChange] = []
    for path, d in sorted(files.items()):
        old = base_blobs.get(d.base_path)
        new = head_blobs.get(path)
        changes += adapter.changed_functions(
            path,
            old.decode("utf-8", "replace") if old is not None else None,
            new.decode("utf-8", "replace") if new is not None else None,
            set(d.added),
            set(d.removed),
        )
    return changes


async def _diff_for(head: _Head, run: Run, adapter: LanguageAdapter) -> _Diff:
    """``merge-base...head`` classified into function changes by ``adapter`` (empty without a
    mirror or a base): the planner's own helper for Python, :func:`_language_changes` otherwise."""
    if not head.mirror_ok or not run.base_sha or head.head_paths is None:
        return _Diff()
    base = await asyncio.to_thread(merge_base, head.mirror, run.base_sha, run.head_sha) or run.base_sha
    try:
        await asyncio.to_thread(resolve_commit, head.mirror, base)
        diffs, diff_text = await asyncio.to_thread(_diff, head.mirror, base, run.head_sha)
        if isinstance(adapter, PythonAdapter):
            changes = await asyncio.to_thread(
                _changes, adapter, head.mirror, base, head.checkout, diffs, head.head_paths
            )
        else:
            changes = await asyncio.to_thread(
                _language_changes, adapter, head.mirror, base, run.head_sha, diffs
            )
    except (RepoError, GitDiffError, OSError, ValueError) as exc:  # ValueError: an unparsable batch
        log.info("upload run %s: no diff (%s)", run.id, exc)
        return _Diff()
    return _Diff(
        merge_base=base,
        changes=changes,
        changed_lines={d.path: set(d.added) for d in diffs.values() if d.status != "deleted" and d.added},
        changed_files={d.path for d in diffs.values() if d.status != "deleted"},
        diff_text=diff_text,
        ok=True,
    )


def _config(head: _Head) -> CoversetConfig:
    """``[tool.coverset]`` of the head (defaults when the head has no usable ``pyproject.toml``)."""
    if head.source == "none":
        return CoversetConfig()
    try:
        return _load_config(PythonAdapter(), head.checkout)
    except Exception as exc:  # a broken config must not lose the upload
        log.info("upload: [tool.coverset] unusable (%s); using defaults", exc)
        return CoversetConfig()


def _results(outcomes: Sequence[IngestOutcome], quarantined: set[str]) -> dict[str, TestState]:
    """One :class:`TestState` per test from the shards' authoritative results (the latest shard
    wins a test that two shards report). In upload mode a failure is final: there is no retry."""
    states: dict[str, TestState] = {}
    for outcome in sorted((o for o in outcomes if o.ok), key=lambda o: o.shard_id):
        for row in outcome.tests:
            state = TestState(
                nodeid=row.nodeid,
                outcome=row.outcome,
                duration_s=row.duration_s,
                failed_phase=row.failed_phase,
                fail_sig=row.fail_sig,
                longrepr=redact((row.longrepr or "")[:8192]) or None,
                shard_id=outcome.shard_id,
                attempts=1,
            )
            state.confirmed = row.outcome in FAILED and row.nodeid not in quarantined
            states[row.nodeid] = state
    return states


def _collected(outcomes: Sequence[IngestOutcome], states: Mapping[str, TestState]) -> list[CollectedItem]:
    """Collected items of every shard (first shard first), plus a bare item per reported test."""
    items: dict[str, CollectedItem] = {}
    for outcome in sorted((o for o in outcomes if o.ok), key=lambda o: o.shard_id):
        for item in outcome.collected:
            items.setdefault(item.nodeid, item)
    for nodeid in states:
        if nodeid not in items:
            items[nodeid] = CollectedItem(nodeid, nodeid.split("::", 1)[0], None, (), (), False)
    return list(items.values())


def _conclusion(
    states: Mapping[str, TestState], errors: Sequence[str], has_diff: bool, gaps: str | None = None
) -> tuple[str, str, int]:
    """``(conclusion, reason, exit code)`` of an upload run (the runner's wording, no retries).

    Without a diff the reason says so (:data:`NO_DIFF_REASON`) and names the largest input-class
    gaps instead (``gaps``), since there are no changed functions to report on.
    """
    failed = [s for s in states.values() if s.confirmed]
    executed = sum(1 for s in states.values() if s.outcome != "not_run")
    passed = sum(1 for s in states.values() if s.outcome == "passed")
    notes = [f"shard {e}" for e in errors]
    if not has_diff:
        notes.append(NO_DIFF_REASON)
        if gaps:
            notes.append(gaps)
    tail = f" ({'; '.join(notes)})" if notes else ""
    if failed:
        return "failure", f"{len(failed)} {'failure' if len(failed) == 1 else 'failures'}{tail}", 1
    if errors:
        return "neutral", f"incomplete upload{tail}", 2
    if executed == 0:
        return "neutral", f"no tests ran{tail}", 5
    if passed == executed:
        return "success", f"all {executed} tests passed{tail}", 0
    return "success", f"{outcome_tally(states)}{tail}", 0


def _catalogue(language: str, measured: Mapping[str, str], outcomes: Sequence[IngestOutcome]) -> Catalogue:
    """The input-class catalogue: Python's over the head sources, else the shipped declared types."""
    if language == "python" and measured:
        return PythonAdapter().shape_catalogue(measured)
    types: dict[Any, Any] = {}
    for outcome in outcomes:
        types.update(outcome.types)
    return TypeCatalogue(annotations=types)


async def _gap_note(ctx: OrchestratorContext, repo_id: int, run_id: str) -> str | None:
    """Without a diff: ``largest input-class gaps: f (3 never exercised), ...`` for the reason.

    Kept out of the summary's missing-class list on purpose: the comment labels that list as the
    changed functions' and invariant targets' classes, which these are not.
    """
    try:
        gaps = await asyncio.to_thread(top_gaps, ctx.store, repo_id, run_id, GAP_FUNCTIONS)
    except Exception as exc:  # facts unreadable: the reason goes without them
        log.info("upload run %s: no gaps (%s)", run_id, exc)
        return None
    shown = [f"{gap.qualname} ({gap.missing} never exercised)" for gap in gaps if gap.missing]
    return f"largest input-class gaps: {', '.join(shown)}" if shown else None


async def process_run(ctx: OrchestratorContext, run_id: str) -> None:
    """One processing pass over every shard of an upload run (see the module docstring)."""
    try:
        await _process(ctx, run_id)
    except Exception as exc:
        log.exception("processing upload run %s failed", run_id)
        await finalize_crashed(
            ctx.db, ctx.settings, run_id, f"upload processing failed: {type(exc).__name__}"
        )


async def _process(ctx: OrchestratorContext, run_id: str) -> None:
    settings = ctx.settings
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
        if run is None or run.trigger != "upload":
            return
        repo = await session.get(Repo, run.repo_id)
        pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
        shards = sorted(
            await session.scalars(select(Shard).where(Shard.run_id == run_id)), key=lambda s: s.shard_id
        )
        session.expunge_all()
    if repo is None or not shards:
        return
    dirs = {s.shard_id: shard_dir(settings, run_id, s.shard_id) for s in shards}
    await asyncio.to_thread(
        _restore_shards, settings, run, shards, dirs
    )  # after a restart, or another replica's
    adapter = PythonAdapter()
    head = await _head(ctx, run, repo, [d for d in dirs.values() if d.is_dir()])
    try:
        config = _config(head)
        measured, head_functions, invariant_targets = {}, [], {}
        if head.source != "none":
            paths = (
                head.head_paths
                if head.head_paths is not None
                else frozenset(source_store.source_store(settings).list(repo.id, run.head_sha))
            )
            measured, head_functions, invariant_targets = await asyncio.to_thread(
                _index_head, adapter, head.checkout, config, paths
            )
        present = [d for d in dirs.values() if d.is_dir()]
        neutral_languages = await asyncio.to_thread(_neutral_languages, present)
        diff = await _diff_for(head, run, _diff_adapter(_diff_language(present, neutral_languages)))
        plan = RunPlan(
            run_id=run_id,
            repo_id=repo.id,
            default_branch=repo.default_branch,
            head_sha=run.head_sha,
            base_sha=run.base_sha,
            merge_base=diff.merge_base,
            mirror=head.mirror,
            checkout=head.checkout,
            config=config,
            env=EnvRef(env_hash="upload", python=config.python, location="upload"),
            workers=[],
            shards=[],
            collected=[],
            order=[],
            scored={},
            wave1=CoverSet(),
            changes=diff.changes,
            changed_lines=diff.changed_lines,
            changed_files=diff.changed_files,
            head_paths=head.head_paths or frozenset(),
            measured_sources=measured,
            head_functions=head_functions,
            test_ids={},
            durations={},
            full_trace=[],
            prior=None,
            quarantined=set(),
            resources=Resources(),
            sandbox_ready_at=settings.now(),
            diff_text=diff.diff_text,
            invariant_targets=invariant_targets,
        )
        rctx = RunContext(
            repo_id=repo.id,
            run_id=run_id,
            head_sha=run.head_sha,
            base_sha=run.base_sha,
            branch=run.branch,
            pr_number=pr.number if pr is not None else None,
            created_at=run.created_at,
            scoring_basis_run_id=None,
        )
        interest = functions_of_interest(plan)
        use_checkout = head.source != "none"
        jobs = []
        missing: list[str] = []
        seen = {shard.shard_id: shard.worker_ref for shard in shards}
        arriving = {s.shard_id for s in shards if s.status == "pending" and not dirs[s.shard_id].is_dir()}
        for shard in shards:
            directory = dirs[shard.shard_id]
            if shard.shard_id in arriving:
                continue  # registered, its files are being moved in: the next pass takes it
            python_bundle = directory / "bundle.tar.gz"
            neutral_bundle = directory / "neutral.tar.gz"
            if use_checkout and python_bundle.is_file():
                tarball, language, from_bundle = python_bundle, "python", False
            elif neutral_bundle.is_file():
                language = neutral_languages.get(directory) or await asyncio.to_thread(
                    _neutral_language, neutral_bundle
                )
                tarball, from_bundle = neutral_bundle, True
            elif python_bundle.is_file():
                tarball, language, from_bundle = python_bundle, "python", False
            else:
                missing.append(f"{shard.shard_id}: its upload is gone from the data directory")
                continue
            jobs.append(
                IngestJob(
                    facts_dir=str(ctx.store.root),
                    blob_url=blob_url_of(settings),
                    ctx=rctx,
                    shard_id=shard.shard_id,
                    tarball=str(tarball),
                    limits=bundle_limits(settings),
                    source_root=str(head.checkout),
                    head_paths=head.head_paths,
                    functions=tuple(head_functions),
                    keep_paths=frozenset(diff.changed_files),
                    keep_funcs=frozenset(r.triple for r in interest.values()),
                    language=language,
                    regions_from_bundle=from_bundle and language == "python",
                    cache_overrides=tuple(sorted(settings.cache_overrides().items())),
                )
            )
        outcomes = await ctx.ingest_pool().run(jobs) if jobs else []
        errors = missing + [f"{o.shard_id}: {o.error}" for o in outcomes if not o.ok]
        language = "python" if all(job.language == "python" for job in jobs) else "neutral"
        await _conclude(
            ctx,
            run_id,
            plan,
            outcomes,
            _Pass(seen=seen, arriving=arriving),
            errors,
            rctx,
            language=language,
            has_diff=diff.ok,
        )
    finally:
        if head.owns_checkout:
            await asyncio.to_thread(shutil.rmtree, head.checkout, True)


def _neutral_language(tarball: Path) -> str:
    """The ``language`` a neutral bundle's manifest declares (``python`` when unreadable)."""
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            for member in tar:
                if (
                    member.isfile()
                    and member.name.rsplit("/", 1)[-1] == "manifest.json"
                    and member.size < 1 << 20
                ):
                    extracted = tar.extractfile(member)
                    manifest = json.loads(extracted.read()) if extracted is not None else {}
                    language = manifest.get("language") if isinstance(manifest, dict) else None
                    if isinstance(language, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", language):
                        from coverset.lang.neutral import NEUTRAL_LANGUAGES

                        return language if language in NEUTRAL_LANGUAGES or language == "python" else "python"
                    break
    except (tarfile.TarError, OSError, ValueError, EOFError, gzip.BadGzipFile):
        pass
    return "python"


@dataclass(frozen=True, slots=True)
class _Pass:
    """The shards one processing pass worked from."""

    seen: dict[int, str | None]
    """``shard_id -> worker_ref`` when the pass began (the label and archive digest it ingested)."""
    arriving: set[int]
    """Shards registered but not on disk yet (their upload is being moved into place)."""

    @property
    def received(self) -> int:
        return len(self.seen) - len(self.arriving)

    async def current(self, session: AsyncSession, run_id: str) -> bool:
        """True when no shard was added or replaced since the pass began. A stale pass writes
        nothing final: the upload that changed the shards scheduled another pass."""
        rows = await session.execute(select(Shard.shard_id, Shard.worker_ref).where(Shard.run_id == run_id))
        return {shard_id: ref for shard_id, ref in rows.all()} == self.seen and not self.arriving


async def _conclude(
    ctx: OrchestratorContext,
    run_id: str,
    plan: RunPlan,
    outcomes: Sequence[IngestOutcome],
    shards: _Pass,
    errors: Sequence[str],
    rctx: RunContext,
    *,
    language: str,
    has_diff: bool,
) -> None:
    """Finalize the facts, write results, and (once every expected shard is in) the verdict.

    The expected shard count is the larger of the labels registered and ``runs.shard_count`` (the
    uploader's ``--shards``), read fresh because a registration may have raised it since the pass
    began. Results, shard statuses and the verdict are written only if the pass is still current
    (:meth:`_Pass.current`), so an upload that lands mid-pass never sees its shard reported as
    done by a pass that did not read it.
    """
    settings = ctx.settings
    ok = [o for o in outcomes if o.ok]
    async with ctx.db.session() as session:
        run_row = await session.get(Run, run_id)
        declared = run_row.shard_count if run_row is not None else 0
    expected = max(len(shards.seen), declared or 0)
    received = shards.received
    catalogue = await asyncio.to_thread(_catalogue, language, plan.measured_sources, ok)
    nodeids = {row.nodeid for o in ok for row in o.tests}
    economy = await economy_inputs(ctx.db, settings, plan.repo_id, nodeids)
    finalized = await asyncio.to_thread(
        finalize_run, ctx.store, rctx, expected_shards=expected, catalogue=catalogue, economy=economy
    )
    schedule_warm_up(ctx, plan.repo_id, run_id, plan.head_sha, plan.base_sha)

    async def register(session: AsyncSession) -> tuple[dict[str, int], set[str]]:
        nodeids = sorted(
            {row.nodeid for o in ok for row in o.tests} | {i.nodeid for o in ok for i in o.collected}
        )
        rows = await history.register_tests(session, plan.repo_id, nodeids, run_id)
        return {n: r.id for n, r in rows.items()}, {n for n, r in rows.items() if r.quarantined}

    test_ids, quarantined = await run_transaction(ctx.db, register)
    states = _results(outcomes, quarantined)
    plan.collected = _collected(outcomes, states)
    plan.order = [item.nodeid for item in plan.collected]
    plan.test_ids = test_ids
    plan.quarantined = quarantined
    plan.durations = {n: s.duration_s for n, s in states.items() if s.duration_s}
    complete = received >= expected
    gaps = None if has_diff else await _gap_note(ctx, plan.repo_id, run_id)
    conclusion, reason, exit_code = _conclusion(states, errors, has_diff, gaps)
    reason = redact(reason)
    failed_shards = {o.shard_id: o.error for o in outcomes if not o.ok}
    now = settings.now()

    async def write_results(session: AsyncSession) -> bool:
        if not await shards.current(session, run_id):
            return False
        await session.execute(delete(TestAttempt).where(TestAttempt.run_id == run_id))
        await session.execute(delete(TestResult).where(TestResult.run_id == run_id))
        results, attempts = [], []
        for nodeid, state in states.items():
            test_id = test_ids.get(nodeid)
            if test_id is None:
                continue
            duration = round(state.duration_s * 1000, 3) if state.duration_s is not None else None
            results.append(
                {
                    "run_id": run_id,
                    "test_id": test_id,
                    "outcome": state.outcome,
                    "failed_phase": state.failed_phase
                    if state.failed_phase in ("setup", "call", "teardown")
                    else None,
                    "fail_sig": _sig(state.fail_sig),
                    "duration_ms": duration,
                    "shard_id": state.shard_id,
                    "attempts": 1,
                    "flaky": False,
                    "quarantined": nodeid in quarantined,
                }
            )
            attempts.append(
                {
                    "run_id": run_id,
                    "test_id": test_id,
                    "attempt": 1,
                    "outcome": state.outcome,
                    "duration_ms": duration,
                    "shard_id": state.shard_id,
                }
            )
        if results:
            await session.execute(insert(TestResult), results)
            await session.execute(insert(TestAttempt), attempts)
        for shard in await session.scalars(select(Shard).where(Shard.run_id == run_id)):
            error = failed_shards.get(shard.shard_id)
            shard.status = "failed" if error else "completed"
            shard.error = redact(error)[:1000] if error else None
            shard.test_count = sum(1 for s in states.values() if s.shard_id == shard.shard_id)
            shard.finished_at = now
        return True

    if not await run_transaction(ctx.db, write_results):
        log.info("upload run %s: shards changed during the pass; the next pass reports", run_id)
        return
    if not complete:

        async def waiting(session: AsyncSession) -> None:
            run = await session.get(Run, run_id)
            if run is None:
                return
            run.coverage_complete = False
            text = f"{received} of {expected} uploads in; the verdict waits for the rest"
            await _event(session, run_id, "log", {"shard": None, "text": text}, now)

        await run_transaction(ctx.db, waiting)
        return

    report = await _report(ctx, run_id, plan, ok, states, catalogue)

    async def verdict(session: AsyncSession) -> Any:
        run = await session.get(Run, run_id)
        if run is None or not await shards.current(session, run_id):
            return None
        await history.update_test_stats(session, run, list(test_ids.values()))
        run.coverage_complete = finalized.coverage_complete
        run.cost_estimate = 0.0
        run.status = "completed"
        run.claimed_by = None
        stamp = now.isoformat()
        run.timeline = {**(run.timeline or {}), "verdict": stamp, "finished": stamp}
        summary = build_summary(
            settings, run, plan, states, conclusion=conclusion, reason=reason, report=report
        )
        await persist_verdict(session, run, summary)
        counts = dict.fromkeys(("passed", "failed", "error", "skipped", "xfailed", "xpassed", "not_run"), 0)
        for state in states.values():
            counts[state.outcome] = counts.get(state.outcome, 0) + 1
        await _event(
            session,
            run_id,
            "verdict",
            {"conclusion": conclusion, "reason": reason, "label": summary.label},
            now,
        )
        await _event(
            session, run_id, "done", {"conclusion": conclusion, "exit_code": exit_code, "counts": counts}, now
        )
        return summary

    summary = await run_transaction(ctx.db, verdict)
    if summary is None:
        return
    forge = await ctx.current_forge()
    if forge is None:
        return
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
        if run is not None:
            errors_posted = await post_summary(session, run, summary, forge)
            await session.commit()
            for error in errors_posted:
                log.warning("upload run %s: forge: %s", run_id, error)


def _sig(value: str | None) -> bytes | None:
    """A failure signature (16 hex characters in the bundle) as the 8 bytes ``fail_sig`` stores."""
    if not value or len(value) != 16:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


async def _report(
    ctx: OrchestratorContext,
    run_id: str,
    plan: RunPlan,
    outcomes: Sequence[IngestOutcome],
    states: Mapping[str, TestState],
    catalogue: Catalogue,
) -> RunReport:
    """:func:`build_report` in its own transaction; a failure degrades to a report with a note.

    A run is reported again whenever another shard arrives, so the invariant results of the
    previous pass are dropped first (they are keyed by run and invariant, and would collide).
    """
    try:
        async with ctx.db.session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            await session.execute(delete(InvariantResultModel).where(InvariantResultModel.run_id == run_id))
            report = await build_report(
                session, ctx.settings, ctx.store, run, plan, outcomes, states, catalogue=catalogue
            )
            await session.commit()
            return report
    except Exception as exc:  # the verdict still lands, without the extras
        log.exception("building the report of upload run %s failed", run_id)
        return RunReport(notes=[f"report unavailable: {type(exc).__name__}"])
