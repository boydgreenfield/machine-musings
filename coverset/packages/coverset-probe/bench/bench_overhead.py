"""Micro-benchmark: what the probe adds to a call, at 1 % sampling, outside and inside a trace.

Two target functions in a throwaway module ``benchapp``: ``noop(x)`` (returns its argument, the worst
case: the probe's cost is all there is) and ``check(email)`` (a regular-expression match, about the
size of a small real helper). Each is called ``--calls`` times (default 10,000) in a loop, best of
``--repeat`` runs, with no probe, with the probe started at ``--rate`` (default 0.01) and no trace
context, and with the probe inside one head-sampled-out trace (the common case in a service: most
requests are not sampled). Prints a table and, with ``--json``, the numbers.

    uv run --isolated --no-project --python 3.13 --with ./packages/coverset-model \\
        --with ./packages/coverset-probe python packages/coverset-probe/bench/bench_overhead.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import types
from time import perf_counter

import coverset_probe
from coverset_probe.context import TRACE, TraceContext

_SOURCE = """
import json
import re
_EMAIL = re.compile(r"[A-Za-z0-9._+-]+@([A-Za-z0-9-]+\\.[A-Za-z]{2,})")

def noop(x):
    return x

def check(email):
    match = _EMAIL.fullmatch(email)
    return None if match is None else match.group(1).lower()

def render(row):
    return json.dumps(row, sort_keys=True)
"""


def _module() -> types.ModuleType:
    module = types.ModuleType("benchapp")
    sys.modules["benchapp"] = module
    exec(compile(_SOURCE, "benchapp.py", "exec"), module.__dict__)
    return module


def _loop(func: object, arg: object, calls: int, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = perf_counter()
        for _ in range(calls):
            func(arg)  # type: ignore[operator]
        best = min(best, perf_counter() - t0)
    return best / calls


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and print ns per call for each configuration."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=10_000)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--rate", type=float, default=0.01)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    app = _module()
    cases = {
        "noop": (app.noop, 7),
        "check": (app.check, "bob@eng.acme.com"),
        "render": (app.render, {"id": 7, "email": "bob@acme.com", "tags": ["a", "b"], "total": 12.5}),
    }
    results: dict[str, dict[str, float]] = {}
    for name, (func, arg) in cases.items():
        func(arg)
        base = _loop(func, arg, args.calls, args.repeat)
        probe = coverset_probe.start(modules=["benchapp"], sample_rate=args.rate, window_s=0, k_anonymity=1)
        try:
            func(arg)  # discovery
            untraced = _loop(func, arg, args.calls, args.repeat)
            token = TRACE.set(TraceContext(sampled=False, witness=1))
            try:
                traced_out = _loop(func, arg, args.calls, args.repeat)
            finally:
                TRACE.reset(token)
            fast_ns = probe.tracer.fast_call_cost * 1e9
            token = TRACE.set(TraceContext(sampled=True, witness=2))
            try:
                before = probe.tracer.slow_seconds
                n_before = probe.tracer.window.sampled_calls
                probe.tracer.max_calls_per_trace = 10**9
                probe.tracer.max_samples_per_func = 10**9
                probe.tracer.max_obs_per_window = 10**9
                every = _loop(func, arg, args.calls // 10, 1)
                slow_ns = (
                    (probe.tracer.slow_seconds - before)
                    / max(1, probe.tracer.window.sampled_calls - n_before)
                    * 1e9
                )
            finally:
                TRACE.reset(token)
        finally:
            coverset_probe.stop(flush=False)
        results[name] = {
            "base_ns": base * 1e9,
            "untraced_ns": untraced * 1e9,
            "trace_not_sampled_ns": traced_out * 1e9,
            "added_untraced_ns": (untraced - base) * 1e9,
            "added_trace_not_sampled_ns": (traced_out - base) * 1e9,
            "overhead_untraced_pct": 100 * (untraced - base) / base,
            "calibrated_fast_ns": fast_ns,
            "sampled_call_ns": every * 1e9,
            "sampled_path_ns": slow_ns,
        }
    if args.json:
        print(
            json.dumps(
                {"python": sys.version.split()[0], "rate": args.rate, "calls": args.calls, **results},
                indent=2,
            )
        )
        return 0
    print(
        f"python {sys.version.split()[0]}, {args.calls} calls, best of {args.repeat}, sample rate {args.rate}"
    )
    print(
        f"{'function':8} {'no probe':>10} {'untraced':>10} {'trace off':>10} {'added':>9} {'overhead':>9}"
        f" {'recorded':>10}"
    )
    for name, row in results.items():
        print(
            f"{name:8} {row['base_ns']:>8.0f}ns {row['untraced_ns']:>8.0f}ns {row['trace_not_sampled_ns']:>8.0f}ns "
            f"{row['added_untraced_ns']:>7.0f}ns {row['overhead_untraced_pct']:>8.0f}% {row['sampled_call_ns']:>8.0f}ns"
        )
    print(
        "untraced: 1 % per-call sampling outside any trace; trace off: inside a trace that was not sampled;"
    )
    print("recorded: every call recorded (inside a sampled trace), the cost of one sampled call.")
    return 0


if __name__ == "__main__":
    re.purge()
    raise SystemExit(main())
