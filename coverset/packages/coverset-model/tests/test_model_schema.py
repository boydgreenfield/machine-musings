"""The JSON Schemas and the dependency-free validator."""

from __future__ import annotations

import json

import pytest

from coverset_model.schema import SCHEMAS, SchemaError, check, dump, row_schema, write_all


def _violations(stem: str, row: object) -> list[tuple[str, str]]:
    return [(v.path, v.keyword) for v in check(row, row_schema(stem, row))]


def test_good_rows_pass() -> None:
    assert _violations("tests", {"id": "t::a", "file": "t.py", "outcome": None}) == []
    assert _violations("coverage", {"test": "<import>", "file": "a.py", "lines": [1, 2]}) == []
    obs = {
        "kind": "obs", "func": ["a.py", "f", 3], "param": "x", "shape": "int|pos",
        "caller": {"test": "t::a"}, "samples": 1, "tests": ["t::a"],
    }  # fmt: skip
    assert _violations("shapes", obs) == []
    assert _violations("shapes", {**obs, "caller": ["a.py", "g", 9]}) == []
    sampling = {"kind": "sampling", "func": ["a.py", "f", 3], "calls": 4, "tests": [], "cap": None}
    assert _violations("shapes", sampling) == []
    function = {
        "path": "a.ts", "qualname": "f", "ordinal": 0, "def_line": 1, "body_start": 2, "end_line": 3,
        "body_hash": "x", "sig_hash": "y", "params": ["a"], "kind": "function",
        "types": {"a": {"kind": "union", "arms": [{"kind": "int"}, {"kind": "none"}]}, "return": None},
    }  # fmt: skip
    assert _violations("functions", function) == []


@pytest.mark.parametrize(
    ("stem", "row", "expected"),
    [
        ("tests", {"id": "t", "file": "t.py"}, ("/", "required")),
        ("tests", {"id": "", "file": "t.py", "outcome": "passed"}, ("/id", "minLength")),
        ("tests", {"id": "t", "file": "t.py", "outcome": "ok"}, ("/outcome", "enum")),
        ("tests", {"id": "t", "file": "t.py", "outcome": None, "property": 1}, ("/property", "type")),
        (
            "tests",
            {"id": "t", "file": "t.py", "outcome": None, "extra": 1},
            ("/extra", "additionalProperties"),
        ),
        ("coverage", {"test": "t", "file": "a.py", "lines": [0]}, ("/lines/0", "minimum")),
        ("coverage", {"test": "t", "file": "a.py", "lines": [True]}, ("/lines/0", "type")),
        ("files", {"path": "a.py", "statements": [], "branches": [[1, 2, 3]]}, ("/branches/0", "maxItems")),
        ("shapes", {"kind": "obs", "func": ["a.py", 3, "f"]}, ("/func/1", "type")),
        ("manifest", {"format": "nope"}, ("/format", "const")),
        ("functions", {"key": "XYZ"}, ("/key", "pattern")),
    ],
)
def test_bad_rows_are_located(stem: str, row: dict, expected: tuple[str, str]) -> None:
    assert expected in _violations(stem, row)


def test_descriptors_nest_through_refs() -> None:
    row = {"types": {"a": {"kind": "list", "elem": {"kind": "list", "elem": {"kind": "nope"}}}}}
    found = [(v.path, v.keyword) for v in check(row, SCHEMAS["functions"], limit=50)]
    assert ("/types/a", "anyOf") in found  # a descriptor or null: the nested kind fails the first
    good = {"types": {"a": {"kind": "list", "elem": {"kind": "list", "elem": {"kind": "int"}}}}}
    assert [v for v in check(good, SCHEMAS["functions"], limit=50) if v.path.startswith("/types")] == []


def test_unsupported_keywords_are_refused() -> None:
    with pytest.raises(SchemaError):
        check(1, {"oneOf": []})


def test_dump_and_write_all(tmp_path) -> None:
    written = write_all(tmp_path)
    assert sorted(p.name for p in written) == sorted(f"{s}.schema.json" for s in SCHEMAS)
    for stem in SCHEMAS:
        assert json.loads((tmp_path / f"{stem}.schema.json").read_text()) == SCHEMAS[stem]
        assert dump(stem).endswith("}\n")
