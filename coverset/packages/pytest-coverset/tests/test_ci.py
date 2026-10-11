"""``coverset-ci wait`` against a stub of the Addendum A HTTP API."""

from __future__ import annotations

import io
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from stub_api import NOT_FOUND, RUN_ID, SHA, Scenario, event_list, serve, verdict_found

from pytest_coverset.ci import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_NO_TESTS,
    EXIT_SERVER_ERROR,
    Waiter,
    main,
    printable,
)
from pytest_coverset.ci_http import HEARTBEAT, ApiClient, iter_sse

COUNTS_PASS = {"passed": 2, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "not_run": 0}
COUNTS_FAIL = {"passed": 1, "failed": 1, "error": 0, "skipped": 1, "xfailed": 0, "xpassed": 0, "not_run": 0}


def passing_events() -> list[dict]:
    return event_list(
        ("progress", {"done": 0, "total": 2, "passed": 0, "failed": 0, "shards": 2, "elapsed_s": 0.1}),
        (
            "test",
            {"nodeid": "tests/test_a.py::test_one", "outcome": "passed", "duration_s": 0.01, "shard": 0},
        ),
        ("log", {"shard": 1, "text": "warming up"}),
        (
            "test",
            {
                "nodeid": "tests/test_b.py::TestB::test_two[x]",
                "outcome": "passed",
                "duration_s": 0.02,
                "shard": 1,
            },
        ),
        ("progress", {"done": 2, "total": 2, "passed": 2, "failed": 0, "shards": 2, "elapsed_s": 0.4}),
        ("verdict", {"conclusion": "success", "reason": "all tests passed"}),
        ("done", {"conclusion": "success", "exit_code": 0, "counts": COUNTS_PASS}),
    )


def failing_events() -> list[dict]:
    longrepr = "def test_bad():\n>       assert 1 == 2\nE       assert 1 == 2"
    return event_list(
        ("test", {"nodeid": "tests/test_a.py::test_ok", "outcome": "passed", "duration_s": 0.01}),
        (
            "test",
            {
                "nodeid": "tests/test_a.py::test_skip",
                "outcome": "skipped",
                "duration_s": 0.0,
                "reason": "later",
            },
        ),
        (
            "test",
            {
                "nodeid": "tests/test_a.py::test_bad",
                "outcome": "failed",
                "failed_phase": "call",
                "duration_s": 0.03,
                "longrepr": longrepr,
            },
        ),
        (
            "verdict",
            {"conclusion": "failure", "reason": "tests/test_a.py::test_bad failed (confirmed on retry)"},
        ),
        ("done", {"conclusion": "failure", "exit_code": 1, "counts": COUNTS_FAIL}),
    )


def run_cli(server: str, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(
        ["wait", "--server", server, "--repo-id", "7", "--sha", SHA, "--poll-interval", "0.05", *extra],
        out=out,
    )
    return code, out.getvalue()


def test_pass() -> None:
    scenario = Scenario(verdicts=[verdict_found()], events=passing_events())
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == 0
    lines = output.splitlines()
    assert "PASSED tests/test_a.py::test_one" in lines
    assert "PASSED tests/test_b.py::TestB::test_two[x]" in lines
    assert "[shard 1] warming up" in lines
    assert "[2/2] passed 2, failed 0 on 2 shards, 0.4 s" in lines
    assert any("2 passed in" in line and "(success)" in line for line in lines)
    assert f"details: http://coverset.test/runs/{RUN_ID}" in lines
    verdict_request = scenario.requests[0]
    assert verdict_request[0] == f"/v1/verdict?repo_id=7&sha={SHA}"
    assert "authorization" not in verdict_request[1]


def test_fail_prints_longrepr_and_exits_at_the_verdict(tmp_path: Path) -> None:
    scenario = Scenario(verdicts=[verdict_found()], events=failing_events())
    junit = tmp_path / "junit.xml"
    with serve(scenario) as server:
        code, output = run_cli(server, "--junit", str(junit))
    assert code == EXIT_FAILURE
    lines = output.splitlines()
    assert "FAILED tests/test_a.py::test_bad" in lines
    assert "    >       assert 1 == 2" in lines
    assert "SKIPPED tests/test_a.py::test_skip" in lines
    assert any(line.startswith("coverset verdict: failure") for line in lines)
    assert "reason: tests/test_a.py::test_bad failed (confirmed on retry)" in lines

    root = ET.parse(junit).getroot()
    assert root.tag == "testsuites"
    suite = root.find("testsuite")
    assert suite is not None
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")) == (
        "3",
        "1",
        "0",
        "1",
    )
    cases = suite.findall("testcase")
    assert [(c.get("classname"), c.get("name")) for c in cases] == [
        ("tests.test_a", "test_ok"),
        ("tests.test_a", "test_skip"),
        ("tests.test_a", "test_bad"),
    ]
    failure = cases[2].find("failure")
    assert failure is not None and "assert 1 == 2" in (failure.text or "")
    skipped = cases[1].find("skipped")
    assert skipped is not None and skipped.get("message") == "later"


def test_run_not_found_then_appears() -> None:
    scenario = Scenario(verdicts=[NOT_FOUND, NOT_FOUND, verdict_found("queued")], events=passing_events())
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == 0
    assert output.count("waiting for a coverset run") == 1
    assert sum(1 for path, _ in scenario.requests if path.startswith("/v1/verdict")) == 3


def test_timeout_while_run_is_missing() -> None:
    scenario = Scenario(verdicts=[NOT_FOUND])
    started = time.monotonic()
    with serve(scenario) as server:
        code, output = run_cli(server, "--timeout", "0.4")
    assert code == EXIT_INTERRUPTED
    assert "timed out" in output
    assert time.monotonic() - started < 5


def test_sse_unsupported_falls_back_to_polling() -> None:
    scenario = Scenario(verdicts=[verdict_found()], events=failing_events(), sse="unsupported")
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == EXIT_FAILURE
    assert "polling instead" in output
    assert any("/events?since=0" in path for path, _ in scenario.requests)
    assert "FAILED tests/test_a.py::test_bad" in output


def test_sse_reconnects_with_last_event_id() -> None:
    scenario = Scenario(verdicts=[verdict_found()], events=passing_events(), sse="drop_first", drop_after=2)
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == 0
    streams = [headers for path, headers in scenario.requests if path == f"/v1/runs/{RUN_ID}/events"]
    assert len(streams) == 2
    assert "last-event-id" not in streams[0] and streams[1]["last-event-id"] == "2"
    assert output.count("PASSED tests/test_a.py::test_one") == 1


def test_sse_drops_after_progress_do_not_force_polling() -> None:
    scenario = Scenario(verdicts=[verdict_found()], events=passing_events(), sse="reset_each", drop_after=1)
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == 0
    assert scenario.sse_connections == len(passing_events())
    assert "reconnecting" in output
    assert "polling instead" not in output
    assert not any("?since=" in path for path, _ in scenario.requests)
    assert output.count("PASSED tests/test_a.py::test_one") == 1


def test_sse_that_never_delivers_falls_back_to_polling() -> None:
    scenario = Scenario(verdicts=[verdict_found()], events=passing_events(), sse="empty")
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == 0
    assert scenario.sse_connections == 3
    assert any("?since=0" in path for path, _ in scenario.requests)
    assert "PASSED tests/test_a.py::test_one" in output


def test_polling_done_without_done_event_reads_the_run() -> None:
    events = failing_events()[:3]
    run = {
        "run_id": RUN_ID,
        "state": "completed",
        "conclusion": "failure",
        "counts": COUNTS_FAIL,
        "summary": "1 failed",
    }
    scenario = Scenario(verdicts=[verdict_found()], events=events, sse="unsupported", run=run)
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == EXIT_FAILURE
    assert "1 failed, 1 passed, 1 skipped" in output


def test_no_tests_exit_code() -> None:
    zero = dict.fromkeys(COUNTS_PASS, 0)
    events = event_list(("done", {"conclusion": "success", "exit_code": 5, "counts": zero}))
    with serve(Scenario(verdicts=[verdict_found()], events=events)) as server:
        code, _ = run_cli(server)
    assert code == EXIT_NO_TESTS


@pytest.mark.parametrize(("conclusion", "expected"), [("cancelled", 2), ("timed_out", 2), ("neutral", 2)])
def test_other_conclusions(conclusion: str, expected: int) -> None:
    events = event_list(("verdict", {"conclusion": conclusion, "reason": "stopped"}))
    with serve(Scenario(verdicts=[verdict_found()], events=events)) as server:
        code, _ = run_cli(server)
    assert code == expected


def test_token_is_sent_and_auth_failure_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI_OIDC_TOKEN", "header.payload.sig")
    scenario = Scenario(verdicts=[(401, {"error": "invalid_token"})])
    with serve(scenario) as server:
        code, output = run_cli(server, "--token-env", "CI_OIDC_TOKEN")
    assert code == EXIT_SERVER_ERROR
    assert scenario.requests[0][1]["authorization"] == "Bearer header.payload.sig"
    assert "not authorised" in output


def test_server_errors_exit_3_after_retries() -> None:
    scenario = Scenario(verdicts=[verdict_found()], fail_all=True)
    with serve(scenario) as server:
        client = ApiClient(server, deadline=time.monotonic() + 30, max_retries=3, backoff_base=0.01)
        out = io.StringIO()
        code = Waiter(client, repo_id=7, sha=SHA, poll_interval=0.05, out=out).run()
    assert code == EXIT_SERVER_ERROR
    assert len(scenario.requests) == 4
    assert "server unavailable" in out.getvalue()


def test_unreachable_server_exits_3_via_cli() -> None:
    code, output = run_cli("http://127.0.0.1:9", "--timeout", "2")
    assert code == EXIT_SERVER_ERROR
    assert "server unavailable" in output


def test_iter_sse_parsing() -> None:
    raw = [
        b": comment\n",
        b"id: 4\n",
        b"event: test\n",
        b'data: {"a":\n',
        b"data: 1}\n",
        b"\n",
        b"data: x\r\n",
        b"\r\n",
    ]
    events = list(iter_sse(raw))
    assert events[0] is HEARTBEAT  # comments are surfaced so the caller can check its deadline
    dispatched = [e for e in events if e is not HEARTBEAT]
    assert [(e.id, e.event, e.data) for e in dispatched] == [
        ("4", "test", '{"a":\n1}'),
        ("4", "message", "x"),
    ]


def test_errored_run_exits_2_not_5() -> None:
    """An errored run reports neutral with exit_code 2 and no tests: the server's code wins."""
    zero = dict.fromkeys(COUNTS_PASS, 0)
    events = event_list(("done", {"conclusion": "neutral", "exit_code": 2, "counts": zero}))
    with serve(Scenario(verdicts=[verdict_found()], events=events)) as server:
        code, _ = run_cli(server)
    assert code == EXIT_INTERRUPTED


@pytest.mark.parametrize(
    ("conclusion", "server_code", "counts", "expected"),
    [
        ("neutral", 2, {"passed": 0}, 2),
        ("neutral", 0, {"passed": 3}, 2),
        ("neutral", 5, {"passed": 0}, 5),
        ("success", 5, {"passed": 0}, 5),
        ("success", 0, {"passed": 1}, 0),
        ("failure", 0, {"failed": 1}, 1),
        ("cancelled", 2, {}, 2),
        ("success", 99, {"passed": 1}, 2),
        ("neutral", None, {"passed": 0}, 5),
        ("success", None, {"passed": 2}, 0),
        ("timed_out", None, None, 2),
    ],
)
def test_exit_code_for_honours_the_server_code(
    conclusion: str, server_code: int | None, counts: dict[str, int] | None, expected: int
) -> None:
    from pytest_coverset.ci import exit_code_for

    assert exit_code_for(conclusion, server_code, counts) == expected


def test_timeout_is_enforced_while_the_stream_only_pings() -> None:
    """A queued run's stream carries only `: ping` comments; --timeout must still end the wait."""
    scenario = Scenario(verdicts=[verdict_found("queued")], sse="pings")
    started = time.monotonic()
    with serve(scenario) as server:
        code, output = run_cli(server, "--timeout", "1.5")
    elapsed = time.monotonic() - started
    assert code == EXIT_INTERRUPTED
    assert "timed out" in output
    assert elapsed < 6, elapsed


def test_server_text_is_printed_without_terminal_escapes() -> None:
    hostile = "E   boom\x1b[31m red\x1b[0m\r\n\x1b]0;pwned\x07E   next\x9b2J\x00"
    scenario = Scenario(
        verdicts=[verdict_found()],
        events=event_list(
            (
                "test",
                {
                    "nodeid": "tests/test_a.py::test_x\x1b[2K",
                    "outcome": "failed",
                    "failed_phase": "call",
                    "longrepr": hostile,
                },
            ),
            ("log", {"shard": 0, "text": "\x1b[1mbold\x1b[0m log"}),
            ("done", {"conclusion": "failure", "exit_code": 1, "counts": {"failed": 1}}),
        ),
    )
    with serve(scenario) as server:
        code, output = run_cli(server)
    assert code == EXIT_FAILURE
    assert "\x1b" not in output and "\x9b" not in output and "\x00" not in output and "\r" not in output
    lines = output.splitlines()
    assert "FAILED tests/test_a.py::test_x" in lines
    assert "    E   boom red" in lines
    assert "    E   next2J" in lines
    assert "[shard 0] bold log" in lines


def test_printable_keeps_newlines_and_tabs() -> None:
    assert printable("a\tb\nc\x1b[31md\x07\x85e") == "a\tb\ncde"
