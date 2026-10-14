"""Where the agent tools read runs from: a local ``.coverset`` directory, or the facts store.

:class:`LocalBackend` serves one bundle the user produced (``pytest --coverset``'s output
directory, a ``bundle.tar.gz`` or a neutral bundle): one pseudo-repository with one run,
``local``. It is read with the static report's loader (:mod:`coverset.report.load`),
so the trust rules are the report's: a plugin directory is read under the local limits, an archive
or a neutral bundle under the strict ones unless ``trusted``; function regions are those of the
tested text (indexed from the work tree while it is unchanged since the run, else taken from the
neutral copy the plugin wrote); sources are read only from ``repo_root`` (the work tree the bundle sits in by default) and only files git
tracks there. The bundle is re-read when its files change, so ``pytest --coverset`` then another
tool call sees the new run.

:class:`StoreBackend` serves the control plane's runs: run metadata from the database, facts from
the Parquet store (the pages' cached :func:`~coverset.web.facts_view.run_facts`), sources from the
repository's mirror at the run's head commit. Every repository it touches goes through the
``authorize`` hook first (the HTTP API passes :func:`~coverset.web.principal.require_repo`, so an
unseen repository answers 404 exactly like an unknown one), and ``list_repos`` through ``visible``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy import select
from werkzeug.exceptions import HTTPException

from coverset.agent import economy
from coverset.agent.changes import store_changes, worktree_changes
from coverset.agent.context import Changes, RunContext, RunInfo
from coverset.agent.tools import ToolError
from coverset.analysis.diff import GitDiffError
from coverset.facts.untrusted import BundleError
from coverset.lang.base import FunctionRegion
from coverset.report.facts import (
    MAX_SOURCE_BYTES,
    ReportInput,
    TestInfo,
    bundle_regions,
    decode_source,
    read_sources,
)
from coverset.report.load import (
    COVERAGE_SQLITE,
    MANIFEST,
    NEUTRAL_SUBDIR,
    git_toplevel,
    input_from_bundle,
    is_plugin_dir,
    read_source,
    source_access,
    tracked_files,
)

__all__ = ["Backend", "LocalBackend", "StoreBackend", "run_kind", "shipped_functions"]

LOCAL_RUN = "local"
CONTEXT_CACHE = 3
"""Run contexts a store backend keeps by default (each holds a run's facts and derived indexes).

Kept low on purpose: a context for a 5,000-test run, once its cover index, function views and
economy data are built, is several hundred MB (the mainline bundle peaked at about 720 MB), and
the cache is shared by every request of a deployed instance. ``COVERSET_AGENT_CONTEXTS`` raises
or lowers it for a host with more or less memory.
"""
CONTEXT_CACHE_ENV = "COVERSET_AGENT_CONTEXTS"


def context_cache_size() -> int:
    """How many store contexts to keep: ``COVERSET_AGENT_CONTEXTS`` when it is a positive integer.

    The app's configured value (``Settings.agent_contexts``, handed to
    :func:`coverset.facts.cache.configure` by ``create_app``) wins over the environment.
    """
    from coverset.facts.cache import setting

    raw = setting(CONTEXT_CACHE_ENV).strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return CONTEXT_CACHE


class Backend(Protocol):
    """What :class:`~coverset.agent.service.AgentService` needs from a source of runs."""

    label: str

    async def list_repos(self) -> list[dict[str, Any]]:
        """The repositories this caller may read."""
        ...

    async def list_runs(self, repo: str | None, kind: str, limit: int) -> list[dict[str, Any]]:
        """Recent runs of one repository, newest first."""
        ...

    async def context(self, run: str | None, repo: str | None) -> RunContext:
        """The run named by ``run`` (or the default run of ``repo``), loaded and cached."""
        ...

    async def failure_message(self, ctx: RunContext, nodeid: str) -> tuple[str | None, str | None]:
        """``(message, failed_phase)`` of a test's first failing attempt, when recorded."""
        ...


def run_kind(trigger: str | None, pr_id: int | None) -> str:
    """The ``kind`` a run is listed with: ``pr`` for any run of a pull request, else its trigger."""
    if pr_id is not None:
        return "pr"
    return trigger or "manual"


def _run_stamp(store: Any, repo_id: int, run_id: str) -> tuple[tuple[str, int, int], ...]:
    """The run directory's Parquet files with mtimes and sizes: the facts' cache key (empty: no facts).

    The same stamp :func:`coverset.web.facts_view.run_facts` keys its cache on, so a run that is
    still ingesting is re-read and a finished one is read once."""
    run_dir = store.run_dir(repo_id, run_id)
    if not run_dir.is_dir():
        return ()
    entries = []
    for entry in run_dir.iterdir():
        if entry.name.endswith(".parquet"):
            st = entry.stat()
            entries.append((entry.name, st.st_mtime_ns, st.st_size))
    return tuple(sorted(entries))


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# -- a local bundle ----------------------------------------------------------------------------------


class LocalBackend:
    """One local bundle (see the module docstring)."""

    def __init__(self, source: Path, *, repo_root: Path | None = None, trusted: bool = False) -> None:
        self.source = Path(source).expanduser().resolve()
        if not self.source.exists():
            raise ToolError("not_found", f"{self.source} does not exist: run `pytest --coverset` first")
        if repo_root is None and self.source.is_dir():
            repo_root = git_toplevel(self.source.parent)
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self.trusted = trusted
        self.label = f"local bundle {self.source}"
        self._lock = threading.Lock()
        self._loading = asyncio.Lock()
        self._ctx: RunContext | None = None

    @property
    def name(self) -> str:
        """The pseudo-repository's name: the work tree's directory name, else the bundle's parent's."""
        base = self.repo_root if self.repo_root is not None else self.source.parent
        return base.name or str(base)

    def stamp(self) -> tuple[tuple[str, int, int], ...]:
        """mtimes and sizes of the files a reload would read (the cache key)."""
        candidates = [self.source]
        if self.source.is_dir():
            candidates = [
                self.source / COVERAGE_SQLITE,
                self.source / "results.jsonl",
                self.source / "meta.json",
                self.source / MANIFEST,
                self.source / NEUTRAL_SUBDIR / MANIFEST,
            ]
        out = []
        for path in candidates:
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path.name, st.st_mtime_ns, st.st_size))
        return tuple(out)

    def _mtime(self) -> float | None:
        stamps = self.stamp()
        return max(s[1] for s in stamps) / 1e9 if stamps else None

    async def list_repos(self) -> list[dict[str, Any]]:
        """One pseudo-repository: this bundle."""
        mtime = self._mtime()
        return [
            {
                "id": 0,
                "name": self.name,
                "default_branch": None,
                "private": True,
                "language": None,
                "source": str(self.source),
                "repo_root": str(self.repo_root) if self.repo_root else None,
                "latest_run": {"id": LOCAL_RUN, "created_at": _iso(datetime.fromtimestamp(mtime, UTC))}
                if mtime
                else None,
            }
        ]

    async def list_runs(self, repo: str | None, kind: str, limit: int) -> list[dict[str, Any]]:
        """The one run of the bundle (none when ``kind`` asks for PR runs or baselines)."""
        if kind != "any":
            return []
        ctx = await self.context(None, repo)
        info = ctx.info
        return [
            {
                "id": info.run_id,
                "kind": info.kind,
                "branch": info.branch,
                "pr_number": None,
                "head_sha": info.head_sha,
                "created_at": info.created_at,
                "status": info.status,
                "verdict": info.verdict,
            }
        ]

    async def context(self, run: str | None, repo: str | None) -> RunContext:
        """The bundle's run (re-read when its files changed since the last call)."""
        if run not in (None, LOCAL_RUN, "latest"):
            raise ToolError(
                "not_found", f"a local bundle has one run, {LOCAL_RUN!r}; there is no run {run!r}"
            )
        # One load at a time: an agent's parallel first calls wait for it instead of each reading
        # the bundle.
        async with self._loading:
            stamp = self.stamp()
            with self._lock:
                if self._ctx is not None and self._ctx.stamp == stamp:
                    return self._ctx
            ctx = await asyncio.to_thread(self._load, stamp)
            with self._lock:
                self._ctx = ctx
            return ctx

    def _load(self, stamp: object) -> RunContext:
        try:
            report = self._read()
        except BundleError as exc:
            raise ToolError("unavailable", f"cannot read {self.source}: {exc}") from None
        meta = report.meta.get("meta") if isinstance(report.meta.get("meta"), dict) else {}
        counts = meta.get("counts") if isinstance(meta, dict) else None
        failed = isinstance(counts, dict) and (counts.get("failed") or counts.get("error"))
        mtime = self._mtime()
        info = RunInfo(
            run_id=LOCAL_RUN,
            repo=self.name,
            kind="local",
            head_sha=None,
            created_at=_iso(datetime.fromtimestamp(mtime, UTC)) if mtime else None,
            status="completed",
            verdict=("failure" if failed else "success") if isinstance(counts, dict) else None,
            coverage_complete=True,
            origin=str(self.source),
        )
        sources = report.sources
        root = self.repo_root
        facts = report.facts
        bundle_mtime = mtime

        def changes(base: str | None) -> Changes | None:
            if not base:
                return None
            if root is None:
                raise ToolError(
                    "unavailable",
                    "this bundle is not inside a git work tree, so there is no diff to measure "
                    "(start the server with --repo-root)",
                )
            try:
                return worktree_changes(root, base, facts, bundle_mtime=bundle_mtime)
            except GitDiffError as exc:
                raise ToolError("invalid_argument", str(exc)) from None

        tracked: list[frozenset[str] | None] = []

        def load_sources(paths: Iterable[str]) -> dict[str, str | None]:
            """The report's text for measured files; other files (tests, conftests, helpers) from
            the work tree, git-tracked files only, read under the report's rules."""
            paths = list(paths)
            out = {p: sources.get(p) for p in paths}
            rest = [p for p in paths if p not in sources]
            if rest and root is not None:
                if not tracked:
                    tracked.append(tracked_files(root))
                out.update(read_sources(root, rest, tracked=tracked[0]))
            return out

        notes = []
        if report.notice:
            notes.append(report.notice)
        ctx = RunContext(
            info,
            facts,
            report.tests,
            source_loader=load_sources,
            changes_loader=changes,
            cache_changes=False,
            stamp=stamp,
        )
        ctx.cache["notes"] = notes
        ctx.cache["messages"] = {t.nodeid: (t.message, t.failed_phase) for t in report.tests if t.message}
        ctx.cache["economy_loader"] = lambda: economy.from_bundle(ctx, ctx.source)
        return ctx

    def _read(self) -> ReportInput:
        """The bundle as the static report reads it, with function regions of the tested text.

        A ``pytest --coverset`` directory's raw files carry no function regions: they are indexed
        from the work tree's text, which is the tested text unless a measured file changed after the
        run. Then (as ``coverset report --compare`` does for a base) the regions the plugin indexed
        from the tested text are used instead: the ``functions`` rows of its neutral copy, read on
        their own (:func:`shipped_functions`), since reading the whole copy costs more than the run's
        own files on a large suite."""
        read = read_source(self.source, trusted=self.trusted)
        bundle, kind = read.bundle, read.kind
        access = source_access(self.source, kind, bundle.static, self.repo_root)
        stale = self._changed_since_run(access.root, bundle.static)
        if stale and not bundle_regions(bundle) and is_plugin_dir(self.source, kind):
            regions, types = shipped_functions(self.source)
            if regions:
                bundle = dataclasses.replace(bundle, functions=regions, types=bundle.types or types)
        return input_from_bundle(
            bundle,
            source=self.source,
            kind=kind,
            access=access,
            title=self.name,
            prefer_shipped=stale,
            read_from=read.read_from,
            limits=read.limits,
        )

    def _changed_since_run(self, root: Path | None, paths: Iterable[str]) -> bool:
        """Whether any measured file under ``root`` is missing or newer than the bundle (True when
        there is no root: then the shipped regions are all there is)."""
        mtime = self._mtime()
        if root is None or mtime is None:
            return True
        for path in paths:
            try:
                if (root / path).stat().st_mtime > mtime:
                    return True
            except OSError:
                return True
        return False

    async def failure_message(self, ctx: RunContext, nodeid: str) -> tuple[str | None, str | None]:
        """The bundle's own failure text (``message`` in ``tests.jsonl``, pytest's ``longrepr``)."""
        found: Mapping[str, tuple[str | None, str | None]] = ctx.cache.get("messages", {})
        return found.get(nodeid, (None, None))


MAX_FUNCTIONS_BYTES = 256 * 1024 * 1024
"""Decompressed size cap of a neutral copy's ``functions.jsonl`` (the user's own output)."""


def shipped_functions(source: Path) -> tuple[tuple[FunctionRegion, ...], dict[Any, Any]]:
    """The function regions (and declared types, when shipped) of the neutral copy a
    ``pytest --coverset`` directory carries: its ``functions.jsonl[.gz]`` alone, under a size cap,
    no symlinks. Empty when absent, oversized or unreadable (the caller then indexes the work
    tree's text, as the report does)."""
    root = Path(source) / NEUTRAL_SUBDIR
    for name in ("functions.jsonl.gz", "functions.jsonl"):
        path = root / name
        if path.is_file() and not path.is_symlink() and not root.is_symlink():
            break
    else:
        return (), {}
    try:
        opener: Any = gzip.open if name.endswith(".gz") else open
        with opener(path, "rb") as fh:
            data = fh.read(MAX_FUNCTIONS_BYTES + 1)
    except (OSError, EOFError, gzip.BadGzipFile):
        return (), {}
    if len(data) > MAX_FUNCTIONS_BYTES:
        return (), {}
    rows: list[dict[str, Any]] = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    regions = tuple(bundle_regions(SimpleNamespace(functions=rows)))
    types = {
        (r.get("path"), r.get("qualname"), r.get("def_line")): r["types"]
        for r in rows
        if isinstance(r.get("types"), dict)
    }
    return regions, types


# -- the facts store ---------------------------------------------------------------------------------


class StoreBackend:
    """The control plane's runs (see the module docstring)."""

    def __init__(
        self,
        settings: Any,
        db: Any,
        *,
        authorize: Callable[[int], None] | None = None,
        visible: Callable[[list[int]], list[int]] | None = None,
        default_repo: str | None = None,
        default_run: str | None = None,
    ) -> None:
        from coverset.facts.store import facts_store

        self.settings = settings
        self.db = db
        self.store = facts_store(settings)
        self._authorize = authorize
        self._visible = visible
        self.default_repo = default_repo
        self.default_run = default_run
        self.label = "facts store"

    # One cache for the process: contexts are pure functions of (run, facts stamp), so callers with
    # different principals share them; authorization happens before a context is looked up.
    _contexts: OrderedDict[tuple[str, int, str, object], RunContext] = OrderedDict()
    _lock = threading.Lock()

    def authorize(self, repo_id: int) -> None:
        """Raise ``not_found`` unless the caller may read ``repo_id`` (no hook: everything)."""
        if self._authorize is None:
            return
        try:
            self._authorize(repo_id)
        except ToolError:
            raise
        except HTTPException:  # require_repo's NotFound (or Forbidden): never say which
            raise ToolError("not_found", "no such repository or run") from None

    async def _repo(self, session: Any, spec: str | None) -> Any:
        from coverset.db.models import Repo

        spec = spec or self.default_repo
        repo = None
        if spec is None:
            repos = list(await session.scalars(select(Repo).order_by(Repo.id)))
            visible = [r for r in repos if self._may_read(r.id)]
            if len(visible) == 1:
                repo = visible[0]
            else:
                names = [r.full_name for r in visible][:50]
                raise ToolError(
                    "invalid_argument", "pass `repo` (more than one repository)", {"repos": names}
                )
        elif spec.isdigit():
            repo = await session.get(Repo, int(spec))
        if repo is None and spec is not None:
            repo = await session.scalar(select(Repo).where(Repo.full_name == spec))
        if repo is None:
            raise ToolError("not_found", "no such repository or run")
        self.authorize(repo.id)
        return repo

    def _may_read(self, repo_id: int) -> bool:
        try:
            self.authorize(repo_id)
        except ToolError:
            return False
        return True

    async def list_repos(self) -> list[dict[str, Any]]:
        """Every repository the caller may read, with its newest run."""
        from coverset.db.models import Repo, Run

        async with self.db.session() as session:
            repos = list(await session.scalars(select(Repo).order_by(Repo.full_name)))
            ids = [r.id for r in repos]
            allowed = set(self._visible(ids) if self._visible is not None else ids)
            out = []
            for repo in repos:
                if repo.id not in allowed or not self._may_read(repo.id):
                    continue
                latest = await session.scalar(
                    select(Run).where(Run.repo_id == repo.id).order_by(Run.created_at.desc()).limit(1)
                )
                out.append(
                    {
                        "id": repo.id,
                        "name": repo.full_name,
                        "default_branch": repo.default_branch,
                        "private": bool(repo.private),
                        "language": repo.language,
                        "latest_run": {
                            "id": latest.id,
                            "kind": run_kind(latest.trigger, latest.pr_id),
                            "status": latest.status,
                            "verdict": latest.verdict,
                            "created_at": _iso(latest.created_at),
                        }
                        if latest is not None
                        else None,
                    }
                )
                if len(out) >= 200:
                    break
        return out

    async def list_runs(self, repo: str | None, kind: str, limit: int) -> list[dict[str, Any]]:
        """The newest runs of ``repo``: every run, only PR runs, or only default-branch baselines."""
        from coverset.db.models import PullRequest, Run

        async with self.db.session() as session:
            found = await self._repo(session, repo)
            query = select(Run, PullRequest.number).outerjoin(PullRequest, PullRequest.id == Run.pr_id)
            query = query.where(Run.repo_id == found.id)
            if kind == "pr":
                query = query.where(Run.pr_id.is_not(None))
            elif kind == "baseline":
                query = query.where(
                    Run.pr_id.is_(None),
                    Run.branch == found.default_branch,
                    Run.trigger.in_(("baseline", "push")),
                )
            rows = (await session.execute(query.order_by(Run.created_at.desc()).limit(limit))).all()
            return [
                {
                    "id": run.id,
                    "kind": run_kind(run.trigger, run.pr_id),
                    "branch": run.branch,
                    "pr_number": number,
                    "head_sha": run.head_sha,
                    "created_at": _iso(run.created_at),
                    "status": run.status,
                    "verdict": run.verdict,
                }
                for run, number in rows
            ]

    async def context(self, run: str | None, repo: str | None) -> RunContext:
        """The run's context: authorized, then loaded (or found in the cache by its facts stamp)."""
        from coverset.db.models import PullRequest, Repo, Run, TestCase, TestResult
        from coverset.web.api import RUN_ID_PARAM
        from coverset.web.loaders import default_run

        run = run or self.default_run
        async with self.db.session() as session:
            if run in (None, "latest"):
                found_repo = await self._repo(session, repo)
                row = await default_run(session, found_repo)
                if row is None:
                    raise ToolError("not_found", f"{found_repo.full_name} has no completed run yet")
            else:
                if not RUN_ID_PARAM.fullmatch(run):
                    raise ToolError("invalid_argument", "that is not a run id")
                row = await session.get(Run, run)
                if row is None:
                    raise ToolError("not_found", "no such repository or run")
                self.authorize(row.repo_id)
                if repo is not None:
                    named = await self._repo(session, repo)
                    if named.id != row.repo_id:
                        raise ToolError("not_found", "no such repository or run")
                found_repo = await session.get(Repo, row.repo_id)
            stamp = await asyncio.to_thread(_run_stamp, self.store, row.repo_id, row.id)
            if not stamp:
                raise ToolError(
                    "not_found", f"run {row.id} has no facts (status {row.status}); pick a completed run"
                )
            key = (str(self.store.root), row.repo_id, row.id, stamp)
            with self._lock:
                cached = self._contexts.get(key)
                if cached is not None:
                    self._contexts.move_to_end(key)
                    return cached
            pr = await session.get(PullRequest, row.pr_id) if row.pr_id is not None else None
            results = (
                await session.execute(
                    select(TestCase.nodeid, TestResult)
                    .join(TestCase, TestCase.id == TestResult.test_id)
                    .where(TestResult.run_id == row.id)
                )
            ).all()
            tests = [
                TestInfo(
                    nodeid=nodeid,
                    file=nodeid.split("::", 1)[0],
                    outcome="not run" if result.outcome == "not_run" else str(result.outcome),
                    duration_s=(result.duration_ms / 1000.0) if result.duration_ms is not None else None,
                    failed_phase=str(result.failed_phase) if result.failed_phase else None,
                )
                for nodeid, result in results
            ]
            info = RunInfo(
                run_id=row.id,
                repo=found_repo.full_name if found_repo else f"repo {row.repo_id}",
                repo_id=row.repo_id,
                kind=run_kind(row.trigger, row.pr_id),
                branch=row.branch,
                head_sha=row.head_sha,
                base_sha=row.base_sha,
                pr_number=pr.number if pr else None,
                pr_title=pr.title if pr else None,
                created_at=_iso(row.created_at),
                status=row.status,
                verdict=row.verdict,
                reason=row.reason,
                executor=row.executor,
                coverage_complete=bool(row.coverage_complete),
                origin="facts store",
            )
            language = found_repo.language if found_repo else None
        ctx = await asyncio.to_thread(self._build, info, tests, language, stamp)
        with self._lock:
            self._contexts[key] = ctx
            keep = context_cache_size()
            while len(self._contexts) > keep:
                self._contexts.popitem(last=False)
        return ctx

    def _build(self, info: RunInfo, tests: list[TestInfo], language: str | None, stamp: object) -> RunContext:
        from coverset.facts.queries import test_history
        from coverset.orchestrator.repos import RepoError, mirror_path, read_blobs
        from coverset.web.facts_view import run_facts

        assert info.repo_id is not None
        repo_id, run_id, head = info.repo_id, info.run_id, info.head_sha or ""
        facts = run_facts(self.store, repo_id, run_id)
        known = {t.nodeid for t in tests}
        for row in self.store.read_table(repo_id, run_id, "tests_dim").to_pylist():
            nodeid = row["nodeid"]
            if nodeid and nodeid not in known and row["test_id"] != 0:
                known.add(nodeid)
                outcome = row["outcome"]
                tests.append(
                    TestInfo(
                        nodeid,
                        nodeid.split("::", 1)[0],
                        "not run" if outcome == "not_run" else outcome,
                        row["duration_s"],
                    )
                )
        mirror = mirror_path(self.settings, repo_id)

        def sources(paths: Any) -> dict[str, str | None]:
            wanted = [p for p in paths if p in facts.statics or p in facts.paths]
            try:
                blobs = read_blobs(mirror, head, wanted) if head else {}
            except RepoError:
                blobs = {}
            return {
                p: decode_source(blobs[p], p) if p in blobs and len(blobs[p]) <= MAX_SOURCE_BYTES else None
                for p in paths
            }

        def history(nodeid: str, last_n: int) -> list[dict[str, Any]]:
            rows = test_history(self.store, repo_id, nodeid, last_n)
            return [
                {
                    "run_id": r.run_id,
                    "created_at": _iso(r.created_at),
                    "branch": r.branch,
                    "pr_number": r.pr_number,
                    "head_sha": r.head_sha,
                    "outcome": r.outcome,
                    "duration_s": r.duration_s,
                }
                for r in rows
            ]

        is_pr = info.kind == "pr" and bool(info.base_sha)

        def changes(base: str | None) -> Changes | None:
            if not is_pr:
                return None
            return store_changes(mirror, info.base_sha, head, facts, complete=bool(info.coverage_complete))

        ctx = RunContext(
            info,
            facts,
            tests,
            source_loader=sources,
            language=language if language and language != "python" else None,
            history_loader=history,
            changes_loader=changes,
            stamp=stamp,
        )
        ctx.cache["economy_loader"] = lambda: economy.from_store(self.store, repo_id, run_id)
        return ctx

    async def failure_message(self, ctx: RunContext, nodeid: str) -> tuple[str | None, str | None]:
        """The first attempt's ``longrepr`` from the run's ``test`` events.

        A passed test has none, so it costs no query. Otherwise the run's ``test`` events are
        read once per context into a nodeid -> (longrepr, phase) map holding only the attempts
        that carry a message: a 5,000-test run has 5,000+ event payloads, and rescanning them
        on every ``get_test`` (120 calls a minute per principal) would be the dominant cost.
        """
        info = ctx.tests.get(nodeid)
        if info is not None and info.outcome == "passed":
            return None, None
        failures: dict[str, tuple[str | None, str | None]] | None = ctx.cache.get("failure_messages")
        if failures is None:
            failures = await self._failure_messages(ctx.info.run_id)
            ctx.cache["failure_messages"] = failures
        return failures.get(nodeid, (None, None))

    async def _failure_messages(self, run_id: str) -> dict[str, tuple[str | None, str | None]]:
        from coverset.db.models import RunEvent

        async with self.db.session() as session:
            payloads = (
                await session.scalars(
                    select(RunEvent.payload)
                    .where(RunEvent.run_id == run_id, RunEvent.kind == "test")
                    .order_by(RunEvent.seq)
                )
            ).all()
        out: dict[str, tuple[str | None, str | None]] = {}
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get("retry") is not None:
                continue
            nodeid = payload.get("nodeid")
            if not isinstance(nodeid, str) or nodeid in out:
                continue
            text, phase = payload.get("longrepr"), payload.get("failed_phase")
            if isinstance(text, str) or isinstance(phase, str):
                out[nodeid] = (
                    text if isinstance(text, str) else None,
                    phase if isinstance(phase, str) else None,
                )
        return out
