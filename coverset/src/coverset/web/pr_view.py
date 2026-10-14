"""A PR run's diff, read back for the PR page: changed functions, patch coverage and hunks.

The orchestrator stores the verdict summary but not the classified changes, so the page recomputes
them the same way the planner does (``git diff merge-base...head`` on the mirror, both sides' source
from ``git cat-file``, :func:`~coverset.analysis.diff.changed_functions`) and runs
:func:`~coverset.analysis.patch.patch_report` over the run's stored facts. The result is cached per
``(mirror, base, head, facts store, repo, run, run stamp)``: the stamp
(:func:`coverset.facts.cache.run_stamp`) changes whenever the run's facts do, so a re-ingested run
is analysed again, and the key never depends on the identity of the facts object (an evicted
object's ``id`` can be reused by a new one). An analysis made without the mirror, or one git failed,
is returned but not cached (:func:`_final`): a fresh replica (or an evicted cache) clones its
mirror in the background while the first pages render. Synchronous: call through ``asyncio.to_thread``.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from unidiff import PatchSet

from coverset.analysis.diff import FileDiff, GitDiffError, changed_functions, parse_diff, run_git_diff
from coverset.analysis.patch import PatchReport, patch_report
from coverset.facts.cache import RunStamp, run_stamp
from coverset.lang.base import FunctionChange
from coverset.lang.python import PythonAdapter
from coverset.orchestrator.repos import RepoError, merge_base, read_blobs
from coverset.web.facts_view import RunFacts

__all__ = ["DiffAnalysis", "analyse_diff"]

CACHE_SIZE = 16


@dataclass(slots=True)
class DiffAnalysis:
    """What changed between a PR's merge base and head, measured against one run's facts."""

    base: str | None
    head: str
    files: dict[str, FileDiff] = field(default_factory=dict)
    changes: list[FunctionChange] = field(default_factory=list)
    source_changes: list[FunctionChange] = field(default_factory=list)
    patch: PatchReport | None = None
    changed_lines: dict[str, set[int]] = field(default_factory=dict)
    removed: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    """Head-side anchor line → removed base lines shown just before it, per path."""
    head_sources: dict[str, str] = field(default_factory=dict)
    error: str | None = None


type _Key = tuple[str, str, str, str, int, str, RunStamp]

_cache: OrderedDict[_Key, DiffAnalysis] = OrderedDict()
_lock = threading.Lock()


def cache_key(mirror: Path, base_sha: str | None, head_sha: str, facts: RunFacts) -> _Key | None:
    """The cache key of an analysis: the commits, the run, and the run's current stamp (None for
    facts that live in no store, such as a report's in-memory bundle: those are not cached)."""
    store = getattr(facts, "store", None)
    if store is None:
        return None
    stamp = run_stamp(store.run_dir(facts.repo_id, facts.run_id))
    return (str(mirror), base_sha or "", head_sha, str(store.root), facts.repo_id, facts.run_id, stamp)


def analyse_diff(mirror: Path, base_sha: str | None, head_sha: str, facts: RunFacts) -> DiffAnalysis:
    """The (cached) diff analysis of ``base_sha...head_sha`` against ``facts``."""
    key = cache_key(mirror, base_sha, head_sha, facts)
    if key is None:
        return _analyse(mirror, base_sha, head_sha, facts)
    with _lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
            return found
    result = _analyse(mirror, base_sha, head_sha, facts)
    if not _final(mirror, base_sha, result):
        return result
    with _lock:
        _cache[key] = result
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def _final(mirror: Path, base_sha: str | None, result: DiffAnalysis) -> bool:
    """Whether ``result`` may be cached: not one computed without the mirror (a replica whose cache
    lost it renders the page while ``repos.schedule_mirror_restore`` clones it again) or one that
    failed on git (a commit not fetched yet). Either would otherwise stay on the PR page, with no
    changed functions, until the run's facts change; recomputing them costs one ``stat`` or one git
    call per request until the mirror is back."""
    if result.error is not None:
        return False
    return not base_sha or (mirror / "HEAD").is_file()


def _text(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _analyse(mirror: Path, base_sha: str | None, head_sha: str, facts: RunFacts) -> DiffAnalysis:
    if not base_sha or not (mirror / "HEAD").is_file():
        return DiffAnalysis(base=None, head=head_sha)
    base = merge_base(mirror, base_sha, head_sha) or base_sha
    try:
        text = run_git_diff(mirror, base, head_sha)
    except (GitDiffError, RepoError) as exc:
        return DiffAnalysis(base=base, head=head_sha, error=str(exc))
    files = parse_diff(text)
    py = {p: d for p, d in files.items() if p.endswith(".py") or d.base_path.endswith(".py")}
    try:
        base_blobs = read_blobs(mirror, base, [d.base_path for d in py.values()])
        head_blobs = read_blobs(mirror, head_sha, [d.path for d in py.values() if d.status != "deleted"])
    except RepoError as exc:
        return DiffAnalysis(base=base, head=head_sha, files=files, error=str(exc))
    base_tree = {p: _text(b) for p, b in base_blobs.items()}
    head_tree = {p: _text(b) for p, b in head_blobs.items()}
    changes = changed_functions(PythonAdapter(), base_tree, head_tree, py)  # type: ignore[arg-type]
    measured = set(facts.statics)
    source = [
        c for c in changes if any(side is not None and side.path in measured for side in (c.region, c.prev))
    ]
    changed_lines = {d.path: set(d.added) for d in files.values() if d.status != "deleted" and d.added}
    patch = None
    if measured:
        patch = patch_report(
            changed_lines,
            facts.statics,
            facts.union_lines(),
            source,
            lambda path, start, end: facts.tests_in_range(path, start, end, exclude_import=False),
        )
    return DiffAnalysis(
        base=base,
        head=head_sha,
        files=files,
        changes=changes,
        source_changes=source,
        patch=patch,
        changed_lines=changed_lines,
        removed=_removed(text),
        head_sources={p: t for p, t in head_tree.items() if t is not None},
    )


def _removed(text: str) -> dict[str, dict[int, list[str]]]:
    """Removed lines of every hunk, anchored to the head line they precede."""
    out: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    try:
        patches = PatchSet(io.StringIO(text))
    except Exception:  # unidiff raises its own parse errors; a diff we cannot parse shows no removals
        return {}
    for patched in patches:
        if patched.is_removed_file:
            continue
        path = patched.target_file.removeprefix("b/")
        for hunk in patched:
            removed = [line.value.rstrip("\n") for line in hunk if line.is_removed]
            if not removed:
                continue
            anchor = hunk.target_start if hunk.target_length else hunk.target_start + 1
            out[path][anchor].extend(removed)
    return {p: dict(v) for p, v in out.items()}
