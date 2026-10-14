"""One export window: aggregated shape observations, the k-anonymity gate, optional noise, the payload.

The tracer adds sampled calls to the current :class:`Window`; every ``window_s`` seconds the flush
thread swaps in a fresh one and turns the old one into a ``prod-flows`` version-1 payload
(``docs/schema/prod-flows.v1.json``) with :func:`build_payload`. Three row kinds leave the process:

* ``rows``: one per ``(function, parameter, shape key)``, with the estimated number of calls, the
  number of distinct witnesses (traces or subjects) and the estimated calls per hashed caller;
* ``calls``: one per ``(function, call vector)``, the vector being every parameter's shape key of one
  call: the unit archetypes are mined from;
* ``flows``: one per set of functions a sampled trace called (function-level presence only).

**The k-anonymity gate** (:class:`KGate`) keeps a row back until ``k`` distinct witnesses showed its
key over the process's lifetime; held-back rows are counted under ``probe.suppressed`` and never
exported, even later, for the window they were seen in. Witness sets are capped at ``k`` per pending
key, and the pending store at ``max_pending`` keys, so the gate's memory is bounded.

**Counts are estimates.** Each sampled call carries the weight ``1 / p`` of the probability it was
sampled with (Horvitz-Thompson), so the sum of weights estimates calls whatever the adaptive rate
did. ``traces`` is exact up to :data:`WITNESS_CAP` and saturates there. ``funcs[].calls`` is exact:
the tracer counts every call of a target function, sampled or not.

**Noise** (``dp_epsilon``): Laplace noise on ``calls`` and ``traces`` of every exported row,
scale ``sensitivity / epsilon`` (1 for traces, ``max_calls_per_trace`` for calls, the most one
trace can add). The gate decides on true counts, so the noise protects the published counts, not
the fact that a key passed the gate; the design doc spells out what that does and does not buy.
"""

from __future__ import annotations

import math
import platform
import random
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from coverset_probe.shapes import FEATURES_SOURCE, valid_key

__all__ = [
    "EXTERNAL_CALLER",
    "MAX_CALLERS",
    "OTHER_CALLER",
    "PAYLOAD_KIND",
    "PAYLOAD_VERSION",
    "WITNESS_CAP",
    "FuncInfo",
    "FuncStats",
    "KGate",
    "Window",
    "build_payload",
    "iso_utc",
]

PAYLOAD_KIND: Final = "coverset.prod-flows"
PAYLOAD_VERSION: Final = 1
WITNESS_CAP: Final = 64
"""Distinct witnesses counted per row; ``traces`` saturates here. With ``max_keys`` (20,000) this bounds
the witness sets of one window to about 70 MB in the worst case (a few hundred keys is typical)."""
MAX_CALLERS: Final = 8
"""Callers kept per row; the rest are summed under :data:`OTHER_CALLER`."""
OTHER_CALLER: Final = "<other>"
EXTERNAL_CALLER: Final = "<external>"
"""The caller of a call made from code outside the allow-listed modules (a framework, a test)."""
PROBE_VERSION: Final = "0.1.0"


@dataclass(frozen=True, slots=True)
class FuncInfo:
    """A target function's identity: module and qualname always, a source-root-relative path when known."""

    module: str
    qualname: str
    line: int
    path: str | None


@dataclass(frozen=True, slots=True)
class FuncStats:
    """One function's counters for one window."""

    calls: int
    """Every call in the window (exact)."""
    sampled: int
    """Calls whose arguments were recorded."""
    capped: bool
    """The per-window sample cap stopped recording; shape counts then cover only the first part."""


@dataclass(slots=True)
class _Agg:
    calls: float = 0.0
    witnesses: set[int] = field(default_factory=set)
    callers: dict[str, float] = field(default_factory=dict)

    def add(self, weight: float, witness: int, caller: str | None) -> None:
        self.calls += weight
        if len(self.witnesses) < WITNESS_CAP:
            self.witnesses.add(witness)
        if caller is not None:
            if caller in self.callers or len(self.callers) < MAX_CALLERS:
                self.callers[caller] = self.callers.get(caller, 0.0) + weight
            else:
                self.callers[OTHER_CALLER] = self.callers.get(OTHER_CALLER, 0.0) + weight


type ShapeKey = tuple[int, str, str]
type VectorKey = tuple[int, tuple[tuple[str, str], ...]]


class Window:
    """Aggregates of one window; written by tracer callbacks, read once by the flush thread."""

    def __init__(self, start: float, *, max_keys: int) -> None:
        self.start = start
        self.max_keys = max_keys
        self.shapes: dict[ShapeKey, _Agg] = {}
        self.vectors: dict[VectorKey, _Agg] = {}
        self.flows: dict[frozenset[int], set[int]] = {}
        self.sampled_calls = 0
        self.sampled_returns = 0
        self.dropped_keys = 0
        self.budget_hit = False

    @property
    def n_keys(self) -> int:
        """Distinct keys held (shape rows plus call vectors plus flows)."""
        return len(self.shapes) + len(self.vectors) + len(self.flows)

    def add_shape(
        self, func: int, label: str, key: str, weight: float, witness: int, caller: str | None
    ) -> None:
        """Count one sampled value of ``label``; a new key beyond ``max_keys`` is dropped and counted."""
        agg = self.shapes.get((func, label, key))
        if agg is None:
            if self.n_keys >= self.max_keys:
                self.dropped_keys += 1
                return
            agg = self.shapes[(func, label, key)] = _Agg()
        agg.add(weight, witness, caller)

    def add_vector(self, func: int, vector: tuple[tuple[str, str], ...], weight: float, witness: int) -> None:
        """Count one sampled call's full vector of parameter shapes."""
        agg = self.vectors.get((func, vector))
        if agg is None:
            if self.n_keys >= self.max_keys:
                self.dropped_keys += 1
                return
            agg = self.vectors[(func, vector)] = _Agg()
        agg.add(weight, witness, None)

    def add_flow(self, funcs: Iterable[int], witness: int) -> None:
        """Count one finished, sampled trace's set of called functions."""
        flow = frozenset(funcs)
        if not flow:
            return
        witnesses = self.flows.get(flow)
        if witnesses is None:
            if self.n_keys >= self.max_keys:
                self.dropped_keys += 1
                return
            witnesses = self.flows[flow] = set()
        if len(witnesses) < WITNESS_CAP:
            witnesses.add(witness)


class KGate:
    """Lets a key through once ``k`` distinct witnesses have shown it (over the process lifetime)."""

    def __init__(self, k: int, *, max_pending: int = 50_000) -> None:
        self.k = max(1, k)
        self.max_pending = max_pending
        self._ok: set[Hashable] = set()
        self._pending: dict[Hashable, set[int]] = {}

    def admit(self, key: Hashable, witnesses: Iterable[int]) -> bool:
        """Record ``witnesses`` for ``key``; True when the key has now been shown by ``k`` of them."""
        if self.k <= 1 or key in self._ok:
            return True
        pending = self._pending.get(key)
        if pending is None:
            if len(self._pending) >= self.max_pending:
                return False
            pending = self._pending[key] = set()
        for witness in witnesses:
            pending.add(witness)
            if len(pending) >= self.k:
                break
        if len(pending) < self.k:
            return False
        del self._pending[key]
        self._ok.add(key)
        return True

    @property
    def pending(self) -> int:
        """Keys seen but not yet shown by ``k`` witnesses."""
        return len(self._pending)


def iso_utc(epoch_s: float) -> str:
    """``2026-10-13T10:00:00Z`` (second precision, UTC)."""
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _laplace(rng: random.Random, scale: float) -> float:
    u = rng.random() or 1e-300
    v = rng.random() or 1e-300
    return scale * (math.log(u) - math.log(v))


@dataclass(slots=True)
class _Remap:
    """Old function index -> position in the payload's ``funcs`` list (only referenced functions)."""

    funcs: list[FuncInfo]
    stats: Mapping[int, FuncStats]
    index: dict[int, int] = field(default_factory=dict)
    out: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, func: int) -> int:
        found = self.index.get(func)
        if found is None:
            info = self.funcs[func]
            stats = self.stats.get(func, FuncStats(0, 0, False))
            found = self.index[func] = len(self.out)
            self.out.append(
                {
                    "module": info.module,
                    "qualname": info.qualname,
                    "line": info.line,
                    "path": info.path,
                    "calls": stats.calls,
                    "sampled": stats.sampled,
                    "capped": stats.capped,
                }
            )
        return found


def build_payload(
    window: Window,
    *,
    end: float,
    funcs: list[FuncInfo],
    stats: Mapping[int, FuncStats],
    gate: KGate,
    service: str,
    env: str,
    release: str | None,
    settings: Mapping[str, Any],
    health: Mapping[str, Any],
    dp_epsilon: float | None = None,
    call_sensitivity: float = 1.0,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """The ``prod-flows`` v1 payload of one finished window (rows sorted, so output is stable).

    ``settings`` and ``health`` are copied under ``probe`` (sampling settings; overhead, errors,
    disabled reason). Every function with calls in the window is listed, whether or not any of
    its rows passed the gate, so function-level call presence is complete.
    """
    noise = rng or random.Random()
    remap = _Remap(funcs, stats)
    for func in sorted(f for f, s in stats.items() if s.calls > 0):
        remap(func)
    suppressed = {"rows": 0, "calls": 0, "flows": 0}
    invalid = 0
    valid: dict[str, bool] = {}

    def key_ok(key: str) -> bool:
        if key not in valid:
            valid[key] = valid_key(key)
        return valid[key]

    def counts(calls: float, traces: int) -> tuple[float, int]:
        if dp_epsilon is None:
            return round(calls, 3), traces
        noisy_calls = max(0.0, calls + _laplace(noise, call_sensitivity / dp_epsilon))
        noisy_traces = max(0, round(traces + _laplace(noise, 1.0 / dp_epsilon)))
        return round(noisy_calls, 3), noisy_traces

    rows: list[dict[str, Any]] = []
    for (func, label, key), agg in sorted(window.shapes.items(), key=lambda item: item[0]):
        if not key_ok(key):
            invalid += 1
            continue
        if not gate.admit(("r", func, label, key), agg.witnesses):
            suppressed["rows"] += 1
            continue
        calls, traces = counts(agg.calls, len(agg.witnesses))
        callers = {c: round(w, 3) for c, w in sorted(agg.callers.items())}
        rows.append(
            {"f": remap(func), "p": label, "s": key, "calls": calls, "traces": traces, "callers": callers}
        )

    vectors: list[dict[str, Any]] = []
    for (func, vector), agg in sorted(window.vectors.items(), key=lambda item: item[0]):
        if not all(key_ok(key) for _, key in vector):
            invalid += 1
            continue
        if not gate.admit(("v", func, vector), agg.witnesses):
            suppressed["calls"] += 1
            continue
        calls, traces = counts(agg.calls, len(agg.witnesses))
        vectors.append(
            {"f": remap(func), "v": [list(pair) for pair in vector], "calls": calls, "traces": traces}
        )

    flows: list[dict[str, Any]] = []
    for flow, witnesses in sorted(window.flows.items(), key=lambda item: sorted(item[0])):
        if not gate.admit(("f", flow), witnesses):
            suppressed["flows"] += 1
            continue
        _, traces = counts(0.0, len(witnesses))
        flows.append({"fs": sorted(remap(f) for f in flow), "traces": traces})

    probe: dict[str, Any] = {
        "version": PROBE_VERSION,
        "features": FEATURES_SOURCE,
        "python": platform.python_version(),
        **settings,
        "dp_epsilon": dp_epsilon,
        "sampled_calls": window.sampled_calls,
        "sampled_returns": window.sampled_returns,
        "suppressed": suppressed,
        "pending_keys": gate.pending,
        "dropped_keys": window.dropped_keys,
        "invalid_keys": invalid,
        "budget_hit": window.budget_hit,
        **health,
    }
    return {
        "version": PAYLOAD_VERSION,
        "kind": PAYLOAD_KIND,
        "service": service,
        "env": env,
        "release": release,
        "window": {"start": iso_utc(window.start), "end": iso_utc(end)},
        "probe": probe,
        "funcs": remap.out,
        "rows": rows,
        "calls": vectors,
        "flows": flows,
    }
