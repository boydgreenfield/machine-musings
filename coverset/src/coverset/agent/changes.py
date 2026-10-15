"""The change a run is measured against: a PR's diff, or a local work tree against a base ref.

For a facts-store PR run this is exactly the PR page's analysis
(:func:`coverset.web.pr_view.analyse_diff`: ``git diff merge-base...head`` on the mirror, both
sides' source from the mirror, :func:`~coverset.analysis.patch.patch_report` over the run's facts),
so ``get_patch_coverage`` and the PR page agree line for line.

For a local ``.coverset`` directory the agent is usually *about* to push: the change is the work
tree against the merge base of a ref it names (``origin/main``), uncommitted edits included. The
base side's text comes from git (``cat-file`` at the merge base), the head side's from the work tree
(only files git tracks, read under the same rules as ``coverset report``: no symlink escapes, size
caps). A changed file modified after the bundle was written is listed in ``stale``: its line numbers
may no longer match the coverage, and the right move is to run ``pytest --coverset`` again.

Untracked files never appear in ``git diff``; ``git add -N <file>`` makes a new file part of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coverset.agent.context import Changes
from coverset.analysis.diff import GitDiffError, changed_functions, parse_diff, validate_ref
from coverset.analysis.patch import patch_report
from coverset.lang.python import PythonAdapter
from coverset.orchestrator.repos import RepoError, git_env, read_blobs
from coverset.report.facts import read_sources
from coverset.report.load import tracked_files
from coverset.web.facts_view import RunFacts
from coverset.web.pr_view import analyse_diff, utf8_text

__all__ = ["store_changes", "worktree_changes"]

GIT_TIMEOUT_S = 60
DIFF_FLAGS = (
    "-c",
    "core.quotePath=false",
    "-c",
    "core.fsmonitor=false",
    "diff",
    "-M50%",
    "-C",
    "--diff-filter=ACDMR",
    "-U0",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
"""The flags :func:`coverset.analysis.diff.run_git_diff` uses (rename detection, zero context,
no external diff or textconv programs), plus the file-system monitor off."""


def store_changes(
    mirror: Path, base_sha: str | None, head_sha: str, facts: RunFacts, *, complete: bool
) -> Changes:
    """A facts-store PR run's change: the PR page's diff analysis, as :class:`Changes`."""
    found = analyse_diff(mirror, base_sha, head_sha, facts)
    notes = []
    if not complete:
        notes.append(
            "The run is missing shards: changed lines no test ran may still be run by a missing shard "
            "(treat them as unknown, not uncovered)."
        )
    error = found.error
    if error is None and found.base is None:
        error = "this server has no mirror of the repository (or the run has no base commit)"
    return Changes(
        base=found.base,
        head=found.head,
        changed_lines={p: set(v) for p, v in found.changed_lines.items()},
        changes=list(found.source_changes),
        patch=found.patch,
        complete=complete,
        error=error,
        notes=notes,
    )


def _git(root: Path, *args: str) -> bytes:
    """Run git in ``root`` with the mirrors' scrubbed environment (no user or system config, so the
    repository's own config cannot start a program); raise :class:`GitDiffError` on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            env=git_env(),
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitDiffError(f"git {args[0]} failed: {type(exc).__name__}") from None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise GitDiffError(f"git {' '.join(a for a in args if not a.startswith('-'))[:80]} failed: {stderr}")
    return proc.stdout


def worktree_changes(root: Path, base: str, facts: RunFacts, *, bundle_mtime: float | None) -> Changes:
    """The work tree under ``root`` against the merge base of ``base`` and ``HEAD``.

    Raises :class:`GitDiffError` when ``base`` is not a valid ref or ``root`` is not a git work tree.
    """
    root = Path(root).resolve()
    validate_ref(base, repo=root)
    merge = _git(root, "merge-base", "--end-of-options", base, "HEAD").decode().strip()
    if len(merge) != 40:
        raise GitDiffError(f"{base} and HEAD share no history")
    text = _git(root, *DIFF_FLAGS, merge, "--").decode("utf-8", "surrogateescape")
    files = parse_diff(text)
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").decode().strip())
    py = {p: d for p, d in files.items() if p.endswith(".py") or d.base_path.endswith(".py")}
    try:
        base_blobs = read_blobs(git_dir, merge, [d.base_path for d in py.values()])
    except RepoError as exc:
        raise GitDiffError(str(exc)) from None
    base_tree = {p: utf8_text(b) for p, b in base_blobs.items()}
    tracked = tracked_files(root)
    head_tree = read_sources(root, [d.path for d in py.values() if d.status != "deleted"], tracked=tracked)
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
    stale: list[str] = []
    if bundle_mtime is not None:
        for path in sorted(changed_lines):
            try:
                if (root / path).stat().st_mtime > bundle_mtime:
                    stale.append(path)
            except OSError:
                continue
    notes = [f"diff: working tree against the merge base of {base} and HEAD ({merge[:12]})"]
    if stale:
        notes.append(
            f"{len(stale)} changed file(s) were modified after the tests ran; their line numbers may not "
            "match the coverage: run `pytest --coverset` again."
        )
    if any(not p.endswith(".py") for p in changed_lines):
        notes.append("Changed functions are classified for Python files only; other files count lines only.")
    return Changes(
        base=merge,
        head="working tree",
        changed_lines=changed_lines,
        changes=source,
        patch=patch,
        stale=stale,
        notes=notes,
    )
