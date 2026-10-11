"""Untrusted-bundle limits: hostile files are dropped and counted or rejected, never crash convert."""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from coverage.numbits import nums_to_numbits
from facts_helpers import FIXTURE_DIR, MODULE_PATH, T_ADD, TESTS

from coverset.facts import convert
from coverset.facts.convert import BundleError, BundleLimits, extract_bundle, read_python_bundle
from coverset.lang.base import IMPORT_CONTEXT

# -- limits --------------------------------------------------------------------------------------


def _append(path: Path, *rows: object) -> None:
    with path.open("a") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


@pytest.mark.security
def test_oversized_and_bad_result_lines_are_dropped(bundle_copy: Path) -> None:
    limits = BundleLimits(max_json_line_bytes=4096)
    _append(
        bundle_copy / "results.jsonl",
        {"nodeid": "tests/x.py::huge", "outcome": "passed", "longrepr": "x" * 10_000},
        "{not json",
        {"nodeid": "tests/x.py::" + "y" * 600, "outcome": "passed"},
        {"nodeid": "tests/x.py::weird", "outcome": "exploded"},
        {"nodeid": "../../etc/passwd::t", "outcome": "passed"},
        {"kind": "heartbeat"},
    )
    bundle = read_python_bundle(bundle_copy, limits=limits)
    assert {row.nodeid for row in bundle.tests} == set(TESTS)
    dropped = bundle.meta["convert"]["dropped"]
    assert dropped["results.jsonl.oversized_lines"] == 1
    assert dropped["results.jsonl.bad_lines"] == 1
    assert dropped["long_nodeids"] == 1
    assert dropped["results.bad_rows"] == 3
    assert dropped["bad_paths"] == 1


@pytest.mark.security
def test_path_traversal_in_coverage_json_is_dropped(bundle_copy: Path) -> None:
    path = bundle_copy / "coverage.json"
    report = json.loads(path.read_text())
    entry = report["files"][MODULE_PATH]
    report["files"]["../outside.py"] = entry
    report["files"]["/abs/path.py"] = entry
    path.write_text(json.dumps(report))
    bundle = read_python_bundle(bundle_copy)
    assert set(bundle.static) == {MODULE_PATH, "src/factspkg/__init__.py"}
    assert bundle.meta["convert"]["dropped"]["bad_paths"] == 2


def _rewrite_shapes(bundle_dir: Path, edit) -> None:
    with gzip.open(bundle_dir / "shapes.jsonl.gz", "rt") as fh:
        rows = [json.loads(line) for line in fh]
    rows = edit(rows)
    with gzip.open(bundle_dir / "shapes.jsonl.gz", "wt") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)


@pytest.mark.security
def test_shape_rows_with_bad_paths_or_indexes_are_dropped(bundle_copy: Path) -> None:
    def edit(rows: list[dict]) -> list[dict]:
        rows[0]["funcs"].append(["../../evil.py", "f", 1])
        bad_func = len(rows[0]["funcs"]) - 1
        rows.append(
            {"kind": "obs", "f": bad_func, "p": "x", "s": 0, "c": 0, "n": 1, "t": [0], "trunc": False}
        )
        rows.append({"kind": "obs", "f": 0, "p": "x", "s": 99, "c": 0, "n": 1, "t": [0], "trunc": False})
        rows.append({"kind": "obs", "f": 0, "p": "x", "s": 0, "c": 0, "n": -1, "t": [0], "trunc": False})
        rows.append({"kind": "mystery"})
        rows[1]["ex"] = "z" * 500
        rows[1]["t"] = [0, 77]  # an unknown test index is dropped from the list, not the row
        return rows

    _rewrite_shapes(bundle_copy, edit)
    bundle = read_python_bundle(bundle_copy)
    dropped = bundle.meta["convert"]["dropped"]
    assert dropped["shapes.bad_rows"] == 4
    assert dropped["bad_paths"] == 1
    assert dropped["shapes.truncated_reprs"] == 1
    first = bundle.shapes[0]
    assert len(first.example) == 200 and first.example.endswith("\u2026")
    assert first.tests == (T_ADD,)
    assert len(bundle.shapes) == 9


def test_shapes_with_bad_header_are_ignored(bundle_copy: Path) -> None:
    _rewrite_shapes(bundle_copy, lambda rows: [{**rows[0], "version": 2}, *rows[1:]])
    bundle = read_python_bundle(bundle_copy)
    assert bundle.shapes == () and bundle.sampling == ()
    assert bundle.meta["convert"]["dropped"]["shapes.bad_header"] == 1


@pytest.mark.security
def test_gzip_bomb_is_capped(bundle_copy: Path) -> None:
    with gzip.open(bundle_copy / "shapes.jsonl.gz", "ab") as fh:
        fh.write(b" " * (8 * 1024 * 1024))
    limits = BundleLimits(gzip_floor_bytes=64 * 1024, gzip_ratio=10)
    bundle = read_python_bundle(bundle_copy, limits=limits)
    assert bundle.meta["convert"]["dropped"]["shapes.capped"] == 1
    assert len(bundle.shapes) == 9  # rows before the cap are kept


@pytest.mark.security
def test_total_bundle_size_limit(minimal_bundle: Path) -> None:
    with pytest.raises(BundleError, match="limit"):
        read_python_bundle(minimal_bundle, limits=BundleLimits(max_bundle_bytes=10_000))


def test_required_files(bundle_copy: Path) -> None:
    (bundle_copy / "results.jsonl").unlink()
    with pytest.raises(BundleError, match="results.jsonl"):
        read_python_bundle(bundle_copy)


def test_optional_files_may_be_missing(bundle_copy: Path) -> None:
    for name in ("coverage.json", "shapes.jsonl.gz", "collect.json", "durations.json", "meta.json"):
        (bundle_copy / name).unlink()
    bundle = read_python_bundle(bundle_copy)
    assert bundle.static == {} and bundle.shapes == () and bundle.collected == ()
    assert bundle.durations[T_ADD] == pytest.approx(0.01)  # falls back to results.jsonl
    assert set(bundle.run_arcs[MODULE_PATH]) >= {(10, 11), (31, 33)}  # from coverage.sqlite arcs


@pytest.mark.security
def test_symlinked_bundle_file_is_refused(bundle_copy: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"stolen": True}))
    (bundle_copy / "meta.json").unlink()
    os.symlink(secret, bundle_copy / "meta.json")
    bundle = read_python_bundle(bundle_copy)
    assert "stolen" not in bundle.meta
    assert bundle.meta["convert"]["dropped"]["refused_files"] == 1
    (bundle_copy / "results.jsonl").rename(tmp_path / "results.jsonl")
    os.symlink(tmp_path / "results.jsonl", bundle_copy / "results.jsonl")
    with pytest.raises(BundleError, match="missing results.jsonl"):
        read_python_bundle(bundle_copy)


@pytest.mark.security
def test_sqlite_views_masquerading_as_tables_are_refused(bundle_copy: Path) -> None:
    path = bundle_copy / "coverage.sqlite"
    path.unlink()
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE real_file (id integer, path text);"
        "CREATE VIEW file AS SELECT * FROM real_file;"
        "CREATE TABLE context (id integer, context text);"
    )
    db.close()
    with pytest.raises(BundleError, match="schema-v7"):
        read_python_bundle(bundle_copy)


@pytest.mark.security
def test_garbage_sqlite_is_a_bundle_error(bundle_copy: Path) -> None:
    (bundle_copy / "coverage.sqlite").write_bytes(b"not a database" * 100)
    with pytest.raises(BundleError):
        read_python_bundle(bundle_copy)


# -- extraction ----------------------------------------------------------------------------------


def _tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return path


def test_extract_round_trip(tmp_path: Path) -> None:
    dest = extract_bundle(FIXTURE_DIR / "bundle.tar.gz", tmp_path / "out")
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        p.name for p in (FIXTURE_DIR / "bundle").iterdir()
    )
    bundle = read_python_bundle(dest)
    assert set(bundle.coverage) == {IMPORT_CONTEXT, *TESTS}


def _special(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


@pytest.mark.security
@pytest.mark.parametrize(
    ("name", "kind", "linkname"),
    [
        ("evil-link", tarfile.SYMTYPE, "/etc/passwd"),
        ("hard", tarfile.LNKTYPE, "results.jsonl"),
        ("dev", tarfile.CHRTYPE, ""),
        ("fifo", tarfile.FIFOTYPE, ""),
    ],
    ids=["symlink", "hardlink", "device", "fifo"],
)
def test_extract_refuses_links_and_devices(tmp_path: Path, name: str, kind: bytes, linkname: str) -> None:
    info = _special(name, kind, linkname)
    archive = _tar(tmp_path / "b.tar.gz", [(tarfile.TarInfo("results.jsonl"), b"{}\n"), (info, None)])
    with pytest.raises(BundleError, match="refused"):
        extract_bundle(archive, tmp_path / "out")
    assert not (tmp_path / "out" / info.name).exists()


@pytest.mark.security
@pytest.mark.parametrize("name", ["../escape.txt", "/abs.txt", "a/../../b.txt", "a//b.txt"])
def test_extract_refuses_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = _tar(tmp_path / "b.tar.gz", [(tarfile.TarInfo(name), b"x")])
    with pytest.raises(BundleError, match="path"):
        extract_bundle(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.security
def test_extract_refuses_duplicates_and_member_floods(tmp_path: Path) -> None:
    dup = _tar(tmp_path / "dup.tar.gz", [(tarfile.TarInfo("a"), b"1"), (tarfile.TarInfo("a"), b"2")])
    with pytest.raises(BundleError, match="duplicate"):
        extract_bundle(dup, tmp_path / "dup")
    flood = _tar(tmp_path / "flood.tar.gz", [(tarfile.TarInfo(f"f{i}"), b"x") for i in range(5)])
    with pytest.raises(BundleError, match="members"):
        extract_bundle(flood, tmp_path / "flood", BundleLimits(max_members=4))


@pytest.mark.security
def test_extract_caps_declared_member_sizes(tmp_path: Path) -> None:
    archive = _tar(tmp_path / "bomb.tar.gz", [(tarfile.TarInfo("zeros"), b"\0" * (4 * 1024 * 1024))])
    with pytest.raises(BundleError, match="declare more than"):
        extract_bundle(archive, tmp_path / "out", BundleLimits(gzip_floor_bytes=256 * 1024))
    assert not (tmp_path / "out" / "zeros").exists()  # refused before a byte was written
    with pytest.raises(BundleError, match="limit"):
        extract_bundle(archive, tmp_path / "out2", BundleLimits(max_bundle_bytes=100))


@pytest.mark.security
def test_extract_caps_the_decompressed_stream(tmp_path: Path) -> None:
    """Bytes outside any member's declared size (here a huge PAX header) still count."""
    info = tarfile.TarInfo("results.jsonl")
    info.pax_headers = {"comment": "x" * (4 * 1024 * 1024)}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info.size = 3
        tar.addfile(info, io.BytesIO(b"{}\n"))
    archive = tmp_path / "pax.tar.gz"
    archive.write_bytes(gzip.compress(buffer.getvalue()))
    with pytest.raises(BundleError, match="expands"):
        extract_bundle(archive, tmp_path / "out", BundleLimits(gzip_floor_bytes=256 * 1024))


@pytest.mark.security
def test_extract_refuses_sparse_members(tmp_path: Path) -> None:
    """A PAX GNU sparse 1.0 member declares 300 MiB from a few hundred bytes of archive."""
    sparse_map = b"1\n0\n512\n".ljust(512, b"\0")
    payload = sparse_map + b"X" * 512
    info = tarfile.TarInfo("GNUSparseFile.0/coverage.sqlite")
    info.size = len(payload)
    info.pax_headers = {
        "GNU.sparse.major": "1",
        "GNU.sparse.minor": "0",
        "GNU.sparse.name": "coverage.sqlite",
        "GNU.sparse.realsize": str(300 * 1024 * 1024),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(payload))
    archive = tmp_path / "sparse.tar.gz"
    archive.write_bytes(gzip.compress(buffer.getvalue()))
    assert archive.stat().st_size < 1024
    with pytest.raises(BundleError, match="sparse"):
        extract_bundle(archive, tmp_path / "out")
    written = sum(f.stat().st_size for f in (tmp_path / "out").rglob("*") if f.is_file())
    assert written == 0


@pytest.mark.security
def test_extract_needs_an_empty_destination(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "leftover").write_text("x")
    with pytest.raises(BundleError, match="not empty"):
        extract_bundle(FIXTURE_DIR / "bundle.tar.gz", tmp_path / "out")


def test_extract_nested_directories(tmp_path: Path) -> None:
    folder = tarfile.TarInfo("logs")
    folder.type = tarfile.DIRTYPE
    archive = _tar(tmp_path / "n.tar.gz", [(folder, None), (tarfile.TarInfo("logs/a/b.txt"), b"hi")])
    dest = extract_bundle(archive, tmp_path / "out")
    assert (dest / "logs" / "a" / "b.txt").read_bytes() == b"hi"
    shutil.rmtree(dest)


# -- decoded-size caps -----------------------------------------------------------------------------


def _numbits_bundle(tmp_path: Path, blob: bytes) -> Path:
    bundle_dir = tmp_path / "nb"
    bundle_dir.mkdir()
    db = sqlite3.connect(bundle_dir / "coverage.sqlite")
    db.executescript(
        "CREATE TABLE file (id integer primary key, path text);"
        "CREATE TABLE context (id integer primary key, context text);"
        "CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);"
    )
    db.execute("INSERT INTO file VALUES (1, 'src/m.py')")
    db.execute("INSERT INTO context VALUES (1, 't.py::t')")
    db.execute("INSERT INTO line_bits VALUES (1, 1, ?)", [blob])
    db.commit()
    db.close()
    (bundle_dir / "results.jsonl").write_text(json.dumps({"nodeid": "t.py::t", "outcome": "passed"}) + "\n")
    return bundle_dir


@pytest.mark.security
def test_numbits_bomb_is_dropped_before_decoding(tmp_path: Path) -> None:
    """8 MiB of 0xFF compresses ~1000x but would decode to 67M lines: the blob is refused."""
    bundle = read_python_bundle(_numbits_bundle(tmp_path, b"\xff" * (8 * 1024 * 1024)))
    assert bundle.coverage == {}
    assert bundle.meta["convert"]["dropped"]["coverage.bad_numbits"] == 1


@pytest.mark.security
def test_coverage_cells_are_capped(tmp_path: Path, minimal_bundle: Path) -> None:
    bundle_dir = _numbits_bundle(tmp_path, nums_to_numbits(range(1, 2001)))
    with pytest.raises(BundleError, match="cells"):
        read_python_bundle(bundle_dir, limits=BundleLimits(max_coverage_cells=1000))
    with pytest.raises(BundleError, match="cells"):  # 26 distinct cells from 38 arcs
        read_python_bundle(minimal_bundle, limits=BundleLimits(max_coverage_cells=25))
    with pytest.raises(BundleError, match="arcs"):  # refused before the arcs are decoded
        read_python_bundle(minimal_bundle, limits=BundleLimits(max_coverage_cells=10))


@pytest.mark.security
def test_arc_rows_with_bad_values_are_dropped(bundle_copy: Path) -> None:
    db = sqlite3.connect(bundle_copy / "coverage.sqlite")
    db.execute("INSERT INTO arc (file_id, context_id, fromno, tono) VALUES (1, 1, 'abc', 5)")
    db.execute("INSERT INTO arc (file_id, context_id, fromno, tono) VALUES (1, 1, 5000000000, 5)")
    db.execute("INSERT INTO arc (file_id, context_id, fromno, tono) VALUES (1, 1, 2000000, -1)")
    db.commit()
    db.close()
    bundle = read_python_bundle(bundle_copy)
    assert bundle.meta["convert"]["dropped"]["coverage.bad_arcs"] == 3
    assert set(bundle.coverage) == {IMPORT_CONTEXT, *TESTS}


@pytest.mark.security
@pytest.mark.parametrize(
    ("limits", "match"),
    [
        (BundleLimits(max_tests=3), "nodeids"),
        (BundleLimits(max_paths=1), "paths"),
        (BundleLimits(max_shape_keys=2), "shape keys"),
    ],
    ids=["tests", "paths", "shape-keys"],
)
def test_distinct_names_are_capped(minimal_bundle: Path, limits: BundleLimits, match: str) -> None:
    with pytest.raises(BundleError, match=match):
        read_python_bundle(minimal_bundle, limits=limits)


# -- hostile values never escape as other exceptions -----------------------------------------------


@pytest.mark.security
def test_deeply_nested_json_is_counted_not_raised(bundle_copy: Path) -> None:
    _append(bundle_copy / "results.jsonl", "[" * 200_000, '{"a":' * 100_000 + "1" + "}" * 100_000)
    for name in ("coverage.json", "meta.json", "collect.json"):
        (bundle_copy / name).write_text("[" * 500_000)
    bundle = read_python_bundle(bundle_copy)
    dropped = bundle.meta["convert"]["dropped"]
    assert dropped["results.jsonl.bad_lines"] == 2
    assert dropped["coverage.json.unparseable"] == 1 and dropped["meta.json.unparseable"] == 1
    assert {row.nodeid for row in bundle.tests} == set(TESTS)


@pytest.mark.security
def test_huge_and_odd_numbers_are_dropped(bundle_copy: Path) -> None:
    _append(
        bundle_copy / "results.jsonl",
        '{"nodeid": "tests/x.py::slow", "outcome": "passed", "duration_s": ' + "9" * 400 + "}",
        {"nodeid": "tests/x.py::nan", "outcome": "passed", "duration_s": 1e308, "start": True},
        {"nodeid": IMPORT_CONTEXT, "outcome": "passed"},
    )
    report = json.loads((bundle_copy / "coverage.json").read_text())
    report["files"][MODULE_PATH]["missing_lines"].append(2**40)
    (bundle_copy / "coverage.json").write_text(json.dumps(report))
    bundle = read_python_bundle(bundle_copy)
    by_id = {row.nodeid: row for row in bundle.tests}
    assert by_id["tests/x.py::slow"].duration_s == 0.0
    assert by_id["tests/x.py::nan"].start is None
    assert IMPORT_CONTEXT not in by_id
    dropped = bundle.meta["convert"]["dropped"]
    assert dropped["coverage.json.bad_files"] == 1 and dropped["bad_nodeids"] == 1
    assert MODULE_PATH not in bundle.static


@pytest.mark.security
def test_shape_counts_and_keys_are_validated(bundle_copy: Path) -> None:
    def edit(rows: list[dict]) -> list[dict]:
        rows[0]["shapes"] += ["int|zero|big", "obj:" + "x" * 600]
        rows.append({"kind": "obs", "f": 0, "p": "a", "s": 5, "c": 0, "n": 1, "t": [0]})
        rows.append({"kind": "obs", "f": 0, "p": "a", "s": 0, "c": 0, "n": 10**30, "t": [0]})
        rows.append({"kind": "func_sampling", "f": 0, "calls": 2**64, "tests": [0], "truncated": False})
        return rows

    _rewrite_shapes(bundle_copy, edit)
    bundle = read_python_bundle(bundle_copy)
    dropped = bundle.meta["convert"]["dropped"]
    assert dropped["shapes.bad_keys"] == 2 and dropped["shapes.bad_rows"] == 3
    assert len(bundle.shapes) == 9 and len(bundle.sampling) == 3


def test_corrupt_shapes_stream_is_counted(bundle_copy: Path) -> None:
    data = (bundle_copy / "shapes.jsonl.gz").read_bytes()
    (bundle_copy / "shapes.jsonl.gz").write_bytes(data[:20] + b"\xff" * 40 + data[60:])
    bundle = read_python_bundle(bundle_copy)
    assert bundle.meta["convert"]["dropped"]["shapes.corrupt"] == 1


@pytest.mark.security
def test_unexpected_errors_become_bundle_errors(bundle_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(self: object) -> None:
        raise TypeError("unsupported operand")

    monkeypatch.setattr(convert._Reader, "tests", explode)
    with pytest.raises(BundleError, match="TypeError"):
        read_python_bundle(bundle_copy)


def test_fixture_tarball_extracts_and_converts_cleanly(tmp_path: Path) -> None:
    bundle = read_python_bundle(extract_bundle(FIXTURE_DIR / "bundle.tar.gz", tmp_path / "out"))
    assert bundle.meta["convert"]["dropped"] == {}
    assert bundle.coverage[T_ADD] == {MODULE_PATH: (5,)}
    shutil.rmtree(tmp_path / "out")
    assert not os.path.exists(tmp_path / "out")
