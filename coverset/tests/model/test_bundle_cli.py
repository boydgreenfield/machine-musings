"""``coverset bundle validate | info | convert``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from neutral_helpers import MINIMAL, NEUTRAL_TS, copy_fixture, strip_reader_meta

from coverset.cli import cli
from coverset.facts.convert import read_python_bundle
from coverset_model.io import conform, read_bundle, validate


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_validate_a_valid_bundle(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["bundle", "validate", str(NEUTRAL_TS / "bundle.tar.gz")])
    assert result.exit_code == 0, result.output
    assert result.output.strip().endswith(": valid")


def test_validate_prints_problems_and_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    bundle = copy_fixture(tmp_path / "bundle")
    rows = (bundle / "coverage.jsonl").read_text().splitlines()
    row = json.loads(rows[0])
    row["file"] = "/etc/passwd"
    (bundle / "coverage.jsonl").write_text("\n".join([json.dumps(row), *rows[1:]]) + "\n")
    result = runner.invoke(cli, ["bundle", "validate", str(bundle)])
    assert result.exit_code == 1
    assert "coverage.jsonl:1: file: not a safe relative POSIX path: '/etc/passwd'" in result.output
    as_json = runner.invoke(cli, ["bundle", "validate", "--json", str(bundle)])
    assert as_json.exit_code == 1
    first = json.loads(as_json.output.splitlines()[0])
    assert first == {
        "file": "coverage.jsonl", "row": 1, "field": "file", "code": "path", "fatal": False,
        "message": "not a safe relative POSIX path: '/etc/passwd'",
    }  # fmt: skip


def test_info(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["bundle", "info", str(NEUTRAL_TS / "bundle")])
    assert result.exit_code == 0, result.output
    assert "language    typescript" in result.output and "vitest-coverset-fixture 0.0.1" in result.output
    assert "tests.jsonl            12" in result.output and "limits and checks hit: none" in result.output
    bundle = copy_fixture(tmp_path / "bundle")
    with open(bundle / "shapes.jsonl", "a") as fh:
        fh.write(json.dumps({"kind": "obs", "func": ["src/x.ts", "f", 1], "param": "p", "shape": "int|weird",
                             "caller": {"test": "t"}, "samples": 1, "tests": []}) + "\n")  # fmt: skip
    summary = json.loads(runner.invoke(cli, ["bundle", "info", "--json", str(bundle)]).output)
    assert summary["languages"] == ["typescript"]
    assert summary["limits_hit"] == {"shapes.shape_key": 1}  # the row is dropped at its first problem
    assert summary["rows"]["shapes"] == 32 and summary["fatal"] is None


def test_convert_a_python_bundle(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "neutral"
    result = runner.invoke(
        cli,
        [
            "bundle",
            "convert",
            str(MINIMAL / "bundle.tar.gz"),
            "--to",
            str(out),
            "--source-root",
            str(MINIMAL / "repo"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "0 problems" in result.output
    assert validate(out) == []
    converted = read_bundle(out)
    original = read_python_bundle(MINIMAL / "bundle", source_root=MINIMAL / "repo")
    assert converted.meta["bundle"]["language"] == "python"
    assert converted.meta["bundle"]["producer"]["name"] in ("pytest-coverset", "coverset-bundle-convert")
    assert {r.qualname for r in converted.functions} >= {"add", "classify", "Box.total"}
    add = next(r for r in converted.functions if r.qualname == "add")
    assert set(converted.types[add.triple]) >= {"a", "b", "return"}
    plain = strip_reader_meta(read_bundle(out))
    expected = strip_reader_meta(conform(original))
    assert (plain.tests, plain.collected, plain.shapes, plain.static) == (
        expected.tests, expected.collected, expected.shapes, expected.static,
    )  # fmt: skip
    gz = runner.invoke(
        cli,
        [
            "bundle",
            "convert",
            str(MINIMAL / "bundle"),
            "--to",
            str(tmp_path / "gz"),
            "--gzip",
            "--shard",
            "3",
        ],
    )
    assert gz.exit_code == 0, gz.output
    assert (tmp_path / "gz" / "tests.jsonl.gz").is_file()
    assert read_bundle(tmp_path / "gz").meta["bundle"]["shard"] == 3


def test_convert_refuses_an_unreadable_bundle(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = runner.invoke(cli, ["bundle", "convert", str(tmp_path / "empty"), "--to", str(tmp_path / "out")])
    assert result.exit_code == 1 and "cannot read" in result.output
