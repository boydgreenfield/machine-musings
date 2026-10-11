"""Reading, validating and writing neutral bundles, including hostile ones."""

from __future__ import annotations

import dataclasses
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from model_helpers import ADD, PATH, T1, T2, T3, rewrite_jsonl, small_bundle

from coverset_model.io import (
    READER_META_KEYS,
    BundleRejected,
    Limits,
    conform,
    inspect,
    is_neutral,
    pack,
    read_bundle,
    read_manifest,
    safe_rel_path,
    validate,
    write_bundle,
)
from coverset_model.neutral import IMPORT_CONTEXT, NeutralBundle

MANIFEST = {"language": "typescript", "producer": {"name": "vitest-coverset", "version": "0.1.0"}, "shard": 0}


def _strip(bundle: NeutralBundle) -> NeutralBundle:
    return dataclasses.replace(
        bundle, meta={k: v for k, v in bundle.meta.items() if k not in READER_META_KEYS}
    )


@pytest.fixture
def written(tmp_path: Path) -> Path:
    return write_bundle(small_bundle(), tmp_path / "bundle", manifest=MANIFEST)


def _codes(path: Path, **kwargs: object) -> set[tuple[str, str]]:
    return {(p.file, p.code) for p in validate(path, **kwargs)}  # type: ignore[arg-type]


def test_round_trip(written: Path) -> None:
    assert validate(written) == []
    bundle = read_bundle(written)
    assert _strip(bundle) == _strip(conform(small_bundle()))
    assert bundle.meta["runner"] == "vitest"
    assert bundle.meta["bundle"]["language"] == "typescript"
    assert bundle.meta["convert"]["dropped"] == {}
    assert len(bundle.meta["convert"]["bundle_sha256"]) == 64
    assert bundle.types[ADD.triple]["b"].kind == "union"
    assert bundle.sampling[0].cap == "sample"
    assert [c.nodeid for c in bundle.collected] == sorted([T1, T2, T3])
    assert {t.nodeid for t in bundle.tests} == {T1, T2}
    assert is_neutral(written) and read_manifest(written)["shard"] == 0


def test_write_is_idempotent_and_reproducible(written: Path, tmp_path: Path) -> None:
    again = write_bundle(read_bundle(written), tmp_path / "again")
    for name in os.listdir(written):
        assert (written / name).read_bytes() == (again / name).read_bytes(), name
    assert (
        pack(written, tmp_path / "a.tar.gz").read_bytes() == pack(again, tmp_path / "b.tar.gz").read_bytes()
    )


def test_gzip_files_and_archives_read_the_same(written: Path, tmp_path: Path) -> None:
    packed = write_bundle(read_bundle(written), tmp_path / "gz", compress=True)
    assert sorted(os.listdir(packed))[0].endswith(".jsonl.gz")
    archive = pack(packed, tmp_path / "bundle.tar.gz")
    expected = _strip(read_bundle(written))
    assert _strip(read_bundle(packed)) == expected
    assert _strip(read_bundle(archive)) == expected
    assert validate(archive) == []


def test_conform_keeps_only_statement_lines_and_synthesises_collected_items() -> None:
    bundle = dataclasses.replace(
        small_bundle(), collected=(), coverage={IMPORT_CONTEXT: {PATH: (1, 5, 6)}, "other": {"x.ts": (3,)}}
    )
    shaped = conform(bundle)
    assert shaped.coverage == {IMPORT_CONTEXT: {PATH: (1, 6)}, "other": {"x.ts": (3,)}}
    assert [c.nodeid for c in shaped.collected] == [T1, T2]
    assert conform(shaped) == shaped


@pytest.mark.parametrize(
    ("stem", "edit", "expected"),
    [
        ("tests", lambda rows: [{**rows[0], "file": "../etc/passwd"}, *rows[1:]], ("tests.jsonl", "path")),
        ("tests", lambda rows: [{**rows[0], "file": "/abs/t.ts"}, *rows[1:]], ("tests.jsonl", "path")),
        ("tests", lambda rows: [*rows, rows[0]], ("tests.jsonl", "duplicate")),
        ("tests", lambda rows: [{**rows[0], "id": IMPORT_CONTEXT}, *rows[1:]], ("tests.jsonl", "reserved")),
        ("tests", lambda rows: [{**rows[0], "id": "x" * 600}, *rows[1:]], ("tests.jsonl", "schema")),
        ("tests", lambda rows: [{**rows[0], "message": "m" * 9000}, *rows[1:]], ("tests.jsonl", "clipped")),
        ("tests", lambda rows: [{**rows[0], "retries": 2}, *rows[1:]], ("tests.jsonl", "unknown_field")),
        ("coverage", lambda rows: [{**rows[0], "lines": [1, 5]}, *rows[1:]], ("coverage.jsonl", "not_statement")),
        ("coverage", lambda rows: [*rows, {"test": "ghost", "file": PATH, "lines": [2]}], ("coverage.jsonl", "unknown_test")),
        ("coverage", lambda rows: [*rows, {"test": T1, "file": "zz.ts", "lines": [2]}], ("coverage.jsonl", "unknown_file")),
        ("functions", lambda rows: [{**rows[0], "key": "0" * 16}, *rows[1:]], ("functions.jsonl", "key")),
        ("functions", lambda rows: [{**rows[0], "body_start": 99}, *rows[1:]], ("functions.jsonl", "lines")),
        ("functions", lambda rows: [{**rows[0], "types": {"zz": {"kind": "int"}}}, *rows[1:]], ("functions.jsonl", "unknown_param")),
        ("functions", lambda rows: [{**rows[0], "types": {"a": {"kind": "integer"}}}, *rows[1:]], ("functions.jsonl", "schema")),
        ("shapes", lambda rows: [{**rows[0], "shape": "int|positive"}, *rows[1:]], ("shapes.jsonl", "shape_key")),
        ("shapes", lambda rows: [{**rows[0], "kind": "header"}, *rows[1:]], ("shapes.jsonl", "schema")),
        ("shapes", lambda rows: [*rows[:-1], {**rows[-1], "truncated": False}], ("shapes.jsonl", "cap")),
    ],
)  # fmt: skip
def test_row_problems_are_reported_and_dropped(
    written: Path, stem: str, edit, expected: tuple[str, str]
) -> None:
    rewrite_jsonl(written / f"{stem}.jsonl", edit)
    problems = validate(written)
    assert expected in {(p.file, p.code) for p in problems}, problems
    assert all(p.row is not None or p.file == "functions.jsonl" for p in problems if not p.fatal)
    bundle = read_bundle(written)  # never raises for a row problem
    assert bundle.meta["convert"]["dropped"][f"{stem}.{expected[1]}"] >= 1


def test_dropped_rows_do_not_reach_the_bundle(written: Path) -> None:
    rewrite_jsonl(written / "tests.jsonl", lambda rows: [{**rows[0], "file": "../x"}, *rows[1:]])
    rewrite_jsonl(written / "coverage.jsonl", lambda rows: [{**rows[0], "lines": [1, 5, 6]}, *rows[1:]])
    bundle = read_bundle(written)
    assert T1 not in {c.nodeid for c in bundle.collected}
    assert bundle.coverage[IMPORT_CONTEXT][PATH] == (1, 6)


@pytest.mark.security
def test_lone_surrogates_and_bad_json(written: Path) -> None:
    with open(written / "tests.jsonl", "ab") as fh:
        fh.write(b'{"id": "t\\ud800", "file": "t.ts", "outcome": null}\n')
        fh.write(b'{"id": "nan", "file": "t.ts", "outcome": null, "duration_s": NaN}\n')
        fh.write(b"\xff\xfe not utf-8\n")
        fh.write(b"[1, 2]\n")
    messages = {(p.row, p.message) for p in validate(written)}
    assert (4, "contains a lone surrogate") in messages
    assert (5, "not valid JSON") in messages
    assert (6, "not UTF-8") in messages
    assert (7, "not a JSON object") in messages


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        ({"format": "coverset-neutral", "version": 2, "language": "typescript", "producer": {"name": "x", "version": "1"}}, "version"),
        ({"format": "coverset-neutral", "version": True, "language": "typescript", "producer": {"name": "x", "version": "1"}}, "version"),
        ({"format": "something-else", "version": 1}, "format"),
        ({"format": "coverset-neutral", "version": 1, "language": "TypeScript!", "producer": {"name": "x", "version": "1"}}, "schema"),
    ],
)  # fmt: skip
@pytest.mark.security
def test_manifest_problems_are_fatal(written: Path, manifest: dict, code: str) -> None:
    (written / "manifest.json").write_text(json.dumps(manifest))
    problems = validate(written)
    assert problems[-1].fatal and problems[-1].code == code and problems[-1].file == "manifest.json"
    with pytest.raises(BundleRejected):
        read_bundle(written)


def test_missing_files(written: Path) -> None:
    (written / "functions.jsonl").unlink()
    assert _codes(written) == {("functions.jsonl", "missing")}  # typescript needs shipped regions
    assert read_bundle(written).functions == ()  # not fatal: the run just has no regions
    (written / "coverage.jsonl").unlink()
    assert ("coverage.jsonl", "missing") in _codes(written)
    (written / "manifest.json").unlink()
    assert _codes(written) == {("manifest.json", "missing")}
    assert not is_neutral(written)


@pytest.mark.security
def test_both_variants_of_a_file_are_ambiguous(written: Path) -> None:
    data = (written / "tests.jsonl").read_bytes()
    (written / "tests.jsonl.gz").write_bytes(gzip.compress(data))
    assert ("tests.jsonl", "ambiguous") in _codes(written)


@pytest.mark.security
def test_size_limits(written: Path) -> None:
    rewrite_jsonl(written / "tests.jsonl", lambda rows: [{**rows[0], "message": "m" * 5000}, *rows[1:]])
    small = Limits(max_json_line_bytes=1000)
    assert ("tests.jsonl", "oversized") in _codes(written, limits=small)
    assert read_bundle(written, limits=small).meta["convert"]["dropped"]["tests.oversized"] == 1
    assert ("bundle", "limit") in _codes(written, limits=Limits(max_bundle_bytes=100))
    assert ("tests.jsonl", "limit") in _codes(written, limits=Limits(max_tests=2))
    assert ("coverage.jsonl", "limit") in _codes(written, limits=Limits(max_coverage_cells=3))
    assert ("shapes.jsonl", "limit") in _codes(written, limits=Limits(max_shape_keys=1))


@pytest.mark.security
def test_a_gzip_bomb_is_refused(written: Path) -> None:
    (written / "shapes.jsonl").unlink()
    (written / "shapes.jsonl.gz").write_bytes(gzip.compress(b" " * (4 * 1024 * 1024)))
    limits = Limits(gzip_floor_bytes=1024, gzip_ratio=10)
    problems = validate(written, limits=limits)
    assert problems[-1].fatal and "expands beyond" in problems[-1].message
    with pytest.raises(BundleRejected):
        read_bundle(written, limits=limits)


@pytest.mark.security
def test_links_devices_and_nested_members_are_refused(written: Path, tmp_path: Path) -> None:
    (written / "links.jsonl").symlink_to(written / "tests.jsonl")
    assert any(p.fatal and "regular file" in p.message for p in validate(written))
    (written / "links.jsonl").unlink()

    def archive(name: str, member: tarfile.TarInfo, data: bytes = b"") -> Path:
        path = tmp_path / name
        with tarfile.open(path, "w:gz") as tar:
            for child in sorted(os.listdir(written)):
                tar.add(written / child, arcname=child)
            tar.addfile(member, io.BytesIO(data) if data else None)
        return path

    link = tarfile.TarInfo("links.jsonl")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    nested = tarfile.TarInfo("sub/tests.jsonl")
    nested.size = 2
    for path in (archive("link.tar.gz", link), archive("nested.tar.gz", nested, b"{}")):
        problems = validate(path)
        assert problems and problems[-1].fatal and problems[-1].code == "archive", problems
    (tmp_path / "junk.tar.gz").write_bytes(b"not an archive")
    assert validate(tmp_path / "junk.tar.gz")[-1].code == "archive"


@pytest.mark.security
def test_allowed_paths_filter_rows(written: Path) -> None:
    bundle = read_bundle(written, allowed_paths={"tests/calc.test.ts"})
    assert bundle.static == {} and bundle.functions == ()
    assert bundle.meta["convert"]["dropped"]["files.paths_not_in_head"] == 1
    assert bundle.meta["convert"]["head_checked"] is True


@pytest.mark.security
def test_validate_never_raises(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert validate(tmp_path / "empty")[0].code == "missing"
    assert validate(tmp_path / "nothing-here")[0].fatal
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert validate(fifo)[0].fatal


def test_inspect_counts_rows(written: Path) -> None:
    info = inspect(written)
    assert info.language == "typescript" and info.problems == []
    assert info.rows == {
        "manifest": 1,
        "files": 1,
        "tests": 3,
        "coverage": 3,
        "arcs": 1,
        "functions": 2,
        "shapes": 4,
    }
    assert info.files["tests"] == "tests.jsonl"


@pytest.mark.parametrize(
    ("value", "ok"),
    [("a/b.ts", True), ("a//b", False), ("./a", False), ("a/../b", False), ("/a", False), ("C:/a", False),
     ("a\\b", False), ("a\0b", False), ("a\nb", False), ("", False), ("x" * 513, False), (3, False)],
)  # fmt: skip
@pytest.mark.security
def test_safe_rel_path(value: object, ok: bool) -> None:
    assert (safe_rel_path(value) is not None) is ok
