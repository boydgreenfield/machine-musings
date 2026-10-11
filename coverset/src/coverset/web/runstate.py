"""A run's live view, rebuilt from its ``run_events`` (the same code serves live runs and replays).

:class:`RunState` applies events one at a time (``status``, ``shard``, ``test``, ``progress``,
``failure``, ``verdict``, ``done``; ``log`` is counted only) and answers what the run page draws:
shard lanes for the Gantt chart, outcome counts, confirmed failures and the verdict so far. Gantt
blocks are built from test events: a shard runs its tests one after another, so a test's block
spans from the previous report on that shard (or, for the first test, its own duration before its
report) to its own report; a retry that passes turns the original block into a flaky one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = ["Block", "Lane", "RunState", "TestReport"]

FAILED = frozenset({"failed", "error"})


@dataclass(slots=True)
class TestReport:
    """The latest report of one test."""

    __test__ = False

    nodeid: str
    outcome: str
    shard: int | None
    at: datetime
    duration_s: float
    failed_phase: str | None = None
    longrepr: str | None = None
    flaky: bool = False


@dataclass(slots=True)
class Block:
    """One test's span on a shard lane (seconds from the run's clock start)."""

    nodeid: str
    start: float
    end: float
    outcome: str
    flaky: bool = False


@dataclass(slots=True)
class Lane:
    """One shard."""

    shard: int
    status: str = "pending"
    planned: int = 0
    started: datetime | None = None
    finished: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    reports: list[TestReport] = field(default_factory=list)


@dataclass(slots=True)
class RunState:
    """Everything the run page shows that changes while the run streams."""

    run_id: str
    last_seq: int = 0
    clock: datetime | None = None
    status: str = "queued"
    planned_tests: int | None = None
    planned_shards: int | None = None
    wave1: int | None = None
    prior: str | None = None
    lanes: dict[int, Lane] = field(default_factory=dict)
    tests: dict[str, TestReport] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    early_verdict: dict[str, Any] | None = None
    done: dict[str, Any] | None = None
    logs: int = 0

    def apply(self, seq: int, kind: str, payload: dict[str, Any] | None, at: datetime) -> None:
        """Apply one stored event."""
        data = payload or {}
        self.last_seq = max(self.last_seq, seq)
        self.clock = at if self.clock is None or at > self.clock else self.clock
        if kind == "status":
            self.status = str(data.get("status") or self.status)
            if data.get("status") == "planned":
                self.planned_tests = _int(data.get("tests"))
                self.planned_shards = _int(data.get("shards"))
                self.wave1 = _int(data.get("wave1"))
                self.prior = data.get("prior") if isinstance(data.get("prior"), str) else None
        elif kind == "shard":
            shard = _int(data.get("shard"))
            if shard is None:
                return
            lane = self.lanes.setdefault(shard, Lane(shard))
            lane.status = str(data.get("status") or lane.status)
            if data.get("status") == "running":
                lane.started = lane.started or at
                lane.planned = _int(data.get("tests")) or lane.planned
            else:
                lane.finished = at
                lane.exit_code = _int(data.get("exit_code"))
                lane.error = data.get("error") if isinstance(data.get("error"), str) else None
        elif kind == "test":
            self._test(data, at)
        elif kind == "progress":
            self.progress = dict(data)
        elif kind == "failure":
            self.failures.append(dict(data))
        elif kind == "verdict":
            if data.get("early"):
                self.early_verdict = dict(data)
            self.verdict = dict(data)
        elif kind == "done":
            self.done = dict(data)
            self.status = "finished"
        elif kind == "log":
            self.logs += 1

    def _test(self, data: dict[str, Any], at: datetime) -> None:
        nodeid = data.get("nodeid")
        if not isinstance(nodeid, str):
            return
        shard = _int(data.get("shard"))
        outcome = str(data.get("outcome") or "passed")
        duration = _float(data.get("duration_s"))
        if data.get("retry") is not None:
            existing = self.tests.get(nodeid)
            if existing is not None and data.get("flaky"):
                existing.flaky = True
                existing.outcome = "passed"
            return
        report = TestReport(
            nodeid=nodeid,
            outcome=outcome,
            shard=shard,
            at=at,
            duration_s=duration,
            failed_phase=data.get("failed_phase") if isinstance(data.get("failed_phase"), str) else None,
            longrepr=data.get("longrepr") if isinstance(data.get("longrepr"), str) else None,
            flaky=bool(data.get("flaky")),
        )
        self.tests[nodeid] = report
        if shard is not None:
            lane = self.lanes.setdefault(shard, Lane(shard, status="running"))
            lane.started = lane.started or at
            lane.reports.append(report)

    # -- derived views ----------------------------------------------------------------------------

    @property
    def counts(self) -> Counter[str]:
        """Outcome counts of the tests reported so far (flaky tests count as passed)."""
        return Counter(r.outcome for r in self.tests.values())

    @property
    def reported(self) -> int:
        """Tests reported so far."""
        return len(self.tests)

    def failed_reports(self) -> list[TestReport]:
        """Failed and errored tests, in report order."""
        return sorted((r for r in self.tests.values() if r.outcome in FAILED), key=lambda r: r.at)

    def blocks(self, origin: datetime) -> dict[int, list[Block]]:
        """Gantt blocks per shard, in seconds after ``origin``.

        A block runs from the previous report on its shard to its own report. The first test of a
        shard also carries pytest's start-up, so its block gets the lane's median block width
        (never more than the time since the shard started) instead.
        """
        lanes: dict[int, list[Block]] = {}
        for shard, lane in sorted(self.lanes.items()):
            reports = sorted(lane.reports, key=lambda r: r.at)
            ends = [(r.at - origin).total_seconds() for r in reports]
            gaps = sorted(b - a for a, b in zip(ends, ends[1:], strict=False) if b > a)
            typical = gaps[len(gaps) // 2] if gaps else 0.0
            started = (lane.started - origin).total_seconds() if lane.started is not None else None
            out: list[Block] = []
            for index, (report, end) in enumerate(zip(reports, ends, strict=True)):
                if index:
                    start = ends[index - 1]
                else:
                    start = end - max(report.duration_s, typical)
                    if started is not None:
                        start = max(start, started)
                out.append(Block(report.nodeid, min(start, end), end, report.outcome, report.flaky))
            lanes[shard] = out
        return lanes


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
