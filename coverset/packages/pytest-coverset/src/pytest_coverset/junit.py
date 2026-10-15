"""JUnit XML (xunit2-like) from coverset ``test`` events, for CI systems that render it."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_ILLEGAL_XML = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")
_COUNTED = {"failed": "failures", "error": "errors", "skipped": "skipped", "xfailed": "skipped"}


def _clean(text: str) -> str:
    return _ILLEGAL_XML.sub("\N{REPLACEMENT CHARACTER}", text)


def split_nodeid(nodeid: str) -> tuple[str, str]:
    """``tests/test_a.py::TestX::test_y[1]`` -> (``tests.test_a.TestX``, ``test_y[1]``), like pytest's junitxml."""
    parts = nodeid.split("::")
    module = parts[0].removesuffix(".py").replace("/", ".")
    if len(parts) == 1:
        return module, module
    return ".".join([module, *parts[1:-1]]), parts[-1]


def junit_tree(tests: Sequence[Mapping[str, Any]], suite_name: str = "coverset") -> ET.ElementTree:
    """Build the XML tree: one ``testcase`` per test event, in arrival order."""
    totals = {"tests": len(tests), "failures": 0, "errors": 0, "skipped": 0}
    total_time = 0.0
    suite = ET.Element(
        "testsuite", name=suite_name, timestamp=datetime.now(UTC).isoformat(timespec="seconds")
    )
    for event in tests:
        nodeid = str(event.get("nodeid", ""))
        outcome = str(event.get("outcome", ""))
        duration = float(event.get("duration_s") or 0.0)
        total_time += duration
        classname, name = split_nodeid(nodeid)
        case = ET.SubElement(
            suite, "testcase", classname=_clean(classname), name=_clean(name), time=f"{duration:.3f}"
        )
        file = nodeid.split("::", 1)[0]
        if file:
            case.set("file", _clean(file))
        if outcome in _COUNTED:
            totals[_COUNTED[outcome]] += 1
        longrepr = _clean(str(event.get("longrepr") or ""))
        if outcome == "failed":
            failure = ET.SubElement(
                case, "failure", message=longrepr.splitlines()[-1][:200] if longrepr else "failed"
            )
            failure.text = longrepr
        elif outcome == "error":
            phase = event.get("failed_phase") or "setup"
            error = ET.SubElement(case, "error", message=f"error in {phase}")
            error.text = longrepr
        elif outcome in ("skipped", "xfailed"):
            reason = _clean(str(event.get("reason") or outcome))
            ET.SubElement(
                case,
                "skipped",
                type="pytest.xfail" if outcome == "xfailed" else "pytest.skip",
                message=reason,
            )
    for key, value in totals.items():
        suite.set(key, str(value))
    suite.set("time", f"{total_time:.3f}")
    root = ET.Element("testsuites", name=suite_name)
    root.append(suite)
    for key, value in totals.items():
        root.set(key, str(value))
    root.set("time", f"{total_time:.3f}")
    return ET.ElementTree(root)


def write_junit(path: str | os.PathLike[str], tests: Sequence[Mapping[str, Any]]) -> None:
    """Write JUnit XML for ``tests`` to ``path``."""
    tree = junit_tree(tests)
    ET.indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)
