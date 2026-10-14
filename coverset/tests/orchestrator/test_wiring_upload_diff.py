"""Upload mode classifies a diff with the adapter of the bundles' language: a Rust upload's changed
functions come from :meth:`~coverset.lang.rust.RustAdapter.changed_functions`, not from Python's."""

from __future__ import annotations

import shutil
from pathlib import Path

from orch_fakes import git

from coverset.db.models import Run
from coverset.lang.python import PythonAdapter
from coverset.lang.rust import RustAdapter
from coverset.orchestrator.repos import tree_paths
from coverset.orchestrator.upload import (
    _diff_adapter,
    _diff_for,
    _diff_language,
    _Head,
    _neutral_languages,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "bundles" / "neutral-rust"
MONEY = "src/money.rs"


def _rust_repo(root: Path) -> tuple[Path, str, str]:
    """The fixture crate committed as base, then a head that changes one function of money.rs."""
    shutil.copytree(FIXTURE / "repo", root)
    git(root, "init", "-q", "--initial-branch", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    text = (root / MONEY).read_text()
    marker = "pub fn allocate_cents"
    assert marker in text
    start = text.index(marker)
    body = text.index("{", start) + 1
    (root / MONEY).write_text(text[:body] + "\n    let _unused_marker = 0;" + text[body:])
    git(root, "commit", "-q", "-am", "change allocate_cents")
    return root, base, git(root, "rev-parse", "HEAD")


def test_the_diff_language_follows_the_uploaded_bundles(tmp_path: Path) -> None:
    rust = tmp_path / "shard-0"
    rust.mkdir()
    shutil.copy(FIXTURE / "bundle.tar.gz", rust / "neutral.tar.gz")
    python = tmp_path / "shard-1"
    python.mkdir()
    (python / "bundle.tar.gz").write_bytes(b"pytest bundle")
    assert _diff_language([rust]) == "rust"
    assert _diff_language([python]) == "python"
    assert _diff_language([rust, python]) == "python", "a mix keeps the Python classification"
    assert _diff_language([]) == "python"
    assert isinstance(_diff_adapter("rust"), RustAdapter)
    assert isinstance(_diff_adapter("python"), PythonAdapter)
    assert isinstance(_diff_adapter("cobol"), PythonAdapter)


def test_the_neutral_languages_are_read_once_and_reused(tmp_path: Path) -> None:
    """:func:`_neutral_languages` maps each shard with a neutral bundle to its language, and
    :func:`_diff_language` takes that map instead of re-reading the archives."""
    rust = tmp_path / "shard-0"
    rust.mkdir()
    shutil.copy(FIXTURE / "bundle.tar.gz", rust / "neutral.tar.gz")
    python = tmp_path / "shard-1"
    python.mkdir()
    (python / "bundle.tar.gz").write_bytes(b"pytest bundle")
    known = _neutral_languages([rust, python])
    assert known == {rust: "rust"}
    garbage = tmp_path / "shard-2"
    garbage.mkdir()
    (garbage / "neutral.tar.gz").write_bytes(b"not a tarball")
    assert _diff_language([garbage]) == "python", "read from the archive: unreadable is python"
    assert _diff_language([garbage], {garbage: "rust"}) == "rust", "the given map wins, no re-read"


async def test_a_rust_upload_diff_is_classified_by_the_rust_adapter(tmp_path: Path) -> None:
    repo, base, head = _rust_repo(tmp_path / "crate")
    mirror = repo / ".git"
    run = Run(repo_id=1, head_sha=head, base_sha=base, trigger="upload", status="running")
    state = _Head(repo, frozenset(tree_paths(mirror, head)), mirror, True, "mirror")
    diff = await _diff_for(state, run, RustAdapter())
    assert diff.ok and diff.merge_base == base
    assert MONEY in diff.changed_files and diff.changed_lines[MONEY]
    changed = {(c.region.path, c.region.qualname, c.kind) for c in diff.changes if c.region is not None}
    assert (MONEY, "allocate_cents", "modified") in changed
    assert all(path.endswith(".rs") for path, _, _ in changed)
    python = await _diff_for(state, run, PythonAdapter())
    assert python.ok and python.changes == [], "the Python adapter sees no function in a .rs file"
