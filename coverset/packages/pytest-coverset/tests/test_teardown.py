"""Teardown: the tool id is freed and a second session in the same process instruments again."""

from __future__ import annotations

import json

import pytest
from support import read_shapes

TWO_SESSIONS = """
import json
import sys

import pytest

results = {}
for name in ("first", "second"):
    code = pytest.main(["-q", "-p", "no:cacheprovider", "--coverset-out", name, "--coverset-source", "fixturepkg"])
    results[name] = {
        "exit": int(code),
        "tool_after": sys.monitoring.get_tool(4),
        "trace_after": sys.gettrace() is not None,
    }
print("RESULT " + json.dumps(results))
"""


def test_second_session_in_same_process(project: pytest.Pytester) -> None:
    script = project.makepyfile(two_sessions=TWO_SESSIONS)
    result = project.runpython(script)
    line = next(line for line in result.outlines if line.startswith("RESULT "))
    summary = json.loads(line.removeprefix("RESULT "))
    for name in ("first", "second"):
        assert summary[name] == {"exit": 0, "tool_after": None, "trace_after": False}
        meta = json.loads((project.path / name / "meta.json").read_text())
        assert meta["tool_id"] == 4 and meta["tracer"] == "CTracer" and meta["tracer_errors"] == 0
        assert meta["counts"]["passed"] == 24
    second = read_shapes(project.path / "second")
    assert ["src/fixturepkg/core.py", "validate_email", 13] in second.header["funcs"]
    assert second.keys("validate_email", "email")
