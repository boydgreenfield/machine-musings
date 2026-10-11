"""Instrumentation must never break or silently lose a run: strict warnings, odd encodings, SIGTERM,
hostile argument values and secrets."""

from __future__ import annotations

import gzip
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import NONCE, PREFIX, events, read_jsonl, read_shapes

HAZARD_MODULE = '''
class Sneaky:
    """A receiver whose ``__class__`` property must never be evaluated."""

    @property
    def __class__(self):
        raise RuntimeError("__class__ called")


class Thing:
    def act(self, x):
        return x


def big(n, label):
    return label


def make_token(user):
    return "rst_" + user + "_s3cr3tvalue"


def with_kwargs(**kw):
    return len(kw)
'''

HAZARD_TESTS = """
from hazpkg.mod import Sneaky, Thing, big, make_token, with_kwargs


def test_big():
    assert big(10**5000, "x") == "x"
    assert big(1, "y") == "y"


def test_sneaky_receiver():
    assert Thing.act(Sneaky(), 1) == 1


def test_secrets():
    assert make_token("alice").startswith("rst_")
    assert with_kwargs(password="hunter2", api_key="abc", name="bob") == 3
"""


def test_hostile_values_keep_every_parameter_and_hide_secrets(pytester: pytest.Pytester) -> None:
    pytester.mkpydir("hazpkg")
    (pytester.path / "hazpkg" / "mod.py").write_text(HAZARD_MODULE)
    pytester.makepyfile(test_hazards=HAZARD_TESTS)
    result = pytester.runpytest_subprocess(
        "--coverset-out", "out", "--coverset-source", "hazpkg", "--coverset-repr-examples", "all"
    )
    result.assert_outcomes(passed=3)
    out = pytester.path / "out"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["tracer_errors"] == 0
    shapes = read_shapes(out)

    assert shapes.params("big") == {"n", "label", "return"}
    big_n = {shapes.header["shapes"][row["s"]]: row["ex"] for row in shapes.rows("big", "n")}
    assert big_n == {"int|big|pos": "<int 16610 bits>", "int|pos": "1"}
    assert [(row["n"], row["ex"]) for row in shapes.rows("big", "label")] == [(2, "'x'")]

    # Called unbound with an unrelated object: not a Thing receiver, so recorded as `self` (and the
    # Sneaky __class__ property was never evaluated: tracer_errors is 0 and the class name is right).
    assert shapes.keys("Thing.act", "self") == {"obj:hazpkg.mod.Sneaky"}
    assert shapes.keys("Thing.act", "x") == {"int|pos"}

    assert [row["ex"] for row in shapes.rows("make_token", "return")] == ["<redacted:len=21>"]
    [kwargs_example] = [row["ex"] for row in shapes.rows("with_kwargs", "**kw")]
    assert kwargs_example == "{'password': <redacted:len=7>, 'api_key': <redacted:len=3>, 'name': 'bob'}"
    raw = gzip.decompress((out / "shapes.jsonl.gz").read_bytes()).decode()
    assert "hunter2" not in raw and "s3cr3t" not in raw


def test_strict_warnings_do_not_break_the_run(pytester: pytest.Pytester) -> None:
    pytester.mkpydir("unimported")
    pytester.makeini("[pytest]\nfilterwarnings =\n    error\n")
    pytester.makepyfile(
        test_strict="""
        def test_fails():
            assert 1 == 2

        def test_passes():
            pass
        """
    )
    result = pytester.runpytest_subprocess(
        "-W", "error", "--coverset-out", "out", "--coverset-source", "unimported"
    )
    result.assert_outcomes(failed=1, passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()
    assert "CoverageWarning" not in result.stdout.str()
    out = pytester.path / "out"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["errors"] == []
    assert meta["counts"]["failed"] == 1 and meta["counts"]["not_run"] == 0
    assert (out / "coverage.json").is_file() and (out / "bundle.tar.gz").is_file()


def test_non_ascii_results_on_an_ascii_stdout(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    pytester.makepyfile(
        test_uni="""
        def test_cafe():
            assert "caf\\u00e9" == "cafe"

        def test_ok():
            pass
        """
    )
    result = pytester.runpytest_subprocess("--coverset-out", "out")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    stream = events(pytester)
    failed = next(e for e in stream if e["kind"] == "test" and e["outcome"] == "failed")
    assert "café" in failed["longrepr"]
    assert stream[-1]["kind"] == "done" and stream[-1]["counts"]["not_run"] == 0


@pytest.mark.security
def test_nonce_is_hidden_from_tests_and_their_subprocesses(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    pytester.makepyfile(
        test_env="""
        import os, subprocess, sys

        def test_env_has_no_nonce():
            assert "COVERSET_EVENT_NONCE" not in os.environ
            child = "import os; print(os.environ.get('COVERSET_EVENT_NONCE'))"
            assert subprocess.check_output([sys.executable, "-c", child], text=True).strip() == "None"
        """
    )
    pytester.runpytest_subprocess("--coverset-out", "out").assert_outcomes(passed=1)
    assert [e["kind"] for e in events(pytester)] == ["collected", "test", "bundle_written", "done"]


SLOW_TESTS = """
import pathlib, time
from slowpkg import double


def test_quick():
    assert double(2) == 4


def test_hangs():
    pathlib.Path("started").write_text("started")
    time.sleep(120)


def test_after():
    pass
"""


def _wait_for(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_interrupts_a_hung_test_and_writes_the_bundle(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    pytester.mkpydir("slowpkg")
    (pytester.path / "slowpkg" / "__init__.py").write_text("def double(n):\n    return n * 2\n")
    pytester.makepyfile(test_slow=SLOW_TESTS)
    stdout_path = pytester.path / "worker-stdout"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        "--coverset-out",
        "out",
        "--coverset-source",
        "slowpkg",
        "test_slow.py",
    ]
    with stdout_path.open("wb") as stdout:
        worker = subprocess.Popen(
            command, cwd=pytester.path, stdout=stdout, stderr=subprocess.STDOUT, env=dict(os.environ)
        )
        try:
            _wait_for(pytester.path / "started", timeout=60)
            worker.send_signal(signal.SIGTERM)
            returncode = worker.wait(timeout=60)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    assert returncode == pytest.ExitCode.INTERRUPTED

    out = pytester.path / "out"
    assert (out / "bundle.tar.gz").is_file()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["stop_requested"] is True and meta["exit_code"] == 2
    assert meta["counts"]["passed"] == 1 and meta["counts"]["not_run"] == 2
    assert [row["nodeid"] for row in read_jsonl(out / "results.jsonl")] == ["test_slow.py::test_quick"]
    assert read_shapes(out).keys("double", "n") == {"int|pos"}

    raw = stdout_path.read_text(encoding="utf-8")
    kinds = [json.loads(line[len(PREFIX) :])["kind"] for line in raw.split("\n") if line.startswith(PREFIX)]
    assert kinds[-2:] == ["bundle_written", "done"]
    assert "coverset: SIGTERM received" in raw
