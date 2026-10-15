"""Tests for scripts/gen_bench_suite.py, the synthetic fan-out benchmark suite generator."""

from __future__ import annotations

import ast
import importlib.util
import math
import re
import subprocess
import sys
import time
import tomllib
import zlib
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_bench_suite.py"
BUSY_CALL = re.compile(r"^    busy\((\d+)\)$", re.MULTILINE)


def _generate(out: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the generator CLI into ``out`` with extra arguments."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` (relative POSIX path) to its bytes."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a module from an explicit file path without touching ``sys.path``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_functions(tests_dir: Path) -> list[ast.FunctionDef]:
    """Return every top-level ``test_*`` function in the generated bench files."""
    found: list[ast.FunctionDef] = []
    for path in sorted(tests_dir.glob("test_bench_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    return found


def test_same_seed_is_byte_identical(tmp_path: Path) -> None:
    args = ("--tests", "60", "--files", "5", "--seed", "11")
    _generate(tmp_path / "a", *args)
    _generate(tmp_path / "b", *args)
    first, second = _snapshot(tmp_path / "a"), _snapshot(tmp_path / "b")
    assert first == second
    assert "tests/test_bench_05.py" in first


def test_different_seed_changes_durations(tmp_path: Path) -> None:
    _generate(tmp_path / "a", "--tests", "40", "--files", "2", "--seed", "1")
    _generate(tmp_path / "b", "--tests", "40", "--files", "2", "--seed", "2")
    a = BUSY_CALL.findall((tmp_path / "a" / "tests" / "test_bench_01.py").read_text())
    b = BUSY_CALL.findall((tmp_path / "b" / "tests" / "test_bench_01.py").read_text())
    assert a != b


def test_test_count_matches_and_splits_evenly(tmp_path: Path) -> None:
    _generate(tmp_path, "--tests", "103", "--files", "4")
    per_file = [
        len(BUSY_CALL.findall(p.read_text())) for p in sorted((tmp_path / "tests").glob("test_bench_*.py"))
    ]
    assert per_file == [26, 26, 26, 25]
    names = [f.name for f in _test_functions(tmp_path / "tests")]
    assert len(names) == 103
    assert len(set(names)) == 103


def test_regenerating_with_fewer_files_removes_stale_ones(tmp_path: Path) -> None:
    _generate(tmp_path, "--tests", "30", "--files", "6")
    _generate(tmp_path, "--tests", "30", "--files", "3")
    assert sorted(p.name for p in (tmp_path / "tests").glob("test_bench_*.py")) == [
        "test_bench_01.py",
        "test_bench_02.py",
        "test_bench_03.py",
    ]


def test_durations_respect_clamp(tmp_path: Path) -> None:
    _generate(
        tmp_path, "--tests", "500", "--files", "5", "--sigma", "1.5", "--min-ms", "60", "--max-ms", "90"
    )
    durations = [
        int(ms) for p in (tmp_path / "tests").glob("*.py") for ms in BUSY_CALL.findall(p.read_text())
    ]
    assert len(durations) == 500
    assert min(durations) == 60
    assert max(durations) == 90
    readme = (tmp_path / "README.md").read_text()
    assert f"Expected serial runtime: {sum(durations) / 1000:.1f} s" in readme


def test_pyproject_parses_with_coverset_table(tmp_path: Path) -> None:
    _generate(tmp_path, "--tests", "10", "--files", "2")
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert data["project"]["name"] == "benchsuite"
    assert data["project"]["requires-python"] == ">=3.12"
    assert data["dependency-groups"]["dev"] == ["pytest==9.1.1"]
    assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert data["tool"]["coverset"] == {
        "source": ["benchsuite"],
        "source_root": "src",
        "max_shards": 32,
        "target_shard_seconds": 10,
        "fail_fast": "finish_in_background",
    }


def test_invalid_arguments_are_rejected(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(tmp_path), "--min-ms", "500", "--max-ms", "100"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--min-ms" in result.stderr


def test_generated_work_library_and_tests_are_correct(tmp_path: Path) -> None:
    """Load the generated library and run every generated assertion with busy() stubbed out."""
    _generate(tmp_path, "--tests", "40", "--files", "2")
    work = _load_module("_bench_work_under_test", tmp_path / "src" / "benchsuite" / "work.py")
    assert work.checksum(b"") == 1
    assert work.collatz_steps(27) == 111
    assert work.primes_below(30) == [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
    assert work.run_length("aabccc") == [("a", 2), ("b", 1), ("c", 3)]
    with pytest.raises(ValueError):
        work.collatz_steps(0)

    namespace = {
        name: getattr(work, name) for name in ("checksum", "collatz_steps", "primes_below", "run_length")
    }
    namespace |= {"busy": lambda ms: 0, "math": math, "zlib": zlib}
    for fn in _test_functions(tmp_path / "tests"):
        code = compile(ast.Module(body=fn.body, type_ignores=[]), fn.name, "exec")
        exec(code, dict(namespace))


def test_busy_burns_roughly_the_requested_time(tmp_path: Path) -> None:
    _generate(tmp_path, "--tests", "2", "--files", "1")
    work = _load_module("_bench_work_timing", tmp_path / "src" / "benchsuite" / "work.py")
    work.busy(5)  # warm up
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        work.busy(30)
        timings.append((time.perf_counter() - start) * 1000)
    assert 20 <= min(timings) <= 90, timings
