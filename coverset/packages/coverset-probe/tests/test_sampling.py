"""Sampling: per-call Bernoulli sampling with unbiased weights, the adaptive rate, trace sampling, what is recorded."""

from __future__ import annotations

import json
import sys

import coverset_probe
import pytest
from coverset_probe.context import TRACE, TraceContext
from coverset_probe.tracer import RECEIVER, RETURN, caller_hash
from coverset_probe.window import EXTERNAL_CALLER
from probe_helpers import func_of, rows_of, shapes_of

APP = """
def double(x):
    return x * 2

def outer(items):
    return [inner(i) for i in items]

def inner(i):
    return i + 1

def boom(x):
    raise ValueError("no")

class Account:
    def rename(self, name, *tags, **extra):
        return name

    @classmethod
    def build(cls, n):
        return n
"""


def test_every_call_is_recorded_at_rate_one(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    for value in (1, 2, -3, 0, 2.5):
        app.double(value)
    payload = coverset_probe.stop()
    assert func_of(payload, "double") | {"line": 0} == {
        "module": app.__name__,
        "qualname": "double",
        "line": 0,
        "path": None,
        "calls": 5,
        "sampled": 5,
        "capped": False,
    }
    assert shapes_of(payload, "double", "x") == {
        "int|pos": 2.0,
        "int|neg": 1.0,
        "int|zero": 1.0,
        "float|pos": 1.0,
    }
    # 2.5 * 2 == 5.0: an integral float
    assert shapes_of(payload, "double", RETURN) == {
        "int|pos": 2.0,
        "int|neg": 1.0,
        "int|zero": 1.0,
        "float|integral|pos": 1.0,
    }
    vectors = [(row["v"], row["calls"]) for row in payload["calls"]]
    assert ([["x", "int|pos"]], 2.0) in vectors


def test_low_rate_weights_estimate_the_true_call_count(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__], sample_rate=0.05, k_anonymity=1, window_s=0, seed=7, settle=10**9
    )
    for i in range(20_000):
        app.double(i % 3 - 1)  # -1, 0, 1 in equal thirds
    stats = probe.stats()
    payload = coverset_probe.stop()
    func = func_of(payload, "double")
    assert func["calls"] == 20_000
    assert 800 < func["sampled"] < 1200  # Bernoulli(0.05) over 20k calls: mean 1000, sd ~31
    estimated = shapes_of(payload, "double", "x")
    assert sum(estimated.values()) == pytest.approx(20_000, rel=0.1)
    for key in ("int|neg", "int|zero", "int|pos"):
        assert estimated[key] == pytest.approx(20_000 / 3, rel=0.2)
    assert stats["fast_call_ns"] > 0


def test_settled_functions_back_off_and_stay_unbiased(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__], sample_rate=0.5, k_anonymity=1, window_s=0, seed=3, settle=16
    )
    for _ in range(40_000):
        app.double(1)
    state = probe.tracer._states[0]
    assert state.rate < 0.5  # one shape only: the rate backed off
    assert state.rate >= 0.5 / 64
    before = state.sampled
    while state.sampled == before:  # the first sampled call with a new shape restores the base rate
        app.double("new kind")
    assert state.rate == 0.5
    for _ in range(2_000):
        app.double("new kind")
    payload = coverset_probe.stop()
    total = sum(shapes_of(payload, "double", "x").values())
    assert total == pytest.approx(func_of(payload, "double")["calls"], rel=0.15)


def test_receiver_varargs_and_kwargs_labels(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    app.Account().rename("bob", "a", "b", force=True)
    app.Account.build(3)
    payload = coverset_probe.stop()
    rename = {row["p"]: row["s"] for row in rows_of(payload, "Account.rename")}
    assert rename == {
        RECEIVER: f"obj:{app.__name__}.Account",
        "name": "str|ascii|len:2-10",
        "*tags": "tuple|elem:str|len:2-10|order:sorted",
        "**extra": "dict|elem:bool|key:str|len:1",
        RETURN: "str|ascii|len:2-10",
    }
    build = {row["p"]: row["s"] for row in rows_of(payload, "Account.build")}
    assert build[RECEIVER] == f"obj:{app.__name__}.Account"


def test_callers_are_hashed_targets_or_external(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    app.outer([1, 2])
    payload = coverset_probe.stop()
    inner = [row for row in rows_of(payload, "inner") if row["p"] == "i"]
    # inner is called from a list comprehension inside outer: the comprehension frame is skipped
    # (Python 3.12+ inlines comprehensions anyway) and the caller is outer, hashed.
    assert inner[0]["callers"] == {caller_hash(app.__name__, "outer"): 2.0}
    outer = [row for row in rows_of(payload, "outer") if row["p"] == "items"]
    assert outer[0]["callers"] == {EXTERNAL_CALLER: 1.0}


def test_a_call_that_raises_records_its_arguments_and_no_return(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    for _ in range(3):
        with pytest.raises(ValueError):
            app.boom("x")
    state = next(s for s in probe.tracer._states if s.code.co_name == "boom")
    # Their returns never come. Entries are keyed by frame id, and CPython reuses the memory of a
    # finished frame, so the three calls may share one key: what matters is that rotation clears it.
    assert 1 <= len(state.pending) <= 3
    payload = probe.flush()
    assert shapes_of(payload, "boom", "x") == {"str|ascii|len:1": 3.0}
    assert shapes_of(payload, "boom", RETURN) == {}
    assert state.pending == {}  # cleared at rotation


def test_trace_sampling_records_whole_sampled_traces_only(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    probe.tracer.max_calls_per_trace = 3
    token = TRACE.set(TraceContext(sampled=False, witness=11))
    try:
        for _ in range(10):
            app.double(1)
    finally:
        TRACE.reset(token)
    ctx = TraceContext(sampled=True, witness=12)
    token = TRACE.set(ctx)
    try:
        for _ in range(5):
            app.double(2)
        app.inner(1)
    finally:
        TRACE.reset(token)
    probe.end_trace(ctx)
    payload = coverset_probe.stop()
    assert func_of(payload, "double")["calls"] == 15
    assert func_of(payload, "double")["sampled"] == 3  # max_calls_per_trace; unsampled trace: none
    assert shapes_of(payload, "double", "x") == {"int|pos": 3.0}
    assert rows_of(payload, "inner") == []  # the trace's call budget was spent
    assert [sorted(payload["funcs"][i]["qualname"] for i in flow["fs"]) for flow in payload["flows"]] == [
        ["double"]
    ]


def test_values_never_leave_the_process(make_module) -> None:
    app = make_module(APP)
    secret_values = ["alice.smith@example.com", "hunter2-correct-horse", "4111111111111111"]
    coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    for value in secret_values:
        app.double(value)
    app.Account().rename(secret_values[0], secret_values[1], card=secret_values[2])
    text = json.dumps(coverset_probe.stop())
    for value in secret_values:
        assert value not in text
        assert value[:8] not in text


def test_only_allow_listed_modules_are_targets(make_module) -> None:
    app = make_module(APP)
    other = make_module(APP)
    coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    app.double(1)
    other.double(1)
    payload = coverset_probe.stop()
    assert {f["module"] for f in payload["funcs"]} == {app.__name__}


def test_start_requires_an_allow_list_and_a_valid_rate() -> None:
    with pytest.raises(ValueError, match="modules"):
        coverset_probe.start(modules=[])
    with pytest.raises(ValueError, match="sample_rate"):
        coverset_probe.start(modules=["x"], sample_rate=0)
    assert coverset_probe.current() is None


def test_one_probe_per_process(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(modules=[app.__name__], window_s=0)
    with pytest.raises(RuntimeError, match="already running"):
        coverset_probe.start(modules=[app.__name__], window_s=0)
    coverset_probe.stop(flush=False)
    probe = coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    app.double(1)
    assert probe.active
    assert shapes_of(coverset_probe.stop(), "double", "x") == {"int|pos": 1.0}


def test_threads_discovering_a_function_together_register_it_once(make_module) -> None:
    import threading

    app = make_module(APP)
    probe = coverset_probe.start(modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for i in range(200):
            app.double(i)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [f.qualname for f in probe.tracer.funcs] == ["double"]
    payload = coverset_probe.stop()
    # Counter increments from concurrent threads may be lost only on free-threaded builds.
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    assert func_of(payload, "double")["calls"] == 1600 or not gil
