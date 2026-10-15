"""JSON-line events: framing, final outcomes, failure signatures, the STOP file."""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from support import NONCE, PREFIX, events, read_jsonl

from pytest_coverset.events import EventWriter, parse_event_line

OUTCOMES_MODULE = """
import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("setup boom")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown boom")

def test_pass():
    pass

def test_fail():
    assert 1 == 2, "x" * 20000

def test_fail_again():
    assert 1 == 2, "different message"

def test_setup_error(broken_setup):
    pass

def test_teardown_error(broken_teardown):
    pass

def test_skip():
    pytest.skip("not today")

@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False

@pytest.mark.xfail(reason="fixed?")
def test_xpass():
    pass

@pytest.mark.xfail(reason="strict", strict=True)
def test_xpass_strict():
    pass
"""


def outcomes_by_nodeid(pytester: pytest.Pytester) -> dict[str, dict]:
    return {e["nodeid"].split("::")[-1]: e for e in events(pytester) if e["kind"] == "test"}


def test_event_stream(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    pytester.makepyfile(test_outcomes=OUTCOMES_MODULE)
    result = pytester.runpytest_subprocess("--coverset-out", "out")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    stream = events(pytester)
    kinds = [e["kind"] for e in stream]
    assert kinds[0] == "collected" and stream[0]["count"] == 9 and stream[0]["errors"] == 0
    assert kinds[1:-2] == ["test"] * 9
    assert kinds[-2:] == ["bundle_written", "done"]
    raw = (pytester.path / "stdout").read_text()
    assert raw.count(PREFIX) == len(stream)
    assert all(line.startswith(PREFIX) for line in raw.split("\n") if NONCE in line)

    tests = outcomes_by_nodeid(pytester)
    assert {name: e["outcome"] for name, e in tests.items()} == {
        "test_pass": "passed",
        "test_fail": "failed",
        "test_fail_again": "failed",
        "test_setup_error": "error",
        "test_teardown_error": "error",
        "test_skip": "skipped",
        "test_xfail": "xfailed",
        "test_xpass": "xpassed",
        "test_xpass_strict": "failed",
    }
    assert tests["test_setup_error"]["failed_phase"] == "setup"
    assert tests["test_teardown_error"]["failed_phase"] == "teardown"
    assert tests["test_fail"]["failed_phase"] == "call"
    assert tests["test_skip"]["reason"] == "not today" and tests["test_xfail"]["reason"] == "known bug"
    assert tests["test_pass"]["fail_sig"] is None and tests["test_pass"]["longrepr"] is None

    long = tests["test_fail"]
    assert long["truncated"] is True and len(long["longrepr"].encode()) <= 8192
    assert (
        tests["test_fail_again"]["truncated"] is False
        and "different message" in tests["test_fail_again"]["longrepr"]
    )
    assert len(long["fail_sig"]) == 16
    assert long["fail_sig"] != tests["test_fail_again"]["fail_sig"]
    assert tests["test_setup_error"]["fail_sig"] != tests["test_teardown_error"]["fail_sig"]
    for event in tests.values():
        assert event["start"] <= event["stop"] and event["duration_s"] >= 0

    done = stream[-1]
    assert done["exit_code"] == 1
    assert done["counts"] == {
        "passed": 1,
        "failed": 3,
        "error": 2,
        "skipped": 1,
        "xfailed": 1,
        "xpassed": 1,
        "not_run": 0,
    }
    written = stream[-2]
    with tarfile.open(written["path"]) as tar:
        assert "results.jsonl" in tar.getnames()
    rows = read_jsonl(pytester.path / "out" / "results.jsonl")
    assert [r["nodeid"] for r in rows] == [e["nodeid"] for e in stream if e["kind"] == "test"]


def test_fail_sig_is_stable_across_messages(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_sig="""
        import os
        def test_flaky_message():
            raise ValueError(os.environ["MESSAGE"])
        """
    )
    sigs = []
    for message in ("first", "second"):
        monkeypatch.setenv("MESSAGE", message)
        pytester.runpytest_subprocess("--coverset-out", f"out-{message}")
        rows = read_jsonl(pytester.path / f"out-{message}" / "results.jsonl")
        sigs.append(rows[0]["fail_sig"])
    assert sigs[0] == sigs[1]


def test_no_nonce_no_events(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Events are for the orchestrator, which always sets a nonce: without one nothing is written."""
    monkeypatch.delenv("COVERSET_EVENT_NONCE", raising=False)
    pytester.makepyfile(test_one="def test_one():\n    pass\n")
    pytester.runpytest_subprocess("--coverset-out", "out").assert_outcomes(passed=1)
    raw = (pytester.path / "stdout").read_text()
    assert "\x1ecoverset" not in raw and '{"kind":' not in raw
    assert (pytester.path / "out" / "bundle.tar.gz").is_file()


def test_stop_file_ends_the_run_and_keeps_the_bundle(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    stop = pytester.path / "STOP"
    pytester.makepyfile(
        test_stop=f"""
        import pathlib

        def test_first():
            pass

        def test_writes_stop():
            pathlib.Path({str(stop)!r}).write_text("stop")

        def test_never_runs():
            raise AssertionError("ran after STOP")

        def test_never_runs_either():
            raise AssertionError("ran after STOP")
        """
    )
    result = pytester.runpytest_subprocess("--coverset-out", "out", "--coverset-stop-file", str(stop))
    assert result.ret == pytest.ExitCode.INTERRUPTED
    out = pytester.path / "out"
    assert (out / "bundle.tar.gz").is_file()
    rows = read_jsonl(out / "results.jsonl")
    assert [r["nodeid"].split("::")[-1] for r in rows] == ["test_first", "test_writes_stop"]
    meta = json.loads((out / "meta.json").read_text())
    assert meta["stop_requested"] is True and meta["exit_code"] == 2
    assert meta["counts"]["not_run"] == 2
    done = events(pytester)[-1]
    assert done["kind"] == "done" and done["exit_code"] == 2


def test_event_writer_unit() -> None:
    stream = io.StringIO()
    mid_line = iter([True, False])
    writer = EventWriter("n1", stream=stream, mid_line=lambda: next(mid_line))
    writer.emit("collected", count=3)
    writer.emit("heartbeat", done=1, total=3)
    lines = stream.getvalue().split("\n")
    assert lines[0] == "" and lines[-1] == ""
    first, second = (parse_event_line(line, "n1") for line in lines[1:3])
    assert first is not None and first["kind"] == "collected" and first["count"] == 3
    assert second is not None and second["kind"] == "heartbeat"
    assert parse_event_line(lines[1], "other") is None
    assert parse_event_line("coverset n1 {}", "n1") is None
    assert not writer.heartbeat_due(writer.last_emit + 9.9)
    assert writer.heartbeat_due(writer.last_emit + 10.0)
