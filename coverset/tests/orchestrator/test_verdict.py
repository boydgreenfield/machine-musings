"""Verdict numbers: the file-order estimate, first-failure timing, summary JSON, forge identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from coverset.config import Settings
from coverset.db.models import Repo, Run
from coverset.forge.verdict import FunctionRow, InvariantRow, MissingClass, VerdictSummary
from coverset.lang.base import CollectedItem
from coverset.orchestrator.verdict import (
    TestState,
    build_summary,
    file_order_estimate,
    repo_ref,
    summary_from_json,
    summary_to_json,
    t0_of,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def item(nodeid: str, line: int = 1) -> CollectedItem:
    return CollectedItem(nodeid, nodeid.split("::")[0], line, (), (), False)


@dataclass
class StubPlan:
    """The parts of a RunPlan the verdict reads."""

    collected: list[CollectedItem]
    durations: dict[str, float]
    order: list[str] = field(default_factory=list)

    @property
    def file_ranks(self) -> dict[str, int]:
        return {i.nodeid: n for n, i in enumerate(self.collected, start=1)}

    @property
    def items_by_nodeid(self) -> dict[str, CollectedItem]:
        return {i.nodeid: i for i in self.collected}


def test_file_order_estimate_sums_p50_of_earlier_tests() -> None:
    plan: Any = StubPlan(
        collected=[item("a.py::t1"), item("a.py::t2"), item("b.py::t3"), item("c.py::t4")],
        durations={"a.py::t1": 1.0, "a.py::t2": 2.0, "c.py::t4": 4.0},
    )
    # sandbox ready after 0.5 s; t3 is preceded by t1 (1.0) and t2 (2.0)
    assert file_order_estimate(plan, "b.py::t3", 0.5) == 3.5
    # t4 is preceded by t3 whose p50 is unknown: the median of the known ones (2.0) stands in
    assert file_order_estimate(plan, "c.py::t4", 0.0) == 5.0
    assert file_order_estimate(plan, "a.py::t1", 0.25) == 0.25
    assert file_order_estimate(plan, "missing", 0.0) is None


def test_t0_prefers_webhook_arrival() -> None:
    run = Run(
        timeline={"queued": (T0 + timedelta(seconds=2)).isoformat()}, created_at=T0 + timedelta(seconds=5)
    )
    assert t0_of(run) == T0 + timedelta(seconds=2)
    run.timeline["webhook_received"] = T0.isoformat()
    assert t0_of(run) == T0
    assert t0_of(Run(timeline={}, created_at=T0)) == T0


def test_build_summary_failure_numbers() -> None:
    settings = Settings.for_tests(public_url="http://ci.example.test")
    run = Run(
        id="run_x",
        timeline={"queued": T0.isoformat(), "first_failure": (T0 + timedelta(seconds=1.25)).isoformat()},
        created_at=T0,
        rank_coverset=1,
        rank_file_order=66,
        t_first_failure_file_order_est=41.5,
    )
    plan: Any = StubPlan(collected=[item("t.py::a", 3), item("t.py::b", 9)], durations={})
    states = {
        "t.py::a": TestState(
            "t.py::a", outcome="failed", confirmed=True, longrepr="x\nE   assert 1 == 2\nmore"
        ),
        "t.py::b": TestState("t.py::b", outcome="failed", confirmed=True, preexisting=True),
    }
    summary = build_summary(settings, run, plan, states, conclusion="failure", reason="1 confirmed failure")
    assert summary.label == "FAIL"
    assert summary.first_failure_s == 1.25
    assert (summary.rank_coverset, summary.rank_file_order, summary.t_first_failure_file_order_est) == (
        1,
        66,
        41.5,
    )
    assert summary.title == "t.py::a failed"
    assert [(f.nodeid, f.message, f.path, f.line) for f in summary.failures] == [
        ("t.py::a", "assert 1 == 2", "t.py", 3)
    ]
    assert summary.details_url == "http://ci.example.test/runs/run_x"
    assert summary.tests_total == 2


def test_summary_json_round_trip() -> None:
    summary = VerdictSummary(
        conclusion="success",
        title="2 tests passed",
        patch_pct=100.0,
        patch_covered=3,
        patch_total=3,
        function_rows=(
            FunctionRow(
                "f", "modified", 2, 100.0, ("x:negative",), (InvariantRow("inv", "satisfied", "ok"),)
            ),
        ),
        missing_input_classes=(MissingClass("f", "x", "x:negative", "st.integers(max_value=-1)"),),
        invariants=(InvariantRow("inv", "satisfied", "ok"),),
    )
    data = summary_to_json(summary)
    assert data["label"] == "PASS with gaps"
    assert summary_from_json(data) == summary


def test_repo_ref_for_local_and_forge_repos() -> None:
    local = Repo(
        id=1,
        forge="local",
        external_id="ab" * 32,
        full_name="local/demo",
        default_branch="main",
        private=True,
    )
    ref = repo_ref(local)
    assert ref.external_id == int("ab" * 6, 16) and ref.full_name == "local/demo" and ref.id == 1
    forge = Repo(
        id=2, forge="github", external_id="424242", full_name="o/r", default_branch="main", private=False
    )
    assert repo_ref(forge).external_id == 424242


def test_states_default_to_not_run(tmp_path: Path) -> None:
    state = TestState("t.py::x")
    assert (state.outcome, state.attempts, state.flaky, state.confirmed) == ("not_run", 0, False, False)
