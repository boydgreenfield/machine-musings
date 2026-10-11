"""The overhead script runs all three modes on the fixture project and reports medians."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_overhead.py"


def test_measure_overhead_script(pytester: pytest.Pytester) -> None:
    output = pytester.path / "numbers.json"
    result = pytester.run(sys.executable, str(SCRIPT), "--repeats", "1", "--output", str(output))
    assert result.ret == 0
    numbers = json.loads(output.read_text())
    assert {"plain_s", "coverage_s", "coverage_shapes_s", "overhead_x"} <= numbers.keys()
    assert numbers["plain_s"] > 0 and numbers["overhead_x"] > 0
    assert all(len(runs) == 1 for runs in numbers["runs_s"].values())
