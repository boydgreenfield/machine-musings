"""The synthetic TypeScript fixture, the JSON Schemas in docs/schema, and round trips through the format."""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
from neutral_helpers import (
    MINIMAL,
    NEUTRAL_TS,
    PROJECT_ROOT,
    copy_fixture,
    run_demo_plugin,
    strip_reader_meta,
)

from coverset.facts.convert import read_python_bundle
from coverset_model import schema as schemas
from coverset_model.io import conform, inspect, read_bundle, validate, write_bundle

SCRIPT = PROJECT_ROOT / "scripts" / "make_neutral_fixture.py"


def _load_script():  # noqa: ANN202
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_neutral_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixture_validates_with_zero_problems() -> None:
    assert validate(NEUTRAL_TS / "bundle") == []
    assert validate(NEUTRAL_TS / "bundle.tar.gz") == []
    info = inspect(NEUTRAL_TS / "bundle")
    assert info.language == "typescript"
    assert info.manifest["producer"] == {"name": "vitest-coverset-fixture", "version": "0.0.1"}
    assert info.rows["tests"] == 12 and info.rows["functions"] == 8 and info.rows["links"] == 2


def test_the_fixture_is_what_the_script_writes() -> None:
    assert _load_script().main(["--check"]) == 0


def test_the_fixture_matches_its_description() -> None:
    bundle = read_bundle(NEUTRAL_TS / "bundle")
    outcomes = sorted(str(t.outcome) for t in bundle.tests)
    assert outcomes == ["failed"] * 2 + ["passed"] * 8 + ["skipped"]
    assert len(bundle.collected) == 12 and sum(c.hypothesis for c in bundle.collected) == 1
    assert {i for c in bundle.collected for i in c.invariant_ids} == {
        "discount_never_negative",
        "shipping_cost_monotone",
    }
    kinds = {d.kind for types in bundle.types.values() for d in types.values() if d is not None}
    assert {"int", "float", "str", "union", "literal", "enum", "list"} <= kinds
    assert "<import>" in bundle.coverage
    capped = [s for s in bundle.sampling if s.truncated]
    assert [(s.func[1], s.cap) for s in capped] == [("rateFor", "sample")]


def _rewrite(path: Path, edit) -> None:  # noqa: ANN001
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(row) + "\n" for row in edit(rows)))


def test_a_mutated_copy_reports_the_right_problems(tmp_path: Path) -> None:
    bundle = copy_fixture(tmp_path / "bundle")
    _rewrite(bundle / "tests.jsonl", lambda rows: [{**rows[0], "file": "../../outside.ts"}, *rows[1:]])
    with open(bundle / "tests.jsonl", "ab") as fh:
        fh.write(b'{"id": "tests/x.test.ts > \\udc00", "file": "tests/x.test.ts", "outcome": "passed"}\n')
        fh.write(
            b'{"id": "big", "file": "tests/x.test.ts", "outcome": "passed", "message": "'
            + b"m" * 1_100_000
            + b'"}\n'
        )
    _rewrite(
        bundle / "coverage.jsonl", lambda rows: [*rows, {**rows[-1], "test": rows[-1]["test"], "lines": [4]}]
    )
    problems = validate(bundle)
    found = {(p.file, p.row, p.code) for p in problems}
    assert ("tests.jsonl", 1, "path") in found
    assert ("tests.jsonl", 13, "json") in found and any("lone surrogate" in p.message for p in problems)
    assert ("tests.jsonl", 14, "oversized") in found
    assert any(p.code == "not_statement" and p.file == "coverage.jsonl" for p in problems)
    assert not any(p.fatal for p in problems)
    (bundle / "manifest.json").write_text(
        (bundle / "manifest.json").read_text().replace('"version": 1', '"version": 7')
    )
    last = validate(bundle)[-1]
    assert last.fatal and last.code == "version" and "unknown version 7" in last.message


def _round_trip(bundle, tmp_path: Path, name: str, **manifest) -> None:  # noqa: ANN001
    out = write_bundle(bundle, tmp_path / name, manifest=manifest or None)
    assert validate(out) == []
    assert strip_reader_meta(read_bundle(out)) == strip_reader_meta(conform(bundle))
    packed = write_bundle(bundle, tmp_path / f"{name}-gz", manifest=manifest or None, compress=True)
    assert strip_reader_meta(read_bundle(packed)) == strip_reader_meta(conform(bundle))


def test_round_trip_of_the_fixture(tmp_path: Path) -> None:
    bundle = read_bundle(NEUTRAL_TS / "bundle")
    _round_trip(bundle, tmp_path, "ts")
    assert read_bundle(tmp_path / "ts").meta["bundle"]["language"] == "typescript"  # from the bundle's meta


def test_round_trip_of_a_python_bundle(tmp_path: Path) -> None:
    bundle = read_python_bundle(MINIMAL / "bundle")
    _round_trip(bundle, tmp_path, "minimal", language="python")
    again = read_bundle(tmp_path / "minimal")
    # What the format drops: executed lines that are not statements (line 1 of a module docstring,
    # an excluded def under <import>); everything the facts need survives.
    assert again.tests == conform(bundle).tests and again.shapes == conform(bundle).shapes
    assert again.coverage != bundle.coverage


@pytest.mark.slow
def test_round_trip_of_the_demo_bundle(tmp_path: Path) -> None:
    pytest.importorskip("pytest_coverset")
    if shutil.which("git") is None:
        pytest.skip("needs git")
    bundle = read_python_bundle(run_demo_plugin(tmp_path))
    assert len(bundle.tests) > 50 and bundle.shapes
    _round_trip(bundle, tmp_path, "demo", language="python")


def test_docs_schema_files_are_current() -> None:
    for stem in schemas.SCHEMAS:
        path = PROJECT_ROOT / "docs" / "schema" / f"{stem}.schema.json"
        assert path.read_text() == schemas.dump(stem), (
            f"run: uv run python -m coverset_model schemas docs/schema ({stem})"
        )


@pytest.mark.parametrize("stem", sorted(schemas.SCHEMAS))
def test_schemas_are_valid_and_agree_with_jsonschema(stem: str) -> None:
    schema = schemas.SCHEMAS[stem]
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    bundle = NEUTRAL_TS / "bundle"
    if stem == "manifest":
        rows = [json.loads((bundle / "manifest.json").read_text())]
    else:
        path = bundle / f"{stem}.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    broken = [{**row, "unexpected": 1} for row in rows[:2]] + [{"kind": 3}, [], {"path": ""}]
    for row in [*rows, *broken]:
        ours = not schemas.check(row, schemas.row_schema(stem, row))
        assert ours == validator.is_valid(row), (stem, row)


def test_gzip_variant_of_the_fixture_reads_the_same(tmp_path: Path) -> None:
    bundle = copy_fixture(tmp_path / "bundle")
    for path in sorted(bundle.glob("*.jsonl")):
        path.with_name(path.name + ".gz").write_bytes(gzip.compress(path.read_bytes(), mtime=0))
        path.unlink()
    assert validate(bundle) == []
    assert strip_reader_meta(read_bundle(bundle)) == strip_reader_meta(read_bundle(NEUTRAL_TS / "bundle"))


def _spec_examples() -> list[tuple[str, object]]:
    """``(file stem, row)`` for every JSON example under a ``### `<file>` `` heading of the spec."""
    text = (PROJECT_ROOT / "docs" / "neutral-bundle.md").read_text()
    found: list[tuple[str, object]] = []
    stem = None
    decoder = json.JSONDecoder()
    for block in text.split("```"):
        for line in block.splitlines():
            if line.startswith("### `"):
                stem = line.split("`")[1].split(".")[0]
        if block.startswith("json\n") and stem in schemas.SCHEMAS:
            body, pos = block[5:], 0
            while (pos := len(body) - len(body[pos:].lstrip())) < len(body):
                row, pos = decoder.raw_decode(body, pos)
                found.append((stem, row))
    return found


def test_the_spec_examples_follow_the_schemas() -> None:
    examples = _spec_examples()
    assert {stem for stem, _ in examples} == set(schemas.SCHEMAS)
    for stem, row in examples:
        assert schemas.check(row, schemas.row_schema(stem, row)) == [], (stem, row)
    function = next(row for stem, row in examples if stem == "functions")
    from coverset_model import func_key

    assert function["key"] == func_key(function["path"], function["qualname"], function["ordinal"])
