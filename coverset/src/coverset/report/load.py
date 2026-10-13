"""Turn a report SOURCE into a :class:`~coverset.report.facts.ReportInput`.

Sources: a ``pytest --coverset-out`` directory, a ``bundle.tar.gz`` (extracted safely into a
temporary directory with :func:`~coverset.facts.untrusted.extract_bundle`), a neutral bundle
directory or ``tar.gz`` (``manifest.json``; read with ``coverset.lang.read_any_bundle``), or a run
in the facts store (:func:`load_run`).

Which local files are read is never the bundle's choice (its ``git_root``, ``rootdir`` and paths are
untrusted): :func:`source_access` decides. With ``--repo-root`` sources are read under that root (only
the files git tracks there when it is a git work tree); without it, a ``--coverset-out`` directory
reads from the current directory only when that is a git work tree, and only files git tracks; an
archive or a neutral bundle reads no source at all. When nothing is read, every page says why.

Which limits apply (:func:`report_limits`): a directory the user's own ``pytest --coverset`` wrote
(``coverage.sqlite`` or a ``neutral/`` copy at its top) is trusted input and is read under
``BundleLimits.local()``; an archive or a neutral bundle may be a pull request's output (a CI
artifact) and is read under the strict limits ingest uses, unless the caller says it is trusted
(``coverset report --trusted``). A plugin directory is read from its raw files: the
``bundle.tar.gz`` next to them packs the same files and is never extracted, and only the files
that are read count against any byte cap.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coverset.facts.convert import read_python_bundle
from coverset.facts.untrusted import BundleError, BundleLimitError, BundleLimits, extract_bundle
from coverset.lang.base import IMPORT_CONTEXT, NeutralBundle
from coverset.report.facts import (
    MAX_SOURCE_BYTES,
    NO_RESULT,
    NOT_RUN,
    BundleFacts,
    ReportInput,
    TestInfo,
    borrowed_signatures,
    bundle_regions,
    bundle_types,
    catalogue_from_descriptors,
    decode_source,
    read_sources,
    regions_with_owners,
)

__all__ = [
    "BundleRead",
    "SourceAccess",
    "input_from_bundle",
    "load_base",
    "load_bundle",
    "load_run",
    "model_cap",
    "neutral_reader",
    "read_bundle_path",
    "read_source",
    "report_limits",
    "source_access",
    "tracked_files",
]

MANIFEST = "manifest.json"
ARCHIVE = "bundle.tar.gz"
COVERAGE_SQLITE = "coverage.sqlite"
NEUTRAL_SUBDIR = "neutral"
"""Where ``pytest --coverset`` writes the neutral copy of a ``--coverset-out`` bundle."""
_NEUTRAL_REQUIRED = ("tests", "files", "coverage")

LOCAL_LIMITS = (
    "local: a directory your own pytest --coverset wrote, read without the byte cap and with the "
    "generous caps of BundleLimits.local()"
)
STRICT_LIMITS = (
    "strict: an archive or a neutral bundle may be a pull request's output (a CI artifact), so the "
    "caps ingest uses apply (--trusted lifts them)"
)
TRUSTED_LIMITS = "local: passed with --trusted"
RUN_LIMITS = "local: a run in the facts store, already checked under the strict limits when it was ingested"


def neutral_reader() -> Callable[[Path], NeutralBundle] | None:
    """The reader of neutral bundles: ``coverset.lang.read_any_bundle`` (picks the reader by the
    manifest), else ``coverset_model.io.read_bundle``, else None in a build that has neither."""
    try:
        from coverset.lang import read_any_bundle  # type: ignore[attr-defined]
    except ImportError:
        pass
    else:
        return read_any_bundle  # type: ignore[no-any-return]
    try:
        from coverset_model.io import read_bundle  # type: ignore[import-not-found]
    except ImportError:
        return None
    return read_bundle  # type: ignore[no-any-return]


def _rejected() -> tuple[type[Exception], ...]:
    """The neutral reader's refusal type (a ``ValueError``), when the model package is present."""
    try:
        from coverset_model.io import BundleRejected  # type: ignore[import-not-found]
    except ImportError:
        return ()
    return (BundleRejected,)


@dataclass(frozen=True, slots=True)
class BundleRead:
    """A bundle as :func:`read_source` read it."""

    bundle: NeutralBundle
    kind: str
    """``python`` (converted from a pytest-coverset bundle) or ``neutral``."""
    read_from: str
    """Which files were read (the about data's ``read_from``)."""
    limits: str
    """Which limits profile applied, and why (the about data's ``limits``)."""


def is_local_output(source: Path) -> bool:
    """A directory the user's own ``pytest --coverset`` wrote: ``coverage.sqlite`` or a ``neutral/``
    copy at its top (and no ``manifest.json``, which makes it a neutral bundle)."""
    source = Path(source)
    if source.is_symlink() or not source.is_dir() or (source / MANIFEST).exists():
        return False
    return (source / COVERAGE_SQLITE).is_file() or _complete_neutral(source / NEUTRAL_SUBDIR)


def report_limits(source: Path, *, trusted: bool = False) -> tuple[BundleLimits, str]:
    """The limits ``coverset report`` reads ``source`` under, and why: the local profile for a
    ``pytest --coverset`` output directory (:func:`is_local_output`) or with ``trusted``, the strict
    profile ingest uses for anything else (an archive or a neutral bundle may be a PR's output)."""
    if is_local_output(source):
        return BundleLimits.local(), LOCAL_LIMITS
    if trusted:
        return BundleLimits.local(), TRUSTED_LIMITS
    return BundleLimits(), STRICT_LIMITS


def read_source(source: Path, *, trusted: bool = False) -> BundleRead:
    """Read a bundle directory or archive under :func:`report_limits`."""
    source = Path(source)
    limits, why = report_limits(source, trusted=trusted)
    if source.is_dir():
        bundle, kind, read_from = _read_dir(source, limits)
        return BundleRead(bundle, kind, read_from, why)
    if not source.is_file():
        raise BundleError(f"{source} is neither a bundle directory nor a bundle archive")
    bundle, kind, read_from = _read_archive(source, limits)
    return BundleRead(bundle, kind, read_from, why)


def read_bundle_path(source: Path, *, trusted: bool = False) -> tuple[NeutralBundle, str]:
    """Read a bundle directory or archive: ``(bundle, kind)`` where kind is ``python`` or ``neutral``."""
    read = read_source(source, trusted=trusted)
    return read.bundle, read.kind


def _read_archive(source: Path, limits: BundleLimits) -> tuple[NeutralBundle, str, str]:
    with tempfile.TemporaryDirectory(prefix="coverset-report-") as tmp:
        root = extract_bundle(source, Path(tmp) / "bundle", limits)
        inner = _single_subdir(root)
        bundle, kind, read_from = _read_dir(inner, limits)
    return bundle, kind, f"{source.name}, extracted: {read_from}"


def _complete_neutral(root: Path) -> bool:
    """``root`` is a neutral bundle directory with its manifest and every required file."""
    if root.is_symlink() or not root.is_dir() or not (root / MANIFEST).is_file():
        return False
    return all(
        (root / f"{stem}.jsonl").is_file() or (root / f"{stem}.jsonl.gz").is_file()
        for stem in _NEUTRAL_REQUIRED
    )


def _single_subdir(root: Path) -> Path:
    """The bundle root inside an extracted archive (flat members, or one top-level directory)."""
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return root


def _read_dir(root: Path, limits: BundleLimits) -> tuple[NeutralBundle, str, str]:
    """``(bundle, kind, read_from)`` of a bundle directory. The raw files win: a directory with
    ``coverage.sqlite`` is read from it (its ``bundle.tar.gz`` and ``neutral/`` copy are never
    opened); without it, a complete ``neutral/`` copy is read; a directory holding only
    ``bundle.tar.gz`` is read as that archive."""
    if (root / MANIFEST).is_file():
        return _read_neutral(root, limits), "neutral", f"{MANIFEST} and the neutral files next to it"
    if (root / COVERAGE_SQLITE).is_file():
        bundle = read_python_bundle(root, limits=limits)
        convert = bundle.meta.get("convert") if isinstance(bundle.meta, dict) else None
        files = convert.get("files") if isinstance(convert, dict) else None
        read_from = "the raw files (" + ", ".join(files or [COVERAGE_SQLITE]) + ")"
        unread = [
            name
            for name in (ARCHIVE, "neutral.tar.gz", NEUTRAL_SUBDIR + "/")
            if (root / name.rstrip("/")).exists()
        ]
        if unread:
            read_from += f"; {', '.join(unread)} not read (the same run, packed or converted)"
        return bundle, "python", read_from
    neutral = root / NEUTRAL_SUBDIR
    if _complete_neutral(neutral):
        read_from = f"{NEUTRAL_SUBDIR}/ (the plugin's neutral copy; there is no {COVERAGE_SQLITE} here)"
        return _read_neutral(neutral, limits), "neutral", read_from
    archive = root / ARCHIVE
    if archive.is_file():
        return _read_archive(archive, limits)
    return read_python_bundle(root, limits=limits), "python", "the raw files"


def _read_neutral(root: Path, limits: BundleLimits) -> NeutralBundle:
    """A neutral bundle directory under ``limits``; a breached cap of the neutral reader is a
    :class:`BundleLimitError` (so the CLI can say how to lift it)."""
    try:
        from coverset.lang import read_any_bundle
    except ImportError:  # pragma: no cover - every build of coverset has coverset.lang
        raise BundleError(f"{root} is a neutral bundle ({MANIFEST}), but this build cannot read it") from None
    try:
        return read_any_bundle(root, limits=limits)
    except BundleError as exc:
        problem = getattr(exc.__cause__, "problem", None)
        if getattr(problem, "code", None) == "limit":
            from coverset.lang.neutral import model_limits

            cap = model_cap(str(exc))
            value = getattr(model_limits(limits), cap, None)
            raise BundleLimitError(
                f"{exc} ({cap} of the {limits.profile} limits)",
                cap=cap,
                value=None,
                limit=int(value) if isinstance(value, (int, float)) else None,
                profile=limits.profile,
            ) from exc
        raise
    except _rejected() as exc:
        raise BundleError(f"neutral bundle refused: {exc}") from exc


_MODEL_CAPS = (
    ("manifest.json", "max_manifest_bytes"),
    ("bytes (limit", "max_bundle_bytes"),
    ("expands beyond", "gzip_max_bytes"),
    ("members declare", "gzip_max_bytes"),
    ("members", "max_members"),
    ("coverage cells", "max_coverage_cells"),
    ("distinct test ids", "max_tests"),
    ("distinct paths", "max_paths"),
    ("distinct shape keys", "max_shape_keys"),
    ("functions", "max_functions"),
    ("coverage.jsonl", "max_coverage_rows"),
    ("lines", "max_rows"),
    ("wall-clock", "max_seconds"),
)


def model_cap(message: str) -> str:
    """The ``coverset_model.io.Limits`` field a neutral reader refusal names (best effort)."""
    return next((cap for needle, cap in _MODEL_CAPS if needle in message), "a neutral reader cap")


@dataclass(frozen=True, slots=True)
class SourceAccess:
    """Where a report may read source files from."""

    root: Path | None
    """The directory sources are read under; None reads no source at all."""
    tracked: frozenset[str] | None = None
    """When set, only these root-relative paths (the files git tracks under ``root``) are read."""
    notice: str | None = None
    """Why no source was read (shown on every page and by the CLI)."""

    def describe(self) -> str:
        """The about page's "Sources read from" value."""
        if self.root is None:
            return "nothing (no source file was read)"
        return f"{self.root} (only files git tracks there)" if self.tracked is not None else str(self.root)


def git_toplevel(path: Path) -> Path | None:
    """The top of the git work tree ``path`` is in, or None (not a work tree, or git cannot run).

    Bundle paths are repository-relative, so sources are always resolved from the top of the work
    tree, whichever directory inside it the report is built from or ``--repo-root`` names."""
    from coverset.orchestrator.repos import git_env

    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(path), "rev-parse", "--show-toplevel"],
            env=git_env(),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.decode("utf-8", "surrogateescape").strip()
    return Path(top).resolve() if top else None


def tracked_files(root: Path) -> frozenset[str] | None:
    """The paths (relative to ``root``, POSIX) git tracks under ``root``, or None when ``root`` is not
    in a git work tree or git cannot be run. Git runs with the scrubbed environment of the mirrors
    (no user or system config) and with the file-system monitor off, so the repository's own config
    cannot start a program."""
    from coverset.orchestrator.repos import git_env

    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files", "-z", "--cached"],
            env=git_env(),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return frozenset(p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\0") if p)


def is_plugin_dir(source: Path, kind: str) -> bool:
    """A ``pytest --coverset-out`` directory (``coverage.sqlite`` at its top), as opposed to an
    archive or a neutral bundle."""
    source = Path(source)
    return kind == "python" and source.is_dir() and (source / "coverage.sqlite").is_file()


def source_access(
    source: Path, kind: str, paths: Iterable[str], repo_root: Path | None = None, *, cwd: Path | None = None
) -> SourceAccess:
    """Where to read the sources of a bundle from (never from the bundle's own meta):

    * ``repo_root`` given: under it, and only the files git tracks there when it is a git work tree
      (a subdirectory that tracks none of the bundle's repository-relative paths falls back to the
      top of its work tree when that tracks some);
    * a ``--coverset-out`` directory: the current directory, when it is a git work tree that tracks
      at least one of the bundle's paths (and only the tracked ones are read);
    * otherwise (an archive, a neutral bundle, or a current directory that is not such a work tree):
      nothing, with a notice saying to pass ``--repo-root``.
    """
    wanted = list(paths)
    if repo_root is not None:
        root = Path(repo_root).resolve()
        tracked = tracked_files(root)
        # An explicit root is read as given. Only when git tracks none of the bundle's paths under
        # it, but does under the enclosing work tree (bundle paths are repository-relative and the
        # root named a subdirectory), the work tree's top is used instead.
        if tracked is not None and wanted and not any(p in tracked for p in wanted):
            top = git_toplevel(root)
            if top is not None and top != root:
                top_tracked = tracked_files(top)
                if top_tracked and any(p in top_tracked for p in wanted):
                    root, tracked = top, top_tracked
        return SourceAccess(root, tracked)
    if not is_plugin_dir(source, kind):
        return SourceAccess(
            None,
            notice="this bundle is an archive or a neutral bundle, which never chooses the local "
            "directory sources come from: pass --repo-root DIR to show them.",
        )
    here = (cwd or Path.cwd()).resolve()
    here = git_toplevel(here) or here
    tracked = tracked_files(here)
    if tracked is None:
        return SourceAccess(
            None,
            notice=f"the current directory ({here}) is not a git work tree, and a bundle never chooses "
            "the local directory sources come from: run from the repository, or pass --repo-root DIR.",
        )
    if wanted and not any(p in tracked for p in wanted):
        return SourceAccess(
            None,
            notice=f"git tracks none of the bundle's {len(wanted)} measured files in the current "
            f"directory ({here}): run from the repository the tests ran in, or pass --repo-root DIR.",
        )
    return SourceAccess(here, tracked)


def load_bundle(
    source: Path, *, repo_root: Path | None = None, title: str | None = None, trusted: bool = False
) -> ReportInput:
    """A report input from a bundle directory or archive, read under :func:`report_limits` (an
    archive or a neutral bundle under the strict limits unless ``trusted``), with sources read as
    :func:`source_access` decides."""
    read = read_source(source, trusted=trusted)
    access = source_access(Path(source), read.kind, read.bundle.static, repo_root)
    return input_from_bundle(
        read.bundle,
        source=Path(source),
        kind=read.kind,
        access=access,
        title=title,
        read_from=read.read_from,
        limits=read.limits,
    )


def load_base(
    source: Path, *, repo_root: Path | None = None, title: str | None = "base", trusted: bool = False
) -> ReportInput:
    """A ``--compare`` base: as :func:`load_bundle`, except that its function index is its own
    wherever the base ships one. A ``--coverset-out`` directory carries the neutral copy
    ``neutral/`` (with the ``functions`` rows the plugin indexed from the tested text), and a
    neutral bundle carries its own rows; either wins over indexing the local text, which is this
    run's and no longer the base's. A base without shipped rows (a ``bundle.tar.gz``) is indexed
    from the local text as before, and :mod:`coverset.report.compare` uses those regions only
    where the file is unchanged."""
    import dataclasses

    source = Path(source)
    read = read_source(source, trusted=trusted)
    bundle, kind = read.bundle, read.kind
    if not bundle_regions(bundle) and is_plugin_dir(source, kind):
        shipped = _neutral_copy(source)
        if shipped is not None and bundle_regions(shipped):
            bundle = dataclasses.replace(
                bundle, functions=shipped.functions, types=bundle.types or shipped.types
            )
    access = source_access(source, kind, bundle.static, repo_root)
    return input_from_bundle(
        bundle,
        source=source,
        kind=kind,
        access=access,
        title=title,
        prefer_shipped=True,
        read_from=read.read_from,
        limits=read.limits,
    )


def _neutral_copy(source: Path) -> NeutralBundle | None:
    """The neutral copy a ``--coverset-out`` directory carries, or None (absent or unreadable). It
    is the user's own output, so it is read under the local limits."""
    root = source / NEUTRAL_SUBDIR
    if root.is_symlink() or not (root / MANIFEST).is_file():
        return None
    try:
        bundle, kind, _ = _read_dir(root, BundleLimits.local())
    except (BundleError, OSError, ValueError, KeyError):
        return None
    return bundle if kind == "neutral" else None


def input_from_bundle(
    bundle: NeutralBundle,
    *,
    source: Path,
    kind: str,
    repo_root: Path | None = None,
    access: SourceAccess | None = None,
    title: str | None = None,
    prefer_shipped: bool = False,
    read_from: str | None = None,
    limits: str | None = None,
) -> ReportInput:
    """A report input from an in-memory bundle (``bundle.functions`` and ``bundle.types`` are used
    when the neutral model carries them). Sources are read as ``access`` says; without it, under
    ``repo_root`` (git-tracked files only when it is a git work tree), and with neither, not at all.
    With ``prefer_shipped`` (a ``--compare`` base) the shipped ``functions`` rows win over indexing
    the local text (see :func:`load_base`). ``read_from`` and ``limits`` (which files were read,
    under which limits profile) go into the about data when given.
    """
    meta = dict(bundle.meta) if isinstance(bundle.meta, dict) else {}
    if access is None:
        if repo_root is not None:
            root = Path(repo_root).resolve()
            access = SourceAccess(root, tracked_files(root))
        else:
            access = SourceAccess(None, notice="no repository root was given: pass --repo-root DIR.")
    paths = sorted(bundle.static)
    if access.root is not None:
        sources = read_sources(access.root, paths, tracked=access.tracked)
    else:
        sources = dict.fromkeys(paths)
    regions, own = regions_with_owners(paths, sources, bundle, prefer_shipped=prefer_shipped)
    types = bundle_types(bundle, regions)
    # Declared types shipped in the bundle win over whatever the source parser would read: a
    # TypeScript file has no Python annotations, and a converted Python bundle's descriptors
    # came from those very annotations.
    catalogues = {
        r.func_key: catalogue_from_descriptors(r, types[r.func_key]) for r in regions if r.func_key in types
    }
    if prefer_shipped:
        # A base's own regions over a local text that may be another commit's: the local
        # annotations are read only for a function whose signature there is the same.
        for key, found in borrowed_signatures(regions, sources, own).items():
            catalogues.setdefault(key, found)
    facts = BundleFacts(bundle, regions, catalogues=catalogues)
    coverage = {
        nodeid: {path: frozenset(lines) for path, lines in by_path.items()}
        for nodeid, by_path in bundle.coverage.items()
    }
    name = access.root.name if access.root is not None else ""
    read = sum(1 for text in sources.values() if text is not None)
    untracked = sum(1 for p in paths if p not in access.tracked) if access.tracked is not None else 0
    about = {
        "source": str(source),
        "bundle_kind": kind,
        "repo_root": access.describe(),
        "root": str(access.root) if access.root is not None else None,
        "sources": f"{read} of {len(paths)} measured files read"
        + (f"; {untracked} not tracked by git there, so not read" if untracked else ""),
        "regions_from_bundle": bool(bundle_regions(bundle)),
        "types_from_bundle": len(types),
        "meta": meta,
    }
    if read_from is not None:
        about["read_from"] = read_from
    if limits is not None:
        about["limits"] = limits
    return ReportInput(
        facts=facts,
        tests=tests_from_bundle(bundle),
        coverage=coverage,
        sources=sources,
        meta=about,
        title=title or (f"Coverage report · {name}" if name else "Coverage report"),
        origin=f"bundle {source.name}",
        types=types,
        notice=access.notice,
        own_regions=own,
    )


def tests_from_bundle(bundle: NeutralBundle) -> list[TestInfo]:
    """Every test of the bundle: its result row, else its collected item (never ran), else
    ``no result`` for a test that only appears in the coverage."""
    out: dict[str, TestInfo] = {}
    for row in bundle.tests:
        out[row.nodeid] = TestInfo(
            nodeid=row.nodeid,
            file=row.file,
            outcome=row.outcome,
            duration_s=row.duration_s,
            failed_phase=row.failed_phase,
            message=row.longrepr,
        )
    for item in bundle.collected:
        out.setdefault(item.nodeid, TestInfo(nodeid=item.nodeid, file=item.file, outcome=None))
    for nodeid in bundle.coverage:
        if nodeid != IMPORT_CONTEXT and nodeid not in out:
            # it ran measured lines, but the bundle has no result for it
            out[nodeid] = TestInfo(nodeid=nodeid, file=nodeid.split("::", 1)[0], outcome=NO_RESULT)
    return sorted(out.values(), key=lambda t: t.nodeid)


# -- the facts store ------------------------------------------------------------------------------


async def load_run(
    settings: Any, run_id: str, repo_spec: str | None = None, *, title: str | None = None
) -> ReportInput:
    """A report input for one run in the facts store, sources from the repo's mirror at its head."""
    import asyncio

    from sqlalchemy import select

    from coverset.commands.common import find_repo, prepare_db
    from coverset.db.models import PullRequest, Repo, Run, RunEvent, TestCase, TestResult
    from coverset.db.session import Database
    from coverset.facts.store import FactsStore
    from coverset.orchestrator.repos import RepoError, mirror_path, read_blobs
    from coverset.web.facts_view import run_facts

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        async with db.session() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise LookupError(f"there is no run {run_id!r}")
            if repo_spec is not None:
                repo = await find_repo(session, repo_spec)
                if repo.id != run.repo_id:
                    raise LookupError(f"run {run_id} does not belong to {repo.full_name}")
            else:
                repo = await session.get(Repo, run.repo_id)
            pr = await session.get(PullRequest, run.pr_id) if run.pr_id is not None else None
            results = (
                await session.execute(
                    select(TestCase.nodeid, TestResult)
                    .join(TestCase, TestCase.id == TestResult.test_id)
                    .where(TestResult.run_id == run.id)
                )
            ).all()
            events = (
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id, RunEvent.kind == "test")
                    .order_by(RunEvent.seq)
                )
            ).all()
            run_info = {
                "run_id": run.id,
                "repository": repo.full_name if repo else None,
                "head_sha": run.head_sha,
                "base_sha": run.base_sha,
                "branch": run.branch,
                "trigger": run.trigger,
                "pull_request": pr.number if pr else None,
                "pull_request_title": pr.title if pr else None,
                "verdict": run.verdict,
                "status": run.status,
                "executor": run.executor,
                "shards": run.shard_count,
                "coverage_complete": run.coverage_complete,
                "summary": run.summary_json,
            }
            head_sha, repo_id = run.head_sha, run.repo_id
            repo_name = repo.full_name if repo else f"repo {run.repo_id}"
    finally:
        await db.engine.dispose()
    store = FactsStore(settings.facts_dir)
    if not store.run_dir(repo_id, run_id).is_dir():
        raise LookupError(f"run {run_id} has no facts in {settings.facts_dir}")
    facts = await asyncio.to_thread(run_facts, store, repo_id, run_id)
    paths = sorted(facts.statics)
    try:
        blobs = await asyncio.to_thread(read_blobs, mirror_path(settings, repo_id), head_sha, paths)
    except RepoError:
        blobs = {}
    sources: dict[str, str | None] = {p: None for p in paths}
    for path, data in blobs.items():
        if len(data) <= MAX_SOURCE_BYTES:
            sources[path] = decode_source(data, path)
    coverage = await asyncio.to_thread(_store_coverage, store, repo_id, run_id)
    messages: dict[str, tuple[str | None, str | None]] = {}
    for event in events:
        payload = event.payload or {}
        nodeid = payload.get("nodeid")
        if isinstance(nodeid, str) and payload.get("retry") is None:
            text = payload.get("longrepr")
            phase = payload.get("failed_phase")
            messages[nodeid] = (
                text if isinstance(text, str) else None,
                phase if isinstance(phase, str) else None,
            )
    tests: dict[str, TestInfo] = {}
    for nodeid, result in results:
        text, phase = messages.get(nodeid, (None, None))
        tests[nodeid] = TestInfo(
            nodeid=nodeid,
            file=nodeid.split("::", 1)[0],
            outcome=_outcome_label(result.outcome),
            duration_s=(result.duration_ms or 0.0) / 1000.0 if result.duration_ms is not None else None,
            failed_phase=str(result.failed_phase) if result.failed_phase else phase,
            message=text,
        )
    for row in store.read_table(repo_id, run_id, "tests_dim").to_pylist():
        nodeid = row["nodeid"]
        if nodeid and nodeid != IMPORT_CONTEXT and nodeid not in tests:
            text, phase = messages.get(nodeid, (None, None))
            tests[nodeid] = TestInfo(
                nodeid,
                nodeid.split("::", 1)[0],
                _outcome_label(row["outcome"]),
                row["duration_s"],
                phase,
                text,
            )
    convert = [
        {"shard": row["shard_id"], "attempt": row["attempt"], "status": row["status"],
         "convert": _json_or_none(row["convert_json"])}
        for row in store.read_table(repo_id, run_id, "ingest_log").to_pylist()
    ]  # fmt: skip
    about = {
        "source": f"facts store run {run_id}",
        "bundle_kind": "facts store",
        "limits": RUN_LIMITS,
        "repo_root": f"mirror of {repo_name} at {head_sha}",
        "run": run_info,
        "ingest": convert,
    }
    label = f"PR #{run_info['pull_request']}" if run_info["pull_request"] else (run_info["branch"] or run_id)
    return ReportInput(
        facts=facts,
        tests=sorted(tests.values(), key=lambda t: t.nodeid),
        coverage=coverage,
        sources=sources,
        meta=about,
        title=title or f"Coverage report · {repo_name} · {label}",
        origin=f"run {run_id} ({head_sha[:8]})",
        commit=head_sha,
    )


def _outcome_label(value: object) -> str | None:
    """A stored outcome as the report labels it (the planner's ``not_run`` is "not run")."""
    if value is None:
        return None
    text = str(value)
    return NOT_RUN if text == "not_run" else text


def _json_or_none(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _store_coverage(store: Any, repo_id: int, run_id: str) -> dict[str, dict[str, frozenset[int]]]:
    """nodeid → path → executed lines, from the run's ``test_lines`` (``<import>`` is test id 0)."""
    ids = store.dictionary(repo_id)
    by_file = {row["file_id"]: row["path"] for row in store.read_table(repo_id, run_id, "files").to_pylist()}
    table = store.read_table(repo_id, run_id, "test_lines")
    acc: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    for test_id, file_id, line in zip(
        table.column("test_id").to_pylist(),
        table.column("file_id").to_pylist(),
        table.column("line").to_pylist(),
        strict=True,
    ):
        acc[test_id][file_id].add(line)
    out: dict[str, dict[str, frozenset[int]]] = {}
    for test_id, files in acc.items():
        nodeid = IMPORT_CONTEXT if test_id == 0 else ids.nodeid(test_id)
        out[nodeid] = {by_file[f]: frozenset(lines) for f, lines in files.items() if f in by_file}
    return out
