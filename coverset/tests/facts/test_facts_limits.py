"""The limit profiles of the Python reader: only the files read are counted, every cap breach names
its cap, the local profile lifts the caps, and coverage.sqlite is decoded pair by pair."""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
from coverage.numbits import nums_to_numbits

from coverset.facts.convert import read_python_bundle
from coverset.facts.untrusted import BundleLimitError, BundleLimits, extract_bundle

KNOWN = (
    "coverage.sqlite",
    "coverage.json",
    "shapes.jsonl.gz",
    "results.jsonl",
    "collect.json",
    "durations.json",
    "invariant_links.jsonl",
    "meta.json",
)


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.truncate(size)


@pytest.mark.security
def test_only_the_files_read_count_against_the_byte_cap(bundle_copy: Path) -> None:
    over = BundleLimits().max_bundle_bytes + 1
    _sparse(bundle_copy / "bundle.tar.gz", over)
    _sparse(bundle_copy / "neutral" / "coverage.jsonl", over)
    bundle = read_python_bundle(bundle_copy)  # the strict limits
    read = sum((bundle_copy / name).stat().st_size for name in KNOWN if (bundle_copy / name).exists())
    assert bundle.meta["convert"]["bundle_bytes"] == read
    _sparse(bundle_copy / "durations.json", over)  # a file that is read
    with pytest.raises(BundleLimitError, match="max_bundle_bytes of the strict limits") as caught:
        read_python_bundle(bundle_copy)
    assert caught.value.cap == "max_bundle_bytes" and caught.value.limit == over - 1
    assert read_python_bundle(bundle_copy, limits=BundleLimits.local()).meta["convert"]["bundle_bytes"] > over


@pytest.mark.security
def test_cap_breaches_name_their_cap(bundle_copy: Path, tmp_path: Path) -> None:
    with pytest.raises(BundleLimitError) as caught:
        read_python_bundle(bundle_copy, limits=BundleLimits(max_coverage_cells=25))
    assert caught.value.cap == "max_coverage_cells" and caught.value.limit == 25
    assert caught.value.profile == "strict" and "max_coverage_cells of the strict limits" in str(caught.value)
    with pytest.raises(BundleLimitError) as caught:
        read_python_bundle(bundle_copy, limits=BundleLimits(max_tests=2))
    assert caught.value.cap == "max_tests" and caught.value.value == 3
    with pytest.raises(BundleLimitError) as caught:
        read_python_bundle(bundle_copy, limits=BundleLimits(max_paths=1))
    assert caught.value.cap == "max_paths"
    archive = tmp_path / "b.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(bundle_copy.iterdir()):
            tar.add(path, arcname=path.name)
    with pytest.raises(BundleLimitError) as caught:
        extract_bundle(archive, tmp_path / "x", BundleLimits(max_bundle_bytes=100))
    assert caught.value.cap == "max_bundle_bytes" and caught.value.value == archive.stat().st_size
    with pytest.raises(BundleLimitError) as caught:
        extract_bundle(archive, tmp_path / "y", BundleLimits(max_members=2))
    assert caught.value.cap == "max_members"
    with pytest.raises(BundleLimitError) as caught:
        extract_bundle(archive, tmp_path / "z", BundleLimits(gzip_floor_bytes=1024, gzip_ratio=1))
    assert caught.value.cap == "gzip_ratio"


def _db(tmp_path: Path, *, unique: bool) -> tuple[Path, sqlite3.Connection]:
    bundle_dir = tmp_path / "db"
    bundle_dir.mkdir()
    db = sqlite3.connect(bundle_dir / "coverage.sqlite")
    constraint = ", unique (file_id, context_id)" if unique else ""
    db.executescript(
        "CREATE TABLE file (id integer primary key, path text);"
        "CREATE TABLE context (id integer primary key, context text);"
        f"CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob{constraint});"
        "CREATE TABLE arc (file_id integer, context_id integer, fromno integer, tono integer);"
    )
    db.execute("INSERT INTO file VALUES (1, 'src/m.py'), (2, 'src/n.py')")
    db.execute("INSERT INTO context VALUES (1, 't.py::a'), (2, 't.py::b')")
    rows = [{"nodeid": n, "outcome": "passed"} for n in ("t.py::a", "t.py::b")]
    (bundle_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return bundle_dir, db


def test_pairs_seen_twice_are_merged(tmp_path: Path) -> None:
    """A pair in line_bits twice (a table without its unique index) and in arc too: one union."""
    bundle_dir, db = _db(tmp_path, unique=False)
    db.execute("INSERT INTO line_bits VALUES (1, 1, ?)", [nums_to_numbits([1, 2, 300])])
    db.execute("INSERT INTO line_bits VALUES (1, 1, ?)", [nums_to_numbits([2, 3])])
    db.executemany(
        "INSERT INTO arc VALUES (?, ?, ?, ?)",
        [(1, 1, -1, 7), (1, 1, 7, 8), (1, 1, 8, -1), (2, 2, -1, 300), (2, 2, 300, -2), (2, 2, "x", 4)],
    )
    db.commit()
    db.close()
    bundle = read_python_bundle(bundle_dir)
    assert bundle.coverage == {"t.py::a": {"src/m.py": (1, 2, 3, 7, 8, 300)}, "t.py::b": {"src/n.py": (300,)}}
    assert bundle.meta["convert"]["dropped"]["coverage.bad_arcs"] == 1
    # one int object per line number across the whole bundle (8 bytes a cell, not 36)
    a, b = bundle.coverage["t.py::a"]["src/m.py"][-1], bundle.coverage["t.py::b"]["src/n.py"][0]
    assert a == b == 300 and a is b


def test_the_local_profile_reads_what_the_strict_one_refuses(tmp_path: Path) -> None:
    bundle_dir, db = _db(tmp_path, unique=True)
    lines = range(1, 3001)
    db.execute("INSERT INTO line_bits VALUES (1, 1, ?)", [nums_to_numbits(lines)])
    db.commit()
    db.close()
    strict = BundleLimits(max_coverage_cells=1000)
    with pytest.raises(BundleLimitError, match="cells"):
        read_python_bundle(bundle_dir, limits=strict)
    bundle = read_python_bundle(bundle_dir, limits=BundleLimits.local())
    assert bundle.coverage["t.py::a"]["src/m.py"] == tuple(lines)
