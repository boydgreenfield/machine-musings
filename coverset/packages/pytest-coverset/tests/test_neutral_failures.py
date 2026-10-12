"""The neutral export when things go wrong: the Python bundle comes first, failures are recorded in
``meta.json``, nothing claims success it did not have, and odd sources still index."""

from __future__ import annotations

import gzip
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from support import NONCE, events

from pytest_coverset.neutral import line_count

MODULE = "def f(x):\n    if x:\n        return 1\n    return 2\n"
TESTS = (
    "from npkg.m import f\n\n\ndef test_a():\n    assert f(1) == 1\n\n\ndef test_b():\n    assert f(0) == 2\n"
)


def _project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, **modules: str) -> Path:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    monkeypatch.delenv("COVERSET_NEUTRAL", raising=False)
    root = pytester.path
    (root / "src" / "npkg").mkdir(parents=True)
    (root / "src" / "npkg" / "__init__.py").write_text("")
    for name, text in {"m": MODULE, **modules}.items():
        path = root / "src" / "npkg" / f"{name}.py"
        path.write_bytes(text.encode("latin-1") if name == "latin" else text.encode("utf-8"))
    (root / "tests").mkdir()
    (root / "tests" / "test_n.py").write_text(TESTS)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n'
    )
    return root / "out"


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess(
        "--coverset-out", "out", "--coverset-source", "npkg", "--coverset-neutral", *args
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    data = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def _setup_tar_is_dir(out: Path) -> None:
    (out / "neutral.tar.gz" / "x").mkdir(parents=True)


def _setup_neutral_is_file(out: Path) -> None:
    out.mkdir()
    (out / "neutral").write_text("{}")


def _setup_member_is_dir(out: Path) -> None:
    (out / "neutral" / "tests.jsonl.gz" / "x").mkdir(parents=True)


def _setup_read_only(out: Path) -> None:
    (out / "neutral").mkdir(parents=True)
    (out / "neutral").chmod(0o555)


@pytest.mark.parametrize(
    "setup", [_setup_tar_is_dir, _setup_neutral_is_file, _setup_member_is_dir, _setup_read_only]
)
def test_a_failed_export_is_recorded_and_never_announced(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, setup: Any
) -> None:
    out = _project(pytester, monkeypatch)
    setup(out)
    if setup is _setup_read_only and os.access(out / "neutral", os.W_OK):
        pytest.skip("running as a user that ignores directory permissions")
    try:
        result = _run(pytester)
    finally:
        if (out / "neutral").is_dir():
            (out / "neutral").chmod(0o755)
    result.assert_outcomes(passed=2)
    kinds = [e["kind"] for e in events(pytester)]
    assert "neutral_written" not in kinds and kinds[-2:] == ["bundle_written", "done"]
    assert (out / "bundle.tar.gz").is_file()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["neutral"]["status"] == "failed" and meta["neutral"]["requested"] is True
    assert any(error.startswith("neutral") for error in meta["errors"])
    neutral = out / "neutral"
    assert not (neutral.is_dir() and (neutral / "manifest.json").exists())  # no manifest over a gap
    assert not (out / "neutral.tar.gz").is_file()


def test_stale_files_of_either_variant_are_replaced(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _project(pytester, monkeypatch)
    (out / "neutral").mkdir(parents=True)
    (out / "neutral" / "tests.jsonl").write_text('{"id": "stale", "file": "x.py", "outcome": "passed"}\n')
    (out / "neutral" / "links.jsonl.gz").write_bytes(gzip.compress(b'{"test": "x", "invariant": "y"}\n'))
    _run(pytester).assert_outcomes(passed=2)
    assert (
        not (out / "neutral" / "tests.jsonl").exists() and not (out / "neutral" / "links.jsonl.gz").exists()
    )
    assert json.loads((out / "meta.json").read_text())["neutral"]["status"] == "written"
    assert [row["id"] for row in _rows(out / "neutral" / "tests.jsonl.gz")] == [
        "tests/test_n.py::test_a",
        "tests/test_n.py::test_b",
    ]


def test_an_interrupt_during_the_export_keeps_the_python_bundle(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _project(pytester, monkeypatch)
    pytester.makeconftest(
        """
        import pytest_coverset.neutral as neutral

        def interrupted(self, static):
            raise KeyboardInterrupt("Ctrl-C during the export")
            yield

        neutral.NeutralWriter._function_rows = interrupted
        """
    )
    result = _run(pytester)
    assert result.ret != 0
    kinds = [e["kind"] for e in events(pytester)]
    assert "bundle_written" in kinds and "neutral_written" not in kinds
    assert (out / "bundle.tar.gz").is_file()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["counts"]["passed"] == 2 and meta["neutral"]["status"] == "interrupted"
    assert not (out / "neutral" / "manifest.json").exists()


def test_sources_decode_like_cpython_and_one_bad_file_spares_the_rest(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = "def total():\n    return " + "+".join(["1"] * 3000) + "\n"
    out = _project(
        pytester,
        monkeypatch,
        bom="\ufeffdef with_bom(a):\n    return a\n",
        latin="# -*- coding: latin-1 -*-\ndef caf\u00e9_label(b):\n    return 'caf\u00e9' + b\n",
        deep=deep,
        cr="def lone(c):\r    return c\r\rdef other(d):\r    return d\r",
    )
    (pytester.path / "tests" / "test_more.py").write_text(
        "from npkg import bom, cr, deep, latin\n\n\n"
        "def test_more():\n"
        "    assert bom.with_bom(1) == 1 and deep.total() == 3000 and cr.lone(2) == 2\n"
        "    assert latin.caf\u00e9_label('!') == 'caf\u00e9!'\n"
    )
    _run(pytester).assert_outcomes(passed=3)
    neutral = out / "neutral"
    functions = {(r["path"], r["qualname"]) for r in _rows(neutral / "functions.jsonl.gz")}
    assert {("src/npkg/bom.py", "with_bom"), ("src/npkg/latin.py", "caf\u00e9_label")} <= functions
    assert {("src/npkg/m.py", "f"), ("src/npkg/cr.py", "lone"), ("src/npkg/cr.py", "other")} <= functions
    assert not any(path == "src/npkg/deep.py" for path, _ in functions)
    meta = json.loads((out / "meta.json").read_text())
    assert meta["neutral"]["dropped"]["functions_unindexable_sources"] == 1
    files = {r["path"]: r for r in _rows(neutral / "files.jsonl.gz")}
    assert files["src/npkg/cr.py"]["n_lines"] == 5 >= max(files["src/npkg/cr.py"]["statements"])
    assert line_count(b"a\rb\r\nc\nd") == 4 and line_count(b"") == 0 and line_count(b"x\n") == 1


def test_overlong_test_ids_are_left_out_and_counted(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _project(pytester, monkeypatch)
    long_param = "x" * 600
    (pytester.path / "tests" / "test_long.py").write_text(
        "import pytest\nfrom npkg.m import f\n\n\n"
        f"@pytest.mark.parametrize('v', [{long_param!r}, 'short'])\n"
        "def test_param(v):\n    assert f(v) == 1\n"
    )
    _run(pytester).assert_outcomes(passed=4)
    neutral = out / "neutral"
    ids = [row["id"] for row in _rows(neutral / "tests.jsonl.gz")]
    assert len(ids) == 3 and all(len(i) <= 512 for i in ids)
    shapes = [r for r in _rows(neutral / "shapes.jsonl.gz") if r["kind"] == "obs"]
    assert shapes and all(all(len(t) <= 512 for t in r["tests"]) for r in shapes)
    assert any("test_param[short]" in t for r in shapes for t in r["tests"])
    dropped = json.loads((out / "meta.json").read_text())["neutral"]["dropped"]
    assert dropped["tests_long_ids"] == 1 and dropped["coverage_long_test_ids"] == 1


def test_a_measured_file_missing_from_coverage_json_keeps_its_lines(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _project(pytester, monkeypatch, gone="def gone(x):\n    return x\n")
    (pytester.path / "tests" / "test_gone.py").write_text(
        "import pathlib\nfrom npkg import gone\n\n\n"
        "def test_gone():\n"
        "    assert gone.gone(3) == 3\n"
        "    pathlib.Path(gone.__file__).unlink()  # coverage.json cannot report a vanished source\n"
    )
    _run(pytester).assert_outcomes(passed=3)
    neutral = out / "neutral"
    assert "src/npkg/gone.py" not in {r["path"] for r in _rows(neutral / "files.jsonl.gz")}
    kept = [r for r in _rows(neutral / "coverage.jsonl.gz") if r["file"] == "src/npkg/gone.py"]
    assert any(r["test"] == "tests/test_gone.py::test_gone" and 2 in r["lines"] for r in kept)
    dropped = json.loads((out / "meta.json").read_text())["neutral"]["dropped"]
    assert dropped["coverage_files_not_in_files"] == 1


def test_meta_json_carries_the_outcome_counters(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _project(pytester, monkeypatch)
    _run(pytester).assert_outcomes(passed=2)
    meta = json.loads((out / "meta.json").read_text())
    written = next(e for e in events(pytester) if e["kind"] == "neutral_written")
    assert meta["neutral"]["rows"] == written["rows"] and meta["neutral"]["dropped"] == written["dropped"]
    with __import__("tarfile").open(out / "bundle.tar.gz") as tar:
        inner = json.load(tar.extractfile("meta.json"))  # type: ignore[arg-type]
    assert inner["neutral"] == {"requested": True, "status": "pending"}  # packed before the export
    assert stat.S_ISREG((out / "neutral.tar.gz").stat().st_mode)
