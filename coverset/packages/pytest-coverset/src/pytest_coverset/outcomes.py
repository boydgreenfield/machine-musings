"""Fold a test's setup/call/teardown reports into one final result row.

Outcomes follow pytest's vocabulary: ``passed``, ``failed`` (the test body failed, including a strict
XPASS), ``error`` (setup or teardown failed), ``skipped``, ``xfailed`` and ``xpassed``. ``fail_sig`` is
the blake2b-8 hex digest of the exception type plus the crash location, so the same failure on two
commits gets the same signature even when the message differs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from pytest_coverset.paths import norm_path

MAX_LONGREPR_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class TestResult:
    """The authoritative result of one test (one line of ``results.jsonl``)."""

    __test__ = False

    nodeid: str
    file: str
    outcome: str
    duration_s: float
    failed_phase: str | None
    fail_sig: str | None
    longrepr: str | None
    truncated: bool
    reason: str | None
    start: float | None
    stop: float | None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready mapping of every field (every field is a scalar: no ``asdict`` deep copy)."""
        return {
            "nodeid": self.nodeid,
            "file": self.file,
            "outcome": self.outcome,
            "duration_s": self.duration_s,
            "failed_phase": self.failed_phase,
            "fail_sig": self.fail_sig,
            "longrepr": self.longrepr,
            "truncated": self.truncated,
            "reason": self.reason,
            "start": self.start,
            "stop": self.stop,
        }


def exception_name(exc_type: type[BaseException]) -> str:
    """``module.QualName`` of an exception type (bare name for builtins)."""
    module = getattr(exc_type, "__module__", "") or ""
    qualname = getattr(exc_type, "__qualname__", exc_type.__name__)
    return qualname if module == "builtins" else f"{module}.{qualname}"


def truncate_utf8(text: str, limit: int = MAX_LONGREPR_BYTES) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes; return the text and whether it was cut.

    The result is always valid UTF-8: a lone surrogate (``"\\udcff"`` from a ``surrogateescape``d
    file name or a hand-built message) becomes ``?``, so it can never fail a later encode in
    ``results.jsonl``, the bundle's JSON files or the control plane's database.
    """
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    return data[:limit].decode("utf-8", errors="ignore"), True


def fail_signature(exc_type: str | None, path: str | None, line: int | None) -> str:
    """blake2b-8 hex of ``<exception type>\\0<path>:<line>``."""
    material = f"{exc_type or '?'}\0{path or '?'}:{line if line is not None else '?'}"
    return hashlib.blake2b(material.encode("utf-8"), digest_size=8).hexdigest()


def _crash_location(report: pytest.TestReport, repo_root: str) -> tuple[str | None, int | None]:
    crash = getattr(report.longrepr, "reprcrash", None)
    if crash is None:
        path, line, _ = report.location
        return path, (line + 1) if line is not None else None
    rel = norm_path(crash.path, repo_root)
    return (rel if rel is not None else crash.path), crash.lineno


def _skip_reason(report: pytest.TestReport) -> str | None:
    """The skip/xfail reason, sanitised and cut to :data:`MAX_LONGREPR_BYTES` like a longrepr."""
    reason: str | None = None
    if hasattr(report, "wasxfail"):
        reason = str(report.wasxfail) or None
    else:
        longrepr = report.longrepr
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            reason = str(longrepr[2]).removeprefix("Skipped: ")
    return truncate_utf8(reason)[0] if reason is not None else None


def fold_reports(
    nodeid: str,
    file: str,
    reports: Sequence[pytest.TestReport],
    exc_types: Mapping[str, str],
    repo_root: str,
) -> TestResult:
    """Combine the phase reports of one test into its final :class:`TestResult`."""
    by_when = {r.when: r for r in reports}
    setup, call, teardown = by_when.get("setup"), by_when.get("call"), by_when.get("teardown")
    outcome, failed_phase, failing, reason = "passed", None, None, None
    if setup is not None and setup.failed:
        outcome, failed_phase, failing = "error", "setup", setup
    elif setup is not None and setup.skipped:
        outcome = "xfailed" if hasattr(setup, "wasxfail") else "skipped"
        reason = _skip_reason(setup)
    elif call is not None:
        if call.failed:
            outcome, failed_phase, failing = "failed", "call", call
        elif call.skipped:
            outcome = "xfailed" if hasattr(call, "wasxfail") else "skipped"
            reason = _skip_reason(call)
        elif hasattr(call, "wasxfail"):
            outcome, reason = "xpassed", _skip_reason(call)
    if teardown is not None and teardown.failed and failing is None:
        outcome, failed_phase, failing = "error", "teardown", teardown
    longrepr, truncated, fail_sig = None, False, None
    if failing is not None:
        longrepr, truncated = truncate_utf8(failing.longreprtext)
        path, line = _crash_location(failing, repo_root)
        fail_sig = fail_signature(exc_types.get(failing.when), path, line)
    starts = [r.start for r in reports if getattr(r, "start", None) is not None]
    stops = [r.stop for r in reports if getattr(r, "stop", None) is not None]
    return TestResult(
        nodeid=nodeid,
        file=file,
        outcome=outcome,
        duration_s=round(sum(r.duration for r in reports), 6),
        failed_phase=failed_phase,
        fail_sig=fail_sig,
        longrepr=longrepr,
        truncated=truncated,
        reason=reason,
        start=min(starts) if starts else None,
        stop=max(stops) if stops else None,
    )
