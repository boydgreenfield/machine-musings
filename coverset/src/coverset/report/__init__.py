"""``coverset report``: a static, self-contained HTML coverage report of one test run.

The rich equivalent of ``coverage html``: line and branch coverage per test, the tests behind every
line, the input classes every function's tests passed in (and those they never did), one page per
file, function and test, all from one run's bundle and with no server. :func:`build_report` writes
it; :mod:`.load` reads the sources (a ``--coverset-out`` directory, a bundle archive, a neutral
bundle, or a run in the facts store). See ``docs/report.md``.
"""

from __future__ import annotations

from coverset.report.build import BuildResult, build_report, default_out
from coverset.report.facts import BundleFacts, ReportInput, TestInfo
from coverset.report.output import MARKER, OutDirError

__all__ = [
    "MARKER",
    "BuildResult",
    "BundleFacts",
    "OutDirError",
    "ReportInput",
    "TestInfo",
    "build_report",
    "default_out",
]
