"""Privacy: the k-anonymity gate, witnesses (traces or subjects), parameter-name opt-out, noise, key validation."""

from __future__ import annotations

import random

import coverset_probe
from coverset_probe.context import TRACE, TraceContext, begin_trace
from coverset_probe.window import FuncInfo, FuncStats, KGate, Window, build_payload
from probe_helpers import shapes_of

APP = """
def check(email):
    return email

def login(user, password, api_key=None, session_id=None, *, remember=False):
    return user
"""


def _in_trace(func, value, witness: int) -> None:
    token = TRACE.set(TraceContext(sampled=True, witness=witness))
    try:
        func(value)
    finally:
        TRACE.reset(token)


def test_a_shape_is_held_back_until_k_witnesses_saw_it(make_module) -> None:
    app = make_module(APP)
    probe = coverset_probe.start(
        modules=[app.__name__], sample_rate=1.0, k_anonymity=3, window_s=0, record_returns=False
    )
    for witness in (1, 1, 2):  # two distinct traces: below k
        _in_trace(app.check, "bob@eng.acme.com", witness)
    for witness in (1, 2, 3, 4):
        _in_trace(app.check, "alice@example.com", witness)
    first = probe.flush()
    assert shapes_of(first, "check", "email") == {"str|ascii|kind:email/plain|len:11-100": 4.0}
    assert first["probe"]["suppressed"] == {"rows": 1, "calls": 1, "flows": 0}
    assert first["probe"]["pending_keys"] == 2  # the held-back row and its call vector
    _in_trace(app.check, "carol@eng.acme.com", 3)  # the third distinct witness, one window later
    second = coverset_probe.stop()
    # Only this window's count is exported: the first window's held-back calls are never published.
    assert shapes_of(second, "check", "email") == {"str|ascii|kind:email/multi-label-domain|len:11-100": 1.0}
    assert second["rows"][0]["traces"] == 1


def test_untraced_calls_are_each_their_own_witness(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(
        modules=[app.__name__], sample_rate=1.0, k_anonymity=3, window_s=0, record_returns=False
    )
    app.check("x@y.com")
    app.check("x@y.com")
    app.check("x@y.com")
    payload = coverset_probe.stop()
    assert shapes_of(payload, "check", "email") == {"str|ascii|kind:email/plain|len:2-10": 3.0}


def test_a_subject_counts_once_however_many_requests_it_makes() -> None:
    contexts = [begin_trace(1.0, None, subject="user-42") for _ in range(5)]
    assert len({c.witness for c in contexts}) == 1
    anonymous = [begin_trace(1.0, None) for _ in range(5)]
    assert len({c.witness for c in anonymous}) == 5
    other = begin_trace(1.0, None, subject="user-43")
    assert other.witness != contexts[0].witness


def test_secret_looking_parameters_are_never_observed(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(
        modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0, record_returns=False
    )
    app.login("bob", "hunter2", api_key="k", session_id="s", remember=True)
    payload = coverset_probe.stop()
    params = {row["p"] for row in payload["rows"]}
    assert params == {"user", "remember"}
    assert [pair[0] for pair in payload["calls"][0]["v"]] == ["user", "remember"]


def test_the_exclusion_pattern_is_configurable(make_module) -> None:
    app = make_module(APP)
    coverset_probe.start(
        modules=[app.__name__],
        sample_rate=1.0,
        k_anonymity=1,
        window_s=0,
        record_returns=False,
        exclude_params=r"^user$",
    )
    app.login("bob", "hunter2")
    payload = coverset_probe.stop()
    assert {row["p"] for row in payload["rows"]} == {"password", "api_key", "session_id", "remember"}
    coverset_probe.start(
        modules=[app.__name__], sample_rate=1.0, k_anonymity=1, window_s=0, exclude_params=None
    )
    app.login("bob", "hunter2")
    assert "password" in {row["p"] for row in coverset_probe.stop()["rows"]}


def test_the_gate_memory_is_bounded() -> None:
    gate = KGate(5, max_pending=2)
    assert not gate.admit("a", [1])
    assert not gate.admit("b", [1])
    assert not gate.admit("c", range(100))  # the pending store is full: c is refused outright
    assert not gate.admit("a", [2, 3, 4])
    assert gate.admit("a", [5])
    assert gate.pending == 1
    assert gate.admit("a", [])  # once admitted, always admitted
    assert KGate(1).admit("z", [])


def _window_with(rows: dict[tuple[int, str, str], float]) -> Window:
    window = Window(0.0, max_keys=100)
    for (func, param, key), calls in rows.items():
        for witness in range(int(calls)):
            window.add_shape(func, param, key, 1.0, witness, None)
    return window


def test_noise_perturbs_counts_but_never_makes_them_negative() -> None:
    window = _window_with({(0, "x", "int|pos"): 50, (0, "x", "int|neg"): 5})
    common = {
        "end": 60.0,
        "funcs": [FuncInfo("m", "f", 1, None)],
        "stats": {0: FuncStats(55, 55, False)},
        "service": "svc",
        "env": "prod",
        "release": None,
        "settings": {},
        "health": {},
    }
    exact = build_payload(window, gate=KGate(1), **common)
    noisy = build_payload(
        window, gate=KGate(1), dp_epsilon=0.5, call_sensitivity=1.0, rng=random.Random(1), **common
    )
    assert [r["calls"] for r in exact["rows"]] == [5.0, 50.0]
    assert [r["calls"] for r in noisy["rows"]] != [5.0, 50.0]
    assert all(r["calls"] >= 0 and r["traces"] >= 0 for r in noisy["rows"])
    assert noisy["probe"]["dp_epsilon"] == 0.5


def test_keys_outside_the_grammar_are_dropped_not_exported() -> None:
    window = _window_with({(0, "x", "int|pos"): 1, (0, "x", "free text with spaces"): 1})
    payload = build_payload(
        window,
        end=1.0,
        funcs=[FuncInfo("m", "f", 1, None)],
        stats={0: FuncStats(2, 2, False)},
        gate=KGate(1),
        service="s",
        env="e",
        release=None,
        settings={},
        health={},
    )
    assert [r["s"] for r in payload["rows"]] == ["int|pos"]
    assert payload["probe"]["invalid_keys"] == 1
