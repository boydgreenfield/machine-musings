"""Retention of what upload mode keeps on disk: uploaded sources per commit and shard archives."""

from __future__ import annotations

from pathlib import Path

from coverset.facts.gc import remove_sources, remove_upload_dirs

SHA_A = "a" * 40
SHA_B = "b" * 40


def _sources(data_dir: Path, repo_id: int, sha: str) -> Path:
    target = data_dir / "sources" / str(repo_id) / sha / "pkg"
    target.mkdir(parents=True)
    (target / "mod.py").write_text("x = 1\n")
    return target.parent


def test_remove_sources_deletes_only_the_named_commits(tmp_path: Path) -> None:
    a, b = _sources(tmp_path, 3, SHA_A), _sources(tmp_path, 3, SHA_B)
    other = _sources(tmp_path, 4, SHA_A)
    assert remove_sources(tmp_path, 3, [SHA_A, "c" * 40], dry_run=True) == [SHA_A]
    assert a.is_dir()
    assert remove_sources(tmp_path, 3, [SHA_A]) == [SHA_A]
    assert not a.exists() and b.is_dir() and other.is_dir()
    assert remove_sources(tmp_path, 3, [SHA_B]) == [SHA_B]
    assert not (tmp_path / "sources" / "3").exists(), "an emptied repository directory goes too"


def test_remove_sources_ignores_bad_names_and_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    root = tmp_path / "data" / "sources" / "1"
    root.mkdir(parents=True)
    (root / SHA_A).symlink_to(outside, target_is_directory=True)
    assert remove_sources(tmp_path / "data", 1, [SHA_A, "../outside", "not-a-sha"]) == []
    assert (outside / "keep.txt").is_file()


def test_remove_upload_dirs_keeps_other_scratch(tmp_path: Path) -> None:
    for run_id in ("run_a", "run_b"):
        (tmp_path / run_id / "upload" / "shard-0").mkdir(parents=True)
        (tmp_path / run_id / "upload" / "shard-0" / "bundle.tar.gz").write_bytes(b"x")
    (tmp_path / "run_b" / "shard-0.tar.gz").write_bytes(b"managed scratch")
    assert remove_upload_dirs(tmp_path, ["run_a", "run_b", "run_c", "../evil"], dry_run=True) == [
        "run_a",
        "run_b",
    ]
    assert (tmp_path / "run_a" / "upload").is_dir()
    assert remove_upload_dirs(tmp_path, ["run_a", "run_b", "run_c"]) == ["run_a", "run_b"]
    assert not (tmp_path / "run_a").exists()
    assert not (tmp_path / "run_b" / "upload").exists() and (tmp_path / "run_b" / "shard-0.tar.gz").is_file()
