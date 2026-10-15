"""Lifecycle: the flush thread, window rotation, caps and the budget, the overhead self-check and shedding."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path
from time import perf_counter

import coverset_probe
import pytest
from coverset_probe import JsonlExporter
from coverset_probe.window import PAYLOAD_KIND, PAYLOAD_VERSION
from probe_helpers import func_of, shapes_of

APP = """
def f(x):
    return x

def g(x):
    return x
"""


def _wait_for_lines(path: Path, n: int, timeout_s: float = 5.0) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            if len(lines) >= n:
                return lines
        time.sleep(0.01)
    raise AssertionError(f"fewer than {n} windows were flushed to {path}")


def test_the_flush_thread_exports_a_window_per_period(make_module, tmp_path: Path) -> None:
    app = make_module(APP)
    out = tmp_path / "flows.jsonl"
    coverset_probe.start(
        modules=[app.__name__],
        sample_rate=1.0,
        k_anonymity=1,
        window_s=0.05,
        export=JsonlExporter(out),
        service="ledger-api",
        env="staging",
        release="abc123",
    )
    app.f(1)
    first = _wait_for_lines(out, 1)[0]
    assert first["version"] == PAYLOAD_VERSION and first["kind"] == PAYLOAD_KIND
    assert (first["service"], first["env"], first["release"]) == ("ledger-api", "staging", "abc123")
    assert first["window"]["start"] <= first["window"]["end"]
    assert shapes_of(first, "f", "x") == {"int|pos": 1.0}
    app.f("later")
    coverset_probe.stop()  # flushes the partial last window
    windows = _wait_for_lines(out, 2)
    assert any(shapes_of(w, "f", "x") == {"str|ascii|len:2-10": 1.0} for w in windows[1:])
    assert out.stat().st_mode & 0o777 == 0o600


def test_rotation_resets_counters(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    app.f(1)
    app.f(1)
    first = probe.flush()
    app.f(1)
    second = coverset_probe.stop()
    assert func_of(first, "f")["calls"] == 2
    assert func_of(second, "f")["calls"] == 1
    assert probe.windows == 2


def test_the_per_function_cap_keeps_exact_call_counts(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0, max_samples_per_func=10
    )
    for i in range(100):
        app.f(i)
    payload = probe.flush()
    assert func_of(payload, "f") | {"line": 0} == {
        "module": app.__name__,
        "qualname": "f",
        "line": 0,
        "path": None,
        "calls": 100,
        "sampled": 10,
        "capped": True,
    }
    app.f(1)  # a new window: the cap is lifted
    assert func_of(coverset_probe.stop(), "f")["sampled"] == 1


def test_the_process_budget_stops_recording_until_the_next_window(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__],
        sample_rate=1.0,
        k_anonymity=1,
        window_s=0,
        max_obs_per_window=20,
        record_returns=False,
    )
    for i in range(50):
        app.f(i)
        app.g(i)
    payload = probe.flush()
    assert payload["probe"]["budget_hit"] is True
    assert payload["probe"]["sampled_calls"] == 20
    assert func_of(payload, "f")["calls"] == 50 and func_of(payload, "g")["calls"] == 50
    app.f(1)
    after = coverset_probe.stop()
    assert after["probe"]["budget_hit"] is False
    assert after["probe"]["sampled_calls"] == 1


def test_the_overhead_check_switches_the_probe_off(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__],
        sample_rate=1.0,
        k_anonymity=1,
        window_s=0,
        max_overhead=1e-9,
        min_overhead_wall_s=0,
    )
    probe.tracer.shed = lambda index: None  # isolate the whole-probe switch from shedding
    for i in range(100):
        app.f(i)
    payload = probe.flush()
    assert payload["probe"]["disabled"].startswith("overhead ")
    assert not probe.active
    app.f(1)
    assert probe.flush() is None  # nothing more is exported
    assert coverset_probe.stop() is None


def test_a_hot_function_is_shed_and_the_rest_keep_going(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__],
        sample_rate=0.01,
        k_anonymity=1,
        window_s=0,
        max_overhead=0.02,
        min_overhead_wall_s=0,
    )
    probe.tracer.fast_call_cost = 1e-6  # pretend: one microsecond per call
    app.f(1)
    app.g(1)
    for i in range(100_000):
        app.f(i)
    probe._wall_mark = perf_counter() - 10.0  # pretend: a ten-second window
    payload = probe.flush()
    # f: 100k x 1 us = 0.1 s over 10 s = 1 % of a core, more than a quarter of the 2 % budget.
    assert payload["probe"]["shed"] == [f"{app.__name__}.f"]
    assert payload["probe"]["disabled"] is None and probe.active
    for _ in range(10):
        app.f(1)
        app.g(1)
    after = coverset_probe.stop()
    assert {f["qualname"] for f in after["funcs"]} == {"g"}
    assert after["probe"]["shed"] == [f"{app.__name__}.f"]


def test_an_exporter_failure_is_counted_not_raised(make_module) -> None:
    class Broken:
        def export(self, payload: dict) -> None:
            raise OSError("collector down")

    app = make_module(APP)
    probe = coverset_probe.start(modules=[app.__name__], window_s=0, export=[Broken()])
    app.f(1)
    probe.flush()
    assert probe.export_errors == 1
    assert coverset_probe.stop()["probe"]["export_errors"] == 1


def test_tracer_errors_are_counted_and_capped(make_module, monkeypatch) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    probe.tracer.max_errors = 5

    def explode(value: object) -> str:
        raise RuntimeError("feature bug")

    monkeypatch.setattr("coverset_probe.tracer.shape_key", explode)
    for i in range(10):
        assert app.f(i) == i  # the application never sees the error
    assert probe.tracer.errors == 5
    assert probe.disabled_reason == "5 tracer errors"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_worker_gets_its_own_window(make_module, tmp_path: Path) -> None:
    app = make_module(APP)
    out = tmp_path / "child.jsonl"
    probe = coverset_probe.start(
        modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0, export=JsonlExporter(out)
    )
    app.f("parent")  # in the parent's window only
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # "fork with threads": pytest's, not ours
        pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        code = 1
        try:
            app.f(1)
            probe.flush()
            code = 0
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    [child] = [json.loads(line) for line in out.read_text().splitlines()]
    assert shapes_of(child, "f", "x") == {"int|pos": 1.0}  # the parent's call stayed with the parent
    assert shapes_of(coverset_probe.stop(), "f", "x") == {"str|ascii|len:2-10": 1.0}


EXIT_SCRIPT = """
import sys
import coverset_probe
from coverset_probe import JsonlExporter

coverset_probe.start(modules=["exitapp"], sample_rate=1.0, k_anonymity=1, window_s=3600,
                     export=JsonlExporter(sys.argv[1]))
import exitapp
for i in range(50):
    exitapp.f(i)
# no stop(): a short job, or a worker recycled before its first window
"""


def test_the_last_window_is_exported_at_interpreter_exit(tmp_path: Path) -> None:
    (tmp_path / "exitapp.py").write_text("def f(x):\n    return x\n")
    out = tmp_path / "exit.jsonl"
    proc = subprocess.run(
        [sys.executable, "-c", EXIT_SCRIPT, str(out)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path])},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    [window] = [json.loads(line) for line in out.read_text().splitlines()]
    assert func_of(window, "f")["calls"] == 50
    assert window["window"]["end"] >= window["window"]["start"]


def test_stop_is_idempotent_and_the_exit_hook_tolerates_no_probe(make_module) -> None:
    from coverset_probe import probe as probe_module

    app = make_module(APP)
    coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    app.f(1)
    assert coverset_probe.stop() is not None
    assert coverset_probe.stop() is None
    probe_module._at_exit()  # nothing running: a no-op, never an error
