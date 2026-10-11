"""Turn a report SOURCE into a :class:`~coverset.report.facts.ReportInput`.

Sources: a ``pytest --coverset-out`` directory, a ``bundle.tar.gz`` (extracted safely into a
temporary directory with :func:`~coverset.facts.untrusted.extract_bundle`), a neutral bundle
directory or ``tar.gz`` (``manifest.json``; read with ``coverset.lang.read_any_bundle``), or a run
in the facts store (:func:`load_run`). Source files are read from ``repo_root`` only (default: the
bundle's ``git_root``/``rootdir`` when that is an existing directory, else the current directory).
"""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from coverset.facts.convert import read_python_bundle
from coverset.facts.untrusted import BundleError, extract_bundle
from coverset.lang.base import IMPORT_CONTEXT, NeutralBundle
from coverset.report.facts import (
    BundleFacts,
    ReportInput,
    TestInfo,
    bundle_regions,
    bundle_types,
    catalogue_from_descriptors,
    read_sources,
    regions_for,
)

__all__ = ["input_from_bundle", "load_bundle", "load_run", "neutral_reader", "read_bundle_path"]

MANIFEST = "manifest.json"
ARCHIVE = "bundle.tar.gz"


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


def read_bundle_path(source: Path) -> tuple[NeutralBundle, str]:
    """Read a bundle directory or archive: ``(bundle, kind)`` where kind is ``python`` or ``neutral``."""
    source = Path(source)
    if source.is_dir():
        return _read_dir(source)
    if not source.is_file():
        raise BundleError(f"{source} is neither a bundle directory nor a bundle archive")
    with tempfile.TemporaryDirectory(prefix="coverset-report-") as tmp:
        root = extract_bundle(source, Path(tmp) / "bundle")
        inner = _single_subdir(root)
        return _read_dir(inner)


def _single_subdir(root: Path) -> Path:
    """The bundle root inside an extracted archive (flat members, or one top-level directory)."""
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return root


def _read_dir(root: Path) -> tuple[NeutralBundle, str]:
    archive = root / ARCHIVE
    if not (root / MANIFEST).is_file() and not (root / "coverage.sqlite").is_file() and archive.is_file():
        return read_bundle_path(archive)
    if (root / MANIFEST).is_file():
        reader = neutral_reader()
        if reader is None:
            raise BundleError(
                f"{root} is a neutral bundle ({MANIFEST}), but this build has no "
                "coverset.lang.read_any_bundle to read it"
            )
        try:
            return reader(root), "neutral"
        except _rejected() as exc:
            raise BundleError(f"neutral bundle refused: {exc}") from exc
    return read_python_bundle(root), "python"


def default_repo_root(meta: dict[str, Any]) -> Path:
    """The bundle's ``git_root`` (else ``rootdir``, ``repo_root``) when it is an existing directory,
    else the current directory."""
    for key in ("git_root", "rootdir", "repo_root"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value)
            if candidate.is_absolute() and candidate.is_dir():
                return candidate
    return Path.cwd()


def load_bundle(source: Path, *, repo_root: Path | None = None, title: str | None = None) -> ReportInput:
    """A report input from a bundle directory or archive, with sources read under ``repo_root``."""
    bundle, kind = read_bundle_path(source)
    return input_from_bundle(bundle, source=Path(source), kind=kind, repo_root=repo_root, title=title)


def input_from_bundle(
    bundle: NeutralBundle,
    *,
    source: Path,
    kind: str,
    repo_root: Path | None = None,
    title: str | None = None,
) -> ReportInput:
    """A report input from an in-memory bundle (``bundle.functions`` and ``bundle.types`` are used
    when the neutral model carries them)."""
    meta = dict(bundle.meta) if isinstance(bundle.meta, dict) else {}
    root = Path(repo_root) if repo_root is not None else default_repo_root(_flat_meta(meta))
    paths = sorted(bundle.static)
    sources = read_sources(root, paths)
    regions = regions_for(paths, sources, bundle)
    types = bundle_types(bundle, regions)
    # Declared types shipped in the bundle win over whatever the source parser would read: a
    # TypeScript file has no Python annotations, and a converted Python bundle's descriptors
    # came from those very annotations.
    catalogues = {
        r.func_key: catalogue_from_descriptors(r, types[r.func_key]) for r in regions if r.func_key in types
    }
    facts = BundleFacts(bundle, regions, catalogues=catalogues)
    coverage = {
        nodeid: {path: frozenset(lines) for path, lines in by_path.items()}
        for nodeid, by_path in bundle.coverage.items()
    }
    name = root.name or "repository"
    about = {
        "source": str(source),
        "bundle_kind": kind,
        "repo_root": str(root),
        "regions_from_bundle": bool(bundle_regions(bundle)),
        "types_from_bundle": len(types),
        "meta": meta,
    }
    return ReportInput(
        facts=facts,
        tests=tests_from_bundle(bundle),
        coverage=coverage,
        sources=sources,
        meta=about,
        title=title or f"Coverage report · {name}",
        origin=f"bundle {source.name}",
        types=types,
    )


def _flat_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """The plugin's meta (top level), or a neutral manifest's ``meta`` merged under it."""
    inner = meta.get("meta")
    return {**inner, **meta} if isinstance(inner, dict) else meta


def tests_from_bundle(bundle: NeutralBundle) -> list[TestInfo]:
    """Every test of the bundle: its result row, else its collected item (never ran)."""
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
            out[nodeid] = TestInfo(nodeid=nodeid, file=nodeid.split("::", 1)[0], outcome=None)
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
        if len(data) <= 4 * 1024 * 1024:
            sources[path] = data.decode("utf-8", "replace")
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
            outcome=str(result.outcome),
            duration_s=(result.duration_ms or 0.0) / 1000.0 if result.duration_ms is not None else None,
            failed_phase=str(result.failed_phase) if result.failed_phase else phase,
            message=text,
        )
    for row in store.read_table(repo_id, run_id, "tests_dim").to_pylist():
        nodeid = row["nodeid"]
        if nodeid and nodeid != IMPORT_CONTEXT and nodeid not in tests:
            text, phase = messages.get(nodeid, (None, None))
            tests[nodeid] = TestInfo(
                nodeid, nodeid.split("::", 1)[0], row["outcome"], row["duration_s"], phase, text
            )
    convert = [
        {"shard": row["shard_id"], "attempt": row["attempt"], "status": row["status"],
         "convert": _json_or_none(row["convert_json"])}
        for row in store.read_table(repo_id, run_id, "ingest_log").to_pylist()
    ]  # fmt: skip
    about = {
        "source": f"facts store run {run_id}",
        "bundle_kind": "facts store",
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
    )


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
